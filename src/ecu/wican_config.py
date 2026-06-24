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

import contextlib
import json
import logging
import os
import re
import socket
import tempfile
import time
import urllib.error
import urllib.request
from typing import Optional

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
        safe_host = re.sub(r"[^A-Za-z0-9]", "_", self.host)
        return os.path.join(tempfile.gettempdir(), f"wican_recovery_{safe_host}.json")

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
