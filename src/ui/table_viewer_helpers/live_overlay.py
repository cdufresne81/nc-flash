"""
Live table cursor drawn over the table cells.

Where the Datalogger's live values fall on the table, the way tuning tools mark
an operating point: a compact cross on the exact (interpolated) point with a dot
at its centre, a bold outline on the nearest cell, and the live values tagged
on the axis headers. A chip next to the cross gives the table's own value at
that point (blended between the surrounding cells), and the nearest cell's
number is redrawn over the cross so the cursor never hides it. Drawn after the
cells (``DecoratedTable.paintEvent``), so
cell colors, selection and editing are untouched. A move repaints only the few
cells the cross left and entered: full-length crosshair lines (a whole row +
column per move, per window) cost most of a CPU core with three tables open.
"""

import dataclasses
import logging
import math
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QRegion
from PySide6.QtWidgets import QTableWidget

from .. import theme

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LiveCursorState:
    """What to draw, in data coordinates (flips are resolved when drawing)."""

    rows: int
    cols: int  # 1 for a 2D table (no vertical line, no X tag)
    fr: Optional[float] = None  # None: nothing to point at (message only)
    fc: float = 0.0
    y_tag: Tuple[str, bool] = ("", False)  # (text, off the table)
    x_tag: Tuple[str, bool] = ("", False)
    message: str = ""  # e.g. "Live cursor needs: LOAD (not being logged)"
    value_tag: str = ""  # the table's value at the point, formatted like a cell


class LiveCursorOverlay:
    """Geometry + painting of the live cursor for one table widget."""

    HALO = 3.0  # white halo under the dark lines: readable on any cell color
    DOT = 6.0  # dot radius
    ARM = 0.5  # cross arm length, in cells (from the centre)
    #: A move smaller than this (logical px) from where the cross is drawn
    #: leaves it there: a redraw repaints ~10 cells, and sensor noise would
    #: otherwise redraw on nearly every update (a cell is ~70 x 24 px; the
    #: nearest-cell outline still moves the moment the nearest cell changes).
    #: 3 px cut CPU 49% -> 30% with 3 tables + graphs on a steady signal.
    MOVE_PX = 3.0

    def __init__(self, table: QTableWidget, to_ui: Callable[[int, int], tuple]):
        self._tw = table
        self._to_ui = to_ui  # data (row, col) -> UI (row, col), flips included
        self.state: Optional[LiveCursorState] = None
        self._font = QFont(table.font())
        self._font.setBold(True)
        self._metrics = QFontMetrics(self._font)
        self._last_value_rect = QRect()  # where the value chip was last drawn

    # --- state ------------------------------------------------------------------

    def set_state(self, state: Optional[LiveCursorState]):
        """Show *state* (None hides); repaints only what changed.

        If the point moved less than MOVE_PX from where the cross is drawn
        (and the nearest cell is the same), the cross stays put and only a tag
        whose text changed is repainted. The comparison is with the drawn
        position, so a slow drift still moves the cross once it adds up.
        """
        if state == self.state:
            return
        old_state = self.state
        if self._same_spot(old_state, state):
            state = dataclasses.replace(state, fr=old_state.fr, fc=old_state.fc)
            changed = self._changed_tag_region(old_state, state)
            self.state = state
            if old_state.value_tag != state.value_tag:
                changed = changed.united(self._value_region()).united(
                    QRegion(self._last_value_rect)
                )
            if not changed.isEmpty():
                self._tw.viewport().update(changed)
            return
        old = self._safe_region()
        self.state = state
        self._tw.viewport().update(old.united(self._safe_region()))

    def _same_spot(self, old, new) -> bool:
        if (
            old is None
            or new is None
            or old.fr is None
            or new.fr is None
            or (old.rows, old.cols, old.message) != (new.rows, new.cols, new.message)
        ):
            return False
        try:
            drawn = self.geometry()
            self.state = new
            moved = self.geometry()
        except Exception:  # noqa: BLE001 - let the normal path handle it
            return False
        finally:
            self.state = old
        return (
            abs(moved["x"] - drawn["x"]) < self.MOVE_PX
            and abs(moved["y"] - drawn["y"]) < self.MOVE_PX
            and moved["peak"] == drawn["peak"]
        )

    def _changed_tag_region(self, old, new) -> QRegion:
        """The tags (same positions: the cross didn't move) whose text changed."""
        region = QRegion()
        if (old.y_tag, old.x_tag) == (new.y_tag, new.x_tag):
            return region
        geo = self.geometry()  # self.state is still *old*: same tag rects
        texts = [old.y_tag != new.y_tag, old.x_tag != new.x_tag]
        if new.cols <= 1 or not new.x_tag[0]:
            texts = texts[:1]
        for (rect, _, _), changed in zip(geo["tags"], texts):
            if changed:
                region = region.united(QRegion(rect.toAlignedRect()))
        return region

    def _value_region(self) -> QRegion:
        """Where the value chip of the current state is drawn."""
        try:
            geo = self.geometry()
        except Exception:  # noqa: BLE001 - display only
            return QRegion(self._tw.viewport().rect())
        if geo is None or geo["value"] is None:
            return QRegion()
        return QRegion(geo["value"][0].toAlignedRect().adjusted(-1, -1, 1, 1))

    def _safe_region(self) -> QRegion:
        """The area to repaint; the whole viewport if it can't be worked out,
        so a cursor that can't be placed is still erased."""
        try:
            return self._dirty_region()
        except Exception:  # noqa: BLE001 - display only
            return QRegion(self._tw.viewport().rect())

    # --- geometry -----------------------------------------------------------------

    def _col_center(self, ui_col: int) -> float:
        tw = self._tw
        return tw.columnViewportPosition(ui_col) + tw.columnWidth(ui_col) / 2

    def _row_center(self, ui_row: int) -> float:
        tw = self._tw
        return tw.rowViewportPosition(ui_row) + tw.rowHeight(ui_row) / 2

    def _cell_rect(self, ui_row: int, ui_col: int) -> QRectF:
        tw = self._tw
        return QRectF(
            tw.columnViewportPosition(ui_col),
            tw.rowViewportPosition(ui_row),
            tw.columnWidth(ui_col),
            tw.rowHeight(ui_row),
        )

    @staticmethod
    def _bracket(f: float, n: int) -> Tuple[int, int, float]:
        lo = min(max(int(math.floor(f)), 0), n - 1)
        hi = min(lo + 1, n - 1)
        return lo, hi, (min(max(f - lo, 0.0), 1.0) if hi != lo else 0.0)

    def geometry(self) -> Optional[dict]:
        """Pixel positions of everything drawn (viewport coordinates)."""
        s = self.state
        if s is None or s.fr is None:
            return None
        to_ui = self._to_ui
        r0, r1, tr = self._bracket(s.fr, s.rows)
        c0, c1, tc = self._bracket(s.fc, s.cols)
        y0, y1 = (self._row_center(to_ui(r, 0)[0]) for r in (r0, r1))
        x0, x1 = (self._col_center(to_ui(0, c)[1]) for c in (c0, c1))
        y = y0 + tr * (y1 - y0)
        x = x0 + tc * (x1 - x0)
        first, last = to_ui(0, 0), to_ui(s.rows - 1, s.cols - 1)
        corners = [self._cell_rect(*first), self._cell_rect(*last)]
        area = corners[0].united(corners[1])  # every data cell (flips included)
        # Nearest cell; a point exactly halfway goes to the higher breakpoint
        # (plain round() would alternate, rounding halves to even).
        nearest_r = min(int(math.floor(s.fr + 0.5)), s.rows - 1)
        nearest_c = min(int(math.floor(s.fc + 0.5)), s.cols - 1)
        peak = self._cell_rect(*to_ui(nearest_r, nearest_c))
        arm_x = self.ARM * self._tw.columnWidth(to_ui(0, c0)[1])
        arm_y = self.ARM * self._tw.rowHeight(to_ui(r0, 0)[0])
        cross = QRectF(x - arm_x, y - arm_y, 2 * arm_x, 2 * arm_y).intersected(area)
        geo = {
            "x": x,
            "y": y,
            "cross": cross,
            "peak": peak,
            "peak_ui": to_ui(nearest_r, nearest_c),
            "tags": [],
            "value": None,
        }
        if s.value_tag:
            geo["value"] = (self._value_rect(x, y, s.value_tag), s.value_tag)
        y_text, y_off = s.y_tag
        if y_text:  # Y tag in the Y-axis column (UI column 0)
            band = self._cell_rect(to_ui(r0, 0)[0], 0)
            geo["tags"].append((self._tag_rect(band, None, y), y_text, y_off))
        x_text, x_off = s.x_tag
        if s.cols > 1 and x_text:  # X tag in the X-axis row (UI row 0)
            band = self._cell_rect(0, to_ui(0, c0)[1])
            geo["tags"].append((self._tag_rect(band, x, None), x_text, x_off))
        return geo

    def _value_rect(self, x: float, y: float, text: str) -> QRectF:
        """The value chip: up and right of the dot, clear of the axis headers
        (row 0 of a 3D table, column 0) and the viewport edges; flipped left /
        down where it wouldn't fit."""
        tw = self._tw
        w = self._metrics.horizontalAdvance(text) + 10
        h = self._metrics.height() + 4
        free = QRectF(tw.viewport().rect())
        free.setLeft(tw.columnViewportPosition(0) + tw.columnWidth(0))
        if self.state.cols > 1:
            free.setTop(tw.rowViewportPosition(0) + tw.rowHeight(0))
        gap = self.DOT + 3
        left, top = x + gap, y - gap - h
        if left + w > free.right():
            left = x - gap - w
        if top < free.top():
            top = y + gap
        left = max(free.left(), min(left, free.right() - w))
        top = max(free.top(), min(top, free.bottom() - h))
        return QRectF(left, top, w, h)

    def _tag_rect(self, band: QRectF, x, y) -> QRectF:
        """A tag sized to its band (axis header cell), centred on x or y."""
        h = band.height() - 2
        w = band.width() - 2
        if x is not None:
            return QRectF(x - w / 2, band.top() + 1, w, h)
        return QRectF(band.left() + 1, y - h / 2, w, h)

    def _message_rect(self) -> QRectF:
        text = self.state.message if self.state is not None else ""
        if not text:
            return QRectF()
        w = self._metrics.horizontalAdvance(text) + 16
        h = self._metrics.height() + 8
        vw = self._tw.viewport().width()
        return QRectF(max(0.0, (vw - w) / 2), 4, w, h)

    def _dirty_region(self) -> QRegion:
        region = QRegion()
        if self.state is None:
            return region
        # Only what's drawn: every repainted cell goes through the delegate,
        # so the outline and tags stay inside their cells and only the dot
        # (radius + ring) spills past the cross.
        pad = self.DOT + 2
        msg = self._message_rect()
        if not msg.isEmpty():
            region = region.united(msg.adjusted(-2, -2, 2, 2).toAlignedRect())
        geo = self.geometry()
        if geo is None:
            return region
        strips = [
            geo["cross"].adjusted(-pad, -pad, pad, pad),
            geo["peak"],
        ]
        strips += [rect for rect, _, _ in geo["tags"]]
        if geo["value"] is not None:
            strips.append(geo["value"][0].adjusted(-1, -1, 1, 1))
        for rect in strips:
            region = region.united(QRegion(rect.toAlignedRect()))
        return region

    # --- painting -------------------------------------------------------------------

    def paint(self, painter: QPainter):
        painter.setRenderHint(QPainter.Antialiasing, True)
        geo = self.geometry()
        if geo is not None:
            self._paint_cursor(painter, geo)
        msg = self._message_rect()
        if not msg.isEmpty():
            self._paint_chip(painter, msg, self.state.message, theme.LIVE_TAG_TEXT)

    def _paint_cursor(self, painter: QPainter, geo: dict):
        cross, x, y = geo["cross"], geo["x"], geo["y"]
        lines = [((cross.left(), y), (cross.right(), y))]
        if self.state.cols > 1:
            lines.append(((x, cross.top()), (x, cross.bottom())))
        halo = QPen(QColor(theme.LIVE_CURSOR_HALO), self.HALO)
        line = QPen(QColor(theme.LIVE_CURSOR_LINE), 1.2)
        for pen in (halo, line):
            painter.setPen(pen)
            for a, b in lines:
                painter.drawLine(QPointF(*a), QPointF(*b))
        # Nearest cell: bold outline (haloed like the lines)
        peak = geo["peak"].adjusted(2.5, 2.5, -2.5, -2.5)  # pens stay inside
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(theme.LIVE_CURSOR_HALO), 4.0))
        painter.drawRect(peak)
        painter.setPen(QPen(QColor(theme.LIVE_CURSOR_LINE), 2.0))
        painter.drawRect(peak)
        # The exact point
        painter.setPen(QPen(QColor(theme.LIVE_CURSOR_HALO), 2.0))
        painter.setBrush(QColor(theme.LIVE_CURSOR))
        painter.drawEllipse(QPointF(x, y), self.DOT, self.DOT)
        painter.setBrush(Qt.NoBrush)
        self._paint_peak_text(painter, geo["peak_ui"])
        for rect, text, off in geo["tags"]:
            color = theme.LIVE_TAG_OFF if off else theme.LIVE_TAG_TEXT
            self._paint_chip(painter, rect, text, color)
        self._last_value_rect = QRect()
        if geo["value"] is not None:
            rect, text = geo["value"]
            self._paint_chip(painter, rect, text, theme.LIVE_TAG_TEXT)
            self._last_value_rect = rect.toAlignedRect().adjusted(-1, -1, 1, 1)

    def _paint_peak_text(self, painter: QPainter, ui_cell):
        """The nearest cell's own number, redrawn where Qt drew it, over the
        lines and the dot, with a halo of the cell's color so it stays
        readable."""
        layout = getattr(self._tw, "cell_text_layout", None)
        item = self._tw.item(*ui_cell)
        if layout is None or item is None or not item.text():
            return
        rect, align, font, text, color, halo = layout(self._tw.indexFromItem(item))
        painter.save()
        painter.setFont(font)
        painter.setPen(halo)
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            painter.drawText(rect.translated(dx, dy), align, text)
        painter.setPen(color)
        painter.drawText(rect, align, text)
        painter.restore()

    def _paint_chip(self, painter: QPainter, rect: QRectF, text: str, color: str):
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(theme.LIVE_TAG_BG))
        painter.drawRoundedRect(rect, 3, 3)
        painter.setBrush(Qt.NoBrush)
        painter.setFont(self._font)
        painter.setPen(QColor(color))
        painter.drawText(rect, Qt.AlignCenter, text)

    def tag_texts(self) -> list:
        """The tag texts as drawn (tests)."""
        geo = self.geometry()
        return [text for _, text, _ in geo["tags"]] if geo else []

    def value_text(self) -> str:
        """The value chip's text as drawn, "" when none (tests)."""
        geo = self.geometry()
        return geo["value"][1] if geo and geo["value"] else ""
