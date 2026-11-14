"""
Table cell marks and the cell editor

The table widget (``DecoratedTable``) lets Qt draw the cells, then paints the
per-cell marks over them in one pass (``CellDecorations``): a gray border around
cells modified this session, a yellow fill on cells that differ from the base
version in diff mode, the axis separator lines, and the accent outline on the
axis breakpoints of the selection. Then the live cursor.

The marks used to be drawn by a Python ``paint()`` override on the item
delegate, called once per cell on every repaint: that call alone cost ~57 of
the ~169 us each cell took to draw (Load Scaling, Oct 2026), most of what the
live cursor's repaints cost with several tables open. Now Qt's own C++ delegate
draws the cells, and the Python work runs once per repaint, only for the cells
that carry a mark.

The delegate (``CellEditorDelegate``) only restricts the cell editor to numeric
input; it doesn't override ``paint()``.
"""

import logging

from PySide6.QtCore import QRect, QRegularExpression, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QPainter,
    QPalette,
    QPen,
    QRegularExpressionValidator,
)
from PySide6.QtWidgets import (
    QLineEdit,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
)

from ...core.rom_definition import TableType
from ...utils.formatting import (
    HEX_INPUT_PATTERN,
    NUMERIC_INPUT_PATTERN,
    is_hex_format,
)
from .. import theme

logger = logging.getLogger(__name__)


class CellEditorDelegate(QStyledItemDelegate):
    """Delegate that restricts the cell editor to numeric input"""

    def __init__(self, viewer, parent=None):
        super().__init__(parent)
        self.viewer = viewer

    def createEditor(self, parent, option, index):
        """Return a line edit that only accepts numeric keystrokes.

        Digits, one decimal separator and a leading sign; no letter can be
        typed at all, so 'nan'/'inf' can never reach the ROM. The
        parse-and-revert path in TableEditHelper remains the second line of
        defence for values arriving by other routes (paste, scripting).

        Hex-formatted cells (e.g. Tire Size Correction) accept hex digits
        instead, matching what the cell displays.
        """
        editor = QLineEdit(parent)
        pattern = NUMERIC_INPUT_PATTERN
        if is_hex_format(self.viewer.cell_format(index.data(Qt.UserRole))):
            pattern = HEX_INPUT_PATTERN
        editor.setValidator(
            QRegularExpressionValidator(QRegularExpression(pattern), editor)
        )
        return editor


class CellDecorations:
    """Paints the per-cell marks over the cells Qt has drawn"""

    # Color for diff highlighting (yellow/gold with transparency)
    DIFF_HIGHLIGHT_COLOR = QColor(255, 255, 100, 80)

    def __init__(self, viewer):
        self.viewer = viewer
        self._axis_pen = None
        self._failed = False  # log a paint error once, not on every repaint

    def _axis_highlight_pen(self) -> QPen:
        if self._axis_pen is None:
            self._axis_pen = QPen(QColor(theme.AXIS_HIGHLIGHT), 2)
            self._axis_pen.setJoinStyle(Qt.MiterJoin)
        return self._axis_pen

    def paint(self, painter: QPainter, region):
        """Paint the marks of every cell in ``region`` (viewport coordinates)."""
        try:
            self._paint(painter, region)
        except Exception:  # noqa: BLE001 - never break the table's paint
            if not self._failed:
                self._failed = True
                logger.exception("Table cell marks could not be drawn")

    def _paint(self, painter, region):
        viewer = self.viewer
        tw = viewer.table_widget
        rows, cols = tw.rowCount(), tw.columnCount()
        if not rows or not cols:
            return
        # Without diff, selection or edits, only row 0 and column 0 (where the
        # axis separators are) can carry a mark.
        every_cell = (
            viewer.show_diff_highlights()
            or viewer.has_axis_highlight()
            or viewer.has_modified_cells()
        )
        cells = set()
        for rect in region:
            row_span = _span(tw.rowAt, rect.top(), rect.bottom(), rows)
            col_span = _span(tw.columnAt, rect.left(), rect.right(), cols)
            if row_span is None or col_span is None:
                continue
            for row in range(row_span[0], row_span[1] + 1):
                if every_cell or row == 0:
                    cells.update((row, c) for c in range(col_span[0], col_span[1] + 1))
                elif col_span[0] == 0:
                    cells.add((row, 0))
        for row, col in sorted(cells):
            item = tw.item(row, col)
            if item is not None:
                self._paint_cell(painter, tw, item, row, col)

    def _paint_cell(self, painter, tw, item, row, col):
        viewer = self.viewer
        coords = item.data(Qt.UserRole)
        is_axis = coords is not None and isinstance(coords[0], str)

        diff = (
            coords is not None
            and not is_axis
            and viewer.show_diff_highlights()
            and viewer.is_cell_changed_from_base(coords[0], coords[1])
        )
        separator = item.data(Qt.UserRole + 2) == "axis_separator"
        axis_highlight = (
            is_axis
            and viewer.has_axis_highlight()
            and viewer.is_axis_highlighted(coords[0], coords[1])
        )
        modified = viewer.has_modified_cells() and viewer.is_cell_modified(row, col)
        if not (diff or separator or axis_highlight or modified):
            return

        rect = tw.visualItemRect(item)
        painter.save()
        # As when each cell painted its own marks: nothing spills onto the
        # grid lines or the neighbouring cells.
        painter.setClipRect(rect)

        # Fill with semi-transparent yellow
        if diff:
            painter.fillRect(rect, self.DIFF_HIGHLIGHT_COLOR)

        # Axis separator: a border in the window background color
        if separator:
            painter.setPen(QPen(tw.palette().window().color(), 3))
            # Row 0 of a 3D table is the X-axis row: bottom border. For 2D
            # tables row 0 is a data row.
            table = viewer.current_table
            if row == 0 and table and table.type == TableType.THREE_D:
                painter.drawLine(rect.bottomLeft(), rect.bottomRight())
            # Column 0 is the Y-axis (2D and 3D): right border
            if col == 0:
                painter.drawLine(rect.topRight(), rect.bottomRight())

        # Axis breakpoint of the current selection: accent outline only (no bold:
        # bold text is wider and got elided, e.g. "100.0" -> "10...").
        if axis_highlight:
            painter.setPen(self._axis_highlight_pen())
            painter.drawRect(rect.adjusted(1, 1, -1, -1))

        # Modified this session: 2px gray border, inset to avoid clipping
        if modified:
            pen = QPen(QColor(100, 100, 100), 2)
            pen.setJoinStyle(Qt.MiterJoin)
            painter.setPen(pen)
            painter.drawRect(rect.adjusted(1, 1, -1, -1))

        painter.restore()


def _span(at, low: int, high: int, count: int):
    """First and last row (or column) a viewport span covers; None if none."""
    first = at(low)
    if first < 0:
        if low > 0:
            return None  # past the last row / column
        first = 0
    last = at(high)
    if last < 0:
        last = count - 1
    return first, last


class DecoratedTable(QTableWidget):
    """The viewer's table widget: Qt draws the cells, then the cell marks and
    the live cursor are painted over them."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.decorations = None  # CellDecorations
        self.live_overlay = None  # LiveCursorOverlay

    def cell_text_layout(self, index):
        """Where and how Qt draws a cell's text: (rect, alignment, font, text,
        text color, cell color). The live cursor redraws the nearest cell's
        number over itself with this, so it lands exactly on Qt's own text."""
        option = QStyleOptionViewItem()
        self.initViewItemOption(option)
        option.rect = self.visualRect(index)
        self.itemDelegate().initStyleOption(option, index)
        style = self.style()
        # As QCommonStyle lays out an item's text
        margin = style.pixelMetric(QStyle.PM_FocusFrameHMargin, None, self) + 1
        rect = style.subElementRect(QStyle.SE_ItemViewItemText, option, self)
        rect = rect.adjusted(margin, 0, -margin, 0)
        if self.selectionModel().isSelected(index):
            color = option.palette.color(QPalette.HighlightedText)
            cell = option.palette.color(QPalette.Highlight)
        else:
            color = option.palette.color(QPalette.Text)
            cell = option.backgroundBrush.color()
            if option.backgroundBrush.style() == Qt.NoBrush:
                cell = option.palette.color(QPalette.Base)
        # Copies: the option owns its font and colors and is freed on return
        return (
            QRect(rect),
            option.displayAlignment,
            QFont(option.font),
            str(option.text),
            QColor(color),
            QColor(cell),
        )

    def paintEvent(self, event):  # noqa: N802 - Qt override
        super().paintEvent(event)
        decorations, overlay = self.decorations, self.live_overlay
        live = overlay is not None and overlay.state is not None
        if decorations is None and not live:
            return
        painter = QPainter(self.viewport())
        try:
            if decorations is not None:
                decorations.paint(painter, event.region())
            if live:
                try:
                    overlay.paint(painter)
                except Exception:  # noqa: BLE001 - never break the table's paint
                    logger.exception("Live table cursor could not be drawn; hiding it")
                    overlay.state = None
                    self.viewport().update()
        finally:
            painter.end()
