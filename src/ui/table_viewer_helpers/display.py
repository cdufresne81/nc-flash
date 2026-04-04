"""
Table Display Helper

Handles rendering, formatting, and coloring for TableViewer.
"""

import numpy as np
import logging

from PySide6.QtWidgets import QTableWidgetItem, QHeaderView
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QBrush

from ...core.rom_definition import Table, TableType, AxisType
from ...utils.settings import get_settings
from ...utils.colormap import get_colormap
from ...utils.formatting import printf_to_python_format, format_value, get_scaling_range
from .context import (
    TableViewerContext,
    save_header_resize_modes,
    set_headers_fixed,
    restore_header_resize_modes,
)

logger = logging.getLogger(__name__)


class TableDisplayHelper:
    """Helper class for table display and formatting operations"""

    def __init__(self, ctx: TableViewerContext):
        self.ctx = ctx
        # Cache for min/max values during bulk operations to avoid O(n²) complexity
        self._cached_min_max = None

    def cache_value_range(self, values: np.ndarray):
        """Cache min/max values for bulk operations to avoid repeated calculations"""
        if values is not None and values.size > 0:
            # Prefer scaling-defined range over data-derived range
            scaling_range = self._get_scaling_range()
            if scaling_range:
                self._cached_min_max = scaling_range
            else:
                self._cached_min_max = (float(np.min(values)), float(np.max(values)))

    def clear_value_range_cache(self):
        """Clear the cached min/max values"""
        self._cached_min_max = None

    def begin_bulk_update(self):
        """
        Prepare for bulk cell updates - save header modes and cache value range.

        This optimization prevents expensive per-cell recalculations during bulk
        operations like undo/redo of multi-cell changes.
        """
        h_header, v_header, h_modes, v_modes = save_header_resize_modes(
            self.ctx.table_widget
        )
        self._saved_h_header = h_header
        self._saved_v_header = v_header
        self._saved_h_resize_modes = h_modes
        self._saved_v_resize_modes = v_modes
        set_headers_fixed(h_header, v_header)

        # Cache min/max values for color calculations (avoids O(n^2) complexity)
        if self.ctx.current_data and "values" in self.ctx.current_data:
            self.cache_value_range(self.ctx.current_data["values"])

    def end_bulk_update(self):
        """
        Complete bulk update - restore header modes and clear cache.
        """
        # Clear value range cache
        self.clear_value_range_cache()

        # Restore header resize modes
        if hasattr(self, "_saved_h_resize_modes"):
            restore_header_resize_modes(
                self._saved_h_header,
                self._saved_v_header,
                self._saved_h_resize_modes,
                self._saved_v_resize_modes,
            )
            del self._saved_h_header
            del self._saved_v_header
            del self._saved_h_resize_modes
            del self._saved_v_resize_modes

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

        # Update info label - TEMPORARILY HIDDEN (user request)
        values = data["values"]

        if table.type == TableType.ONE_D:
            self._display_1d(values)
        elif table.type == TableType.TWO_D:
            self._display_2d(values, data.get("y_axis"))
        elif table.type == TableType.THREE_D:
            self._display_3d(values, data.get("x_axis"), data.get("y_axis"))

    def clear(self):
        """Clear the viewer"""
        self.ctx.current_table = None
        self.ctx.current_data = None
        self.ctx.table_widget.setRowCount(0)
        self.ctx.table_widget.setColumnCount(0)
        # Hide axis labels
        self.ctx.viewer.x_axis_label.setVisible(False)
        self.ctx.viewer.y_axis_label.setVisible(False)

    def _is_toggle_category(self) -> bool:
        """Check if the current table's category should use toggle display"""
        if not self.ctx.current_table:
            return False
        category = self.ctx.current_table.category or ""
        toggle_categories = get_settings().get_toggle_categories()
        return category in toggle_categories

    def _display_1d(self, values: np.ndarray):
        """Display 1D table (single value)"""
        # Hide axis labels (not used for 1D tables)
        self.ctx.viewer.x_axis_label.setVisible(False)
        self.ctx.viewer.y_axis_label.setVisible(False)

        use_toggle = self._is_toggle_category()

        self.ctx.editing_in_progress = True
        try:
            # Hide header for 1D tables - no need for "Value" label
            self.ctx.table_widget.horizontalHeader().setVisible(False)
            self.ctx.table_widget.setRowCount(1)
            self.ctx.table_widget.setColumnCount(1)

            value_fmt = self.get_value_format()
            item = QTableWidgetItem(self.format_value(values[0], value_fmt))
            color = self.get_cell_color(values[0], values, 0, 0)
            item.setBackground(QBrush(color))
            # Store data row/col for change tracking (data_row, data_col)
            item.setData(Qt.UserRole, (0, 0))
            if self.ctx.read_only:
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self.ctx.table_widget.setItem(0, 0, item)

            if use_toggle:
                # Toggle mode: hide table grid, show toggle switch
                self.ctx.table_widget.setVisible(False)
                viewer = self.ctx.viewer
                viewer.toggle_container.setVisible(True)

                # Store original non-zero value for restoring on toggle ON
                val = float(values[0])
                viewer._toggle_original_nonzero_value = val if val != 0 else 1.0

                checked = val != 0
                viewer.toggle_switch.setChecked(checked)
                viewer._update_toggle_label(checked)

                if self.ctx.read_only:
                    viewer.toggle_switch.setEnabled(False)
            else:
                # Normal mode: show table grid, hide toggle
                self.ctx.table_widget.setVisible(True)
                self.ctx.viewer.toggle_container.setVisible(False)
        finally:
            self.ctx.editing_in_progress = False

    def _display_2d(self, values: np.ndarray, y_axis: np.ndarray):
        """Display 2D table (1D array with Y axis) - ECUFlash style layout with CSS border"""
        self.ctx.editing_in_progress = True
        try:
            num_values = len(values)
            self.ctx.table_widget.horizontalHeader().setVisible(False)
            self.ctx.table_widget.setRowCount(num_values)
            self.ctx.table_widget.setColumnCount(2)  # Y-axis, data (no spacer)

            # Get axis label with unit - display in separate rotated label (ECUFlash style)
            y_label = self._get_axis_label(self.ctx.current_table, AxisType.Y_AXIS)
            self.ctx.viewer.y_axis_label.setText(y_label)
            self.ctx.viewer.y_axis_label.setVisible(True)
            self.ctx.viewer.x_axis_label.setVisible(False)  # No X-axis for 2D tables

            # Get format specs
            y_fmt = self._get_axis_format(AxisType.Y_AXIS)
            value_fmt = self.get_value_format()

            # Apply flip if needed
            flipy = self.ctx.current_table.flipy if self.ctx.current_table else False
            display_values = values[::-1] if flipy else values
            display_y_axis = y_axis[::-1] if (y_axis is not None and flipy) else y_axis

            # Calculate Y axis gradient range (prefer scaling-defined range)
            y_scaling_range = None
            y_axis_table = self.ctx.current_table.get_axis(AxisType.Y_AXIS)
            if y_axis_table and y_axis_table.scaling:
                y_scaling_range = self._get_scaling_range(y_axis_table.scaling)
            if y_scaling_range:
                y_min, y_max = y_scaling_range
            elif display_y_axis is not None and len(display_y_axis) > 0:
                y_min, y_max = float(np.min(display_y_axis)), float(
                    np.max(display_y_axis)
                )
            else:
                y_min, y_max = 0, num_values - 1

            # For 2D tables, no special column sizing needed - just resize to contents
            self.ctx.table_widget.resizeColumnsToContents()

            for i in range(num_values):
                # Col 0: Y axis value with gradient and right border
                if display_y_axis is not None and i < len(display_y_axis):
                    y_item = QTableWidgetItem(
                        self.format_value(display_y_axis[i], y_fmt)
                    )
                    # Apply gradient based on Y axis values
                    if y_max != y_min:
                        ratio = (display_y_axis[i] - y_min) / (y_max - y_min)
                        ratio = max(0.0, min(1.0, ratio))
                    else:
                        ratio = 0.5
                    y_item.setBackground(QBrush(self.ratio_to_color(ratio)))
                    # Store axis identification: ('y_axis', data_index)
                    # Account for flip when storing the actual data index
                    data_idx = (num_values - 1 - i) if flipy else i
                    y_item.setData(Qt.UserRole, ("y_axis", data_idx))
                    y_item.setData(
                        Qt.UserRole + 2, "axis_separator"
                    )  # Mark for border styling
                    if self.ctx.read_only:
                        y_item.setFlags(y_item.flags() & ~Qt.ItemIsEditable)
                else:
                    y_item = QTableWidgetItem(str(i))
                    y_item.setBackground(QBrush(QColor(240, 240, 240)))
                    y_item.setFlags(
                        y_item.flags() & ~Qt.ItemIsEditable
                    )  # No axis data, not editable
                    y_item.setData(
                        Qt.UserRole + 2, "axis_separator"
                    )  # Mark for border styling
                self.ctx.table_widget.setItem(i, 0, y_item)

                # Col 1: Data value with gradient color
                value_item = QTableWidgetItem(
                    self.format_value(display_values[i], value_fmt)
                )
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
        # Block signals and disable updates to prevent per-cell repaints (audit #22)
        self.ctx.table_widget.blockSignals(True)
        self.ctx.table_widget.setUpdatesEnabled(False)
        try:
            rows, cols = values.shape

            # Set up table dimensions (ECUFlash style with CSS borders for separation):
            # Rows: +1 (row 0 for X-axis values, rows 1+ for data)
            # Cols: +1 (col 0 for Y-axis values, cols 1+ for data)
            # Note: Axis labels are separate widgets, visual separation via CSS borders
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

            # === Row 0: X-axis VALUES row (colored) with bottom border for separation ===
            # Cell (0,0) - empty gray cell (corner) with bottom border
            empty_item = QTableWidgetItem("")
            empty_item.setFlags(empty_item.flags() & ~Qt.ItemIsEditable)
            empty_item.setBackground(label_bg)
            empty_item.setData(
                Qt.UserRole + 2, "axis_separator"
            )  # Mark for border styling
            self.ctx.table_widget.setItem(0, 0, empty_item)

            # X-axis values in row 0 (columns 1+) with gradient and bottom border
            if display_x_axis is not None and len(display_x_axis) == cols:
                # Prefer scaling-defined range for X axis
                x_scaling_range = None
                x_axis_table = self.ctx.current_table.get_axis(AxisType.X_AXIS)
                if x_axis_table and x_axis_table.scaling:
                    x_scaling_range = self._get_scaling_range(x_axis_table.scaling)
                if x_scaling_range:
                    x_min, x_max = x_scaling_range
                else:
                    x_min, x_max = float(np.min(display_x_axis)), float(
                        np.max(display_x_axis)
                    )
                for col in range(cols):
                    x_item = QTableWidgetItem(
                        self.format_value(display_x_axis[col], x_fmt)
                    )
                    if x_max != x_min:
                        ratio = (display_x_axis[col] - x_min) / (x_max - x_min)
                        ratio = max(0.0, min(1.0, ratio))
                    else:
                        ratio = 0.5
                    x_item.setBackground(QBrush(self.ratio_to_color(ratio)))
                    data_idx = (cols - 1 - col) if flipx else col
                    x_item.setData(Qt.UserRole, ("x_axis", data_idx))
                    x_item.setData(
                        Qt.UserRole + 2, "axis_separator"
                    )  # Mark for border styling
                    if self.ctx.read_only:
                        x_item.setFlags(x_item.flags() & ~Qt.ItemIsEditable)
                    self.ctx.table_widget.setItem(0, col + 1, x_item)
            else:
                for col in range(cols):
                    x_item = QTableWidgetItem(str(col))
                    x_item.setFlags(x_item.flags() & ~Qt.ItemIsEditable)
                    x_item.setBackground(label_bg)
                    x_item.setData(
                        Qt.UserRole + 2, "axis_separator"
                    )  # Mark for border styling
                    self.ctx.table_widget.setItem(0, col + 1, x_item)

            # === Column 0: Y-axis VALUES (rows 1+) with right border for separation ===
            if display_y_axis is not None and len(display_y_axis) == rows:
                # Prefer scaling-defined range for Y axis
                y_scaling_range = None
                y_axis_table = self.ctx.current_table.get_axis(AxisType.Y_AXIS)
                if y_axis_table and y_axis_table.scaling:
                    y_scaling_range = self._get_scaling_range(y_axis_table.scaling)
                if y_scaling_range:
                    y_min, y_max = y_scaling_range
                else:
                    y_min, y_max = float(np.min(display_y_axis)), float(
                        np.max(display_y_axis)
                    )
                for row in range(rows):
                    # Col 0: Y-axis value (colored) with right border
                    y_val_item = QTableWidgetItem(
                        self.format_value(display_y_axis[row], y_fmt)
                    )
                    if y_max != y_min:
                        ratio = (display_y_axis[row] - y_min) / (y_max - y_min)
                        ratio = max(0.0, min(1.0, ratio))
                    else:
                        ratio = 0.5
                    y_val_item.setBackground(QBrush(self.ratio_to_color(ratio)))
                    data_idx = (rows - 1 - row) if flipy else row
                    y_val_item.setData(Qt.UserRole, ("y_axis", data_idx))
                    y_val_item.setData(
                        Qt.UserRole + 2, "axis_separator"
                    )  # Mark for border styling
                    if self.ctx.read_only:
                        y_val_item.setFlags(y_val_item.flags() & ~Qt.ItemIsEditable)
                    self.ctx.table_widget.setItem(row + 1, 0, y_val_item)
            else:
                for row in range(rows):
                    # Col 0: Row index (gray, no axis data) with right border
                    y_val_item = QTableWidgetItem(str(row))
                    y_val_item.setFlags(y_val_item.flags() & ~Qt.ItemIsEditable)
                    y_val_item.setBackground(label_bg)
                    y_val_item.setData(
                        Qt.UserRole + 2, "axis_separator"
                    )  # Mark for border styling
                    self.ctx.table_widget.setItem(row + 1, 0, y_val_item)

            # === Data cells (rows 1+, cols 1+) ===
            self.cache_value_range(values)
            try:
                for row in range(rows):
                    for col in range(cols):
                        value_item = QTableWidgetItem(
                            self.format_value(display_values[row, col], value_fmt)
                        )
                        color = self.get_cell_color(
                            display_values[row, col], values, row, col
                        )
                        value_item.setBackground(QBrush(color))
                        data_row = (rows - 1 - row) if flipy else row
                        data_col = (cols - 1 - col) if flipx else col
                        value_item.setData(Qt.UserRole, (data_row, data_col))
                        if self.ctx.read_only:
                            value_item.setFlags(value_item.flags() & ~Qt.ItemIsEditable)
                        self.ctx.table_widget.setItem(row + 1, col + 1, value_item)
            finally:
                self.clear_value_range_cache()
        finally:
            self.ctx.editing_in_progress = False
            # Re-enable signals and updates, trigger a single repaint (audit #22)
            self.ctx.table_widget.blockSignals(False)
            self.ctx.table_widget.setUpdatesEnabled(True)
            self.ctx.table_widget.viewport().update()

        # Apply uniform column width to data columns
        self._apply_uniform_column_width_3d()

    def _apply_uniform_column_width_3d(self):
        """
        Apply uniform width to DATA columns only (excluding Y-axis).
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
        return printf_to_python_format(printf_format)

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
        return format_value(value, format_spec)

    def ratio_to_color(self, ratio: float) -> QColor:
        """
        Convert 0-1 ratio to color using the configured color map
        """
        return get_colormap().ratio_to_color(ratio)

    def _get_scaling_range(self, scaling_name: str = None):
        """
        Get min/max from a scaling definition.

        Returns (min, max) tuple, or None if scaling has no valid range defined.
        """
        name = scaling_name or (
            self.ctx.current_table.scaling if self.ctx.current_table else None
        )
        return get_scaling_range(self.ctx.rom_definition, name)

    def get_cell_color(
        self, value: float, values: np.ndarray, row: int, col: int
    ) -> QColor:
        """Calculate cell background color based on gradient mode"""
        mode = get_settings().get_gradient_mode()

        if mode == "neighbors":
            ratio = self._get_neighbor_ratio(value, values, row, col)
        else:
            # Use cached min/max if available (during bulk operations), otherwise calculate
            if self._cached_min_max is not None:
                min_val, max_val = self._cached_min_max
            else:
                # Prefer scaling-defined range over data-derived range
                scaling_range = self._get_scaling_range()
                if scaling_range:
                    min_val, max_val = scaling_range
                else:
                    min_val = float(np.min(values))
                    max_val = float(np.max(values))

            if max_val == min_val:
                ratio = 0.5
            else:
                ratio = (value - min_val) / (max_val - min_val)
                ratio = max(0.0, min(1.0, ratio))

        return self.ratio_to_color(ratio)

    def get_axis_format(self, axis_type: AxisType) -> str:
        """Public method to get axis format (used by editing helper)"""
        return self._get_axis_format(axis_type)

    def get_axis_color(
        self, value: float, axis_values: np.ndarray, axis_type: AxisType = None
    ) -> QColor:
        """Calculate axis cell color based on value within axis range"""
        if axis_values is None or len(axis_values) == 0:
            return QColor(240, 240, 240)

        # Try scaling-defined range for this axis
        min_val = max_val = None
        if axis_type and self.ctx.current_table and self.ctx.rom_definition:
            axis_table = self.ctx.current_table.get_axis(axis_type)
            if axis_table and axis_table.scaling:
                scaling_range = self._get_scaling_range(axis_table.scaling)
                if scaling_range:
                    min_val, max_val = scaling_range

        # Fall back to data-derived range
        if min_val is None:
            min_val = float(np.min(axis_values))
            max_val = float(np.max(axis_values))

        if max_val == min_val:
            ratio = 0.5
        else:
            ratio = (value - min_val) / (max_val - min_val)
            ratio = max(0.0, min(1.0, ratio))

        return self.ratio_to_color(ratio)

    def _get_neighbor_ratio(
        self, value: float, values: np.ndarray, row: int, col: int
    ) -> float:
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
