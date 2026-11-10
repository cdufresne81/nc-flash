"""Replaying an existing log: reading the CSV and pacing its rows.

A log is a CSV with a header row: an NC Flash capture (``Time`` in seconds,
then one column per channel) or a WiCAN trip log (``timestamp_ms``, ``datetime``,
the channels, ``mark``). :func:`load_log` reads it whole; :class:`ReplayPlayer`
says which rows are due as time passes. Neither touches the ECU, a file sink or
Qt: :class:`~src.ui.log_replay.LogReplay` feeds the rows to the Datalogger's
live values and live CSV.
"""

from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

#: Time columns, and the factor that turns their values into seconds.
TIME_COLUMNS = {
    "time": 1.0,
    "time_s": 1.0,
    "timestamp_ms": 0.001,
    "time_ms": 0.001,
}
#: Columns that are not channels and are left out of the replay.
SKIPPED_COLUMNS = {"datetime", "mark"}

#: A log with no time column plays at this pace.
DEFAULT_ROW_S = 0.1
#: A wait between two rows longer than this (or time going backwards, as a
#: WiCAN restart between trips does) is played as :data:`GAP_PLAY_S` instead.
MAX_GAP_S = 2.0
GAP_PLAY_S = 0.5
#: One :meth:`ReplayPlayer.advance` never plays more than this much wall time,
#: so a stalled UI doesn't release a burst of rows.
MAX_STEP_S = 0.5
#: An empty cell takes the value from up to this many rows earlier (a channel
#: can be polled as rarely as every 64th row).
HOLD_ROWS = 64

#: Other names logs give a channel, spelled lowercase without punctuation.
ALIASES = {
    "enginerpm": "RPM",
    "enginespeed": "RPM",
    "engineload": "LOAD",
    "calculatedload": "LOAD",
    "calcload": "LOAD",
    "coolant": "ECT",
    "coolanttemp": "ECT",
    "coolanttemperature": "ECT",
    "enginecoolanttemp": "ECT",
    "enginecoolanttemperature": "ECT",
    "intakeairtemp": "IAT",
    "intakeairtemperature": "IAT",
    "intaketemp": "IAT",
    "acceleratorpedal": "APP",
    "acceleratorpedalposition": "APP",
    "pedal": "APP",
    "throttle": "TPS",
    "throttleposition": "TPS",
    "tp": "TPS",
    "vehiclespeed": "VSS",
    "speed": "VSS",
    "manifoldpressure": "MAP",
    "manifoldabsolutepressure": "MAP",
    "barometricpressure": "BARO",
    "massairflow": "MAF",
    "mafvoltage": "MAF_V",
    "mafv": "MAF_V",
    "knockretard": "KNOCKR",
    "sparkadvance": "SPARKADV",
    "ignitionadvance": "SPARKADV",
    "timingadvance": "SPARKADV",
}


class ReplayLogError(ValueError):
    """A file that can't be replayed; the message says why in plain words."""


def _key(name: str) -> str:
    """A column name without its unit in brackets, case or punctuation."""
    name = re.sub(r"[\(\[].*?[\)\]]", "", name)
    return re.sub(r"[^a-z0-9]", "", name.lower())


def map_columns(names: Iterable[str], known: Iterable[str]) -> list[str]:
    """The channel name each log column replays as.

    A column keeps its name unless it matches a *known* channel in another
    spelling (case, punctuation, a unit in brackets) or through
    :data:`ALIASES`. Two columns never get the same name: the first one wins
    and the other keeps its own.
    """
    names = [n.strip() for n in names]
    known = list(known)
    by_key = {_key(k): k for k in known}
    known_set = set(known)
    taken = set(names)  # a name already in the log is never given to another
    result = []
    for name in names:
        target = name
        if name not in known_set:
            key = _key(name)
            candidate = by_key.get(key) or ALIASES.get(key)
            if candidate and candidate not in taken:
                target = candidate
                taken.add(candidate)
        result.append(target)
    return result


def _number(cell: str) -> Optional[float]:
    try:
        value = float(cell)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


@dataclass(frozen=True)
class ReplayLog:
    """A log read into memory, ready to replay."""

    path: str
    #: Channel name per replayed column (aliases applied).
    columns: tuple
    #: Per row: the cells of :attr:`columns`, as written in the file.
    cells: tuple
    #: Per row: seconds from the first row at which it plays (gaps shortened).
    offsets: tuple
    #: False: the file had no time column and plays at :data:`DEFAULT_ROW_S`.
    has_time: bool
    #: Lines left out because they could not be read as a row.
    skipped_rows: int = 0
    #: Waits shortened to :data:`GAP_PLAY_S`.
    gaps: int = 0
    #: ``{name in the file: channel}`` for the columns that were renamed.
    renamed: tuple = ()

    @property
    def row_count(self) -> int:
        return len(self.cells)

    @property
    def duration_s(self) -> float:
        return self.offsets[-1] if self.offsets else 0.0

    @property
    def header(self) -> str:
        """The header of the live CSV a replay writes."""
        return ",".join(("Time",) + tuple(self.columns))

    def values(self, index: int) -> dict:
        """``{channel: value}`` at one row. A channel whose cell is empty there
        (the logger polls some channels less often) keeps the value of the
        closest earlier row, up to :data:`HOLD_ROWS` back."""
        values = {}
        missing = set(self.columns)
        for i in range(index, max(index - HOLD_ROWS, 0) - 1, -1):
            for name, cell in zip(self.columns, self.cells[i]):
                if name in missing:
                    number = _number(cell)
                    if number is not None:
                        values[name] = number
                        missing.discard(name)
            if not missing:
                break
        return values


def load_log(path, known_channels: Iterable[str] = ()) -> ReplayLog:
    """Read a CSV log, or raise :class:`ReplayLogError` saying what is wrong."""
    path = Path(path)
    try:
        with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
            lines = [row for row in csv.reader(f) if any(c.strip() for c in row)]
    except (OSError, csv.Error) as exc:
        raise ReplayLogError(f"Could not read {path.name}: {exc}") from exc
    if not lines:
        raise ReplayLogError(f"{path.name} is empty.")
    header = [c.strip() for c in lines[0]]
    if all(_number(c) is not None for c in header if c):
        raise ReplayLogError(
            f"{path.name} has no header row, so its columns can't be told apart."
        )

    time_col, time_scale = None, 1.0
    keep = []  # indexes of the channel columns
    for i, name in enumerate(header):
        low = name.lower()
        if low in TIME_COLUMNS:
            if time_col is None:
                time_col, time_scale = i, TIME_COLUMNS[low]
        elif name and low not in SKIPPED_COLUMNS:
            keep.append(i)
    if not keep:
        raise ReplayLogError(f"{path.name} has no channel columns.")

    names = [header[i] for i in keep]
    columns = map_columns(names, known_channels)
    renamed = tuple((a, b) for a, b in zip(names, columns) if a != b)

    cells, offsets = [], []
    skipped = gaps = 0
    last_t: Optional[float] = None
    for row in lines[1:]:
        if len(row) != len(header):
            skipped += 1  # a cut-off or malformed line
            continue
        kept = tuple(row[i].strip() for i in keep)
        if not any(_number(c) is not None for c in kept):
            skipped += 1  # a units row, a repeated header, text
            continue
        if time_col is None:
            step = DEFAULT_ROW_S
        else:
            t = _number(row[time_col])
            if t is None:
                skipped += 1
                continue
            t *= time_scale
            step = 0.0 if last_t is None else t - last_t
            if step < 0 or step > MAX_GAP_S:
                step = GAP_PLAY_S
                gaps += 1
            last_t = t
        offsets.append(offsets[-1] + step if offsets else 0.0)
        cells.append(kept)
    if not cells:
        raise ReplayLogError(f"{path.name} has no rows of numbers to replay.")
    return ReplayLog(
        path=str(path),
        columns=tuple(columns),
        cells=tuple(cells),
        offsets=tuple(offsets),
        has_time=time_col is not None,
        skipped_rows=skipped,
        gaps=gaps,
        renamed=renamed,
    )


class ReplayPlayer:
    """Which rows of a log are due, as wall time passes.

    The caller reports elapsed wall time to :meth:`advance` and gets the row
    indexes to play, in order. Position is the index of the next row to play.
    """

    def __init__(self, log: ReplayLog, *, speed: float = 1.0, loop: bool = False):
        self._log = log
        self._speed = 1.0
        self.speed = speed
        self.loop = loop
        self._next = 0
        self._clock = 0.0  # log seconds played so far

    @property
    def speed(self) -> float:
        return self._speed

    @speed.setter
    def speed(self, value: float):
        if not value > 0:
            raise ValueError("The replay speed must be above zero.")
        self._speed = float(value)

    @property
    def position(self) -> int:
        """The next row to play (``row_count`` at the end)."""
        return self._next

    @property
    def at_end(self) -> bool:
        return self._next >= self._log.row_count

    def seek(self, index: int):
        """Continue from row *index*; it is the first one the next
        :meth:`advance` plays."""
        index = max(0, min(int(index), self._log.row_count - 1))
        self._next = index
        self._clock = self._log.offsets[index]

    def advance(self, wall_s: float) -> list[int]:
        """The rows that became due during *wall_s* seconds of wall time."""
        offsets = self._log.offsets
        count = len(offsets)
        if self._next >= count:
            if not self.loop:
                return []
            self.seek(0)  # start over; nothing carries across the end
            return [self._take()]
        self._clock += max(0.0, min(wall_s, MAX_STEP_S)) * self._speed
        due = []
        while self._next < count and offsets[self._next] <= self._clock:
            due.append(self._take())
        return due

    def _take(self) -> int:
        index = self._next
        self._next += 1
        return index
