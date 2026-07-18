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
- **Never delete from the device** — downloads are copies; the firmware
  rotates the SD itself.
- Downloads are atomic (``.part`` + size verify, via
  :mod:`src.ecu.wican_http`); a partial transfer never looks complete, and an
  interrupted run keeps its completed files (idempotent re-run).

Headless: standard library only, no PySide6.
"""

from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .wican_http import (
    DEFAULT_TIMEOUT_S,
    WiCANHttpError,
    download_to_file,
    get_json,
    sanitize_basename,
)

logger = logging.getLogger(__name__)

CSV_LIST_PATH = "/csv_list"
CSV_DOWNLOAD_PATH = "/download_csv"
CSV_STATUS_PATH = "/csv_status"

#: How many ``-N`` suffixes to probe before declaring the collision pathological.
_MAX_COLLISION_SUFFIX = 100


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


@dataclass(frozen=True)
class SyncPlan:
    """What one sync run will actually transfer, decided before the first byte.

    Produced by :meth:`WiCANLogClient.plan` — the ONE place the skip decisions
    (unsafe name, active trip file, already downloaded) live. ``total_bytes``
    is the sum of the sizes ``/csv_list`` advertised for ``to_download``, so a
    progress display is byte-accurate from the start.
    """

    to_download: list = field(default_factory=list)  # list[(TripLog, Path)]
    skipped: list = field(default_factory=list)  # list[str] remote names
    total_bytes: int = 0


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

    def plan(self, dest_dir, *, skip_active: bool = True) -> SyncPlan:
        """Decide what a sync run will download, without transferring anything.

        Skips (in ``skipped``, by remote name): logs already present with the
        same size (under the original or a ``-N`` collision-suffixed name),
        the active trip file when *skip_active*, and logs whose device name
        fails sanitization (warned, never trusted).
        """
        dest_dir = Path(dest_dir)
        logs = self.list_logs()

        active = None
        if skip_active:
            # If we cannot determine the active file, downloading a growing CSV
            # is the risk — fail the run rather than guess.
            active = self.active_log_basename()

        to_download = []
        skipped = []
        total_bytes = 0
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
                skipped.append(log.name)
                continue

            if active is not None and name == active:
                logger.info("Skipping active trip file %s (still being written)", name)
                skipped.append(log.name)
                continue

            target = self._resolve_target(dest_dir, name, log.size, reserved)
            if target is None:
                skipped.append(log.name)  # already downloaded
                continue

            reserved.add(target)
            to_download.append((log, target))
            total_bytes += log.size

        return SyncPlan(
            to_download=to_download, skipped=skipped, total_bytes=total_bytes
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
        for log, target in sync_plan.to_download:
            if abort_cb is not None and abort_cb():
                _log_abort()
                break

            per_file_cb = None
            if progress_cb is not None:
                per_file_cb = self._file_progress(progress_cb, base, total, log.name)

            url = self._url(CSV_DOWNLOAD_PATH, {"file": log.name})
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
                    break
                raise
            logger.info("Downloaded trip log %s (%d bytes)", path.name, log.size)
            result.downloaded.append(path)
            base += log.size

        return result

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
        plain = dest_dir / name
        stem, dot, ext = name.rpartition(".")
        if not dot:  # no extension — suffix the whole name
            stem, ext = name, ""

        candidate = plain
        for n in range(2, _MAX_COLLISION_SUFFIX + 1):
            if candidate in reserved:
                pass  # promised to another log this run — probe the next name
            elif not candidate.exists():
                return candidate
            else:
                try:
                    if candidate.stat().st_size == size:
                        return None  # same trip already downloaded
                except OSError:
                    pass  # race/unreadable — treat as occupied, probe the next
            candidate = dest_dir / (f"{stem}-{n}.{ext}" if ext else f"{stem}-{n}")
        raise WiCANLogsError(
            f"More than {_MAX_COLLISION_SUFFIX} local name collisions for "
            f"{name!r} — refusing to continue (corrupt local logs dir?)"
        )
