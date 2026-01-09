"""
Table Viewer Widget

Displays table data in a grid view with gradient coloring and axis labels.
Supports cell editing with change tracking.
"""

from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QSizePolicy,
    QMessageBox,
    QApplication,
    QDialog
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QBrush, QKeySequence, QShortcut

from ..core.rom_definition import Table, TableType, RomDefinition, AxisType
from ..utils.settings import get_settings
import numpy as np
import re
import logging

logger = logging.getLogger(__name__)


class TableViewer(QWidget):
    """Widget for viewing and editing table data with gradient coloring"""

    # Signal emitted when a cell value changes
    # Args: table_name, row, col, old_value, new_value, old_raw, new_raw
    cell_changed = Signal(str, int, int, float, float, float, float)

    # Signal emitted when bulk operation completes (for single undo)
    # Args: list of (row, col, old_value, new_value, old_raw, new_raw) tuples
    bulk_changes = Signal(list)

    def __init__(self, rom_definition: RomDefinition = None, parent=None):
        super().__init__(parent)
        self.rom_definition = rom_definition
        self.current_table = None
        self.current_data = None
        self._editing_in_progress = False
        self._read_only = False
        self.init_ui()

    def init_ui(self):
        """Initialize the user interface"""
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.setLayout(layout)

        # Table info label - compact, allow truncation
        self.info_label = QLabel("Select a table to view")
        self.info_label.setStyleSheet("font-size: 9px; padding: 1px 2px; background: #f0f0f0;")
        self.info_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
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

        # Connect to cell changed signal for editing
        self.table_widget.cellChanged.connect(self._on_cell_changed)

        # Set up copy/paste shortcuts
        copy_shortcut = QShortcut(QKeySequence.Copy, self.table_widget)
        copy_shortcut.activated.connect(self.copy_selection)
        paste_shortcut = QShortcut(QKeySequence.Paste, self.table_widget)
        paste_shortcut.activated.connect(self.paste_selection)

        # Set up increment/decrement shortcuts
        increment_shortcut = QShortcut(QKeySequence("+"), self.table_widget)
        increment_shortcut.activated.connect(self.increment_selection)
        decrement_shortcut = QShortcut(QKeySequence("-"), self.table_widget)
        decrement_shortcut.activated.connect(self.decrement_selection)

        # Set up select all shortcut
        select_all_shortcut = QShortcut(QKeySequence.SelectAll, self.table_widget)
        select_all_shortcut.activated.connect(self.select_all_data)

        # Set up interpolation shortcuts
        v_shortcut = QShortcut(QKeySequence("V"), self.table_widget)
        v_shortcut.activated.connect(self.interpolate_vertical)
        h_shortcut = QShortcut(QKeySequence("H"), self.table_widget)
        h_shortcut.activated.connect(self.interpolate_horizontal)
        b_shortcut = QShortcut(QKeySequence("B"), self.table_widget)
        b_shortcut.activated.connect(self.interpolate_2d)

        layout.addWidget(self.table_widget)

    def set_read_only(self, read_only: bool):
        """Set whether the table is read-only"""
        self._read_only = read_only

    def _apply_table_style(self):
        """Apply table styling based on settings - compact like ECUFlash"""
        font_size = get_settings().get_table_font_size()

        self.table_widget.setStyleSheet(f"""
            QTableWidget {{
                font-size: {font_size}px;
                gridline-color: #a0a0a0;
            }}
            QTableWidget::item {{
                padding: 0px 1px;
            }}
        """)

        # Tight row height - just enough for the font
        row_height = font_size + 2
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
        self._editing_in_progress = True  # Prevent cellChanged during setup
        try:
            self.table_widget.horizontalHeader().setVisible(True)
            self.table_widget.setRowCount(1)
            self.table_widget.setColumnCount(1)
            self.table_widget.setHorizontalHeaderLabels(["Value"])
            self.table_widget.setVerticalHeaderLabels([""])

            value_fmt = self._get_value_format()
            item = QTableWidgetItem(self._format_value(values[0], value_fmt))
            color = self._get_cell_color(values[0], values, 0, 0)
            item.setBackground(QBrush(color))
            # Store data row/col for change tracking (data_row, data_col)
            item.setData(Qt.UserRole, (0, 0))
            if self._read_only:
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self.table_widget.setItem(0, 0, item)
        finally:
            self._editing_in_progress = False

    def _display_2d(self, values: np.ndarray, y_axis: np.ndarray):
        """Display 2D table (1D array with Y axis)"""
        self._editing_in_progress = True  # Prevent cellChanged during setup
        try:
            num_values = len(values)
            self.table_widget.horizontalHeader().setVisible(True)
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

            # Calculate Y axis gradient range
            if display_y_axis is not None and len(display_y_axis) > 0:
                y_min, y_max = np.min(display_y_axis), np.max(display_y_axis)
            else:
                y_min, y_max = 0, num_values - 1

            for i in range(num_values):
                # Y axis value with gradient
                if display_y_axis is not None and i < len(display_y_axis):
                    y_item = QTableWidgetItem(self._format_value(display_y_axis[i], y_fmt))
                    # Apply gradient based on Y axis values
                    if y_max != y_min:
                        ratio = (display_y_axis[i] - y_min) / (y_max - y_min)
                    else:
                        ratio = 0.5
                    y_item.setBackground(QBrush(self._ratio_to_color(ratio)))
                else:
                    y_item = QTableWidgetItem(str(i))
                    y_item.setBackground(QBrush(QColor(240, 240, 240)))
                y_item.setFlags(y_item.flags() & ~Qt.ItemIsEditable)  # Axis always read-only
                self.table_widget.setItem(i, 0, y_item)

                # Data value with gradient color
                value_item = QTableWidgetItem(self._format_value(display_values[i], value_fmt))
                color = self._get_cell_color(display_values[i], values, i, 0)
                value_item.setBackground(QBrush(color))
                # Store the actual data index (accounting for flip)
                data_row = (num_values - 1 - i) if flipy else i
                value_item.setData(Qt.UserRole, (data_row, 0))
                if self._read_only:
                    value_item.setFlags(value_item.flags() & ~Qt.ItemIsEditable)
                self.table_widget.setItem(i, 1, value_item)
        finally:
            self._editing_in_progress = False

    def _display_3d(self, values: np.ndarray, x_axis: np.ndarray, y_axis: np.ndarray):
        """Display 3D table (2D grid with X and Y axes)"""
        if values.ndim != 2:
            self._display_1d(values.flatten())
            return

        self._editing_in_progress = True  # Prevent cellChanged during setup
        try:
            rows, cols = values.shape

            # Set up table dimensions (+1 for axis row and column)
            # Row 0: X axis values, Col 0: Y axis values
            self.table_widget.setRowCount(rows + 1)
            self.table_widget.setColumnCount(cols + 1)

            # Hide headers since we use cells for axes
            self.table_widget.horizontalHeader().setVisible(False)

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

            # Top-left corner cell (axis labels)
            corner_item = QTableWidgetItem(f"{y_label}\\{x_label}")
            corner_item.setFlags(corner_item.flags() & ~Qt.ItemIsEditable)
            corner_item.setBackground(QBrush(QColor(240, 240, 240)))
            self.table_widget.setItem(0, 0, corner_item)

            # Row 0: X axis values with gradient
            if display_x_axis is not None and len(display_x_axis) == cols:
                x_min, x_max = np.min(display_x_axis), np.max(display_x_axis)
                for col in range(cols):
                    x_item = QTableWidgetItem(self._format_value(display_x_axis[col], x_fmt))
                    x_item.setFlags(x_item.flags() & ~Qt.ItemIsEditable)
                    # Apply gradient based on X axis values
                    if x_max != x_min:
                        ratio = (display_x_axis[col] - x_min) / (x_max - x_min)
                    else:
                        ratio = 0.5
                    x_item.setBackground(QBrush(self._ratio_to_color(ratio)))
                    self.table_widget.setItem(0, col + 1, x_item)
            else:
                for col in range(cols):
                    x_item = QTableWidgetItem(str(col))
                    x_item.setFlags(x_item.flags() & ~Qt.ItemIsEditable)
                    x_item.setBackground(QBrush(QColor(240, 240, 240)))
                    self.table_widget.setItem(0, col + 1, x_item)

            # Column 0: Y axis values with gradient (starting at row 1)
            if display_y_axis is not None and len(display_y_axis) == rows:
                y_min, y_max = np.min(display_y_axis), np.max(display_y_axis)
                for row in range(rows):
                    y_item = QTableWidgetItem(self._format_value(display_y_axis[row], y_fmt))
                    y_item.setFlags(y_item.flags() & ~Qt.ItemIsEditable)
                    # Apply gradient based on Y axis values
                    if y_max != y_min:
                        ratio = (display_y_axis[row] - y_min) / (y_max - y_min)
                    else:
                        ratio = 0.5
                    y_item.setBackground(QBrush(self._ratio_to_color(ratio)))
                    self.table_widget.setItem(row + 1, 0, y_item)
            else:
                for row in range(rows):
                    y_item = QTableWidgetItem(str(row))
                    y_item.setFlags(y_item.flags() & ~Qt.ItemIsEditable)
                    y_item.setBackground(QBrush(QColor(240, 240, 240)))
                    self.table_widget.setItem(row + 1, 0, y_item)

            # Fill data values (starting at row 1, col 1)
            for row in range(rows):
                for col in range(cols):
                    value_item = QTableWidgetItem(self._format_value(display_values[row, col], value_fmt))
                    color = self._get_cell_color(display_values[row, col], values, row, col)
                    value_item.setBackground(QBrush(color))
                    # Store the actual data indices (accounting for flip)
                    data_row = (rows - 1 - row) if flipy else row
                    data_col = (cols - 1 - col) if flipx else col
                    value_item.setData(Qt.UserRole, (data_row, data_col))
                    if self._read_only:
                        value_item.setFlags(value_item.flags() & ~Qt.ItemIsEditable)
                    self.table_widget.setItem(row + 1, col + 1, value_item)
        finally:
            self._editing_in_progress = False

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

    def _on_cell_changed(self, row: int, col: int):
        """Handle cell value change from user edit"""
        if self._editing_in_progress or self._read_only:
            return

        if not self.current_table or not self.current_data:
            return

        item = self.table_widget.item(row, col)
        if not item:
            return

        # Get the actual data indices from the item
        data_indices = item.data(Qt.UserRole)
        if data_indices is None:
            # This is an axis cell, not a data cell
            return

        data_row, data_col = data_indices

        # Get the new text value
        new_text = item.text().strip()

        # Try to parse the new value
        try:
            new_value = float(new_text)
        except ValueError:
            # Invalid input - revert to old value
            self._revert_cell(row, col, data_row, data_col)
            return

        # Get the old value from current_data
        values = self.current_data['values']
        if values.ndim == 1:
            old_value = float(values[data_row])
        else:
            old_value = float(values[data_row, data_col])

        # Skip if no change
        if abs(new_value - old_value) < 1e-10:
            return

        # Validate against scaling min/max if available
        if self.rom_definition and self.current_table.scaling:
            scaling = self.rom_definition.get_scaling(self.current_table.scaling)
            if scaling:
                # Check min/max bounds
                if scaling.min is not None and new_value < scaling.min:
                    logger.warning(f"Value {new_value} below minimum {scaling.min}")
                    self._revert_cell(row, col, data_row, data_col)
                    return
                if scaling.max is not None and new_value > scaling.max:
                    logger.warning(f"Value {new_value} above maximum {scaling.max}")
                    self._revert_cell(row, col, data_row, data_col)
                    return

        # Convert display values to raw values
        old_raw = self._display_to_raw(old_value)
        new_raw = self._display_to_raw(new_value)

        if old_raw is None or new_raw is None:
            self._revert_cell(row, col, data_row, data_col)
            return

        # Update the internal data
        if values.ndim == 1:
            self.current_data['values'][data_row] = new_value
        else:
            self.current_data['values'][data_row, data_col] = new_value

        # Update cell display with proper formatting
        self._editing_in_progress = True
        try:
            value_fmt = self._get_value_format()
            item.setText(self._format_value(new_value, value_fmt))

            # Update cell color based on new value
            color = self._get_cell_color(new_value, self.current_data['values'], data_row, data_col)
            item.setBackground(QBrush(color))
        finally:
            self._editing_in_progress = False

        # Emit the change signal
        self.cell_changed.emit(
            self.current_table.name,
            data_row, data_col,
            old_value, new_value,
            old_raw, new_raw
        )

        logger.debug(f"Cell changed: {self.current_table.name}[{data_row},{data_col}] {old_value} -> {new_value}")

    def _revert_cell(self, row: int, col: int, data_row: int, data_col: int):
        """Revert cell to its original value"""
        values = self.current_data['values']
        if values.ndim == 1:
            old_value = values[data_row]
        else:
            old_value = values[data_row, data_col]

        self._editing_in_progress = True
        try:
            value_fmt = self._get_value_format()
            item = self.table_widget.item(row, col)
            if item:
                item.setText(self._format_value(old_value, value_fmt))
        finally:
            self._editing_in_progress = False

    def _display_to_raw(self, display_value: float) -> float:
        """Convert display value to raw binary value using scaling"""
        if not self.rom_definition or not self.current_table:
            return display_value

        scaling = self.rom_definition.get_scaling(self.current_table.scaling)
        if not scaling:
            return display_value

        try:
            from simpleeval import simple_eval
            return simple_eval(scaling.frexpr, names={'x': display_value})
        except Exception as e:
            logger.error(f"Error converting to raw: {e}")
            return None

    def update_cell_value(self, data_row: int, data_col: int, new_value: float):
        """
        Update a cell's value programmatically (for undo/redo)

        Args:
            data_row: Data row index
            data_col: Data column index
            new_value: New display value
        """
        if not self.current_table or not self.current_data:
            return

        # Update internal data
        values = self.current_data['values']
        if values.ndim == 1:
            values[data_row] = new_value
        else:
            values[data_row, data_col] = new_value

        # Find the UI cell that corresponds to this data cell
        ui_row, ui_col = self._data_to_ui_coords(data_row, data_col)
        if ui_row is None:
            return

        # Update UI
        self._editing_in_progress = True
        try:
            item = self.table_widget.item(ui_row, ui_col)
            if item:
                value_fmt = self._get_value_format()
                item.setText(self._format_value(new_value, value_fmt))
                color = self._get_cell_color(new_value, values, data_row, data_col)
                item.setBackground(QBrush(color))
        finally:
            self._editing_in_progress = False

    def _data_to_ui_coords(self, data_row: int, data_col: int) -> tuple:
        """Convert data coordinates to UI table coordinates"""
        if not self.current_table:
            return None, None

        table_type = self.current_table.type
        flipx = self.current_table.flipx if self.current_table else False
        flipy = self.current_table.flipy if self.current_table else False

        if table_type == TableType.ONE_D:
            return 0, 0
        elif table_type == TableType.TWO_D:
            values = self.current_data['values']
            num_values = len(values)
            ui_row = (num_values - 1 - data_row) if flipy else data_row
            return ui_row, 1  # Column 1 is the value column
        elif table_type == TableType.THREE_D:
            values = self.current_data['values']
            rows, cols = values.shape
            ui_row = (rows - 1 - data_row) if flipy else data_row
            ui_col = (cols - 1 - data_col) if flipx else data_col
            return ui_row + 1, ui_col + 1  # +1 for axis row/col

        return None, None

    def copy_selection(self):
        """Copy selected cells to clipboard as tab-separated values"""
        selected = self.table_widget.selectedRanges()
        if not selected:
            return

        # Get the bounding rectangle of selection
        min_row = min(r.topRow() for r in selected)
        max_row = max(r.bottomRow() for r in selected)
        min_col = min(r.leftColumn() for r in selected)
        max_col = max(r.rightColumn() for r in selected)

        # Build tab-separated string
        rows_text = []
        for row in range(min_row, max_row + 1):
            row_values = []
            for col in range(min_col, max_col + 1):
                item = self.table_widget.item(row, col)
                if item:
                    row_values.append(item.text())
                else:
                    row_values.append("")
            rows_text.append("\t".join(row_values))

        clipboard_text = "\n".join(rows_text)
        QApplication.clipboard().setText(clipboard_text)
        logger.debug(f"Copied {max_row - min_row + 1}x{max_col - min_col + 1} cells to clipboard")

    def paste_selection(self):
        """Paste clipboard content into selected cells"""
        if self._read_only:
            return

        clipboard = QApplication.clipboard()
        text = clipboard.text()
        if not text:
            return

        # Parse clipboard data (tab-separated rows)
        rows_data = []
        for line in text.strip().split("\n"):
            row_values = line.split("\t")
            rows_data.append(row_values)

        if not rows_data:
            return

        # Get current selection start
        selected = self.table_widget.selectedRanges()
        if not selected:
            return

        start_row = min(r.topRow() for r in selected)
        start_col = min(r.leftColumn() for r in selected)

        # Paste values
        changes_made = []
        for row_offset, row_values in enumerate(rows_data):
            for col_offset, value_text in enumerate(row_values):
                target_row = start_row + row_offset
                target_col = start_col + col_offset

                # Check bounds
                if target_row >= self.table_widget.rowCount():
                    continue
                if target_col >= self.table_widget.columnCount():
                    continue

                item = self.table_widget.item(target_row, target_col)
                if not item:
                    continue

                # Check if this is a data cell (not axis)
                data_indices = item.data(Qt.UserRole)
                if data_indices is None:
                    continue  # Skip axis cells

                # Try to parse value
                try:
                    new_value = float(value_text.strip())
                except ValueError:
                    continue  # Skip non-numeric values

                data_row, data_col = data_indices

                # Get old value
                values = self.current_data['values']
                if values.ndim == 1:
                    old_value = float(values[data_row])
                else:
                    old_value = float(values[data_row, data_col])

                # Skip if no change
                if abs(new_value - old_value) < 1e-10:
                    continue

                # Validate against scaling if available
                if self.rom_definition and self.current_table.scaling:
                    scaling = self.rom_definition.get_scaling(self.current_table.scaling)
                    if scaling:
                        if scaling.min is not None and new_value < scaling.min:
                            continue
                        if scaling.max is not None and new_value > scaling.max:
                            continue

                # Convert to raw values
                old_raw = self._display_to_raw(old_value)
                new_raw = self._display_to_raw(new_value)
                if old_raw is None or new_raw is None:
                    continue

                # Update the internal data
                if values.ndim == 1:
                    self.current_data['values'][data_row] = new_value
                else:
                    self.current_data['values'][data_row, data_col] = new_value

                # Update cell display
                self._editing_in_progress = True
                try:
                    value_fmt = self._get_value_format()
                    item.setText(self._format_value(new_value, value_fmt))
                    color = self._get_cell_color(new_value, self.current_data['values'], data_row, data_col)
                    item.setBackground(QBrush(color))
                finally:
                    self._editing_in_progress = False

                # Record change for signaling
                changes_made.append((data_row, data_col, old_value, new_value, old_raw, new_raw))

        # Emit signals for all changes
        for data_row, data_col, old_value, new_value, old_raw, new_raw in changes_made:
            self.cell_changed.emit(
                self.current_table.name,
                data_row, data_col,
                old_value, new_value,
                old_raw, new_raw
            )

        if changes_made:
            logger.debug(f"Pasted {len(changes_made)} cell(s)")

    def _apply_bulk_operation(self, operation_fn, operation_name: str):
        """
        Apply an operation to all selected data cells

        Args:
            operation_fn: Function(old_value) -> new_value
            operation_name: Description for logging

        Returns:
            List of (row, col, old_value, new_value, old_raw, new_raw) tuples
        """
        if self._read_only or not self.current_table or not self.current_data:
            return []

        # Get selected ranges
        selected = self.table_widget.selectedRanges()
        if not selected:
            logger.debug(f"{operation_name}: No selection")
            return []

        # Get bounding rectangle of selection
        min_row = min(r.topRow() for r in selected)
        max_row = max(r.bottomRow() for r in selected)
        min_col = min(r.leftColumn() for r in selected)
        max_col = max(r.rightColumn() for r in selected)

        changes_made = []

        # Iterate through selection
        for row in range(min_row, max_row + 1):
            for col in range(min_col, max_col + 1):
                item = self.table_widget.item(row, col)
                if not item:
                    continue

                # Check if this is a data cell (not axis)
                data_indices = item.data(Qt.UserRole)
                if data_indices is None:
                    continue  # Skip axis cells

                data_row, data_col = data_indices

                # Get current value
                values = self.current_data['values']
                if values.ndim == 1:
                    old_value = float(values[data_row])
                else:
                    old_value = float(values[data_row, data_col])

                # Apply operation
                try:
                    new_value = float(operation_fn(old_value))
                except Exception as e:
                    logger.warning(f"Operation failed for cell [{data_row},{data_col}]: {e}")
                    continue

                # Skip if no change
                if abs(new_value - old_value) < 1e-10:
                    continue

                # Validate against scaling if available
                if self.rom_definition and self.current_table.scaling:
                    scaling = self.rom_definition.get_scaling(self.current_table.scaling)
                    if scaling:
                        if scaling.min is not None and new_value < scaling.min:
                            logger.warning(f"Value {new_value} below minimum {scaling.min}, skipping")
                            continue
                        if scaling.max is not None and new_value > scaling.max:
                            logger.warning(f"Value {new_value} above maximum {scaling.max}, skipping")
                            continue

                # Convert to raw values
                old_raw = self._display_to_raw(old_value)
                new_raw = self._display_to_raw(new_value)
                if old_raw is None or new_raw is None:
                    continue

                # Update internal data
                if values.ndim == 1:
                    self.current_data['values'][data_row] = new_value
                else:
                    self.current_data['values'][data_row, data_col] = new_value

                # Update cell display
                self._editing_in_progress = True
                try:
                    value_fmt = self._get_value_format()
                    item.setText(self._format_value(new_value, value_fmt))
                    color = self._get_cell_color(new_value, self.current_data['values'], data_row, data_col)
                    item.setBackground(QBrush(color))
                finally:
                    self._editing_in_progress = False

                # Record change
                changes_made.append((data_row, data_col, old_value, new_value, old_raw, new_raw))

        logger.debug(f"{operation_name}: Modified {len(changes_made)} cell(s)")
        return changes_made

    def increment_selection(self):
        """Increment selected cells by fixed amount"""
        if not self.current_table:
            return

        # Get increment value (default: 1.0)
        # TODO: Read from ROM metadata when implemented
        increment = 1.0

        # Apply operation
        changes = self._apply_bulk_operation(
            lambda v: v + increment,
            f"Increment by {increment}"
        )

        # Emit bulk changes signal
        if changes:
            self.bulk_changes.emit(changes)

    def decrement_selection(self):
        """Decrement selected cells by fixed amount"""
        if not self.current_table:
            return

        # Get decrement value (default: 1.0)
        # TODO: Read from ROM metadata when implemented
        decrement = 1.0

        # Apply operation
        changes = self._apply_bulk_operation(
            lambda v: v - decrement,
            f"Decrement by {decrement}"
        )

        # Emit bulk changes signal
        if changes:
            self.bulk_changes.emit(changes)

    def add_to_selection(self):
        """Add custom value to selected cells (dialog)"""
        if not self.current_table:
            return

        from .data_operation_dialogs import AddValueDialog

        dialog = AddValueDialog(self)
        if dialog.exec() == QDialog.Accepted:
            value = dialog.get_value()

            # Apply operation
            changes = self._apply_bulk_operation(
                lambda v: v + value,
                f"Add {value}"
            )

            # Emit bulk changes signal
            if changes:
                self.bulk_changes.emit(changes)

    def multiply_selection(self):
        """Multiply selected cells by factor (dialog)"""
        if not self.current_table:
            return

        from .data_operation_dialogs import MultiplyDialog

        dialog = MultiplyDialog(self)
        if dialog.exec() == QDialog.Accepted:
            factor = dialog.get_factor()

            # Apply operation
            changes = self._apply_bulk_operation(
                lambda v: v * factor,
                f"Multiply by {factor}"
            )

            # Emit bulk changes signal
            if changes:
                self.bulk_changes.emit(changes)

    def set_value_selection(self):
        """Set all selected cells to value (dialog)"""
        if not self.current_table:
            return

        from .data_operation_dialogs import SetValueDialog

        # Count selected cells for preview
        selected = self.table_widget.selectedRanges()
        if not selected:
            return

        cell_count = sum(
            (r.bottomRow() - r.topRow() + 1) * (r.rightColumn() - r.leftColumn() + 1)
            for r in selected
        )

        dialog = SetValueDialog(cell_count, self)
        if dialog.exec() == QDialog.Accepted:
            value = dialog.get_value()

            # Apply operation
            changes = self._apply_bulk_operation(
                lambda v: value,
                f"Set to {value}"
            )

            # Emit bulk changes signal
            if changes:
                self.bulk_changes.emit(changes)

    def select_all_data(self):
        """Select all data cells (excluding axes)"""
        if not self.current_table:
            return

        from PySide6.QtWidgets import QTableWidgetSelectionRange

        table_type = self.current_table.type

        if table_type == TableType.ONE_D:
            # Single cell at (0, 0)
            selection = QTableWidgetSelectionRange(0, 0, 0, 0)
            self.table_widget.setRangeSelected(selection, True)
            self.table_widget.setCurrentCell(0, 0)

        elif table_type == TableType.TWO_D:
            # Select value column (skip Y axis in column 0)
            num_rows = self.table_widget.rowCount()
            if num_rows > 0:
                # Column 1 is the value column
                selection = QTableWidgetSelectionRange(0, 1, num_rows - 1, 1)
                self.table_widget.setRangeSelected(selection, True)
                self.table_widget.setCurrentCell(0, 1)

        elif table_type == TableType.THREE_D:
            # Select data region (skip X axis in row 0, Y axis in column 0)
            num_rows = self.table_widget.rowCount()
            num_cols = self.table_widget.columnCount()

            if num_rows > 1 and num_cols > 1:
                # Data starts at row 1, col 1
                selection = QTableWidgetSelectionRange(1, 1, num_rows - 1, num_cols - 1)
                self.table_widget.setRangeSelected(selection, True)
                self.table_widget.setCurrentCell(1, 1)

    def interpolate_vertical(self):
        """Fill gaps vertically with linear interpolation (V key)"""
        if not self.current_table or not self.current_data:
            return

        selected_ranges = self.table_widget.selectedRanges()
        if not selected_ranges:
            return

        from PySide6.QtWidgets import QTableWidgetSelectionRange

        all_changes = []

        for sel_range in selected_ranges:
            # For each column in the selection
            for col in range(sel_range.leftColumn(), sel_range.rightColumn() + 1):
                # Collect data cells in this column
                cells = []
                for row in range(sel_range.topRow(), sel_range.bottomRow() + 1):
                    item = self.table_widget.item(row, col)
                    if item and item.data(Qt.UserRole) is not None:
                        # This is a data cell
                        coords = item.data(Qt.UserRole)
                        try:
                            value = float(item.text())
                            cells.append((row, value, coords))
                        except ValueError:
                            continue

                # Need at least 2 cells to interpolate
                if len(cells) < 2:
                    continue

                # Get first and last values
                first_row, first_val, first_coords = cells[0]
                last_row, last_val, last_coords = cells[-1]

                # If only 2 cells, nothing to interpolate between them
                if len(cells) == 2 and last_row - first_row == 1:
                    continue

                # Calculate linear interpolation for rows between first and last
                if last_row == first_row:
                    continue  # Same row, can't interpolate vertically

                for i in range(1, len(cells) - 1):
                    row, old_val, coords = cells[i]
                    # Linear interpolation based on position
                    t = (row - first_row) / (last_row - first_row)
                    new_val = first_val + t * (last_val - first_val)

                    if abs(new_val - old_val) > 1e-9:  # Only if changed
                        # Update cell
                        item = self.table_widget.item(row, col)
                        old_raw = self.current_data['values'][coords]

                        # Convert to raw and back to ensure consistency
                        scaling = self.rom_definition.get_scaling(self.current_table.scaling)
                        if scaling:
                            from ..core.rom_reader import ScalingConverter
                            converter = ScalingConverter(scaling)
                            new_raw = converter.from_display(new_val)
                            new_val = converter.to_display(new_raw)

                            # Update display and data
                            item.setText(f"{new_val:.{self._get_precision()}f}")
                            self.current_data['values'][coords] = new_raw

                            all_changes.append((coords[0], coords[1] if len(coords) > 1 else 0,
                                              old_val, new_val, old_raw, new_raw))

        if all_changes:
            self.bulk_changes.emit(all_changes)

    def interpolate_horizontal(self):
        """Fill gaps horizontally with linear interpolation (H key)"""
        if not self.current_table or not self.current_data:
            return

        selected_ranges = self.table_widget.selectedRanges()
        if not selected_ranges:
            return

        from PySide6.QtWidgets import QTableWidgetSelectionRange

        all_changes = []

        for sel_range in selected_ranges:
            # For each row in the selection
            for row in range(sel_range.topRow(), sel_range.bottomRow() + 1):
                # Collect data cells in this row
                cells = []
                for col in range(sel_range.leftColumn(), sel_range.rightColumn() + 1):
                    item = self.table_widget.item(row, col)
                    if item and item.data(Qt.UserRole) is not None:
                        # This is a data cell
                        coords = item.data(Qt.UserRole)
                        try:
                            value = float(item.text())
                            cells.append((col, value, coords))
                        except ValueError:
                            continue

                # Need at least 2 cells to interpolate
                if len(cells) < 2:
                    continue

                # Get first and last values
                first_col, first_val, first_coords = cells[0]
                last_col, last_val, last_coords = cells[-1]

                # If only 2 cells, nothing to interpolate between them
                if len(cells) == 2 and last_col - first_col == 1:
                    continue

                # Calculate linear interpolation for columns between first and last
                if last_col == first_col:
                    continue  # Same column, can't interpolate horizontally

                for i in range(1, len(cells) - 1):
                    col, old_val, coords = cells[i]
                    # Linear interpolation based on position
                    t = (col - first_col) / (last_col - first_col)
                    new_val = first_val + t * (last_val - first_val)

                    if abs(new_val - old_val) > 1e-9:  # Only if changed
                        # Update cell
                        item = self.table_widget.item(row, col)
                        old_raw = self.current_data['values'][coords]

                        # Convert to raw and back to ensure consistency
                        scaling = self.rom_definition.get_scaling(self.current_table.scaling)
                        if scaling:
                            from ..core.rom_reader import ScalingConverter
                            converter = ScalingConverter(scaling)
                            new_raw = converter.from_display(new_val)
                            new_val = converter.to_display(new_raw)

                            # Update display and data
                            item.setText(f"{new_val:.{self._get_precision()}f}")
                            self.current_data['values'][coords] = new_raw

                            all_changes.append((coords[0], coords[1] if len(coords) > 1 else 0,
                                              old_val, new_val, old_raw, new_raw))

        if all_changes:
            self.bulk_changes.emit(all_changes)

    def interpolate_2d(self):
        """2D bilinear interpolation for 3D tables (B key)"""
        if not self.current_table or not self.current_data:
            return

        # Only works for 3D tables
        if self.current_table.type != TableType.THREE_D:
            QMessageBox.warning(
                self,
                "Invalid Operation",
                "2D interpolation only works on 3D tables"
            )
            return

        selected_ranges = self.table_widget.selectedRanges()
        if not selected_ranges:
            return

        from PySide6.QtWidgets import QTableWidgetSelectionRange

        all_changes = []

        for sel_range in selected_ranges:
            top_row = sel_range.topRow()
            bottom_row = sel_range.bottomRow()
            left_col = sel_range.leftColumn()
            right_col = sel_range.rightColumn()

            # Need at least 2x2 selection
            if (bottom_row - top_row < 1) or (right_col - left_col < 1):
                continue

            # Get corner values
            def get_corner_value(row, col):
                item = self.table_widget.item(row, col)
                if item and item.data(Qt.UserRole) is not None:
                    try:
                        return float(item.text())
                    except ValueError:
                        return None
                return None

            # Get all four corners
            v00 = get_corner_value(top_row, left_col)      # Top-left
            v10 = get_corner_value(top_row, right_col)     # Top-right
            v01 = get_corner_value(bottom_row, left_col)   # Bottom-left
            v11 = get_corner_value(bottom_row, right_col)  # Bottom-right

            # All corners must have values
            if None in (v00, v10, v01, v11):
                continue

            # Apply bilinear interpolation to all cells in the selection
            for row in range(top_row, bottom_row + 1):
                for col in range(left_col, right_col + 1):
                    item = self.table_widget.item(row, col)
                    if not item or item.data(Qt.UserRole) is None:
                        continue

                    coords = item.data(Qt.UserRole)

                    # Normalize position to [0, 1] range
                    if bottom_row == top_row:
                        ty = 0.0
                    else:
                        ty = (row - top_row) / (bottom_row - top_row)

                    if right_col == left_col:
                        tx = 0.0
                    else:
                        tx = (col - left_col) / (right_col - left_col)

                    # Bilinear interpolation formula
                    # f(x,y) = (1-x)(1-y)f00 + x(1-y)f10 + (1-x)yf01 + xyf11
                    new_val = (
                        (1 - tx) * (1 - ty) * v00 +
                        tx * (1 - ty) * v10 +
                        (1 - tx) * ty * v01 +
                        tx * ty * v11
                    )

                    try:
                        old_val = float(item.text())
                    except ValueError:
                        continue

                    if abs(new_val - old_val) > 1e-9:  # Only if changed
                        old_raw = self.current_data['values'][coords]

                        # Convert to raw and back to ensure consistency
                        scaling = self.rom_definition.get_scaling(self.current_table.scaling)
                        if scaling:
                            from ..core.rom_reader import ScalingConverter
                            converter = ScalingConverter(scaling)
                            new_raw = converter.from_display(new_val)
                            new_val = converter.to_display(new_raw)

                            # Update display and data
                            item.setText(f"{new_val:.{self._get_precision()}f}")
                            self.current_data['values'][coords] = new_raw

                            all_changes.append((coords[0], coords[1] if len(coords) > 1 else 0,
                                              old_val, new_val, old_raw, new_raw))

        if all_changes:
            self.bulk_changes.emit(all_changes)
