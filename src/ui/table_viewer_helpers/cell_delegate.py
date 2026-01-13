"""
Cell Delegate for Modified Cell Borders

Renders a thin gray border around cells that have been modified during the session.
"""

from PySide6.QtWidgets import QStyledItemDelegate
from PySide6.QtCore import Qt
from PySide6.QtGui import QPen, QColor

from ...core.rom_definition import TableType


class ModifiedCellDelegate(QStyledItemDelegate):
    """Delegate that draws gray borders around modified cells"""

    def __init__(self, viewer, parent=None):
        super().__init__(parent)
        self.viewer = viewer

    def paint(self, painter, option, index):
        """Paint cell with modified border and/or axis separator if applicable"""
        # Let the default delegate paint the cell first
        super().paint(painter, option, index)

        painter.save()

        # Check if this cell is an axis separator (needs border for visual separation)
        is_axis_separator = index.data(Qt.UserRole + 2) == 'axis_separator'
        if is_axis_separator:
            # Determine border position based on row/column and table type
            # Use window background color for subtle separation
            bg_color = option.palette.window().color()
            pen = QPen(bg_color, 3)  # 3px border in window background color
            painter.setPen(pen)

            # Get current table type to determine which borders to draw
            current_table = self.viewer.current_table
            is_3d_table = current_table and current_table.type == TableType.THREE_D

            # If row 0 AND 3D table (X-axis row), draw bottom border
            # For 2D tables, row 0 is a data row, not an axis row
            if index.row() == 0 and is_3d_table:
                painter.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())

            # If column 0 (Y-axis), draw right border (both 2D and 3D tables have Y-axis)
            if index.column() == 0:
                painter.drawLine(option.rect.topRight(), option.rect.bottomRight())

        # Check if this cell is modified (draw complete border around)
        if self.viewer.is_cell_modified(index.row(), index.column()):
            # Draw a thin gray border around the cell
            pen = QPen(QColor(100, 100, 100), 2)  # 2px gray border
            pen.setJoinStyle(Qt.MiterJoin)
            painter.setPen(pen)
            # Draw rectangle slightly inset to avoid clipping
            rect = option.rect.adjusted(1, 1, -1, -1)
            painter.drawRect(rect)

        painter.restore()
