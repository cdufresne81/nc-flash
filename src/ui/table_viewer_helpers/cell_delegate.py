"""
Cell Delegate for Modified Cell Borders and Diff Highlighting

Renders a thin gray border around cells that have been modified during the session.
Also highlights cells that differ from a base version in diff mode.
"""

from PySide6.QtWidgets import QStyledItemDelegate
from PySide6.QtCore import Qt
from PySide6.QtGui import QPen, QColor, QBrush

from ...core.rom_definition import TableType


class ModifiedCellDelegate(QStyledItemDelegate):
    """Delegate that draws gray borders around modified cells and diff highlights"""

    # Color for diff highlighting (yellow/gold with transparency)
    DIFF_HIGHLIGHT_COLOR = QColor(255, 255, 100, 80)

    def __init__(self, viewer, parent=None):
        super().__init__(parent)
        self.viewer = viewer

    def paint(self, painter, option, index):
        """Paint cell with modified border, diff highlight, and/or axis separator"""
        # Let the default delegate paint the cell first
        super().paint(painter, option, index)

        painter.save()

        # Draw diff highlight if in diff mode and cell differs from base
        if self.viewer.show_diff_highlights():
            # Get data coordinates from the cell
            data_coords = index.data(Qt.UserRole)
            if data_coords is not None:
                data_row, data_col = data_coords
                if self.viewer.is_cell_changed_from_base(data_row, data_col):
                    # Fill with semi-transparent yellow
                    painter.fillRect(option.rect, self.DIFF_HIGHLIGHT_COLOR)

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
