"""
WiCAN device HTTP config — opt-in auto-switch to SLCAN, then restore.

The WiCAN PRO will only pass CAN traffic over its SLCAN TCP socket when its
top-level ``protocol`` is set to ``slcan`` (see :mod:`src.ecu.wican_transport`
and wican-fw #476). Out of the box the user's device is usually in a different
mode (``poll_log``, ``realdash``, ...), so a bench run would silently see zero
frames. This module flips that one setting for us and puts it back afterwards.

SAFETY — this writes the user's ENTIRE device config back to flash, and that
config holds their WiFi/MQTT passwords in PLAINTEXT. The device exposes the
config as one mostly-flat JSON blob via ``GET /load_config`` and accepts the
modified blob via ``POST /store_config`` (after which it REBOOTS). To change
exactly one field without risking any other:

  * We do NOT round-trip through ``json.loads``/``json.dumps`` — that can
    reorder keys, retype values, or drop fields the firmware emits but we don't
    model. Instead we do a *targeted* regex replace on the RAW text, touching
    only the top-level ``"protocol"`` token.
  * The config also contains ``home_protocol`` / ``drive_protocol`` /
    ``batt_alert_protocol``; a negative lookbehind ``(?<!_)`` on the regex
    guarantees those sibling keys are never matched.
  * We enforce EXACTLY ONE match before writing — zero or many is a hard error,
    never a silent partial edit.
  * We defensively ``json.loads`` the modified body purely as a parse check
    (we still POST the raw edited text) so we never write a corrupt config.

This is a core ECU module and MUST stay headless: standard library only
(``urllib.request``, ``json``, ``re``, ``time``, ``logging``). No PySide6, no
third-party imports.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import os
import re
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

from .constants import (
    DATALOG_KEEPALIVE_INTERVAL_S,
)
from .exceptions import ECUError

logger = logging.getLogger(__name__)


#: Targeted match for the TOP-LEVEL ``"protocol"`` key only. The negative
#: lookbehind ``(?<!_)`` makes ``home_protocol`` / ``drive_protocol`` /
#: ``batt_alert_protocol`` NON-matches (they have ``_`` right before the
#: quote-delimited key name). We never use a global json reserialize, so this
#: surgical regex is the entire blast radius of a protocol change.
_TOP_LEVEL_PROTOCOL_RE = re.compile(r'(?<!_)"protocol"\s*:\s*"([^"]*)"')

#: The protocol value that makes the WiCAN pass CAN traffic over its SLCAN
#: socket — the whole reason this module exists.
SLCAN = "slcan"


class WiCANConfigError(ECUError):
    """Raised on WiCAN HTTP-config read/write/verify failures.

    Subclasses :class:`~src.ecu.exceptions.ECUError` so it is caught by the
    same unified handlers as the rest of the ECU stack.
    """

    pass


# ---------------------------------------------------------------------------
# Module-level, unit-testable text helpers (no I/O).
# ---------------------------------------------------------------------------


def get_top_level_protocol(raw: str) -> str:
    """Return the top-level ``protocol`` value from a raw config blob.

    Ignores ``home_protocol`` / ``drive_protocol`` / ``batt_alert_protocol``.

    :raises WiCANConfigError: if the top-level key is absent or appears more
        than once (an ambiguous config we refuse to reason about).
    """
    matches = _TOP_LEVEL_PROTOCOL_RE.findall(raw)
    if len(matches) != 1:
        raise WiCANConfigError(
            f'expected exactly one top-level "protocol" key, found {len(matches)}'
        )
    return matches[0]


def _host_keyed_temp_path(host: object, prefix: str) -> str:
    """Path of a host-keyed JSON sidecar in the OS temp dir.

    Keyed by ``host`` (sanitized to a filesystem-safe token) so concurrent runs
    against different devices never clobber each other, and stable across runs so
    a NEXT run can detect and recover a stranded device. ``prefix`` namespaces the
    file (``wican_recovery`` for the protocol sidecar vs ``wican_datalog`` for the
    datalog-pause breadcrumb) so the two never collide.
    """
    safe_host = re.sub(r"[^A-Za-z0-9]", "_", str(host))
    return os.path.join(tempfile.gettempdir(), f"{prefix}_{safe_host}.json")


def set_top_level_protocol(raw: str, value: str) -> str:
    """Return ``raw`` with ONLY the top-level ``protocol`` value replaced.

    Performs a targeted regex substitution that touches exactly one token and
    leaves every other byte — including the ``home_/drive_/batt_alert_``
    sibling protocols and any secret fields — untouched.

    :raises WiCANConfigError: unless there is EXACTLY ONE top-level protocol
        token to replace (zero or many is refused, never a partial edit).
    """
    matches = _TOP_LEVEL_PROTOCOL_RE.findall(raw)
    if len(matches) != 1:
        raise WiCANConfigError(
            f'refusing to edit: expected exactly one top-level "protocol" '
            f"token, found {len(matches)}"
        )
    # Use a function replacement so a value containing backslashes or group
    # references can't corrupt the substitution. count=1 is belt-and-braces;
    # we already proved there is exactly one match above.
    return _TOP_LEVEL_PROTOCOL_RE.sub(lambda _m: f'"protocol": "{value}"', raw, count=1)


class WiCANConfigurator:
    """Read/modify the WiCAN device config over HTTP to toggle SLCAN mode.

    The public surface is intentionally small and crash-recovery friendly:
    :meth:`switch_to_slcan` returns the PREVIOUS protocol so the caller can
    persist it and :meth:`restore` it later (even after a crash). Every write
    goes through :meth:`set_protocol`, which does the surgical edit, POSTs, and
    waits for the device to come back up reporting the new value.
    """

    def __init__(
        self,
        host: str,
        http_port: int = 80,
        timeout_s: float = 8.0,
        reboot_timeout_s: float = 45.0,
        poll_interval_s: float = 3.0,
    ) -> None:
        self.host = host
        self.http_port = http_port
        self.timeout_s = timeout_s
        self.reboot_timeout_s = reboot_timeout_s
        self.poll_interval_s = poll_interval_s

    # -- URL helpers -------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"http://{self.host}:{self.http_port}{path}"

    # -- Crash-recovery sidecar -------------------------------------------
    #
    # If the process is hard-killed between switching to slcan and restoring,
    # the user's original protocol would be lost forever. We persist it to a
    # small JSON sidecar in the OS temp dir, keyed by host so concurrent runs
    # against different devices never clobber each other, and stable across
    # runs so the NEXT run can detect a stranded device and restore it.

    @property
    def recovery_path(self) -> str:
        """Stable, host-keyed path of the crash-recovery sidecar file."""
        return _host_keyed_temp_path(self.host, "wican_recovery")

    def _write_recovery(self, prev: str) -> None:
        """Persist the TRUE original protocol so it survives a hard kill."""
        payload = json.dumps({"host": self.host, "previous_protocol": prev})
        with open(self.recovery_path, "w", encoding="utf-8") as fh:
            fh.write(payload)
        logger.debug(
            "Wrote WiCAN recovery sidecar %s (previous_protocol=%r)",
            self.recovery_path,
            prev,
        )

    def read_recovery(self) -> Optional[str]:
        """Return the recorded original protocol for THIS host, else ``None``.

        Tolerates a missing or corrupt sidecar (returns ``None``). Only honors a
        file whose ``host`` matches ours, so a stale sidecar from a different
        device is never applied here.
        """
        path = self.recovery_path
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.loads(fh.read())
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        if data.get("host") != self.host:
            return None
        prev = data.get("previous_protocol")
        return prev if isinstance(prev, str) else None

    def clear_recovery(self) -> None:
        """Best-effort delete of the recovery sidecar (ignore if missing)."""
        try:
            os.unlink(self.recovery_path)
        except OSError:
            pass

    def write_recovery(self, protocol: str) -> None:
        """Public: persist the TRUE original protocol to the recovery sidecar.

        Lets an event-driven caller (e.g. ``ECUSession``, which connects and
        disconnects across separate events) get the same hard-kill durability as
        :meth:`slcan_session` without the context manager: write the breadcrumb
        BEFORE switching to ``slcan``, then restore + :meth:`clear_recovery` on a
        clean disconnect. ``switch_to_slcan`` / ``restore`` are left untouched.
        """
        self._write_recovery(protocol)

    # -- Reads -------------------------------------------------------------

    def read_config_raw(self, timeout_s: Optional[float] = None) -> str:
        """GET ``/load_config`` and return the raw response text.

        :param timeout_s: optional per-call socket timeout override; defaults to
            ``self.timeout_s``. Used by the reboot poll loop to cap each read so
            the total wait honors ``reboot_timeout_s`` tightly.
        :raises WiCANConfigError: on any HTTP/socket error or non-200 status.
        """
        url = self._url("/load_config")
        timeout = self.timeout_s if timeout_s is None else timeout_s
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                body = resp.read()
        except urllib.error.HTTPError as exc:
            raise WiCANConfigError(
                f"GET /load_config failed: HTTP {exc.code} from {self.host}"
            ) from exc
        except (urllib.error.URLError, socket.error, OSError) as exc:
            raise WiCANConfigError(
                f"GET /load_config failed: cannot reach {self.host}: {exc}"
            ) from exc

        if status != 200:
            raise WiCANConfigError(
                f"GET /load_config returned HTTP {status} from {self.host}"
            )
        return body.decode("utf-8", errors="replace")

    def read_config(self) -> dict:
        """Return the parsed config dict (``json.loads`` of the raw blob).

        :raises WiCANConfigError: if the device returned non-JSON.
        """
        raw = self.read_config_raw()
        try:
            return json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise WiCANConfigError(f"device config is not valid JSON: {exc}") from exc

    def current_protocol(self) -> str:
        """Return the device's current top-level ``protocol``."""
        return get_top_level_protocol(self.read_config_raw())

    def is_slcan(self) -> bool:
        """True if the device is currently in SLCAN mode."""
        return self.current_protocol() == SLCAN

    # -- Writes ------------------------------------------------------------

    def switch_to_slcan(self) -> str:
        """Switch the device into SLCAN mode; return the PREVIOUS protocol.

        The returned value is what the caller should persist for crash-recovery
        and later hand back to :meth:`restore`. If the device is already in
        SLCAN mode this is a no-op and returns ``"slcan"`` (so a restore of that
        value is also a no-op).
        """
        previous = self.current_protocol()
        if previous == SLCAN:
            logger.info("WiCAN already in slcan mode; no change.")
            return SLCAN
        logger.info("Switching WiCAN protocol %r -> slcan", previous)
        self.set_protocol(SLCAN)
        return previous

    def restore(self, protocol: str) -> None:
        """Restore the device to ``protocol`` (no-op if already there)."""
        self.set_protocol(protocol)

    @contextlib.contextmanager
    def slcan_session(self):
        """Context manager: enter SLCAN mode, restore the TRUE original on exit.

        Durable across a hard kill: the original protocol is persisted to a
        host-keyed recovery sidecar BEFORE we yield, so if this process is
        killed mid-session the NEXT run reads that sidecar and restores the real
        original — even though the device is already ``slcan`` by then.

        The logic deliberately prefers a RECORDED original (from a prior crashed
        run) over the device's CURRENT value:

          * Crash recovery: sidecar present + device already ``slcan`` ->
            ``true_prev`` comes from the sidecar (the real original), NOT the
            current ``slcan`` (which would lose the original forever).
          * Intentional slcan: device already ``slcan`` + NO sidecar -> we do
            nothing: no switch, no recovery write, no restore on exit.

        :yields: the true original protocol the device will be restored to.
        """
        recorded = self.read_recovery()  # original from a prior crashed run
        current = self.current_protocol()
        true_prev = recorded if recorded is not None else current

        if true_prev != SLCAN:
            # Persist the breadcrumb BEFORE switching. A hard kill during the
            # multi-second switch/reboot must never leave the device in slcan
            # without a record of the original. A stale-but-correct sidecar is
            # harmless: the next run restores a value the device is likely
            # already on (a no-op), and a clean exit clears it below.
            self._write_recovery(true_prev)
        if current != SLCAN:
            self.set_protocol(SLCAN)

        try:
            yield true_prev
        finally:
            if true_prev != SLCAN:
                try:
                    self.restore(true_prev)
                finally:
                    self.clear_recovery()

    def set_protocol(self, protocol: str) -> None:
        """Set the device's top-level protocol to ``protocol`` and verify.

        Reads the raw config, performs the surgical one-token edit, defensively
        parse-checks the result, POSTs it (tolerating the reboot dropping the
        connection), then polls until the device reports the new value.

        No-op (no write, no reboot) if the device is already on ``protocol``.

        :raises WiCANConfigError: on a bad edit, a parse-check failure, a
            non-reboot write error, or a reboot-verify timeout.
        """
        raw = self.read_config_raw()
        if get_top_level_protocol(raw) == protocol:
            logger.info("WiCAN already on protocol %r; nothing to do.", protocol)
            return

        modified = set_top_level_protocol(raw, protocol)

        # Defensive parse check: we POST the RAW edited text, but if our edit
        # somehow produced invalid JSON we must NOT write it to the device.
        try:
            json.loads(modified)
        except (ValueError, TypeError) as exc:
            raise WiCANConfigError(
                f"refusing to POST: edited config no longer parses as JSON: {exc}"
            ) from exc

        self._post_config(modified)
        self._wait_for_protocol(protocol, self.reboot_timeout_s)
        logger.info("WiCAN now on protocol %r.", protocol)

    def _post_config(self, body: str) -> None:
        """POST the raw config body to ``/store_config``.

        The device replies 200 ("...Rebooting...") and then reboots, which may
        also drop the connection mid-response. We treat connection errors as the
        EXPECTED reboot signal and return so the caller can poll; only a clear
        non-reboot HTTP failure raises.
        """
        url = self._url("/store_config")
        data = body.encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                text = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            # An HTTP error code IS a real response (not a dropped socket), so
            # this is a genuine refusal by the device — surface it.
            raise WiCANConfigError(
                f"POST /store_config failed: HTTP {exc.code} from {self.host}"
            ) from exc
        except (urllib.error.URLError, socket.error, OSError) as exc:
            # The reboot dropping the connection lands here — this is expected;
            # fall through to polling rather than failing.
            logger.info(
                "POST /store_config connection dropped (expected reboot): %s", exc
            )
            return

        if status != 200:
            raise WiCANConfigError(
                f"POST /store_config returned HTTP {status} from {self.host}: {text!r}"
            )
        logger.info("POST /store_config accepted: %s", text.strip())

    def _wait_for_protocol(self, expected: str, timeout_s: float) -> None:
        """Poll ``/load_config`` until the top-level protocol == ``expected``.

        Tolerates URL/socket errors, timeouts and 5xx responses during the
        reboot window (the device is briefly unreachable). Sleeps
        ``poll_interval_s`` between attempts.

        :raises WiCANConfigError: if ``expected`` is not observed within
            ``timeout_s``.
        """
        deadline = time.monotonic() + timeout_s
        last_error: str = "device never came back up"
        attempt = 0
        while True:
            attempt += 1
            # Cap each poll's read timeout to whatever wall-clock time is left
            # so a slow read can't overshoot the overall deadline by up to one
            # full ``timeout_s``. Floor at a small positive value so the final
            # attempt still actually tries rather than passing a 0/negative
            # timeout to urllib.
            remaining = deadline - time.monotonic()
            poll_timeout = max(0.001, min(self.timeout_s, remaining))
            try:
                actual = get_top_level_protocol(
                    self.read_config_raw(timeout_s=poll_timeout)
                )
                if actual == expected:
                    logger.info(
                        "WiCAN reported protocol %r after %d poll(s).",
                        expected,
                        attempt,
                    )
                    return
                last_error = (
                    f"device reports protocol {actual!r}, expected {expected!r}"
                )
            except WiCANConfigError as exc:
                # Unreachable / 5xx mid-reboot — keep waiting.
                last_error = str(exc)
                logger.debug("Reboot poll %d not ready: %s", attempt, exc)

            if time.monotonic() >= deadline:
                raise WiCANConfigError(
                    f"timed out after {timeout_s:.0f}s waiting for WiCAN to report "
                    f"protocol {expected!r}: {last_error}"
                )
            # Clamp the inter-poll sleep to the remaining budget so the total
            # wait cannot exceed reboot_timeout_s by a whole poll interval.
            time.sleep(min(self.poll_interval_s, max(0.0, deadline - time.monotonic())))


#: Default per-call timeout (s) for the ``/datalog`` coordination endpoint. Short:
#: the endpoint is local + synchronous, and EVERY failure soft-degrades to "rely on
#: the firmware FLASH_ACTIVE_BIT interlock", never aborts a flash.
DATALOG_TIMEOUT_S = 5.0


class WiCANDatalogClient:
    """REST client for the no-reboot coexistence ``/datalog`` endpoint (firmware #36.C).

    Pauses/resumes the WiCAN datalogger around a flash WITHOUT the reboot-switch.
    Deliberately INDEPENDENT of :class:`WiCANConfigurator`: keyed only on ``host``,
    fired at the flash boundary, with its own host-keyed crash-recovery sidecar
    (separate file from the protocol-recovery sidecar so the two never collide).

    BRICK-SAFETY CONTRACT: the firmware ``FLASH_ACTIVE_BIT`` interlock is the real
    guarantee; this REST pause is an *advisory* pre-park that also stops SD logging.
    So every method here is FAILURE-TOLERANT — a 404/405 (a port-only ``NCFRv6``
    build with no ``/datalog``), a timeout, or an unreachable device is logged and
    swallowed, returning ``None``, NEVER raised into the flash path. A broken
    ``/datalog`` must not abort a flash. (Decouples the ``/datalog`` capability from
    the dedicated-port rev gate, per the #36 audit.)
    """

    def __init__(
        self,
        host: str,
        http_port: int = 80,
        timeout_s: float = DATALOG_TIMEOUT_S,
        keepalive_interval_s: float = DATALOG_KEEPALIVE_INTERVAL_S,
    ) -> None:
        self.host = host
        self.http_port = http_port
        self.timeout_s = timeout_s
        self._keepalive_interval_s = keepalive_interval_s
        # Dead-man's-switch lease tokens issued by the firmware (None on a pre-deadman
        # build, which simply has no lease/reaper — we degrade to a plain pause/resume).
        self._park_token: Optional[int] = None
        self._claim_token: Optional[int] = None
        # Keepalive daemon: renews BOTH leases while a pause/claim is held so the
        # firmware reaper never false-expires under a present host. Stopped via an
        # explicit Event (NOT GC/__del__) so a leaked thread can't pin a lease, and
        # registered with atexit as the final backstop.
        self._ka_lock = threading.Lock()
        self._ka_thread: Optional[threading.Thread] = None
        self._ka_stop: Optional[threading.Event] = None
        self._atexit_registered = False

    def _url(self, path: str) -> str:
        return f"http://{self.host}:{self.http_port}{path}"

    @staticmethod
    def _op_path(op: str, **params: object) -> str:
        """Build a ``/datalog?op=<op>[&k=v...]`` path, omitting None-valued params."""
        query = f"/datalog?op={op}"
        for key, value in params.items():
            if value is not None:
                query += f"&{key}={value}"
        return query

    # -- Crash-recovery sidecar (host-keyed; distinct from protocol recovery) --

    @property
    def recovery_path(self) -> str:
        """Stable, host-keyed path of the datalog-pause breadcrumb file."""
        return _host_keyed_temp_path(self.host, "wican_datalog")

    def _mark_stopped(self) -> None:
        """Record (before the pause request) that THIS host paused the datalogger,
        plus whatever lease tokens we currently hold, so a hard kill mid-flash is
        reconciled on the next connect. Best-effort; never fails a flash."""
        try:
            with open(self.recovery_path, "w", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "host": self.host,
                            "datalog_stopped": True,
                            "park_token": self._park_token,
                            "claim_token": self._claim_token,
                        }
                    )
                )
        except OSError as exc:  # sidecar is best-effort; never fail a flash over it
            logger.debug("datalog sidecar write failed (non-fatal): %s", exc)

    def _read_breadcrumb(self) -> Optional[dict]:
        """Return THIS host's datalog-pause breadcrumb dict, or ``None``."""
        try:
            with open(self.recovery_path, "r", encoding="utf-8") as fh:
                data = json.loads(fh.read())
        except (OSError, ValueError, TypeError):
            return None
        if (
            isinstance(data, dict)
            and data.get("host") == self.host
            and bool(data.get("datalog_stopped"))
        ):
            return data
        return None

    def _is_marked_stopped(self) -> bool:
        """True iff a breadcrumb for THIS host says the datalogger was left paused."""
        return self._read_breadcrumb() is not None

    def clear_stopped(self) -> None:
        """Best-effort delete of the datalog breadcrumb (ignore if missing)."""
        try:
            os.unlink(self.recovery_path)
        except OSError:
            pass

    # -- HTTP (soft-degrading) --------------------------------------------------

    def _request_ex(self, method: str, path: str):
        """Issue ``method path`` -> ``(status, data)``.

        ``status`` is the HTTP status code if the device answered (200/404/409/500…)
        or ``None`` if it was unreachable / timed out. ``data`` is the parsed JSON
        dict on a 200-with-JSON, else ``None``. NEVER raises — every failure degrades
        so a broken ``/datalog`` can never abort a flash. The status is surfaced so a
        caller can treat a 409 ("already auto-reaped by the firmware reaper") as
        success rather than a failure.
        """
        url = self._url(path)
        try:
            req = urllib.request.Request(url, method=method)
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            logger.info(
                "%s %s -> HTTP %s (datalog unsupported/stale; firmware interlock holds)",
                method,
                path,
                exc.code,
            )
            return exc.code, None
        except (urllib.error.URLError, socket.error, OSError) as exc:
            logger.info(
                "%s %s failed (%s); relying on firmware interlock", method, path, exc
            )
            return None, None
        if status != 200:
            logger.info(
                "%s %s -> HTTP %s; relying on firmware interlock", method, path, status
            )
            return status, None
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            logger.info("%s %s returned non-JSON; ignoring", method, path)
            return status, None
        return status, (data if isinstance(data, dict) else None)

    def _request(self, method: str, path: str) -> Optional[dict]:
        """Issue ``method path``; return the parsed JSON dict, else ``None`` (any
        non-200 / failure). Thin wrapper over :meth:`_request_ex`."""
        _status, data = self._request_ex(method, path)
        return data

    # -- Dead-man's-switch lease: pause / claim / keepalive / resume ------------

    def pause(self) -> Optional[dict]:
        """Pause the datalogger for a flash; return the state dict or ``None``.

        Writes the crash-recovery breadcrumb BEFORE the request (so a hard kill
        between here and :meth:`resume` is reconciled on the next connect), captures
        the firmware's park-lease token, and starts the keepalive daemon so the lease
        cannot false-expire while we hold the pause (e.g. across a multi-minute read).
        """
        self._mark_stopped()
        state = self._request("POST", self._op_path("pause"))
        if state is not None:
            tok = state.get("park_token")
            self._park_token = tok if isinstance(tok, int) else None
            self._mark_stopped()  # rewrite now that we know the token
            self._ensure_keepalive()
        return state

    def bus_claim(self) -> Optional[dict]:
        """Claim the CAN bus for the WHOLE host-driven programming window.

        Raised BEFORE the UDS auth handshake and held until :meth:`bus_release` (after
        ``fast_write`` returns). This is the brick fence the codec's ``FLASH_ACTIVE_BIT``
        does NOT cover: while the host drives the 0x10/0x27 session the firmware sets
        ``HOST_BUS_CLAIM_BIT`` so the reaper can never auto-resume the poller into a
        live, security-unlocked session. Captures the claim token + starts keepalive.
        Soft-degrading: a pre-deadman build (404) returns ``None`` and the flash
        proceeds on the ``FLASH_ACTIVE_BIT`` interlock alone.
        """
        state = self._request("POST", self._op_path("bus_claim"))
        if state is not None:
            tok = state.get("claim_token")
            self._claim_token = tok if isinstance(tok, int) else None
            self._mark_stopped()
            self._ensure_keepalive()
        return state

    def bus_release(self) -> Optional[dict]:
        """Release the host bus-claim after ``fast_write`` returns (or fails).

        Token-matched on the firmware; a 409 (already reaped) is treated as success.
        Does NOT stop the keepalive while a park is still held — :meth:`resume` does.
        """
        _status, state = self._request_ex(
            "POST", self._op_path("bus_release", token=self._claim_token)
        )
        self._claim_token = None  # released (200) or already-reaped (409): drop it
        self._maybe_stop_keepalive()
        return state

    def resume(self) -> Optional[dict]:
        """Resume the datalogger; clear the breadcrumb + stop the keepalive.

        Token-matched on the firmware. A 409 means the reaper already auto-resumed
        (the host had been gone) — treated as success. We always drop our local park
        state and stop the keepalive, but clear the breadcrumb only when the firmware
        actually answered (200 resumed / 409 already-done); a genuine unreachable or
        timeout (status ``None``) leaves the breadcrumb so the NEXT connect's
        reconcile retries.
        """
        status, state = self._request_ex(
            "POST", self._op_path("resume", token=self._park_token)
        )
        self._park_token = None
        self._maybe_stop_keepalive()
        if status in (200, 409):  # resumed, or reaper already did -> breadcrumb done
            self.clear_stopped()  # a 404/500/timeout leaves it for the next reconcile
        return state

    def get_state(self) -> Optional[dict]:
        """``GET /datalog`` -> live coexistence state dict (or ``None``).

        Deadman build: ``{ok, flash_active, datalog_parked, host_bus_claimed,
        manual_mode, park_token, lease_ttl_ms, claim_ttl_ms, bus_idle_ms,
        stuck_flash_alarm}``.
        """
        return self._request("GET", "/datalog")

    # -- Keepalive daemon -------------------------------------------------------

    def _ensure_keepalive(self) -> None:
        """Start the keepalive daemon if not already running (idempotent)."""
        with self._ka_lock:
            if self._ka_thread is not None and self._ka_thread.is_alive():
                return
            self._ka_stop = threading.Event()
            self._ka_thread = threading.Thread(
                target=self._keepalive_loop,
                args=(self._ka_stop,),
                name=f"wican-datalog-keepalive-{self.host}",
                daemon=True,
            )
            self._ka_thread.start()
            if not self._atexit_registered:
                atexit.register(self._stop_keepalive)
                self._atexit_registered = True

    def _maybe_stop_keepalive(self) -> None:
        """Stop the keepalive once BOTH leases have been released."""
        if self._park_token is None and self._claim_token is None:
            self._stop_keepalive()

    def _stop_keepalive(self) -> None:
        """Signal + join the keepalive daemon (bounded). Safe to call repeatedly."""
        with self._ka_lock:
            stop, thread = self._ka_stop, self._ka_thread
            self._ka_stop, self._ka_thread = None, None
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self.timeout_s + 1.0)

    def _send_keepalive(self) -> None:
        """One keepalive POST renewing whichever leases we currently hold."""
        if self._park_token is None and self._claim_token is None:
            return
        self._request(
            "POST",
            self._op_path(
                "keepalive",
                park_token=self._park_token,
                claim_token=self._claim_token,
            ),
        )

    def _keepalive_loop(self, stop: threading.Event) -> None:
        """Renew the leases every ``keepalive_interval_s`` until signalled to stop.

        ``Event.wait`` returns True at once when stopped (prompt teardown) and False
        on each interval tick, when we fire a renewal.
        """
        while not stop.wait(self._keepalive_interval_s):
            try:
                self._send_keepalive()
            except Exception as exc:  # never let the daemon die on a transient error
                logger.debug("datalog keepalive tick failed (non-fatal): %s", exc)

    def close(self) -> None:
        """Release everything: stop the keepalive daemon and drop local lease state.

        Called from session teardown / window close so a disconnected flasher never
        leaves a host thread renewing a lease. Does NOT itself POST resume (the worker
        ``finally`` / firmware reaper own that) — it just stops our renewals so the
        lease can legitimately expire if it was somehow left armed.
        """
        self._park_token = None
        self._claim_token = None
        self._stop_keepalive()

    def reconcile(self) -> None:
        """At connect: resume a datalogger left paused by a prior (crashed) run.

        No-op unless THIS host has a breadcrumb. Token-aware + brick-safe:

          * If the device reports an ACTIVE flash OR an outstanding host bus-claim, a
            programming session is live (this instance OR a second one) — LEAVE it
            paused; its owner (or the firmware reaper) resumes it. This replaces the
            old, INCORRECT belief that the pre-flash pause window is harmless to
            resume into: the auth window is exactly the unfenced-by-FLASH_ACTIVE_BIT
            danger zone, now covered by ``host_bus_claimed``.
          * Otherwise resume with the breadcrumb's park token. A 409 means the
            firmware reaper already auto-resumed after we vanished — treated as
            success. Fully soft-degrading; a genuine failure leaves the breadcrumb.
        """
        crumb = self._read_breadcrumb()
        if crumb is None:
            return
        state = self.get_state()
        if state is not None and (
            state.get("flash_active") or state.get("host_bus_claimed")
        ):
            logger.info(
                "datalog reconcile skipped: a flash/claim is active "
                "(this or another NC Flash instance owns the bus)"
            )
            return
        tok = crumb.get("park_token")
        self._park_token = tok if isinstance(tok, int) else None
        logger.info(
            "datalog reconcile: resuming a datalogger left paused by a prior run"
        )
        self.resume()
