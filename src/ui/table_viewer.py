"""
Table Viewer Widget

Displays table data in a grid view with gradient coloring and axis labels.
"""

from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QBrush

from ..core.rom_definition import Table, TableType, RomDefinition, AxisType
from ..utils.settings import get_settings
import numpy as np
import re


class TableViewer(QWidget):
    """Widget for viewing table data with gradient coloring"""

    def __init__(self, rom_definition: RomDefinition = None, parent=None):
        super().__init__(parent)
        self.rom_definition = rom_definition
        self.current_table = None
        self.current_data = None
        self.init_ui()

    def init_ui(self):
        """Initialize the user interface"""
        layout = QVBoxLayout()
        layout.setContentsMargins(2, 2, 2, 2)
        self.setLayout(layout)

        # Table info label
        self.info_label = QLabel("Select a table to view")
        layout.addWidget(self.info_label)

        # Table widget for displaying data
        self.table_widget = QTableWidget()
        self.table_widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table_widget.verticalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table_widget.verticalHeader().setVisible(False)  # Hide row numbers
        self.table_widget.setShowGrid(True)
        self.table_widget.setGridStyle(Qt.SolidLine)

        # Apply compact styling from settings
        self._apply_table_style()

        layout.addWidget(self.table_widget)

    def _apply_table_style(self):
        """Apply table styling based on settings"""
        font_size = get_settings().get_table_font_size()

        self.table_widget.setStyleSheet(f"""
            QTableWidget {{
                font-size: {font_size}px;
                gridline-color: #c0c0c0;
            }}
            QTableWidget::item {{
                padding: 0px 2px;
            }}
            QHeaderView::section {{
                font-size: {font_size}px;
                padding: 1px;
            }}
        """)

        # Set row height based on font size
        row_height = font_size + 6
        self.table_widget.verticalHeader().setDefaultSectionSize(row_height)

    def display_table(self, table: Table, data: dict):
        """
        Display table data

        Args:
            table: Table definition
            data: Dictionary with 'values', 'x_axis', 'y_axis' from RomReader
        """
        self.current_table = table
        self.current_data = data

        # Update info label
        info_text = (
            f"{table.name} | "
            f"Type: {table.type.value} | "
            f"Category: {table.category} | "
            f"Address: 0x{table.address}"
        )
        self.info_label.setText(info_text)

        values = data['values']

        if table.type == TableType.ONE_D:
            self._display_1d(values)
        elif table.type == TableType.TWO_D:
            self._display_2d(values, data.get('y_axis'))
        elif table.type == TableType.THREE_D:
            self._display_3d(values, data.get('x_axis'), data.get('y_axis'))

    def _display_1d(self, values: np.ndarray):
        """Display 1D table (single value)"""
        self.table_widget.setRowCount(1)
        self.table_widget.setColumnCount(1)
        self.table_widget.setHorizontalHeaderLabels(["Value"])
        self.table_widget.setVerticalHeaderLabels([""])

        value_fmt = self._get_value_format()
        item = QTableWidgetItem(self._format_value(values[0], value_fmt))
        color = self._get_cell_color(values[0], values, 0, 0)
        item.setBackground(QBrush(color))
        self.table_widget.setItem(0, 0, item)

    def _display_2d(self, values: np.ndarray, y_axis: np.ndarray):
        """Display 2D table (1D array with Y axis)"""
        num_values = len(values)
        self.table_widget.setRowCount(num_values)
        self.table_widget.setColumnCount(2)

        # Get axis label with unit
        y_label = self._get_axis_label(self.current_table, AxisType.Y_AXIS)
        self.table_widget.setHorizontalHeaderLabels([y_label, "Value"])

        # Get format specs
        y_fmt = self._get_axis_format(AxisType.Y_AXIS)
        value_fmt = self._get_value_format()

        # Apply flip if needed
        flipy = self.current_table.flipy if self.current_table else False
        display_values = values[::-1] if flipy else values
        display_y_axis = y_axis[::-1] if (y_axis is not None and flipy) else y_axis

        for i in range(num_values):
            # Y axis value
            if display_y_axis is not None and i < len(display_y_axis):
                y_item = QTableWidgetItem(self._format_value(display_y_axis[i], y_fmt))
            else:
                y_item = QTableWidgetItem(str(i))
            y_item.setFlags(y_item.flags() & ~Qt.ItemIsEditable)  # Read-only
            self.table_widget.setItem(i, 0, y_item)

            # Data value with gradient color
            value_item = QTableWidgetItem(self._format_value(display_values[i], value_fmt))
            color = self._get_cell_color(display_values[i], values, i, 0)
            value_item.setBackground(QBrush(color))
            self.table_widget.setItem(i, 1, value_item)

    def _display_3d(self, values: np.ndarray, x_axis: np.ndarray, y_axis: np.ndarray):
        """Display 3D table (2D grid with X and Y axes)"""
        if values.ndim != 2:
            self._display_1d(values.flatten())
            return

        rows, cols = values.shape

        # Set up table dimensions (+1 for axis column)
        self.table_widget.setRowCount(rows)
        self.table_widget.setColumnCount(cols + 1)

        # Get axis labels with units
        x_label = self._get_axis_label(self.current_table, AxisType.X_AXIS)
        y_label = self._get_axis_label(self.current_table, AxisType.Y_AXIS)

        # Get format specs
        x_fmt = self._get_axis_format(AxisType.X_AXIS)
        y_fmt = self._get_axis_format(AxisType.Y_AXIS)
        value_fmt = self._get_value_format()

        # Apply flip flags if needed
        flipx = self.current_table.flipx if self.current_table else False
        flipy = self.current_table.flipy if self.current_table else False

        # Flip axes and values as needed
        display_x_axis = x_axis[::-1] if (x_axis is not None and flipx) else x_axis
        display_y_axis = y_axis[::-1] if (y_axis is not None and flipy) else y_axis
        display_values = values.copy()
        if flipy:
            display_values = display_values[::-1, :]
        if flipx:
            display_values = display_values[:, ::-1]

        # Set column headers (X axis values)
        headers = [y_label]  # Top-left cell shows Y axis label
        if display_x_axis is not None and len(display_x_axis) == cols:
            headers.extend([self._format_value(x, x_fmt) for x in display_x_axis])
        else:
            headers.extend([str(i) for i in range(cols)])
        self.table_widget.setHorizontalHeaderLabels(headers)

        # Fill table
        for row in range(rows):
            # Y axis value in first column
            if display_y_axis is not None and row < len(display_y_axis):
                y_item = QTableWidgetItem(self._format_value(display_y_axis[row], y_fmt))
            else:
                y_item = QTableWidgetItem(str(row))
            y_item.setFlags(y_item.flags() & ~Qt.ItemIsEditable)  # Make read-only
            self.table_widget.setItem(row, 0, y_item)

            # Data values with gradient coloring
            for col in range(cols):
                value_item = QTableWidgetItem(self._format_value(display_values[row, col], value_fmt))
                color = self._get_cell_color(display_values[row, col], values, row, col)
                value_item.setBackground(QBrush(color))
                self.table_widget.setItem(row, col + 1, value_item)

    def clear(self):
        """Clear the viewer"""
        self.current_table = None
        self.current_data = None
        self.info_label.setText("Select a table to view")
        self.table_widget.setRowCount(0)
        self.table_widget.setColumnCount(0)

    def _get_axis_label(self, table: Table, axis_type: AxisType) -> str:
        """
        Get axis label with unit, e.g., 'Engine Speed (RPM)'

        Args:
            table: Parent table
            axis_type: AxisType.X_AXIS or AxisType.Y_AXIS

        Returns:
            str: Axis label with unit if available
        """
        axis_table = table.get_axis(axis_type)
        if not axis_table:
            return "X" if axis_type == AxisType.X_AXIS else "Y"

        name = axis_table.name
        unit = ""

        # Get unit from scaling if available
        if self.rom_definition and axis_table.scaling:
            scaling = self.rom_definition.get_scaling(axis_table.scaling)
            if scaling and scaling.units:
                unit = scaling.units

        if unit:
            return f"{name} ({unit})"
        return name

    def _printf_to_python_format(self, printf_format: str) -> str:
        """
        Convert printf-style format to Python format spec.

        Args:
            printf_format: Printf format string like "%0.3f", "%.2f", "%d"

        Returns:
            str: Python format spec like ".3f", ".2f", "d"
        """
        if not printf_format:
            return ".2f"  # Default fallback

        # Match printf pattern: %[flags][width][.precision][length]specifier
        match = re.match(r'%[-+0 #]*(\d*)\.?(\d*)([diouxXeEfFgGaAcspn%])', printf_format)
        if not match:
            return ".2f"  # Default fallback

        width = match.group(1)
        precision = match.group(2)
        specifier = match.group(3)

        # Build Python format spec
        result = ""
        if width:
            result += width
        if precision:
            result += f".{precision}"
        result += specifier

        return result

    def _get_value_format(self) -> str:
        """
        Get the Python format spec for the current table's values.

        Returns:
            str: Python format spec (e.g., ".3f")
        """
        if not self.current_table or not self.rom_definition:
            return ".2f"

        scaling_name = self.current_table.scaling
        if not scaling_name:
            return ".2f"

        scaling = self.rom_definition.get_scaling(scaling_name)
        if not scaling or not scaling.format:
            return ".2f"

        return self._printf_to_python_format(scaling.format)

    def _get_axis_format(self, axis_type: AxisType) -> str:
        """
        Get the Python format spec for an axis.

        Args:
            axis_type: AxisType.X_AXIS or AxisType.Y_AXIS

        Returns:
            str: Python format spec (e.g., ".2f")
        """
        if not self.current_table or not self.rom_definition:
            return ".2f"

        axis_table = self.current_table.get_axis(axis_type)
        if not axis_table or not axis_table.scaling:
            return ".2f"

        scaling = self.rom_definition.get_scaling(axis_table.scaling)
        if not scaling or not scaling.format:
            return ".2f"

        return self._printf_to_python_format(scaling.format)

    def _format_value(self, value: float, format_spec: str) -> str:
        """
        Format a value using the given format spec with error handling.

        Args:
            value: The value to format
            format_spec: Python format spec (e.g., ".3f")

        Returns:
            str: Formatted value string
        """
        try:
            return f"{value:{format_spec}}"
        except (ValueError, TypeError):
            return f"{value:.2f}"

    def _ratio_to_color(self, ratio: float) -> QColor:
        """
        Convert 0-1 ratio to thermal/rainbow gradient (blue → cyan → green → yellow → red)
        Similar to ECUFlash's table coloring.

        Args:
            ratio: Value between 0 and 1

        Returns:
            QColor: Gradient color
        """
        # Clamp ratio to valid range
        ratio = max(0.0, min(1.0, ratio))

        # 5-stop gradient: blue → cyan → green → yellow → red
        if ratio <= 0.25:
            # Blue to Cyan
            t = ratio / 0.25
            r = 0
            g = int(t * 255)
            b = 255
        elif ratio <= 0.5:
            # Cyan to Green
            t = (ratio - 0.25) / 0.25
            r = 0
            g = 255
            b = int(255 * (1 - t))
        elif ratio <= 0.75:
            # Green to Yellow
            t = (ratio - 0.5) / 0.25
            r = int(t * 255)
            g = 255
            b = 0
        else:
            # Yellow to Red
            t = (ratio - 0.75) / 0.25
            r = 255
            g = int(255 * (1 - t))
            b = 0

        return QColor(r, g, b)

    def _get_cell_color(self, value: float, values: np.ndarray,
                        row: int, col: int) -> QColor:
        """
        Calculate cell background color based on gradient mode

        Args:
            value: Current cell value
            values: All values in the table (2D array for 3D tables)
            row: Row index
            col: Column index

        Returns:
            QColor: Background color for the cell
        """
        mode = get_settings().get_gradient_mode()

        if mode == "neighbors":
            # Calculate relative to neighboring cells
            ratio = self._get_neighbor_ratio(value, values, row, col)
        else:
            # Default: min/max mode
            min_val = np.min(values)
            max_val = np.max(values)

            if max_val == min_val:
                ratio = 0.5  # All values are the same
            else:
                ratio = (value - min_val) / (max_val - min_val)

        return self._ratio_to_color(ratio)

    def _get_neighbor_ratio(self, value: float, values: np.ndarray,
                            row: int, col: int) -> float:
        """
        Calculate ratio relative to neighboring cells

        Args:
            value: Current cell value
            values: All values in the table
            row: Row index
            col: Column index

        Returns:
            float: Ratio between 0 and 1
        """
        if values.ndim == 1:
            # 1D/2D table - use adjacent values
            neighbors = []
            if row > 0:
                neighbors.append(values[row - 1])
            if row < len(values) - 1:
                neighbors.append(values[row + 1])
        else:
            # 3D table - use surrounding 8 cells
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

        # Calculate how different this value is from neighbors
        diff = value - neighbor_avg
        ratio = 0.5 + (diff / (neighbor_range * 2))

        return max(0.0, min(1.0, ratio))
