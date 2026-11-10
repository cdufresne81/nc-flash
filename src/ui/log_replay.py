"""Replays an existing log through the Datalogger's live path.

One owner (:class:`LogReplay`, held by the Datalogger) of the replay state:
the loaded log, the position, pause, speed and loop. It plays the rows of a
CSV (see :mod:`src.core.log_replay`) at their recorded pace into the same two
places a live run feeds: the Datalogger's :class:`~src.ui.live_values.LiveValues`
(the live table cursor) and a :class:`~src.core.live_csv.LiveCsvCapture` in the
live logs folder (the file MegaLogViewerHD trails).

No ECU, no adapter, no thread: the log is in memory and a GUI-thread timer
releases the rows that are due.

The live CSV's ``Time`` column is playback time, so it always rises: pausing,
jumping with the position bar and looping all keep the same file growing and
MegaLogViewerHD keeps following it.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from src.core.live_csv import LiveCsvCapture, StatusThrottle, live_capture_dir
from src.core.log_replay import (
    DEFAULT_ROW_S,
    ReplayLog,
    ReplayLogError,
    ReplayPlayer,
    load_log,
)
from src.ui.live_values import EMIT_EVERY_S, LiveValues

logger = logging.getLogger(__name__)

#: How often the rows that are due are released.
TICK_MS = 25
SPEEDS = (1.0, 2.0, 5.0)
#: Time added to the live CSV between two rows that don't follow each other in
#: the log (after a jump or a loop).
JUMP_STEP_S = DEFAULT_ROW_S


def format_clock(seconds: float) -> str:
    """``m:ss.s`` for the position readout."""
    minutes, rest = divmod(max(0.0, seconds), 60.0)
    return f"{int(minutes)}:{rest:04.1f}"


class LogReplay(QObject):
    """Single owner of a log replay."""

    #: True from a successful start until stop.
    running_changed = Signal(bool)
    #: True while a running replay is paused.
    paused_changed = Signal(bool)
    #: Short human status.
    status_changed = Signal(str)
    #: Path of the live CSV being written, or "".
    capture_changed = Signal(str)
    #: That CSV now holds enough rows for MegaLogViewerHD to trail it.
    capture_ready = Signal(str)
    #: (row index shown, its time in the replay in seconds).
    position_changed = Signal(int, float)

    def __init__(
        self,
        settings,
        parent=None,
        *,
        live_values: LiveValues,
        clock: Callable[[], float] = time.monotonic,
    ):
        """*live_values* receives the replayed values (owned by the caller)."""
        super().__init__(parent)
        self._settings = settings
        self._live_values = live_values
        self._clock = clock
        self._log: Optional[ReplayLog] = None
        self._player: Optional[ReplayPlayer] = None
        self._capture: Optional[LiveCsvCapture] = None
        self._capture_path = ""
        self._speed = SPEEDS[0]
        self._loop = False
        self._paused = False
        self._shown = 0  # row the cursor shows
        self._last_written: Optional[int] = None
        self._csv_time = 0.0
        self._tick_at = 0.0
        self._values_throttle = StatusThrottle(
            # Half a tick early, so a late tick doesn't stretch it to 8 a second.
            every_rows=10**9,
            every_s=EMIT_EVERY_S - TICK_MS / 2000,
            clock=clock,
        )
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._on_tick)

    # --- state ------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._log is not None

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def log(self) -> Optional[ReplayLog]:
        """The log being replayed, or None."""
        return self._log

    @property
    def position(self) -> int:
        """The row the live cursor shows."""
        return self._shown

    @property
    def current_capture(self) -> str:
        return self._capture_path

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def loop(self) -> bool:
        return self._loop

    # --- controls -----------------------------------------------------------

    def start(self, path, known_channels: Iterable[str] = ()) -> bool:
        """Load *path* and play it from its first row. False (and a status
        saying why) when it can't be replayed or a replay already runs."""
        if self.is_running:
            return False
        try:
            log = load_log(path, known_channels)
        except ReplayLogError as exc:
            logger.warning("Log replay refused: %s", exc)
            self.status_changed.emit(str(exc))
            return False
        capture = LiveCsvCapture(
            live_capture_dir(self._settings.get_logs_directory()), label="Log replay"
        )
        try:
            self._capture_path = capture.open(log.header)
        except OSError as exc:
            logger.warning("Log replay could not create its live CSV: %s", exc)
            self.status_changed.emit(f"Could not create the live CSV: {exc}")
            return False
        self._log = log
        self._capture = capture
        self._player = ReplayPlayer(log, speed=self._speed, loop=self._loop)
        self._paused = False
        self._shown = 0
        self._last_written = None
        self._csv_time = 0.0
        self._tick_at = self._clock()
        logger.info(
            "Replaying %s: %d rows, %.1f s, %d row(s) skipped, %d gap(s) shortened",
            log.path,
            log.row_count,
            log.duration_s,
            log.skipped_rows,
            log.gaps,
        )
        self.running_changed.emit(True)
        self.capture_changed.emit(self._capture_path)
        self.status_changed.emit(self._describe(log))
        self._timer.start()
        self._on_tick()  # the first row shows at once
        return True

    def stop(self):
        """End the replay and close its live CSV. Safe when idle."""
        if not self.is_running:
            return
        self._end("Replay stopped")

    def set_paused(self, paused: bool):
        """Hold the position (the cursor stays up) or carry on."""
        paused = bool(paused)
        if not self.is_running or paused == self._paused:
            return
        self._paused = paused
        if paused:
            self._timer.stop()
        else:
            if self._player.at_end:
                self._player.seek(0)  # resuming at the end plays it again
            self._tick_at = self._clock()
            self._timer.start()
        self.paused_changed.emit(paused)

    def seek(self, index: int):
        """Jump to row *index*. The cursor moves there at once, paused or not;
        the live CSV only gets rows that are played."""
        if not self.is_running:
            return
        self._player.seek(index)
        self._tick_at = self._clock()
        self._show(self._player.position)

    def set_speed(self, speed: float):
        self._speed = float(speed)
        if self._player is not None:
            self._player.speed = self._speed

    def set_loop(self, loop: bool):
        self._loop = bool(loop)
        if self._player is not None:
            self._player.loop = self._loop

    # --- playback -----------------------------------------------------------

    def _on_tick(self):
        if self._player is None:
            return
        now = self._clock()
        due = self._player.advance(now - self._tick_at)
        self._tick_at = now
        try:
            for index in due:
                self._write(index)
        except OSError as exc:  # disk full, folder gone
            logger.warning("Log replay stopped: %s", exc)
            self._end(f"Replay stopped: could not write the live CSV ({exc})")
            return
        ended = self._player.at_end and not self._player.loop
        if due and (ended or self._values_throttle.due(0)):
            self._show(due[-1])
        if ended:
            # Hold the last point so the bar can be dragged back.
            self._paused = True
            self._timer.stop()
            self.paused_changed.emit(True)
            self.status_changed.emit(
                "End of the log. Drag the position bar back, press Play to "
                "start again, or stop the replay."
            )

    def _write(self, index: int):
        log = self._log
        if self._last_written is None:
            step = 0.0
        elif index == self._last_written + 1:
            step = log.offsets[index] - log.offsets[index - 1]
        else:
            step = JUMP_STEP_S
        self._csv_time += step
        self._last_written = index
        line = ",".join((f"{self._csv_time:.3f}",) + log.cells[index])
        if self._capture.write_row(line):
            self.capture_ready.emit(self._capture_path)

    def _show(self, index: int):
        """Put the live cursor on row *index*. The values don't expire: a
        paused replay keeps its cursor until it moves on or stops."""
        self._shown = index
        self._live_values.update(self._log.values(index), expire=False)
        self.position_changed.emit(index, self._log.offsets[index])

    def _end(self, status: str):
        self._timer.stop()
        was_paused = self._paused
        if self._capture is not None:
            self._capture.close()
        self._log = None
        self._player = None
        self._capture = None
        self._capture_path = ""
        self._paused = False
        self._shown = 0
        self._live_values.clear()
        if was_paused:
            self.paused_changed.emit(False)
        self.capture_changed.emit("")
        self.running_changed.emit(False)
        self.status_changed.emit(status)

    @staticmethod
    def _describe(log: ReplayLog) -> str:
        text = (
            f"Replaying {Path(log.path).name}: {log.row_count} rows, "
            f"{format_clock(log.duration_s)}."
        )
        if not log.has_time:
            text += (
                f" No time column, so it plays at {1 / DEFAULT_ROW_S:.0f} rows "
                "a second."
            )
        if log.gaps:
            text += f" {log.gaps} long pause(s) shortened."
        if log.skipped_rows:
            text += f" {log.skipped_rows} unreadable line(s) skipped."
        if log.renamed:
            text += " Read " + ", ".join(f"{a} as {b}" for a, b in log.renamed) + "."
        return text
