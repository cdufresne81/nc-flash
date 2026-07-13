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
from typing import Callable, Optional

from .constants import (
    DATALOG_KEEPALIVE_INTERVAL_S,
    PRE_SESSION_SETTLE_S,
    WICAN_CSV_TRIP_LEASE_MS,
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
        # Refcounted bus reservation (claim + pause). The whole-session reservation
        # (held connect->disconnect) and the flash fence share ONE owner: the real
        # bus_claim()+pause() fire once on the 0->1 transition, bus_release()+resume()
        # once on the 1->0, so nesting can never double-claim the single-owner lease.
        # RLock: the transition work runs under the lock and must be atomic w.r.t.
        # another thread's acquire/release (connect on the UI thread vs the flash
        # worker thread).
        self._reserve_lock = threading.RLock()
        self._reserve_depth = 0
        # Live-trip lifecycle (Live Datalog button). While a leased manual trip streams,
        # the physical park/claim leases are LIFTED (the poller must drive the bus to
        # produce rows) without touching the logical refcount — `_suspended` records that
        # divergence so the physical leases are re-armed when the trip ends. `_live_trip`
        # keeps the keepalive daemon renewing the firmware csv lease (the trip's own
        # dead-man). `_silent_hold` is the one logical ref the trip's STOP takes so
        # "Stop Live Datalog" leaves the device parked (silent) until the app closes,
        # the next trip starts, or release_trip_hold().
        self._suspended = False
        self._live_trip = False
        self._silent_hold = False
        # External-stop detection (web-UI Stop Trip mid-stream). The keepalive renews
        # the csv lease with op=renew, which the firmware 409s once the trip is no
        # longer manual-ON: `_trip_external_stop` remembers that (end_live_trip then
        # leaves the mode to whoever set it), `_trip_external_cb` is the one-shot
        # notification begin_live_trip registered (fires on the keepalive thread).
        # `_csv_renew_legacy` degrades pre-renew firmware back to the old re-start
        # heartbeat (which fights a web Stop — exactly why op=renew exists).
        self._trip_external_stop = False
        self._trip_external_cb: Optional[Callable[[], None]] = None
        self._csv_renew_legacy = False
        # Bulk-transfer window (trip-log download). The device's httpd is a single
        # task: while it streams a multi-MB file every keepalive tick times out by
        # construction, so the tick's failure line is demoted to DEBUG for the
        # window's duration (field incident 2026-07-12: a 2-minute sync logged the
        # same INFO line 11 times). Logging-only — the keepalive itself still fires
        # every tick and renews the leases the moment the httpd frees up between
        # files; the firmware interlock covers the in-file gaps, as designed.
        self._bulk_quiet = False

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

    @staticmethod
    def _csv_op_path(op: str, **params: object) -> str:
        """Build a ``/csv_logger?op=<op>[&k=v...]`` path, omitting None-valued params."""
        query = f"/csv_logger?op={op}"
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

    def _request_ex(self, method: str, path: str, quiet: bool = False):
        """Issue ``method path`` -> ``(status, data)``.

        ``status`` is the HTTP status code if the device answered (200/404/409/500…)
        or ``None`` if it was unreachable / timed out. ``data`` is the parsed JSON
        dict on a 200-with-JSON, else ``None``. NEVER raises — every failure degrades
        so a broken ``/datalog`` can never abort a flash. The status is surfaced so a
        caller can treat a 409 ("already auto-reaped by the firmware reaper") as
        success rather than a failure.

        ``quiet`` demotes the failure lines to DEBUG — logging only, nothing else
        changes. Used by keepalive ticks during a bulk transfer, where timeouts are
        expected by construction (single-task device httpd).
        """
        log_failure = logger.debug if quiet else logger.info
        url = self._url(path)
        try:
            req = urllib.request.Request(url, method=method)
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            log_failure(
                "%s %s -> HTTP %s (datalog unsupported/stale; firmware interlock holds)",
                method,
                path,
                exc.code,
            )
            return exc.code, None
        except (urllib.error.URLError, socket.error, OSError) as exc:
            log_failure(
                "%s %s failed (%s); relying on firmware interlock", method, path, exc
            )
            return None, None
        if status != 200:
            log_failure(
                "%s %s -> HTTP %s; relying on firmware interlock", method, path, status
            )
            return status, None
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            log_failure("%s %s returned non-JSON; ignoring", method, path)
            return status, None
        return status, (data if isinstance(data, dict) else None)

    def _request(self, method: str, path: str, quiet: bool = False) -> Optional[dict]:
        """Issue ``method path``; return the parsed JSON dict, else ``None`` (any
        non-200 / failure). Thin wrapper over :meth:`_request_ex`."""
        _status, data = self._request_ex(method, path, quiet=quiet)
        return data

    def set_bulk_transfer(self, active: bool) -> None:
        """Mark a bulk HTTP transfer window (trip-log download) open/closed.

        While open, keepalive-tick failures log at DEBUG instead of INFO — during
        a large file download against the single-task device httpd every tick
        times out by construction, and 10+ identical INFO lines per sync read as
        an incident when they are the designed interlock fallback. Logging-only:
        the ticks still fire and renew the leases between files.
        """
        self._bulk_quiet = bool(active)

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

    # -- Refcounted whole-session bus reservation -------------------------------

    @contextlib.contextmanager
    def reserved(self):
        """Hold a refcounted bus reservation (claim + pause) for the ``with`` block.

        Reference-counted so the whole-session reservation (held from connect to
        disconnect) and the flash ``_datalog_fence`` can NEST without a double-claim:
        the real :meth:`bus_claim` + :meth:`pause` (and the poll-task settle) fire once
        on the 0->1 transition, and :meth:`bus_release` + :meth:`resume` once on the
        1->0. Soft-degrading — on a device with no ``/datalog`` the underlying ops are
        no-ops and only the depth counter moves.
        """
        self.acquire_bus()
        try:
            yield
        finally:
            self.release_bus()

    def acquire_bus(self) -> None:
        """Raise (or re-enter) the host bus reservation; see :meth:`reserved`.

        Only the FIRST acquire claims the bus, parks the datalogger, and settles long
        enough for the firmware poll task to actually park before the first ECU
        contact. Deeper acquires just bump the refcount — the bus is already ours.
        """
        with self._reserve_lock:
            self._reserve_depth += 1
            if self._reserve_depth == 1:
                self.bus_claim()
                self.pause()
                # Let the firmware poll task observe the park and stop driving the bus
                # before the caller's first Tester-Present / auth handshake.
                time.sleep(PRE_SESSION_SETTLE_S)

    def release_bus(self) -> None:
        """Drop one bus-reservation ref; the LAST release frees the bus.

        Mirrors :meth:`bus_claim`/:meth:`pause` ordering on the way out
        (``bus_release`` then ``resume``) so the keepalive daemon stops only once both
        leases are dropped. A release with no outstanding acquire is a no-op. When the
        physical leases are suspended for a live trip, the last release just clears the
        suspension — there is nothing physical to release, and the running trip keeps
        its own (csv-lease) dead-man.
        """
        with self._reserve_lock:
            if self._reserve_depth == 0:
                return
            self._reserve_depth -= 1
            if self._reserve_depth == 0:
                if self._suspended:
                    self._suspended = False
                else:
                    self.bus_release()
                    self.resume()

    # -- Live-trip lifecycle (Live Datalog button; leased manual trip) ----------

    def begin_live_trip(
        self, on_external_stop: Optional[Callable[[], None]] = None
    ) -> None:
        """Start a NEW leased manual trip: un-park the bus, rotate to a fresh file.

        Order matters: the physical leases are lifted FIRST (``resume`` restores the
        pre-pause mode, and the poller must drive the bus again to produce rows), then
        ``op=start&rotate=1&lease_ms=`` forces logging ON into a fresh trip file with
        the firmware csv lease armed. The logical refcount is untouched — whoever holds
        refs (ECU session / silent hold) gets the park back in :meth:`end_live_trip`.
        Soft-degrading like every op here: on pre-lease firmware the extra params are
        ignored and this is a plain manual start.

        ``on_external_stop`` fires AT MOST ONCE, on the keepalive thread, if the trip
        is stopped out from under us (web-UI Stop Trip → the ``op=renew`` heartbeat
        409s). The caller ends its stream; :meth:`end_live_trip` then leaves the
        device's mode alone — the external operator's Stop must win.
        """
        with self._reserve_lock:
            if self._reserve_depth > 0 and not self._suspended:
                self.bus_release()
                self.resume()
                self._suspended = True
            self._trip_external_stop = False
            self._trip_external_cb = on_external_stop
            self._live_trip = (
                True  # before _ensure_keepalive: ticks must renew the lease
            )
            self._request(
                "POST",
                self._csv_op_path("start", rotate=1, lease_ms=WICAN_CSV_TRIP_LEASE_MS),
            )
            self._ensure_keepalive()

    def end_live_trip(self) -> None:
        """End the leased trip: re-park for any ref holders, device back to AUTO.

        Order matters: outstanding refs (ECU session, silent hold) get the physical
        park/claim re-armed FIRST — while the manual trip still owns the mode, so no
        instant of un-parked AUTO exists where a record could open a stub trip file.
        The pause snapshots the then-current mode ("on"), which is exactly why
        ``op=auto`` runs after: it restores follow-ignition as the steady state
        EVERYWHERE — current mode, the firmware's pre-pause restore target, and the
        csv lease. Idempotent; safe on a trip that never started.

        Externally-stopped trip (web-UI Stop Trip → ``op=renew`` 409): the mode ops
        are SKIPPED entirely — the operator at the device set it OFF and an ``op=auto``
        here would flip their explicit Stop back to follow-ignition (the same
        who-owns-the-mode fight the renewal heartbeat just lost, from the other side).
        The physical re-park for ref holders still happens.
        """
        with self._reserve_lock:
            external = self._trip_external_stop
            self._trip_external_stop = False
            self._trip_external_cb = None
            if self._suspended:
                self._suspended = False
                if self._reserve_depth > 0:
                    self.bus_claim()
                    self.pause()
                    time.sleep(PRE_SESSION_SETTLE_S)
            if not external:
                status, _ = self._request_ex("POST", self._csv_op_path("auto"))
                if status is not None and status != 200:
                    # Pre-op=auto firmware (400): best-effort op=stop — a sticky OFF
                    # beats an orphaned FORCE_ON filling the SD until reboot.
                    self._request("POST", self._csv_op_path("stop"))
            self._live_trip = False
            self._maybe_stop_keepalive()

    def hold_silent(self) -> None:
        """Take the trip's STOP ref: keep the device parked after a stopped trip.

        One logical ref, taken at most once, so "Stop Live Datalog" means the device
        stays quiet (no polling, no SD logging) until :meth:`release_trip_hold`, the
        next :meth:`begin_live_trip` (which lifts the physical leases but keeps the
        ref), or the firmware reaper if this host dies. No-op while already held.
        """
        with self._reserve_lock:
            if self._silent_hold:
                return
            self._silent_hold = True
            self.acquire_bus()

    def release_trip_hold(self) -> None:
        """Drop the silent-hold ref (app close / feature teardown). No-op if unheld."""
        with self._reserve_lock:
            if not self._silent_hold:
                return
            self._silent_hold = False
            self.release_bus()

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
        """Stop the keepalive once both leases are released AND no live trip runs."""
        if (
            self._park_token is None
            and self._claim_token is None
            and not self._live_trip
        ):
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
        """One keepalive POST renewing whichever leases we currently hold.

        A live trip renews the firmware csv lease with ``op=renew`` — a heartbeat
        that can NEVER (re)start logging. The old re-``start`` form silently
        restarted a trip the operator had just stopped from the device's web UI
        (field incident, 2026-07-11): every Stop lost to the next 4 s tick. A 409
        now means exactly that — the trip was stopped under us — so we remember it
        and fire the one-shot ``on_external_stop`` callback; a 400 means pre-renew
        firmware, where we degrade back to the re-start heartbeat (accepting its
        known fight) rather than losing the dead-man entirely.
        """
        quiet = self._bulk_quiet
        if self._live_trip and not self._trip_external_stop:
            if self._csv_renew_legacy:
                self._request(
                    "POST",
                    self._csv_op_path("start", lease_ms=WICAN_CSV_TRIP_LEASE_MS),
                    quiet=quiet,
                )
            else:
                status, _ = self._request_ex(
                    "POST",
                    self._csv_op_path("renew", lease_ms=WICAN_CSV_TRIP_LEASE_MS),
                    quiet=quiet,
                )
                if status == 409:
                    self._trip_external_stop = True
                    callback, self._trip_external_cb = self._trip_external_cb, None
                    logger.info(
                        "live trip was stopped at the device (web UI); "
                        "ending the host stream and leaving the mode alone"
                    )
                    if callback is not None:
                        callback()
                elif status == 400:
                    self._csv_renew_legacy = True
                    self._request(
                        "POST",
                        self._csv_op_path("start", lease_ms=WICAN_CSV_TRIP_LEASE_MS),
                    )
        if self._park_token is None and self._claim_token is None:
            return
        self._request(
            "POST",
            self._op_path(
                "keepalive",
                park_token=self._park_token,
                claim_token=self._claim_token,
            ),
            quiet=quiet,
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
        self._live_trip = False
        self._suspended = False
        self._silent_hold = False
        self._trip_external_stop = False
        self._trip_external_cb = None
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


# Per-host shared WiCANDatalogClient registry. The firmware issues a FRESH token on
# every lease arm, so two independent client instances against one device clobber each
# other's leases (the first holder's renewals start 409ing and its park drops early).
# Every lease holder — the ECU session's whole-session reservation, the flash fence,
# the live-datalog trip — must therefore route through ONE client per device, whose
# refcount + suspension state stay coherent. Keyed by host:port; never evicted (a
# handful of small objects per run at most).
_datalog_clients: dict = {}
_datalog_clients_lock = threading.Lock()


def get_datalog_client(host: str, http_port: int = 80) -> WiCANDatalogClient:
    """THE way to obtain a :class:`WiCANDatalogClient` — one shared instance per device."""
    key = f"{host}:{http_port}"
    with _datalog_clients_lock:
        client = _datalog_clients.get(key)
        if client is None:
            client = WiCANDatalogClient(host, http_port=http_port)
            _datalog_clients[key] = client
        return client


def peek_datalog_client(host: str, http_port: int = 80) -> Optional[WiCANDatalogClient]:
    """Return the existing shared client for this device, or ``None``.

    For callers that only want to nudge an ALREADY-active client (e.g. the
    trip-log sync quieting keepalive noise) — when no session holds a client,
    there are no keepalives to quiet and nothing should be created.
    """
    with _datalog_clients_lock:
        return _datalog_clients.get(f"{host}:{http_port}")
