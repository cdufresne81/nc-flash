"""
Where a live value falls on a table, in data coordinates.

A fractional index ``2.25`` means a quarter of the way from breakpoint 2 to
breakpoint 3. Swap is already resolved when the ROM is read and flips are a
display concern, so callers pass the axes and values exactly as read.
"""

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import numpy as np


def axis_position(breakpoints, value: float) -> Optional[Tuple[float, bool]]:
    """``(fractional index, clamped)`` of *value* on an axis, or None.

    Works for ascending and descending axes; a value off either end sits on the
    end breakpoint with ``clamped`` True. None for an empty axis, a NaN value,
    or an axis that can't be trusted to place a cell: any non-finite breakpoint
    (e.g. an unpatched ROM's 0xFF axis reads as NaN) or one that goes up and
    down (mid-edit), where interpolation would land on a wrong-but-plausible
    cell.
    """
    bp = np.asarray(breakpoints, dtype=float)
    if bp.size == 0 or not np.isfinite(value) or not np.all(np.isfinite(bp)):
        return None
    steps = np.diff(bp)
    if not (np.all(steps >= 0) or np.all(steps <= 0)):
        return None
    if bp.size == 1:
        return 0.0, bool(value != bp[0])
    idx = np.arange(bp.size, dtype=float)
    if bp[-1] < bp[0]:
        bp, idx = bp[::-1], idx[::-1]
    clamped = bool(value < bp[0] or value > bp[-1])
    return float(np.interp(value, bp, idx)), clamped


@dataclass(frozen=True)
class LivePoint:
    """Where the live values put the cursor on one table (data coordinates)."""

    fr: float  # fractional row (Y axis breakpoint index)
    fc: float  # fractional column (X axis index; 0 for a 2D table)
    #: (channel, value, approx, off the table) per axis, the Y axis first
    readings: Tuple[Tuple[str, float, bool, bool], ...]


@dataclass(frozen=True)
class LiveStatus:
    """Outcome of :func:`locate`: a point, or why there is none."""

    point: Optional[LivePoint] = None
    missing: Tuple[str, ...] = ()  # mapped channels absent from the values


def locate(
    data: dict, is_3d: bool, x_map, y_map, values: Dict[str, float]
) -> LiveStatus:
    """Place *values* on a table read by ``RomReader.read_table_data``.

    *x_map*/*y_map* are the :class:`~src.core.axis_channels.AxisMapping` of
    each axis (*x_map* unused for a 2D table); an unmapped axis means no point.
    """
    axes = [("y", y_map, data.get("y_axis"))]
    if is_3d:
        axes.append(("x", x_map, data.get("x_axis")))
    if any(m is None or bp is None for _, m, bp in axes):
        return LiveStatus()
    missing = tuple(m.channel for _, m, _ in axes if m.channel not in values)
    if missing:
        return LiveStatus(missing=missing)
    pos, readings = {}, []
    for key, m, bp in axes:
        value = m.convert(values[m.channel])
        found = axis_position(bp, value)
        if found is None:
            return LiveStatus()
        pos[key] = found[0]
        readings.append((m.channel, value, m.approx, found[1]))
    return LiveStatus(LivePoint(pos["y"], pos.get("x", 0.0), tuple(readings)))


def table_value(values, fr: float, fc: float = 0.0) -> Optional[float]:
    """The table's value at a fractional point, blended linearly between the
    (up to four) surrounding cells; None if one of them isn't a number.

    *values* as read (2D tables: one value per row, *fc* unused).
    """
    v = np.asarray(values, dtype=float)
    if v.ndim == 1:
        v = v[:, None]
        fc = 0.0
    if v.ndim != 2 or v.size == 0:
        return None

    def bracket(f, n):
        lo = min(max(int(np.floor(f)), 0), n - 1)
        hi = min(lo + 1, n - 1)
        return lo, hi, (min(max(f - lo, 0.0), 1.0) if hi != lo else 0.0)

    r0, r1, tr = bracket(fr, v.shape[0])
    c0, c1, tc = bracket(fc, v.shape[1])
    corners = v[[r0, r0, r1, r1], [c0, c1, c0, c1]]
    if not np.all(np.isfinite(corners)):
        return None
    top = corners[0] + tc * (corners[1] - corners[0])
    bottom = corners[2] + tc * (corners[3] - corners[2])
    return float(top + tr * (bottom - top))
