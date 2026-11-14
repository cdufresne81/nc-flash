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

:class:`WiCANSessionList` is the other way to choose what the WiCAN polls: a
TEMPORARY list for one Datalogger run, held in the WiCAN's memory only. The
file above is never touched, and the WiCAN returns to it when the run ends or
NC Flash goes away (see ``docs/internal/WICAN_SESSION_CHANNELS.md``).
"""

from __future__ import annotations

import hashlib
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

#: How long the WiCAN keeps a temporary list without hearing from NC Flash.
SESSION_TTL_S = 30
#: ...so the lease is renewed this often, also while waiting for the list
#: to be applied.
SESSION_RENEW_S = 10.0
SESSION_RENEW_TIMEOUT_S = 5.0
#: The firmware refuses a temporary list over 64 KB.
MAX_SESSION_BYTES = 64 * 1024
#: A WiCAN that can't close its open trip gives up rolling after 15-20 s.
SESSION_APPLY_TIMEOUT_S = 25.0

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class WiCANAutoPidError(ECUError):
    """The WiCAN's channel table could not be read, changed or verified."""


class WiCANListChanged(WiCANAutoPidError):
    """Put Back found a list on the WiCAN other than the one NC Flash stored
    (edited on its web page since, or another WiCAN). Nothing was written."""


class WiCANSessionUnsupported(WiCANAutoPidError):
    """This WiCAN's firmware has no temporary channel list."""


class WiCANSessionBusy(WiCANAutoPidError):
    """The WiCAN can't take a list now: an ECU session (read, flash, connect)
    owns its CAN bus, or it is going to sleep."""


class WiCANSessionStale(WiCANAutoPidError):
    """The WiCAN no longer has our temporary list (lease expired, or reboot)."""


def session_document(rows: list[dict]) -> dict:
    """The document a temporary list is sent as: *rows* and nothing else."""
    return {
        "format": REQUIRED_FORMAT,
        "pids": list(rows),
        "can_filters": [],
        "calculated": [],
    }


def is_own_firmware_file(data) -> bool:
    """Whether *data* is a channel file written by this firmware.

    The firmware's own rule (``main/auto_pid_seed_logic.h``): its Logger page
    always writes ``calculated`` and, since a later release, ``format``; the
    official MeatPi firmware writes neither. A file saved before ``format``
    existed has only ``calculated``. A file with another ``format`` is not ours.
    """
    if not isinstance(data, dict):
        return False
    if "format" in data:
        return data["format"] == REQUIRED_FORMAT
    return isinstance(data.get("calculated"), list)


def _ci(d: dict, key: str, default=None):
    """``d[key]`` ignoring case, as the firmware's cJSON_GetObjectItem does."""
    if key in d:
        return d[key]
    low = key.lower()
    for k, v in d.items():
        if isinstance(k, str) and k.lower() == low:
            return v
    return default


def _on(d: dict) -> bool:
    """The firmware treats a missing or non-bool ``enabled`` as on."""
    value = _ci(d, "enabled", True)
    return value if isinstance(value, bool) else True


def has_engine_rpm(pids: list, can_filters: list) -> bool:
    """Whether the WiCAN's engine detection has an RPM to use: a turned-on
    polled row or broadcast parameter named RPM, any case (firmware
    ``poll_log.c``). Calculated channels do not count. Without one it decides
    "engine running" from the battery voltage alone, so on a charger or a
    healthy battery a trip can open and never end."""
    for row in pids or []:
        if isinstance(row, dict) and _on(row):
            if str(_ci(row, "Name", "")).lower() == "rpm":
                return True
    for flt in can_filters or []:
        if isinstance(flt, dict):
            for prm in _ci(flt, "parameters") or []:
                if isinstance(prm, dict) and _on(prm):
                    if str(_ci(prm, "name", "")).lower() == "rpm":
                        return True
    return False


def other_channel_names(original: dict) -> set[str]:
    """Lower-cased names of the WiCAN's turned-on broadcast (CAN bus)
    channels. The firmware's own list keeps same-name pairs (ECT polled, ECT
    broadcast) with only one of each pair on, so an off one is no clash.
    Calculated channels are left out: they are not read from the car."""
    names = set()
    for flt in original.get("can_filters") or []:
        if isinstance(flt, dict):
            for prm in _ci(flt, "parameters") or []:
                if isinstance(prm, dict) and _on(prm) and _ci(prm, "name"):
                    names.add(str(_ci(prm, "name")).strip().lower())
    return names


def clashing_rows(original: dict, rows: list[dict]) -> list[str]:
    """Turned-on polled rows the WiCAN already reads from the CAN bus (a
    turned-on broadcast channel of the same name). The polled copy is kept but
    turned off, as the firmware's own list does with its pairs: the value is
    still logged, without spending ECU requests on it. RPM is never turned
    off: the engine detection needs it (:func:`has_engine_rpm`)."""
    others = other_channel_names(original)
    out = []
    for r in rows:
        name = str(_ci(r, "Name", "")).strip()
        if _on(r) and name.lower() in others and name.lower() != "rpm":
            out.append(name)
    return out


def replace_pids(original: dict, rows: list[dict]) -> dict:
    """A copy of *original* with ``pids`` replaced by *rows*; all else kept.

    A row that clashes with a broadcast or calculated channel
    (:func:`clashing_rows`) is kept but turned off. The copy carries the
    ``format`` marker, as the firmware's own page writes."""
    if not is_own_firmware_file(original):
        if isinstance(original, dict) and "format" in original:
            why = f"it was written by other software (format {original['format']!r})"
        else:
            why = (
                "it looks like the official MeatPi firmware's file. Save it once "
                "on the WiCAN's web page (Logger), then try again"
            )
        raise WiCANAutoPidError(
            f"NC Flash did not change the WiCAN's channel list: {why}."
        )
    clash = set(clashing_rows(original, rows))
    pids = [
        dict(r, enabled=False) if str(_ci(r, "Name", "")).strip() in clash else r
        for r in rows
    ]
    if not has_engine_rpm(pids, original.get("can_filters")):
        raise WiCANAutoPidError(
            "NC Flash did not change the WiCAN's channel list: it would have no "
            "RPM. The WiCAN uses RPM to tell that the engine runs; without it, "
            "it goes by battery voltage, and a trip can start and never end. "
            "Tick RPM, then try again."
        )
    updated = dict(original)
    updated["format"] = REQUIRED_FORMAT
    updated["pids"] = pids
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
        if not is_own_firmware_file(original):
            replace_pids(original, rows)  # raises the plain refusal
        before = {
            str(p.get("Name"))
            for p in original.get("pids") or []
            if isinstance(p, dict) and p.get("enabled", True)
        }
        clash = set(clashing_rows(original, rows))
        after = {
            str(r.get("Name"))
            for r in rows
            if r.get("enabled", True) and str(r.get("Name")) not in clash
        }
        return {
            "removed": sorted(before - after),
            "added": sorted(after - before),
            "orphaned": orphaned_calculated(original, rows),
            "rpm": has_engine_rpm(
                [
                    dict(r, enabled=False) if str(r.get("Name")) in clash else r
                    for r in rows
                ],
                original.get("can_filters"),
            ),
            "count": len(after),
            "clashes": sorted(clash),
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
        try:
            stored_sha = file_sha(self.load()[0])
        except WiCANAutoPidError:
            stored_sha = ""
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
            stored_sha=stored_sha,
        )

    def restore(
        self,
        backup_path,
        backup_dir=None,
        *,
        expect_sha: Optional[str] = None,
        force=False,
    ) -> dict:
        """Put back a file saved by :meth:`push`.

        The WiCAN's current file is backed up first (to *backup_dir*). With
        *expect_sha* (the file NC Flash stored), a WiCAN holding anything else
        raises :class:`WiCANListChanged` and nothing is written, unless *force*.
        An empty *expect_sha* means "not known" (a push that failed half way)
        and always raises unless *force*; ``None`` skips the check (tools).
        Returns ``{"applied", "reload_pending", "backup"}``; ``applied`` is the
        firmware's word (``live``, ``deferred`` behind an open trip, ``reboot``).
        """
        data = json.loads(Path(backup_path).read_text(encoding="utf-8"))
        if not is_own_firmware_file(data):
            raise WiCANAutoPidError(f"{backup_path} is not a WiCAN channel file.")
        raw, _current = self.load()
        if expect_sha is not None and not force:
            if not expect_sha:
                raise WiCANListChanged(
                    "NC Flash can't tell what the WiCAN holds now: the last update "
                    "did not finish cleanly."
                )
            if file_sha(raw) != expect_sha:
                raise WiCANListChanged(
                    "The WiCAN's channel list changed since NC Flash updated it "
                    "(edited on its web page, or this is another WiCAN)."
                )
        saved = ""
        if backup_dir is not None:
            saved = str(save_backup(backup_dir, self.host, raw))
        reply = self.store(data)
        applied = str(reply.get("applied") or "")
        pending = applied in ("deferred", "reboot")
        if applied in ("live", ""):
            try:
                pending = bool(self.wait_applied().get("reload_pending"))
            except WiCANAutoPidError as exc:
                where = f" The list it had before is saved in {saved}." if saved else ""
                raise WiCANAutoPidError(f"{exc}{where}") from exc
        return {"applied": applied, "reload_pending": pending, "backup": saved}


class PushResult:
    def __init__(
        self,
        applied: str,
        backup: str,
        orphaned_calculated: Optional[list[str]] = None,
        message: str = "",
        stored_sha: str = "",
    ):
        self.applied = applied
        self.backup = backup
        self.orphaned_calculated = orphaned_calculated or []
        self.message = message
        self.stored_sha = stored_sha  # the WiCAN's file right after the push


def file_sha(raw: str) -> str:
    """Fingerprint of the channel file exactly as the WiCAN serves it."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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


class WiCANSessionList:
    """HTTP client for the WiCAN's temporary channel list (one device).

    Failures by kind: :class:`WiCANSessionUnsupported` (old firmware),
    :class:`WiCANSessionBusy`, :class:`WiCANSessionStale`, another
    :class:`WiCANAutoPidError` (the WiCAN refused, with its reason), and
    :class:`~src.ecu.wican_http.WiCANHttpError` for a network failure.
    """

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

    def _url(self, path: str) -> str:
        return f"http://{self.host}:{self.http_port}{path}"

    def supported(self) -> bool:
        """Whether the firmware has the temporary list (``/host_caps``).

        False for firmware without ``/host_caps`` (404) or without the
        ``session_list`` capability; a network failure raises.
        """
        try:
            caps = wican_http.get_json(
                self._url("/host_caps"), timeout_s=self._timeout_s
            )
        except wican_http.WiCANHttpError as exc:
            if exc.status == 404:
                return False
            raise
        return isinstance(caps, dict) and bool(caps.get("session_list"))

    def _post(
        self,
        query: str,
        body: dict,
        timeout_s: Optional[float],
        *,
        stale_on_409: bool = False,
    ) -> dict:
        """POST one session op. *stale_on_409*: for an op on a token, a 409
        that is not a readable ``busy`` means the token is no longer good."""
        try:
            reply = wican_http.post_json(
                self._url(f"/datalog_session?{query}"),
                body,
                timeout_s=self._timeout_s if timeout_s is None else timeout_s,
            )
        except wican_http.WiCANHttpError as exc:
            if exc.status == 404:
                raise WiCANSessionUnsupported(
                    "This WiCAN's firmware has no per-run channel list."
                ) from exc
            if exc.status in (400, 409, 500):
                # Refused, with a reason (500: the WiCAN ran out of memory and
                # changed nothing). Only a 404 means old firmware.
                refusal = _refusal(exc.body)
                if (
                    exc.status == 409
                    and stale_on_409
                    and not isinstance(refusal, WiCANSessionBusy)
                ):
                    refusal = WiCANSessionStale(str(refusal))
                raise refusal from exc
            raise
        if not isinstance(reply, dict) or reply.get("ok") is False:
            raise _refusal(reply)
        return reply

    def begin(
        self,
        rows: list[dict],
        *,
        ttl_s: int = SESSION_TTL_S,
        timeout_s: Optional[float] = None,
    ) -> int:
        """Give the WiCAN *rows* as its temporary list; return the token.

        A begin while a list is in use replaces it (new token).
        """
        document = session_document(rows)
        if len(json.dumps(document).encode("utf-8")) > MAX_SESSION_BYTES:
            raise WiCANAutoPidError(
                "The channel list is too large for the WiCAN. Remove some channels."
            )
        reply = self._post(f"op=begin&ttl_s={int(ttl_s)}", document, timeout_s)
        try:
            token = int(reply.get("token") or 0)
        except (TypeError, ValueError):
            token = 0
        if not token:
            raise WiCANAutoPidError("The WiCAN did not accept the channel list.")
        return token

    def keepalive(self, token: int, *, timeout_s: Optional[float] = None) -> int:
        """Renew the lease; return the milliseconds left on it."""
        reply = self._post(
            f"op=keepalive&token={int(token)}", {}, timeout_s, stale_on_409=True
        )
        return _int(reply.get("remaining_ms"))

    def end(self, token: int, *, timeout_s: Optional[float] = None) -> bool:
        """End the temporary list; False if the WiCAN had already dropped it."""
        try:
            self._post(f"op=end&token={int(token)}", {}, timeout_s, stale_on_409=True)
        except WiCANSessionStale:
            return False
        return True

    def poll_status(self) -> dict:
        status = wican_http.get_json(
            self._url("/poll_status"), timeout_s=self._timeout_s
        )
        return status if isinstance(status, dict) else {}

    def wait_applied(
        self,
        token: Optional[int] = None,
        timeout_s: float = SESSION_APPLY_TIMEOUT_S,
        *,
        should_stop: Optional[Callable[[], bool]] = None,
        renew_every_s: float = SESSION_RENEW_S,
    ) -> dict:
        """Poll ``/poll_status`` until the WiCAN polls the list *token* names.

        Applied = ``session_token`` equals *token* and no reload is pending.
        ``session_list`` alone is not enough (it is also true for an earlier
        session's table); it is the fallback only when the status has no
        ``session_token``. ``reload_ok`` and ``pids_unpollable`` are stale
        while a reload is pending, so they are read only after it.

        The WiCAN keeps ``reload_pending`` true from the begin until it has
        parsed that list, so "nothing pending, and the table is not ours" is
        final: it could not load the list (:class:`WiCANAutoPidError`), or it
        dropped it (:class:`WiCANSessionStale`). Only a pending reload is
        waited for, up to *timeout_s* (the WiCAN could not close the trip it
        is recording). It also raises when some channels can't be polled.

        While waiting, the lease of *token* is renewed every *renew_every_s*,
        and a ``/poll_status`` call that fails is tried again. Returns the
        last status early when *should_stop* turns true.
        """
        deadline = self._clock() + timeout_s
        next_renewal = self._clock() + renew_every_s
        status: dict = {}
        while True:
            failure = None
            try:
                status = self.poll_status()
            except wican_http.WiCANHttpError as exc:
                failure = exc  # a blip while it closes its trip: ask again
                logger.debug("WiCAN /poll_status failed while waiting: %s", exc)
            if failure is None and not status.get("reload_pending"):
                if _loaded(status, token):
                    if status.get("reload_ok") is False:
                        raise WiCANAutoPidError(_NOT_LOADED)
                    unpollable = _int(status.get("pids_unpollable"))
                    if unpollable:
                        raise WiCANAutoPidError(
                            f"The WiCAN can't poll {unpollable} of the channels "
                            "(check their mode and identifier)."
                        )
                    return status
                if token is not None and "session_token" in status:
                    if status.get("reload_ok") is False:
                        raise WiCANAutoPidError(_NOT_LOADED)
                    raise WiCANSessionStale(
                        "The WiCAN dropped the channel list (it went to sleep, "
                        "or another NC Flash took over)."
                    )
            if should_stop is not None and should_stop():
                return status
            if self._clock() >= deadline:
                if failure is not None:
                    raise failure
                if (
                    not status.get("reload_pending")
                    and status.get("reload_ok") is False
                ):
                    raise WiCANAutoPidError(_NOT_LOADED)
                raise WiCANAutoPidError(
                    "The WiCAN could not switch channels right now (it is still "
                    "recording a trip). Try again in a moment."
                )
            if token is not None and self._clock() >= next_renewal:
                next_renewal = self._clock() + renew_every_s
                try:
                    self.keepalive(token, timeout_s=SESSION_RENEW_TIMEOUT_S)
                except WiCANSessionStale:
                    raise
                except (WiCANAutoPidError, wican_http.WiCANHttpError) as exc:
                    logger.debug("WiCAN lease not renewed while waiting: %s", exc)
            self._sleep(0.5)


_NOT_LOADED = "The WiCAN could not load the channel list."


def _int(value) -> int:
    """*value* as an int; 0 for anything that is not a number."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _loaded(status: dict, token: Optional[int]) -> bool:
    """Whether the poller's table is the temporary list *token* names."""
    if token is not None and "session_token" in status:
        return _int(status.get("session_token")) == int(token)
    return bool(status.get("session_list"))


def _refusal(reply) -> WiCANAutoPidError:
    """The exception for a ``{"ok":false,"error":...}`` reply (dict or text)."""
    if isinstance(reply, str):
        try:
            reply = json.loads(reply)
        except json.JSONDecodeError:
            reply = {"msg": reply.strip()[:200]}
    if not isinstance(reply, dict):
        reply = {}
    error = str(reply.get("error") or "")
    detail = str(reply.get("msg") or "")
    if error == "busy":
        return WiCANSessionBusy(
            detail or "The WiCAN's link to the ECU is in use by another session."
        )
    if error == "stale":
        return WiCANSessionStale(detail or "The WiCAN dropped the channel list.")
    if error == "internal":
        return WiCANAutoPidError(
            "The WiCAN could not take the channel list"
            + (f": {detail}" if detail else " (internal error).")
        )
    if error == "inactive":
        # Its own saved list has no polled channel, so its datalogger is off;
        # the WiCAN's message says what to do on its Logger page.
        return WiCANAutoPidError(
            detail
            or "The WiCAN's datalogger is not running: add one channel on the "
            "WiCAN's Logger page, then start again."
        )
    return WiCANAutoPidError(
        "The WiCAN refused the channel list"
        + (f": {detail}" if detail else f" ({error or 'no reason given'}).")
    )
