"""Incremental download of WiCAN SD trip logs into a local directory (#83).

The WiCAN PRO's csv_logger firmware component exposes the full download
surface over plain HTTP on port 80:

- ``GET /csv_list``     -> ``{"files":[{"name","size","mtime"}, ...]}``
  (newest-first; ``mtime`` = unix epoch secs; empty list when nothing logged)
- ``GET /download_csv?file=<name>.csv`` -> streams ``text/csv``
  (NOTE: the URI really is ``/download_csv`` — ``csv_download_uri`` in
  ``csv_logger.c`` registers the handler there; hardware-verified 2026-07-10)
- ``GET /csv_status``   -> ``{"session_active":bool, "file":"<abspath|empty>",
  "mode":..., "columns":N}`` (detects the currently-open trip file)

These are pure HTTP-over-WiFi calls, fully decoupled from the CAN bus / SLCAN
session / ECU: log download never opens an ECU connection and works whichever
adapter (``wican`` / ``j2534``) is selected, car on or off. Treat it as a
**WiCAN device utility**, not an ECU operation.

Sync semantics (issue #83, confirmed):

- **Incremental ("new only")** — a remote log is skipped when a local file
  with the same (sanitized) name and the same size already exists. Identity is
  ``(name, size)``: the firmware appends but never rewrites a closed trip, and
  on a clockless device (no NTP/RTC) names like ``unknown_time_<ms>.csv`` can
  collide across reboots — a same-name-different-size local file is treated as
  a *different* trip and the new one lands with a ``-2``/``-3``… suffix.
- **Skip the active trip file** — the file ``/csv_status`` reports as open is
  still growing; a naive download would store a truncated CSV. It is picked up
  on the next run once the trip closes.
- Downloads are atomic (``.part`` + size verify, via
  :mod:`src.ecu.wican_http`); a partial transfer never looks complete, and an
  interrupted run keeps its completed files (idempotent re-run).

Opt-in delete after download (issue #112, off by default). The firmware never
cleans the card itself (its only rotation splits an oversized file), so
:meth:`WiCANLogClient.delete_verified` removes trips whose local copy is
PROVEN complete, through the SD file manager (``GET /files?op=list&path=logs``
for a fresh type/size/active view, ``POST /files {"op":"delete",
"path":"logs/<name>"}``). Deletion is irreversible, so "proven" is strict:

- A trip downloaded in THIS run is proven by the exact remote → local pair
  :meth:`~WiCANLogClient.download_new` recorded (size-verified transfer), and
  only while the device still lists it with the same name, size and mtime.
- A trip downloaded by an EARLIER run with a DATED name
  (``YYYYMMDD_HHhMMmSSs[_N].csv``) is proven by a same-size local copy whose
  first 64 KB match the device's (a partial fetch — a fraction of a second,
  where a full re-fetch is minutes per backlog). The firmware only dates a
  name when its clock reads 2020 or later, and adds ``_N`` while the name
  exists on the card — but it CAN repeat a name once this pass deleted the
  earlier file and the clock revisits that second (the web UI sets the
  clock from the browser, a timezone change, DST fall-back; older firmware
  has no ``_N`` loop). Such a trip's first rows (millisecond timestamps,
  sensor values) differ from the old copy's; on a mismatch the trip goes
  through the full check below, which saves it under a ``-N`` name.
- Any OTHER earlier-run trip is proven only by fetching it again and
  comparing it byte for byte with a local copy. Name + size is NOT proof: a
  clockless device opens ``unknown_time_<ms>.csv`` with ``fopen("w")``, so
  after a reboot a new trip can take an old trip's name, and possibly its
  size. When the re-fetch matches no local copy, it IS such a trip — it is
  saved locally under a free ``-N`` name first (never downloaded before).
- Never deleted: the newest *keep_newest* trips, the trip being recorded,
  anything not listed as a plain file of the expected size (a folder delete
  is recursive), unsafe names, and everything after a cancelled or failed
  download. A failed delete is logged and retried on the next run.

Headless: standard library only, no PySide6.
"""

from __future__ import annotations

import logging
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

from src.utils.transfer import transfer_summary

from .wican_http import (
    DEFAULT_TIMEOUT_S,
    WiCANHttpError,
    _remove_quietly,
    download_to_file,
    fetch_head,
    get_json,
    post_json,
    sanitize_basename,
)

logger = logging.getLogger(__name__)

CSV_LIST_PATH = "/csv_list"
CSV_DOWNLOAD_PATH = "/download_csv"
CSV_STATUS_PATH = "/csv_status"

#: The SD file manager endpoint (``sd_filemgr``); paths are relative to /sdcard.
FILES_PATH = "/files"

#: The csv_logger's folder, relative to /sdcard (the file manager's root).
SD_LOGS_DIR = "logs"

#: How many ``-N`` suffixes to probe before declaring the collision pathological.
_MAX_COLLISION_SUFFIX = 100

#: Chunk size for the byte-for-byte local comparison.
_COMPARE_CHUNK = 64 * 1024

#: How much of a dated trip is fetched to prove its local copy (the first
#: rows: header, then millisecond timestamps and live sensor values).
_HEAD_CHECK_BYTES = 64 * 1024

#: Names the delete pass may touch: the characters the firmware itself uses.
#: The download URL is query-encoded (a space becomes ``+``) and the firmware
#: never decodes it, while the delete path is sent verbatim — for any other
#: name the file downloaded and the file deleted could differ.
_DELETABLE_NAME = re.compile(r"[A-Za-z0-9._-]+")

#: A trip name the firmware dated from a SET clock (year >= 2020, the
#: firmware's own validity bar), with its optional ``_N`` duplicate suffix.
#: Name + size identifies such a trip; see the module docstring for the rare
#: repeated-second case this accepts.
_DATED_NAME = re.compile(
    r"20[2-9]\d(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])"
    r"_([01]\d|2[0-3])h[0-5]\dm[0-5]\ds(_\d{1,2})?\.csv"
)


class WiCANLogsError(WiCANHttpError):
    """A trip-log listing, status probe, or download failed."""


@dataclass(frozen=True)
class TripLog:
    """One remote trip log as advertised by ``/csv_list``."""

    name: str
    size: int
    mtime: int  # unix epoch secs; unreliable on a clockless device


@dataclass(frozen=True)
class LogSyncResult:
    """Outcome of one :meth:`WiCANLogClient.download_new` run."""

    downloaded: list = field(default_factory=list)  # list[Path], newest-first
    skipped: list = field(default_factory=list)  # list[str] remote names
    bytes_downloaded: int = 0  # sum of the completed files' sizes
    elapsed_s: float = 0.0  # wall time spent transferring (first file → last)
    #: The exact (TripLog, local Path) pair of every file completed THIS run —
    #: the only proof :meth:`WiCANLogClient.delete_verified` accepts without
    #: a byte-for-byte re-check.
    verified: list = field(default_factory=list)
    cancelled: bool = False  # the run stopped early on abort_cb
    #: Set by the caller when the opt-in delete pass ran (see CleanupResult).
    cleanup: Optional["CleanupResult"] = None

    @property
    def downloaded_oldest_first(self) -> list:
        """The device lists newest-first, and that order is the only reliable
        chronology (a clockless device has no trustworthy names or mtimes)."""
        return list(reversed(self.downloaded))


@dataclass(frozen=True)
class SyncPlan:
    """What one sync run will actually transfer, decided before the first byte.

    Derived from :meth:`WiCANLogClient.classify` — the ONE place the skip
    decisions (unsafe name, active trip file, already downloaded) live.
    ``total_bytes`` is the sum of the sizes ``/csv_list`` advertised for
    ``to_download``, so a progress display is byte-accurate from the start.
    """

    to_download: list = field(default_factory=list)  # list[(TripLog, Path)]
    skipped: list = field(default_factory=list)  # list[str] remote names
    total_bytes: int = 0


@dataclass(frozen=True)
class CleanupResult:
    """Outcome of one :meth:`WiCANLogClient.delete_verified` pass."""

    deleted: list = field(default_factory=list)  # list[str] names removed
    failed: list = field(default_factory=list)  # list[(name, reason)]
    #: Earlier-"downloaded" trips whose re-fetch matched NO local copy (a
    #: clockless name reuse): saved locally under a free name before delete.
    rescued: list = field(default_factory=list)  # list[Path]
    cancelled: bool = False


#: Per-file statuses produced by :meth:`WiCANLogClient.classify`.
STATUS_NEW = "new"  # will be downloaded (target path chosen)
STATUS_DOWNLOADED = "downloaded"  # same (name, size) already on disk
STATUS_ACTIVE = "active"  # the trip file still being written
STATUS_UNSAFE_NAME = "unsafe-name"  # device-supplied name failed sanitization


@dataclass(frozen=True)
class LogInventoryEntry:
    """One remote log with its sync decision (a table row for a consumer UI)."""

    log: TripLog
    status: str  # one of the STATUS_* constants
    target: Optional[Path] = None  # local path; set only for STATUS_NEW


class WiCANLogClient:
    """Downloads new SD trip logs from a WiCAN into a local directory."""

    def __init__(
        self, host: str, http_port: int = 80, timeout_s: float = DEFAULT_TIMEOUT_S
    ):
        self.host = host
        self.http_port = http_port
        self.timeout_s = timeout_s

    def _url(self, path: str, query: Optional[dict] = None) -> str:
        url = f"http://{self.host}:{self.http_port}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return url

    # --- endpoint wrappers ---------------------------------------------------

    def list_logs(self) -> list:
        """Return the device's trip logs (list[TripLog], device order = newest-first)."""
        payload = get_json(self._url(CSV_LIST_PATH), timeout_s=self.timeout_s)
        files = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(files, list):
            raise WiCANLogsError(
                f"/csv_list from {self.host}: malformed reply {payload!r}"
            )
        logs = []
        for entry in files:
            if not isinstance(entry, dict) or not entry.get("name"):
                logger.warning("Ignoring malformed /csv_list entry: %r", entry)
                continue
            logs.append(
                TripLog(
                    name=str(entry["name"]),
                    size=int(entry.get("size", 0)),
                    mtime=int(entry.get("mtime", 0)),
                )
            )
        return logs

    def status(self) -> dict:
        """Return the raw ``/csv_status`` payload."""
        payload = get_json(self._url(CSV_STATUS_PATH), timeout_s=self.timeout_s)
        if not isinstance(payload, dict):
            raise WiCANLogsError(
                f"/csv_status from {self.host}: malformed reply {payload!r}"
            )
        return payload

    def active_log_basename(self) -> Optional[str]:
        """Basename of the currently-open trip file, or None when no session.

        The firmware reports an absolute device path (``/sdcard/logs/x.csv``);
        only the basename is comparable to ``/csv_list`` names.
        """
        st = self.status()
        if not st.get("session_active"):
            return None
        path = str(st.get("file") or "")
        return path.rsplit("/", 1)[-1] or None

    # --- the sync ------------------------------------------------------------

    def classify(self, dest_dir, *, skip_active: bool = True) -> list:
        """Classify every remote log against local state (list[LogInventoryEntry]).

        THE one home of the per-file sync decisions, in device order
        (newest-first): :data:`STATUS_DOWNLOADED` when a local copy with the
        same size exists (under the original or a ``-N`` collision-suffixed
        name), :data:`STATUS_ACTIVE` for the still-growing open trip file when
        *skip_active*, :data:`STATUS_UNSAFE_NAME` when the device-supplied name
        fails sanitization (warned, never trusted), else :data:`STATUS_NEW`
        with the chosen local ``target``. Both :meth:`plan` (the download run)
        and inventory consumers (the Trip Logs table, the startup new-log
        check) derive from this.
        """
        dest_dir = Path(dest_dir)
        logs = self.list_logs()

        active = None
        if skip_active:
            # If we cannot determine the active file, downloading a growing CSV
            # is the risk — fail the run rather than guess.
            active = self.active_log_basename()

        entries = []
        # Targets already promised to an earlier log THIS run. The whole plan
        # resolves against pre-run disk state, so without this a collision-
        # suffixed name could land on a later log's literal name and the two
        # downloads would silently clobber each other.
        reserved = set()
        for log in logs:
            try:
                name = sanitize_basename(log.name)
            except WiCANHttpError as exc:
                logger.warning("Skipping device log with unsafe name: %s", exc)
                entries.append(LogInventoryEntry(log=log, status=STATUS_UNSAFE_NAME))
                continue

            if active is not None and name == active:
                logger.info("Skipping active trip file %s (still being written)", name)
                entries.append(LogInventoryEntry(log=log, status=STATUS_ACTIVE))
                continue

            target = self._resolve_target(dest_dir, name, log.size, reserved)
            if target is None:
                entries.append(LogInventoryEntry(log=log, status=STATUS_DOWNLOADED))
                continue

            reserved.add(target)
            entries.append(LogInventoryEntry(log=log, status=STATUS_NEW, target=target))
        return entries

    def plan(self, dest_dir, *, skip_active: bool = True) -> SyncPlan:
        """Decide what a sync run will download, without transferring anything.

        A projection of :meth:`classify`: ``to_download`` keeps the NEW entries
        (device order), ``skipped`` every other remote name.
        """
        entries = self.classify(dest_dir, skip_active=skip_active)
        to_download = [(e.log, e.target) for e in entries if e.status == STATUS_NEW]
        skipped = [e.log.name for e in entries if e.status != STATUS_NEW]
        return SyncPlan(
            to_download=to_download,
            skipped=skipped,
            total_bytes=sum(log.size for log, _ in to_download),
        )

    def download_new(
        self, dest_dir, *, skip_active: bool = True, abort_cb=None, progress_cb=None
    ) -> LogSyncResult:
        """Download every remote log not yet present locally into *dest_dir*.

        The skip decisions live in :meth:`plan`; ``skipped`` in the result is
        the plan's skip list.

        ``abort_cb`` (no-arg, returns truthy to abort) is polled between files
        and between download chunks; an abort returns the partial result —
        completed files remain and the run stays idempotent.

        ``progress_cb`` (three args: cumulative bytes done across the whole
        run, total bytes the plan will transfer, name of the file currently
        transferring) is invoked once up front as ``(0, total, "")`` — so a
        progress display is determinate before the first byte — then after
        every chunk.

        Raises :class:`~src.ecu.wican_http.WiCANHttpError` (or its
        :class:`WiCANLogsError` subclass for malformed device replies) when
        the device is unreachable or a transfer fails mid-run; files
        downloaded before the failure remain (the run is idempotent —
        re-running downloads only what is missing).
        """
        sync_plan = self.plan(dest_dir, skip_active=skip_active)

        result = LogSyncResult(skipped=list(sync_plan.skipped))
        total = sync_plan.total_bytes
        if progress_cb is not None:
            progress_cb(0, total, "")

        def _log_abort():
            logger.info(
                "Trip-log sync aborted; keeping %d downloaded file(s)",
                len(result.downloaded),
            )

        base = 0  # bytes of fully-downloaded files so far
        cancelled = False
        run_start = last_done = time.monotonic()
        for log, target in sync_plan.to_download:
            if abort_cb is not None and abort_cb():
                _log_abort()
                cancelled = True
                break

            per_file_cb = None
            if progress_cb is not None:
                per_file_cb = self._file_progress(progress_cb, base, total, log.name)

            url = self._url(CSV_DOWNLOAD_PATH, {"file": log.name})
            file_start = time.monotonic()
            try:
                path = download_to_file(
                    url,
                    target,
                    expected_size=log.size,
                    timeout_s=self.timeout_s,
                    abort_cb=abort_cb,
                    progress_cb=per_file_cb,
                )
            except WiCANHttpError:
                if abort_cb is not None and abort_cb():
                    # A mid-file cancel surfaces as the aborted-download error;
                    # the user asked for this — return the partial result like
                    # a between-files abort (the .part is already cleaned up).
                    _log_abort()
                    cancelled = True
                    break
                raise
            last_done = time.monotonic()
            logger.info(
                "Downloaded trip log %s (%s)",
                path.name,
                transfer_summary(log.size, last_done - file_start),
            )
            result.downloaded.append(path)
            result.verified.append((log, path))
            base += log.size

        # Timed to the last COMPLETED file, so a cancelled partial transfer
        # does not drag the average down.
        return replace(
            result,
            bytes_downloaded=base,
            elapsed_s=last_done - run_start,
            cancelled=cancelled,
        )

    @staticmethod
    def _file_progress(progress_cb, base: int, total: int, name: str):
        """Adapt the per-chunk byte count of one file to whole-run progress."""

        def cb(received: int):
            progress_cb(base + received, total, name)

        return cb

    @staticmethod
    def _resolve_target(
        dest_dir: Path, name: str, size: int, reserved=frozenset()
    ) -> Optional[Path]:
        """Pick the local path for a remote log, honoring the collision guard.

        Returns None when a local copy with the same size already exists (under
        the plain or any suffixed name) — i.e. "already downloaded". Otherwise
        returns the first free path: ``name``, else ``stem-2.ext``, ``-3``, …
        (clockless devices reuse names across reboots; never clobber a
        different file, never re-download an existing one).

        ``reserved`` holds paths already promised to other logs in the same
        planning pass (not yet on disk) — treated as occupied so two logs in
        one run can never resolve to the same target.
        """
        for candidate in _candidate_paths(dest_dir, name):
            if candidate in reserved:
                continue  # promised to another log this run — probe the next name
            if not candidate.exists():
                return candidate
            try:
                if candidate.stat().st_size == size:
                    return None  # same trip already downloaded
            except OSError:
                pass  # race/unreadable — treat as occupied, probe the next
        raise _collision_error(name)

    # --- opt-in delete after download (#112) ---------------------------------

    def list_sd_logs(self) -> dict:
        """Fresh file-manager view of the logs folder: ``{name: entry}``.

        Each entry is the raw ``sd_filemgr`` item (``type``, ``size``,
        ``mtime``, and ``active`` / ``locked`` flags when set). An unmounted
        card lists empty, so nothing can be deleted from it.
        """
        url = self._url(FILES_PATH, {"op": "list", "path": SD_LOGS_DIR})
        payload = get_json(url, timeout_s=self.timeout_s)
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            raise WiCANLogsError(
                f"/files list from {self.host}: malformed reply {payload!r}"
            )
        return {
            str(e["name"]): e for e in entries if isinstance(e, dict) and e.get("name")
        }

    def delete_log(self, name: str) -> None:
        """Delete ONE trip log file from the device's logs folder.

        Always a single-file path (``logs/<name>``) built from a sanitized
        basename: the file manager deletes folders recursively, so nothing
        else may ever reach it. Raises on any refusal (``409`` for the file
        being recorded, ``403`` reserved, ``404`` gone) or an unexpected reply.
        """
        name = sanitize_basename(name)
        if not _DELETABLE_NAME.fullmatch(name):
            raise WiCANLogsError(f"refusing to delete {name!r}: unsupported characters")
        reply = post_json(
            self._url(FILES_PATH),
            {"op": "delete", "path": f"{SD_LOGS_DIR}/{name}"},
            timeout_s=self.timeout_s,
        )
        if not isinstance(reply, dict) or reply.get("ok") is not True:
            raise WiCANLogsError(f"delete of {name} refused: {reply!r}")
        if reply.get("deleted") != 1:
            # The file-manager listing said it was one plain file; anything
            # else means the device changed under us — make it loud.
            logger.warning(
                "Delete of %s removed %r entries", name, reply.get("deleted")
            )

    def delete_verified(
        self,
        dest_dir,
        verified=(),
        *,
        keep_newest: int,
        abort_cb=None,
        status_cb=None,
    ) -> CleanupResult:
        """Delete from the device every trip whose local copy is proven.

        *verified* is :attr:`LogSyncResult.verified` of the download run that
        just finished (never call this after a cancelled or failed run — the
        caller owns that rule). The *keep_newest* trips with the newest card
        timestamps are always kept. On a WiCAN without a clock the timestamps
        restart at every boot, so "newest" is only approximate there — it
        decides what stays on the card, never what is proven. Proof rules:
        see the module docstring.

        ``status_cb`` (one arg: a short human line) reports each step;
        ``abort_cb`` is polled between files and during re-fetches — trips
        already deleted stay deleted (each was proven first).

        Raises :class:`~src.ecu.wican_http.WiCANHttpError` only when the
        fresh listings cannot be read (nothing is deleted then); a single
        file's failure is recorded in ``failed`` and the pass moves on.
        """
        if keep_newest < 0:
            raise ValueError(f"keep_newest must be >= 0, got {keep_newest}")
        dest_dir = Path(dest_dir)

        # Fresh views taken now, not at download time: the card may have
        # changed (a new trip started, a name reused) since the plan.
        logs = self.list_logs()
        on_card = self.list_sd_logs()
        active = self.active_log_basename()
        this_run = {log.name: (log, path) for log, path in verified}

        # Sorted here, not trusted from /csv_list: past its sort buffer the
        # firmware emits the overflow unsorted. Stable, so ties keep the
        # device order.
        by_age = sorted(logs, key=lambda log: log.mtime, reverse=True)

        deleted, failed, rescued = [], [], []
        cancelled = False
        # Oldest first: an interrupted pass has removed the oldest trips.
        for log in reversed(by_age[keep_newest:]):
            if abort_cb is not None and abort_cb():
                cancelled = True
                break

            refusal = self._delete_refusal(log, on_card.get(log.name), active)
            if refusal:
                logger.debug("Keeping %s on the WiCAN: %s", log.name, refusal)
                continue

            local = None
            if log.name in this_run:
                done_log, path = this_run[log.name]
                if done_log == log and _file_size(path) == log.size:
                    local = path
                else:
                    logger.warning(
                        "Keeping %s on the WiCAN: it changed since it was "
                        "downloaded, or its local copy did",
                        log.name,
                    )
                    continue
            elif _DATED_NAME.fullmatch(log.name):
                if status_cb is not None:
                    status_cb(f"Checking {log.name} against its local copy...")
                local = self._prove_dated_copy(dest_dir, log, abort_cb, rescued)
            else:
                if status_cb is not None:
                    status_cb(f"Checking {log.name} against its local copy...")
                local = self._prove_earlier_copy(dest_dir, log, abort_cb, rescued)
            if local is None:
                if abort_cb is not None and abort_cb():
                    cancelled = True
                    break
                continue

            if abort_cb is not None and abort_cb():
                cancelled = True
                break
            if status_cb is not None:
                status_cb(f"Deleting {log.name} from the WiCAN...")
            try:
                # Re-read the entry right before the POST: proving earlier
                # trips can take minutes, and a rebooted clockless device may
                # have reused this name for a new trip since the pass began.
                fresh = self.list_sd_logs().get(log.name)
                if self._delete_refusal(log, fresh, active) or not _same_entry(
                    fresh, on_card[log.name]
                ):
                    logger.warning(
                        "Keeping %s on the WiCAN: it changed during the delete pass",
                        log.name,
                    )
                    continue
                self.delete_log(log.name)
            except WiCANHttpError as exc:
                logger.warning("Could not delete %s from the WiCAN: %s", log.name, exc)
                failed.append((log.name, str(exc)))
                continue
            logger.info(
                "Deleted trip log %s from the WiCAN (local copy: %s)",
                log.name,
                local.name,
            )
            deleted.append(log.name)

        return CleanupResult(
            deleted=deleted, failed=failed, rescued=rescued, cancelled=cancelled
        )

    @staticmethod
    def _delete_refusal(log: TripLog, entry, active) -> Optional[str]:
        """Why *log* must stay on the card, or None when it may go.

        *entry* is its fresh file-manager item (None when not listed there).
        """
        try:
            name = sanitize_basename(log.name)
        except WiCANHttpError:
            return "unsafe name"
        if not _DELETABLE_NAME.fullmatch(name):
            return "name has characters the download cannot address exactly"
        if active is not None and name == active:
            return "being recorded"
        if entry is None:
            return "not in the file manager listing"
        if entry.get("type") != "file":
            return "not a plain file"
        if entry.get("active") or entry.get("locked"):
            return "active or locked on the device"
        size = entry.get("size")
        if not isinstance(size, (int, float)) or int(size) != log.size:
            return "size differs between the two device listings"
        return None

    def _prove_dated_copy(
        self, dest_dir: Path, log: TripLog, abort_cb, rescued: list
    ) -> Optional[Path]:
        """Prove an earlier-run DATED trip by its size and its first 64 KB.

        Returns the matching local copy, or None (keep it on the card). When
        the heads differ the device reused the name for another trip: the
        full check (:meth:`_prove_earlier_copy`) takes over and saves it.
        """
        local = _same_size_copy(dest_dir, log)
        if local is None:
            return None
        want = min(log.size, _HEAD_CHECK_BYTES)
        try:
            head = fetch_head(
                self._url(CSV_DOWNLOAD_PATH, {"file": log.name}),
                want,
                timeout_s=self.timeout_s,
            )
            with open(local, "rb") as fh:
                local_head = fh.read(want)
        except (WiCANHttpError, OSError) as exc:
            logger.warning(
                "Keeping %s on the WiCAN: could not verify it (%s)", log.name, exc
            )
            return None
        if len(head) == want and head == local_head:
            return local
        logger.info(
            "%s on the WiCAN starts differently from its local copy — "
            "checking the whole file",
            log.name,
        )
        return self._prove_earlier_copy(dest_dir, log, abort_cb, rescued)

    def _prove_earlier_copy(
        self, dest_dir: Path, log: TripLog, abort_cb, rescued: list
    ) -> Optional[Path]:
        """Prove a trip downloaded by an earlier run by re-fetching it.

        Returns the local file that matches it byte for byte, or None (keep
        it on the card). When local same-size copies exist but none matches,
        the re-fetched trip is a different trip that reused the name: it is
        saved under a free ``-N`` name, appended to *rescued*, and returned.
        No same-size local copy at all means it was never downloaded — None.
        """
        name = sanitize_basename(log.name)
        copies = [
            p for p in _candidate_paths(dest_dir, name) if _file_size(p) == log.size
        ]
        if not copies:
            return None

        # Hidden, per-trip temp name, cleaned up whatever happens. It never
        # matches a real trip's candidate names.
        temp = dest_dir / f".{name}.verify"
        try:
            download_to_file(
                self._url(CSV_DOWNLOAD_PATH, {"file": log.name}),
                temp,
                expected_size=log.size,
                timeout_s=self.timeout_s,
                abort_cb=abort_cb,
            )
            for copy in copies:
                if _same_bytes(temp, copy):
                    return copy
            target = _free_path(dest_dir, name)
            os.replace(temp, target)
        except (WiCANHttpError, OSError) as exc:
            logger.warning(
                "Keeping %s on the WiCAN: could not verify it (%s)", name, exc
            )
            return None
        finally:
            _remove_quietly(temp)

        logger.warning(
            "%s on the WiCAN is a different trip from the local file of the same "
            "name and size (the device reused the name) — saved it as %s",
            name,
            target.name,
        )
        rescued.append(target)
        return target


def _candidate_paths(dest_dir: Path, name: str):
    """Every local path a remote *name* may occupy: ``name``, ``stem-2.ext``…

    THE one definition of the collision-suffix scheme, shared by the download
    (:meth:`WiCANLogClient._resolve_target`) and the delete proof.
    """
    stem, dot, ext = name.rpartition(".")
    if not dot:  # no extension — suffix the whole name
        stem, ext = name, ""
    yield dest_dir / name
    for n in range(2, _MAX_COLLISION_SUFFIX):
        yield dest_dir / (f"{stem}-{n}.{ext}" if ext else f"{stem}-{n}")


def _collision_error(name: str) -> WiCANLogsError:
    return WiCANLogsError(
        f"More than {_MAX_COLLISION_SUFFIX} local name collisions for "
        f"{name!r} — refusing to continue (corrupt local logs dir?)"
    )


def _free_path(dest_dir: Path, name: str) -> Path:
    """The first candidate path for *name* with nothing on disk."""
    for candidate in _candidate_paths(dest_dir, name):
        if not candidate.exists():
            return candidate
    raise _collision_error(name)


def _same_size_copy(dest_dir: Path, log: TripLog) -> Optional[Path]:
    """The local copy of a DATED trip: the first candidate path of the
    exact size (the same identity :meth:`WiCANLogClient.classify` uses)."""
    for candidate in _candidate_paths(dest_dir, log.name):
        if _file_size(candidate) == log.size:
            return candidate
    return None


def _same_entry(a: dict, b: dict) -> bool:
    """Two file-manager items describe the same, unchanged file."""
    return all(a.get(k) == b.get(k) for k in ("type", "size", "mtime"))


def _file_size(path: Path) -> Optional[int]:
    """Size of a regular file, or None when missing / not a file / unreadable."""
    try:
        return path.stat().st_size if path.is_file() else None
    except OSError:
        return None


def _same_bytes(a: Path, b: Path) -> bool:
    """Byte-for-byte file equality. Deliberately not :func:`filecmp.cmp`,
    whose cache keys on (size, mtime) and could return a stale answer when the
    same temp path is reused within one mtime tick."""
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            ca = fa.read(_COMPARE_CHUNK)
            cb = fb.read(_COMPARE_CHUNK)
            if ca != cb:
                return False
            if not ca:
                return True
