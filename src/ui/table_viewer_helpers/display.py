"""
Table Display Helper

Handles rendering, formatting, and coloring for TableViewer.
"""

import re
import numpy as np
import logging

from PySide6.QtWidgets import QTableWidgetItem, QHeaderView
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QBrush

from ...core.rom_definition import Table, TableType, AxisType
from ...utils.settings import get_settings
from .context import TableViewerContext

logger = logging.getLogger(__name__)


class TableDisplayHelper:
    """Helper class for table display and formatting operations"""

    def __init__(self, ctx: TableViewerContext):
        self.ctx = ctx

    def apply_table_style(self):
        """Apply table styling based on settings - compact like ECUFlash"""
        font_size = get_settings().get_table_font_size()

        self.ctx.table_widget.setStyleSheet(f"""
            QTableWidget {{
                font-size: {font_size}px;
                gridline-color: #a0a0a0;
            }}
            QTableWidget::item {{
                padding: 0px 1px;
            }}
            QTableWidget::item:selected {{
                background-color: rgba(255, 165, 0, 0.4);
                border: 2px solid #FF8C00;
                color: black;
                font-weight: bold;
            }}
        """)

        # Tight row height - just enough for the font
        row_height = font_size + 2
        self.ctx.table_widget.verticalHeader().setDefaultSectionSize(row_height)

    def display_table(self, table: Table, data: dict):
        """
        Display table data

        Args:
            table: Table definition
            data: Dictionary with 'values', 'x_axis', 'y_axis' from RomReader
        """
        # Update context state
        self.ctx.current_table = table
        self.ctx.current_data = data

        # Update info label
        info_text = (
            f"{table.name} | "
            f"Type: {table.type.value} | "
            f"Category: {table.category} | "
            f"Address: 0x{table.address}"
        )
        self.ctx.info_label.setText(info_text)

        values = data['values']

        if table.type == TableType.ONE_D:
            self._display_1d(values)
        elif table.type == TableType.TWO_D:
            self._display_2d(values, data.get('y_axis'))
        elif table.type == TableType.THREE_D:
            self._display_3d(values, data.get('x_axis'), data.get('y_axis'))

    def clear(self):
        """Clear the viewer"""
        self.ctx.current_table = None
        self.ctx.current_data = None
        self.ctx.info_label.setText("Select a table to view")
        self.ctx.table_widget.setRowCount(0)
        self.ctx.table_widget.setColumnCount(0)
        # Hide axis labels
        self.ctx.viewer.x_axis_label.setVisible(False)
        self.ctx.viewer.y_axis_label.setVisible(False)

    def _display_1d(self, values: np.ndarray):
        """Display 1D table (single value)"""
        # Hide axis labels (not used for 1D tables)
        self.ctx.viewer.x_axis_label.setVisible(False)
        self.ctx.viewer.y_axis_label.setVisible(False)

        self.ctx.editing_in_progress = True
        try:
            self.ctx.table_widget.horizontalHeader().setVisible(True)
            self.ctx.table_widget.setRowCount(1)
            self.ctx.table_widget.setColumnCount(1)
            self.ctx.table_widget.setHorizontalHeaderLabels(["Value"])
            self.ctx.table_widget.setVerticalHeaderLabels([""])

            value_fmt = self.get_value_format()
            item = QTableWidgetItem(self.format_value(values[0], value_fmt))
            color = self.get_cell_color(values[0], values, 0, 0)
            item.setBackground(QBrush(color))
            # Store data row/col for change tracking (data_row, data_col)
            item.setData(Qt.UserRole, (0, 0))
            if self.ctx.read_only:
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self.ctx.table_widget.setItem(0, 0, item)
        finally:
            self.ctx.editing_in_progress = False

    def _display_2d(self, values: np.ndarray, y_axis: np.ndarray):
        """Display 2D table (1D array with Y axis)"""
        # Hide axis labels (not used for 2D tables - Y axis is in the table)
        self.ctx.viewer.x_axis_label.setVisible(False)
        self.ctx.viewer.y_axis_label.setVisible(False)

        self.ctx.editing_in_progress = True
        try:
            num_values = len(values)
            self.ctx.table_widget.horizontalHeader().setVisible(True)
            self.ctx.table_widget.setRowCount(num_values)
            self.ctx.table_widget.setColumnCount(2)

            # Get axis label with unit
            y_label = self._get_axis_label(self.ctx.current_table, AxisType.Y_AXIS)
            self.ctx.table_widget.setHorizontalHeaderLabels([y_label, "Value"])

            # Get format specs
            y_fmt = self._get_axis_format(AxisType.Y_AXIS)
            value_fmt = self.get_value_format()

            # Apply flip if needed
            flipy = self.ctx.current_table.flipy if self.ctx.current_table else False
            display_values = values[::-1] if flipy else values
            display_y_axis = y_axis[::-1] if (y_axis is not None and flipy) else y_axis

            # Calculate Y axis gradient range
            if display_y_axis is not None and len(display_y_axis) > 0:
                y_min, y_max = np.min(display_y_axis), np.max(display_y_axis)
            else:
                y_min, y_max = 0, num_values - 1

            for i in range(num_values):
                # Y axis value with gradient
                if display_y_axis is not None and i < len(display_y_axis):
                    y_item = QTableWidgetItem(self.format_value(display_y_axis[i], y_fmt))
                    # Apply gradient based on Y axis values
                    if y_max != y_min:
                        ratio = (display_y_axis[i] - y_min) / (y_max - y_min)
                    else:
                        ratio = 0.5
                    y_item.setBackground(QBrush(self.ratio_to_color(ratio)))
                    # Store axis identification: ('y_axis', data_index)
                    # Account for flip when storing the actual data index
                    data_idx = (num_values - 1 - i) if flipy else i
                    y_item.setData(Qt.UserRole, ('y_axis', data_idx))
                    if self.ctx.read_only:
                        y_item.setFlags(y_item.flags() & ~Qt.ItemIsEditable)
                else:
                    y_item = QTableWidgetItem(str(i))
                    y_item.setBackground(QBrush(QColor(240, 240, 240)))
                    y_item.setFlags(y_item.flags() & ~Qt.ItemIsEditable)  # No axis data, not editable
                self.ctx.table_widget.setItem(i, 0, y_item)

                # Data value with gradient color
                value_item = QTableWidgetItem(self.format_value(display_values[i], value_fmt))
                color = self.get_cell_color(display_values[i], values, i, 0)
                value_item.setBackground(QBrush(color))
                # Store the actual data index (accounting for flip)
                data_row = (num_values - 1 - i) if flipy else i
                value_item.setData(Qt.UserRole, (data_row, 0))
                if self.ctx.read_only:
                    value_item.setFlags(value_item.flags() & ~Qt.ItemIsEditable)
                self.ctx.table_widget.setItem(i, 1, value_item)
        finally:
            self.ctx.editing_in_progress = False

    def _display_3d(self, values: np.ndarray, x_axis: np.ndarray, y_axis: np.ndarray):
        """
        Display 3D table (2D grid with X and Y axes) - ECUFlash style layout

        Layout:
        - Row 0: X-axis LABEL (gray background)
        - Row 1: X-axis VALUES (colored gradient)
        - Col 0: Y-axis LABEL (gray background)
        - Col 1: Y-axis VALUES (colored gradient)
        - Data starts at (row 2, col 2)
        """
        if values.ndim != 2:
            self._display_1d(values.flatten())
            return

        self.ctx.editing_in_progress = True
        try:
            rows, cols = values.shape

            # Set up table dimensions:
            # Rows: +1 (row 0 for X-axis values, rows 1+ for data)
            # Cols: +1 (col 0 for Y-axis values, cols 1+ for data)
            # Note: Axis labels are now separate widgets, not in the grid
            self.ctx.table_widget.setRowCount(rows + 1)
            self.ctx.table_widget.setColumnCount(cols + 1)

            # Hide Qt headers since we use cells for axes
            self.ctx.table_widget.horizontalHeader().setVisible(False)

            # Get axis labels with units
            x_label = self._get_axis_label(self.ctx.current_table, AxisType.X_AXIS)
            y_label = self._get_axis_label(self.ctx.current_table, AxisType.Y_AXIS)

            # Get format specs
            x_fmt = self._get_axis_format(AxisType.X_AXIS)
            y_fmt = self._get_axis_format(AxisType.Y_AXIS)
            value_fmt = self.get_value_format()

            # Apply flip flags if needed
            flipx = self.ctx.current_table.flipx if self.ctx.current_table else False
            flipy = self.ctx.current_table.flipy if self.ctx.current_table else False

            # Flip axes and values as needed
            display_x_axis = x_axis[::-1] if (x_axis is not None and flipx) else x_axis
            display_y_axis = y_axis[::-1] if (y_axis is not None and flipy) else y_axis
            display_values = values.copy()
            if flipy:
                display_values = display_values[::-1, :]
            if flipx:
                display_values = display_values[:, ::-1]

            # Gray background color for axis cells
            label_bg = QBrush(QColor(220, 220, 220))

            # Set axis labels in separate label widgets (ECUFlash style)
            self.ctx.viewer.x_axis_label.setText(x_label)
            self.ctx.viewer.x_axis_label.setVisible(True)
            self.ctx.viewer.y_axis_label.setText(y_label)
            self.ctx.viewer.y_axis_label.setVisible(True)

            # === Row 0: X-axis VALUES row (colored) ===
            # Cell (0,0) - empty gray cell (corner)
            empty_item = QTableWidgetItem("")
            empty_item.setFlags(empty_item.flags() & ~Qt.ItemIsEditable)
            empty_item.setBackground(label_bg)
            self.ctx.table_widget.setItem(0, 0, empty_item)

            # X-axis values in row 0 (columns 1+) with gradient
            if display_x_axis is not None and len(display_x_axis) == cols:
                x_min, x_max = np.min(display_x_axis), np.max(display_x_axis)
                for col in range(cols):
                    x_item = QTableWidgetItem(self.format_value(display_x_axis[col], x_fmt))
                    if x_max != x_min:
                        ratio = (display_x_axis[col] - x_min) / (x_max - x_min)
                    else:
                        ratio = 0.5
                    x_item.setBackground(QBrush(self.ratio_to_color(ratio)))
                    data_idx = (cols - 1 - col) if flipx else col
                    x_item.setData(Qt.UserRole, ('x_axis', data_idx))
                    if self.ctx.read_only:
                        x_item.setFlags(x_item.flags() & ~Qt.ItemIsEditable)
                    self.ctx.table_widget.setItem(0, col + 1, x_item)
            else:
                for col in range(cols):
                    x_item = QTableWidgetItem(str(col))
                    x_item.setFlags(x_item.flags() & ~Qt.ItemIsEditable)
                    x_item.setBackground(label_bg)
                    self.ctx.table_widget.setItem(0, col + 1, x_item)

            # === Column 0: Y-axis VALUES (rows 1+) ===
            if display_y_axis is not None and len(display_y_axis) == rows:
                y_min, y_max = np.min(display_y_axis), np.max(display_y_axis)
                for row in range(rows):
                    # Col 0: Y-axis value (colored)
                    y_val_item = QTableWidgetItem(self.format_value(display_y_axis[row], y_fmt))
                    if y_max != y_min:
                        ratio = (display_y_axis[row] - y_min) / (y_max - y_min)
                    else:
                        ratio = 0.5
                    y_val_item.setBackground(QBrush(self.ratio_to_color(ratio)))
                    data_idx = (rows - 1 - row) if flipy else row
                    y_val_item.setData(Qt.UserRole, ('y_axis', data_idx))
                    if self.ctx.read_only:
                        y_val_item.setFlags(y_val_item.flags() & ~Qt.ItemIsEditable)
                    self.ctx.table_widget.setItem(row + 1, 0, y_val_item)
            else:
                for row in range(rows):
                    # Col 0: Row index (gray, no axis data)
                    y_val_item = QTableWidgetItem(str(row))
                    y_val_item.setFlags(y_val_item.flags() & ~Qt.ItemIsEditable)
                    y_val_item.setBackground(label_bg)
                    self.ctx.table_widget.setItem(row + 1, 0, y_val_item)

            # === Data cells (rows 1+, cols 1+) ===
            for row in range(rows):
                for col in range(cols):
                    value_item = QTableWidgetItem(self.format_value(display_values[row, col], value_fmt))
                    color = self.get_cell_color(display_values[row, col], values, row, col)
                    value_item.setBackground(QBrush(color))
                    data_row = (rows - 1 - row) if flipy else row
                    data_col = (cols - 1 - col) if flipx else col
                    value_item.setData(Qt.UserRole, (data_row, data_col))
                    if self.ctx.read_only:
                        value_item.setFlags(value_item.flags() & ~Qt.ItemIsEditable)
                    self.ctx.table_widget.setItem(row + 1, col + 1, value_item)
        finally:
            self.ctx.editing_in_progress = False

        # Apply uniform column width to data columns
        self._apply_uniform_column_width_3d()

    def _apply_uniform_column_width_3d(self):
        """
        Apply uniform width to DATA columns only (excluding Y-axis values col 0).
        For 3D tables with ECUFlash layout:
        - Col 0: Y-axis values (resize to contents)
        - Col 1+: Data columns (uniform width)
        """
        # First, let Qt calculate optimal widths for all columns
        self.ctx.table_widget.resizeColumnsToContents()

        # Find the maximum width among DATA columns only (skip column 0)
        max_width = 0
        for col in range(1, self.ctx.table_widget.columnCount()):
            width = self.ctx.table_widget.columnWidth(col)
            if width > max_width:
                max_width = width

        # Apply the maximum width to DATA columns only
        if max_width > 0:
            header = self.ctx.table_widget.horizontalHeader()
            # Set column 0 (Y-axis values) to ResizeToContents
            header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
            # Set data columns (1+) to Fixed with uniform width
            for col in range(1, self.ctx.table_widget.columnCount()):
                header.setSectionResizeMode(col, QHeaderView.Fixed)
                self.ctx.table_widget.setColumnWidth(col, max_width)

            logger.debug(f"Applied uniform width {max_width}px to {self.ctx.table_widget.columnCount() - 1} data columns")

    def _get_axis_label(self, table: Table, axis_type: AxisType) -> str:
        """
        Get axis label with unit, e.g., 'Engine Speed (RPM)'
        """
        axis_table = table.get_axis(axis_type)
        if not axis_table:
            return "X" if axis_type == AxisType.X_AXIS else "Y"

        name = axis_table.name
        unit = ""

        # Get unit from scaling if available
        if self.ctx.rom_definition and axis_table.scaling:
            scaling = self.ctx.rom_definition.get_scaling(axis_table.scaling)
            if scaling and scaling.units:
                unit = scaling.units

        if unit:
            return f"{name} ({unit})"
        return name

    def _printf_to_python_format(self, printf_format: str) -> str:
        """Convert printf-style format to Python format spec."""
        if not printf_format:
            return ".2f"

        match = re.match(r'%[-+0 #]*(\d*)\.?(\d*)([diouxXeEfFgGaAcspn%])', printf_format)
        if not match:
            return ".2f"

        width = match.group(1)
        precision = match.group(2)
        specifier = match.group(3)

        result = ""
        if width:
            result += width
        if precision:
            result += f".{precision}"
        result += specifier

        return result

    def get_value_format(self) -> str:
        """Get the Python format spec for the current table's values."""
        if not self.ctx.current_table or not self.ctx.rom_definition:
            return ".2f"

        scaling_name = self.ctx.current_table.scaling
        if not scaling_name:
            return ".2f"

        scaling = self.ctx.rom_definition.get_scaling(scaling_name)
        if not scaling or not scaling.format:
            return ".2f"

        return self._printf_to_python_format(scaling.format)

    def _get_axis_format(self, axis_type: AxisType) -> str:
        """Get the Python format spec for an axis."""
        if not self.ctx.current_table or not self.ctx.rom_definition:
            return ".2f"

        axis_table = self.ctx.current_table.get_axis(axis_type)
        if not axis_table or not axis_table.scaling:
            return ".2f"

        scaling = self.ctx.rom_definition.get_scaling(axis_table.scaling)
        if not scaling or not scaling.format:
            return ".2f"

        return self._printf_to_python_format(scaling.format)

    def format_value(self, value: float, format_spec: str) -> str:
        """Format a value using the given format spec with error handling."""
        try:
            return f"{value:{format_spec}}"
        except (ValueError, TypeError):
            return f"{value:.2f}"

    def ratio_to_color(self, ratio: float) -> QColor:
        """
        Convert 0-1 ratio to thermal/rainbow gradient (blue -> cyan -> green -> yellow -> red)
        """
        ratio = max(0.0, min(1.0, ratio))

        if ratio <= 0.25:
            t = ratio / 0.25
            r, g, b = 0, int(t * 255), 255
        elif ratio <= 0.5:
            t = (ratio - 0.25) / 0.25
            r, g, b = 0, 255, int(255 * (1 - t))
        elif ratio <= 0.75:
            t = (ratio - 0.5) / 0.25
            r, g, b = int(t * 255), 255, 0
        else:
            t = (ratio - 0.75) / 0.25
            r, g, b = 255, int(255 * (1 - t)), 0

        return QColor(r, g, b)

    def get_cell_color(self, value: float, values: np.ndarray,
                       row: int, col: int) -> QColor:
        """Calculate cell background color based on gradient mode"""
        mode = get_settings().get_gradient_mode()

        if mode == "neighbors":
            ratio = self._get_neighbor_ratio(value, values, row, col)
        else:
            min_val = np.min(values)
            max_val = np.max(values)

            if max_val == min_val:
                ratio = 0.5
            else:
                ratio = (value - min_val) / (max_val - min_val)

        return self.ratio_to_color(ratio)

    def get_axis_format(self, axis_type: AxisType) -> str:
        """Public method to get axis format (used by editing helper)"""
        return self._get_axis_format(axis_type)

    def get_axis_color(self, value: float, axis_values: np.ndarray) -> QColor:
        """Calculate axis cell color based on value within axis range"""
        if axis_values is None or len(axis_values) == 0:
            return QColor(240, 240, 240)

        min_val = np.min(axis_values)
        max_val = np.max(axis_values)

        if max_val == min_val:
            ratio = 0.5
        else:
            ratio = (value - min_val) / (max_val - min_val)

        return self.ratio_to_color(ratio)

    def _get_neighbor_ratio(self, value: float, values: np.ndarray,
                            row: int, col: int) -> float:
        """Calculate ratio relative to neighboring cells"""
        if values.ndim == 1:
            neighbors = []
            if row > 0:
                neighbors.append(values[row - 1])
            if row < len(values) - 1:
                neighbors.append(values[row + 1])
        else:
            rows, cols = values.shape
            neighbors = []
            for dr in [-1, 0, 1]:
                for dc in [-1, 0, 1]:
                    if dr == 0 and dc == 0:
                        continue
                    nr, nc = row + dr, col + dc
                    if 0 <= nr < rows and 0 <= nc < cols:
                        neighbors.append(values[nr, nc])

        if not neighbors:
            return 0.5

        neighbor_avg = np.mean(neighbors)
        neighbor_range = max(neighbors) - min(neighbors) if len(neighbors) > 1 else 1.0

        if neighbor_range == 0:
            neighbor_range = abs(neighbor_avg) * 0.1 if neighbor_avg != 0 else 1.0

        diff = value - neighbor_avg
        ratio = 0.5 + (diff / (neighbor_range * 2))

        return max(0.0, min(1.0, ratio))
