"""THE live CSV capture: the one file sink both Datalogger engines write through.

A capture is a ``live_<YYYYmmdd_HHMMSS>.csv`` in ``{logs_directory}/live``:
the header first, then every row appended and **flushed** at once so
MegaLogViewerHD (or any tailer) sees it. Once a file holds
:data:`TRAIL_MIN_ROWS` rows it is "ready": MegaLogViewerHD only engages its
live trail at 50 samples and gives up after 60 s, so it is launched then.

Qt-free; the engines' worker threads own one instance each and run every call
on that thread.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: Rows a capture must hold before MegaLogViewerHD is launched on it: MLV only
#: engages its live trail at 50 samples and gives up after 60 s.
TRAIL_MIN_ROWS = 50


def live_capture_dir(logs_dir) -> Path:
    """The folder live captures land in: ``{logs_directory}/live``."""
    return Path(logs_dir) / "live"


class LiveCsvCapture:
    """One growing capture file at a time in *directory*."""

    def __init__(self, directory, *, label: str = "Live datalog"):
        self._dir = Path(directory)
        self._label = label  # log-line prefix
        self._file = None
        self._path = ""
        self._header: Optional[str] = None
        self._rows = 0
        self._ready = False

    @property
    def is_open(self) -> bool:
        return self._file is not None

    @property
    def path(self) -> str:
        return self._path

    @property
    def header(self) -> Optional[str]:
        """The open file's header, or None (no file, or written without one)."""
        return self._header

    @property
    def rows(self) -> int:
        """Rows written to the open file."""
        return self._rows

    def open(self, header: Optional[str]) -> str:
        """Close any open file and start a new one; return its path.

        ``header`` None writes no header row (the caller could not learn the
        column names).
        """
        self.close()
        self._dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self._dir / f"live_{ts}.csv"
        n = 2
        while path.exists():  # never clobber a capture from the same second
            path = self._dir / f"live_{ts}-{n}.csv"
            n += 1
        self._file = open(path, "w", encoding="utf-8", newline="")
        self._path = str(path)
        self._header = header
        self._rows = 0
        self._ready = False
        if header is not None:
            self._file.write(header + "\n")
            self._file.flush()
        logger.info("%s recording to %s", self._label, path)
        return self._path

    def write_row(self, line: str) -> bool:
        """Append one row and flush it. True exactly once per file: when the
        file has just reached :data:`TRAIL_MIN_ROWS` rows."""
        if self._file is None:
            return False
        self._file.write(line + "\n")
        self._file.flush()  # per-row flush is the point: MLV tails this file
        self._rows += 1
        if not self._ready and self._rows >= TRAIL_MIN_ROWS:
            self._ready = True
            return True
        return False

    def close(self) -> None:
        if self._file is None:
            return
        try:
            self._file.flush()
            self._file.close()
        except OSError:
            pass
        logger.info(
            "%s capture saved: %s (%d rows)", self._label, self._path, self._rows
        )
        self._file = None
        self._path = ""
        self._header = None


class StatusThrottle:
    """At most one status update per *every_rows* rows or *every_s* seconds,
    whichever comes first (never one signal per row)."""

    def __init__(
        self, every_rows: int = 50, every_s: float = 1.0, clock=time.monotonic
    ):
        self._every_rows = every_rows
        self._every_s = every_s
        self._clock = clock
        self._last_rows = 0
        self._last_t = 0.0

    def due(self, rows_total: int) -> bool:
        now = self._clock()
        if (rows_total - self._last_rows) >= self._every_rows or (
            now - self._last_t
        ) >= self._every_s:
            self._last_rows = rows_total
            self._last_t = now
            return True
        return False
