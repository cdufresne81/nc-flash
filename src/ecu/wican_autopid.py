"""Send the Datalogger's channel list to the WiCAN's own poller.

The WiCAN firmware polls the ECU itself and records what its ``auto_pid.json``
lists; NC Flash then follows that recording over the live stream. This module
is the only place NC Flash WRITES that file (``wican_config`` only reads).

The contract (firmware ``main/config_server.c``):

- ``GET /load_auto_pid`` returns the whole file, or the word ``NONE``.
- ``POST /store_auto_data`` replaces the whole file. It holds more than the
  channel table (``format``, ``initialisation``, the voltage gates,
  ``std_pids``, ``can_filters``, ``calculated``...), so :func:`replace_pids`
  swaps ``pids`` and keeps every other key exactly as read. The reply says how
  the change applies: ``live`` (at once), ``deferred`` (when the trip being
  recorded ends) or ``reboot``.
- ``GET /poll_status`` reports ``reload_pending`` / ``reload_ok`` and
  ``pids_unpollable`` (enabled rows the poller could not use).

A ``deferred`` change is never forced by closing and restarting the trip: the
released firmware's ``op=start`` leaves its logger forced ON for good (it has
no ``op=auto`` to go back to ignition-gated recording). The user ends the trip
with the ignition instead.

A wrong table can't hurt the ECU (the firmware only reads), but it changes
what the WiCAN logs on its own, so the file read before a change is saved
locally first and can be put back with :meth:`WiCANAutoPid.restore`.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from . import wican_http
from .exceptions import ECUError

logger = logging.getLogger(__name__)

DEFAULT_HTTP_PORT = 80
#: The firmware refuses files over 2000 KB; a real table is a few KB.
MAX_FILE_BYTES = 2_000_000
REQUIRED_FORMAT = "nc-flash-1"
APPLY_TIMEOUT_S = 15.0

APPLIED_LIVE = "live"
APPLIED_DEFERRED = "deferred"
APPLIED_REBOOT = "reboot"

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class WiCANAutoPidError(ECUError):
    """The WiCAN's channel table could not be read, changed or verified."""


def replace_pids(original: dict, rows: list[dict]) -> dict:
    """A copy of *original* with ``pids`` replaced by *rows*; all else kept."""
    if original.get("format") != REQUIRED_FORMAT:
        raise WiCANAutoPidError(
            "The WiCAN's channel file is not in the format NC Flash knows "
            f"(format {original.get('format')!r}); not changing it."
        )
    updated = dict(original)
    updated["pids"] = list(rows)
    return updated


def orphaned_calculated(original: dict, rows: list[dict]) -> list[str]:
    """Enabled calculated channels whose formula names a channel that the new
    table (plus the broadcast and calculated channels) no longer has."""
    names = {str(r.get("Name")) for r in rows if r.get("enabled", True)}
    for flt in original.get("can_filters") or []:
        for param in (flt or {}).get("parameters") or []:
            if isinstance(param, dict):
                names.add(str(param.get("name")))
    calcs = [c for c in original.get("calculated") or [] if isinstance(c, dict)]
    names |= {str(c.get("name")) for c in calcs}
    out = []
    for calc in calcs:
        if calc.get("enabled") is False:
            continue
        used = set(_IDENT.findall(str(calc.get("expression", ""))))
        if used - names:
            out.append(str(calc.get("name", "?")))
    return out


class WiCANAutoPid:
    """HTTP client for the WiCAN's channel table (one device)."""

    def __init__(
        self,
        host: str,
        *,
        http_port: int = DEFAULT_HTTP_PORT,
        timeout_s: float = wican_http.DEFAULT_TIMEOUT_S,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.host = host
        self.http_port = http_port
        self._timeout_s = timeout_s
        self._sleep = sleep
        self._clock = clock
        #: The backup written by the last :meth:`push` ("" before one is written).
        self.last_backup = ""
        self._cancelled = False

    def cancel(self):
        """Stop waiting in :meth:`wait_applied` (app closing); thread-safe."""
        self._cancelled = True

    def _url(self, path: str) -> str:
        return f"http://{self.host}:{self.http_port}{path}"

    # --- read -----------------------------------------------------------------

    def load(self) -> tuple[str, dict]:
        """The file as served (for the backup) and parsed."""
        raw = wican_http.fetch_head(
            self._url("/load_auto_pid"), MAX_FILE_BYTES, timeout_s=self._timeout_s
        ).decode("utf-8", errors="replace")
        if raw.strip() == "NONE":
            raise WiCANAutoPidError(
                "The WiCAN has no channel file yet. Set it up once in the WiCAN's "
                "web page (Logger), then send the channels again."
            )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise WiCANAutoPidError(
                "The WiCAN's channel file could not be read; not changing it."
            ) from exc
        if not isinstance(data, dict):
            raise WiCANAutoPidError("The WiCAN's channel file is not a JSON object.")
        return raw, data

    def poll_status(self) -> dict:
        return wican_http.get_json(self._url("/poll_status"), timeout_s=self._timeout_s)

    # --- write ----------------------------------------------------------------

    def store(self, data: dict) -> dict:
        """POST the whole file; return the device's reply (may be {})."""
        reply = wican_http.post_json(
            self._url("/store_auto_data"), data, timeout_s=self._timeout_s
        )
        return reply if isinstance(reply, dict) else {}

    def wait_applied(self, timeout_s: float = APPLY_TIMEOUT_S) -> dict:
        """Poll ``/poll_status`` until the new table is in use; return it.

        Raises :class:`WiCANAutoPidError` if the poller reports a failed reload
        or refuses some rows. Still pending at *timeout_s* (a trip opened in
        between, so the firmware waits for it to end) returns the status with
        ``reload_pending`` true: the list applies after that trip.
        """
        deadline = self._clock() + timeout_s
        status: dict = {}
        while True:
            status = self.poll_status()
            if "reload_pending" not in status:
                # Firmware without the reload report: nothing more to check.
                return status
            if not status.get("reload_pending"):
                if status.get("reload_ok") is False:
                    raise WiCANAutoPidError(
                        "The WiCAN could not load the new channel list (it keeps "
                        "the previous one)."
                    )
                unpollable = int(status.get("pids_unpollable") or 0)
                if unpollable:
                    raise WiCANAutoPidError(
                        f"The WiCAN can't poll {unpollable} of the channels "
                        "(check their mode and identifier)."
                    )
                return status
            if self._cancelled or self._clock() >= deadline:
                return status
            self._sleep(0.5)

    # --- the whole push -------------------------------------------------------

    def preview(self, rows: list[dict]) -> dict:
        """What :meth:`push` of *rows* would change; writes nothing.

        ``removed``: enabled channels the WiCAN logs now that it would stop
        logging. ``added``: enabled channels it would start logging.
        ``orphaned``: calculated channels that would lose an input.
        ``rpm``: whether an enabled ``RPM`` channel remains (the firmware
        detects a running engine, and gates its recording, on it).
        """
        _, original = self.load()
        replace_pids(original, rows)  # refuses an unknown format up front
        before = {
            str(p.get("Name"))
            for p in original.get("pids") or []
            if isinstance(p, dict) and p.get("enabled", True)
        }
        after = {str(r.get("Name")) for r in rows if r.get("enabled", True)}
        return {
            "removed": sorted(before - after),
            "added": sorted(after - before),
            "orphaned": orphaned_calculated(original, rows),
            "rpm": "RPM" in after,
            "count": len(after),
        }

    def push(self, rows: list[dict], backup_dir) -> "PushResult":
        """Back up the current file, then store it with *rows* as ``pids``.

        Does not wait for the change to apply: see :meth:`wait_applied`.
        """
        self.last_backup = ""
        raw, original = self.load()
        updated = replace_pids(original, rows)
        backup = save_backup(backup_dir, self.host, raw)
        self.last_backup = str(backup)
        reply = self.store(updated)
        applied = str(reply.get("applied") or "")
        logger.info(
            "Sent %d channel(s) to the WiCAN (%s); previous list saved to %s",
            len(rows),
            applied or "no apply report",
            backup,
        )
        return PushResult(
            applied=applied,
            backup=str(backup),
            orphaned_calculated=orphaned_calculated(original, rows),
            message=str(reply.get("msg") or ""),
        )

    def restore(self, backup_path) -> dict:
        """Put back a file saved by :meth:`push`."""
        data = json.loads(Path(backup_path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("format") != REQUIRED_FORMAT:
            raise WiCANAutoPidError(f"{backup_path} is not a WiCAN channel file.")
        reply = self.store(data)
        if reply.get("applied") in ("live", None, ""):
            self.wait_applied()
        return reply


class PushResult:
    def __init__(
        self,
        applied: str,
        backup: str,
        orphaned_calculated: Optional[list[str]] = None,
        message: str = "",
    ):
        self.applied = applied
        self.backup = backup
        self.orphaned_calculated = orphaned_calculated or []
        self.message = message


def save_backup(backup_dir, host: str, raw: str) -> Path:
    """Write the file as read from the device; never overwrite an older one."""
    folder = Path(backup_dir)
    folder.mkdir(parents=True, exist_ok=True)
    safe_host = "".join(c if c.isalnum() or c in "-." else "_" for c in host)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = folder / f"auto_pid_{safe_host}_{ts}.json"
    n = 2
    while path.exists():
        path = folder / f"auto_pid_{safe_host}_{ts}-{n}.json"
        n += 1
    path.write_text(raw, encoding="utf-8")
    return path
