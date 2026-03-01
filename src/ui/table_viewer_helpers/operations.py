"""
Table Operations Helper

Handles bulk data operations for TableViewer.
"""

import logging
from typing import TYPE_CHECKING, List, Tuple, Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush
from PySide6.QtWidgets import QDialog, QTableWidgetSelectionRange

from ...core.rom_definition import TableType, AxisType
from ..data_operation_dialogs import AddValueDialog, MultiplyDialog, SetValueDialog
from .context import TableViewerContext, frozen_table_updates

if TYPE_CHECKING:
    from .display import TableDisplayHelper
    from .editing import TableEditHelper

logger = logging.getLogger(__name__)


class TableOperationsHelper:
    """Helper class for bulk data operations"""

    def __init__(self, ctx: TableViewerContext, display: 'TableDisplayHelper', edit: 'TableEditHelper'):
        self.ctx = ctx
        self.display = display
        self.edit = edit

    def apply_bulk_operation(self, operation_fn: Callable[[float], float],
                             operation_name: str,
                             axis_operation_fn: Callable[[float, str], float] = None) -> Tuple[List[Tuple], List[Tuple]]:
        """
        Apply an operation to all selected cells (data and axis)

        Args:
            operation_fn: Function(old_value) -> new_value for data cells
            operation_name: Description for logging
            axis_operation_fn: Optional function(old_value, axis_type) -> new_value for axis cells.
                              If None, operation_fn is used for axis cells too.

        Returns:
            Tuple of (data_changes, axis_changes) where:
            - data_changes: List of (row, col, old_value, new_value, old_raw, new_raw) tuples
            - axis_changes: List of (axis_type, index, old_value, new_value, old_raw, new_raw) tuples
        """
        if self.ctx.read_only or not self.ctx.current_table or not self.ctx.current_data:
            return [], []

        # Get selected ranges
        selected = self.ctx.table_widget.selectedRanges()
        if not selected:
            logger.debug(f"{operation_name}: No selection")
            return [], []

        # Get bounding rectangle of selection
        min_row = min(r.topRow() for r in selected)
        max_row = max(r.bottomRow() for r in selected)
        min_col = min(r.leftColumn() for r in selected)
        max_col = max(r.rightColumn() for r in selected)

        data_changes = []
        axis_changes = []
        changed_items = []  # Track (item, new_value, data_row, data_col) for color updates

        # Cache min/max values before bulk operation to avoid O(n²) complexity in color calculations
        if 'values' in self.ctx.current_data:
            self.display.cache_value_range(self.ctx.current_data['values'])

        # Cache format string to avoid repeated lookups
        value_fmt = self.display.get_value_format()

        with frozen_table_updates(self.ctx.table_widget):
            try:
                # Iterate through selection
                for row in range(min_row, max_row + 1):
                    for col in range(min_col, max_col + 1):
                        item = self.ctx.table_widget.item(row, col)
                        if not item:
                            continue

                        # Check cell type from UserRole data
                        data_indices = item.data(Qt.UserRole)
                        if data_indices is None:
                            continue  # No data associated with this cell

                        # Check if this is an axis cell or data cell
                        if isinstance(data_indices[0], str):
                            # Axis cell: ('x_axis', index) or ('y_axis', index)
                            axis_type_str, data_idx = data_indices
                            axis_type = AxisType.X_AXIS if axis_type_str == 'x_axis' else AxisType.Y_AXIS

                            # Get axis table for scaling
                            axis_table = self.ctx.current_table.get_axis(axis_type)
                            if not axis_table:
                                continue

                            # Get current axis value
                            axis_key = axis_type_str
                            axis_data = self.ctx.current_data.get(axis_key)
                            if axis_data is None:
                                continue

                            old_value = float(axis_data[data_idx])

                            # Apply operation - use axis_operation_fn if provided, otherwise fall back to operation_fn
                            try:
                                if axis_operation_fn:
                                    new_value = float(axis_operation_fn(old_value, axis_type_str))
                                else:
                                    new_value = float(operation_fn(old_value))
                            except Exception as e:
                                logger.warning(f"Operation failed for axis cell [{axis_type_str}][{data_idx}]: {e}")
                                continue

                            # Skip if no change
                            if abs(new_value - old_value) < 1e-10:
                                continue

                            # Convert to raw values using axis scaling
                            old_raw = self.edit._axis_display_to_raw(old_value, axis_table)
                            new_raw = self.edit._axis_display_to_raw(new_value, axis_table)
                            if old_raw is None or new_raw is None:
                                continue

                            # Update internal axis data
                            self.ctx.current_data[axis_key][data_idx] = new_value

                            # Update cell display
                            self.ctx.editing_in_progress = True
                            try:
                                axis_fmt = self.display.get_axis_format(axis_type)
                                item.setText(self.display.format_value(new_value, axis_fmt))
                                color = self.display.get_axis_color(new_value, self.ctx.current_data[axis_key], axis_type)
                                item.setBackground(QBrush(color))
                            finally:
                                self.ctx.editing_in_progress = False

                            # Record axis change
                            axis_changes.append((axis_type_str, data_idx, old_value, new_value, float(old_raw), float(new_raw)))

                        else:
                            # Data cell: (data_row, data_col)
                            data_row, data_col = data_indices

                            # Get current value
                            values = self.ctx.current_data['values']
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

                            # Convert to raw values
                            old_raw = self.edit.display_to_raw(old_value)
                            new_raw = self.edit.display_to_raw(new_value)
                            if old_raw is None or new_raw is None:
                                continue

                            # Update internal data
                            if values.ndim == 1:
                                self.ctx.current_data['values'][data_row] = new_value
                            else:
                                self.ctx.current_data['values'][data_row, data_col] = new_value

                            # Track cell for batch update at end (skip per-cell widget updates)
                            changed_items.append((row, col, new_value, data_row, data_col))

                            # Record data change
                            data_changes.append((data_row, data_col, old_value, new_value, old_raw, new_raw))

                # Batch update: set text and colors for all changed cells at once
                if changed_items and 'values' in self.ctx.current_data:
                    self.display.cache_value_range(self.ctx.current_data['values'])
                    self.ctx.editing_in_progress = True
                    try:
                        for row, col, new_value, data_row, data_col in changed_items:
                            item = self.ctx.table_widget.item(row, col)
                            if item:
                                formatted = self.display.format_value(new_value, value_fmt)
                                item.setText(formatted)
                                color = self.display.get_cell_color(new_value, self.ctx.current_data['values'], data_row, data_col)
                                item.setBackground(QBrush(color))
                    finally:
                        self.ctx.editing_in_progress = False

                return data_changes, axis_changes
            finally:
                # Clear the cached min/max values
                self.display.clear_value_range_cache()

    def _get_axis_increment(self, axis_type_str: str) -> float:
        """Get the increment value for an axis from its scaling metadata"""
        if not self.ctx.rom_definition or not self.ctx.current_table:
            return 1.0

        axis_type = AxisType.X_AXIS if axis_type_str == 'x_axis' else AxisType.Y_AXIS
        axis_table = self.ctx.current_table.get_axis(axis_type)
        if axis_table and axis_table.scaling:
            scaling = self.ctx.rom_definition.get_scaling(axis_table.scaling)
            if scaling and scaling.inc:
                return scaling.inc
        return 1.0

    def increment_selection(self):
        """Increment selected cells by fixed amount (using appropriate increment per cell type)"""
        if not self.ctx.current_table:
            return

        # Get data increment from scaling metadata if available
        data_increment = 1.0
        if self.ctx.rom_definition and self.ctx.current_table.scaling:
            scaling = self.ctx.rom_definition.get_scaling(self.ctx.current_table.scaling)
            if scaling and scaling.inc:
                data_increment = scaling.inc

        # Create axis operation that uses axis-specific increment
        def axis_increment_op(value: float, axis_type_str: str) -> float:
            axis_inc = self._get_axis_increment(axis_type_str)
            return value + axis_inc

        # Apply operation
        data_changes, axis_changes = self.apply_bulk_operation(
            lambda v: v + data_increment,
            f"Increment",
            axis_operation_fn=axis_increment_op
        )

        # Emit signals
        if data_changes:
            self.ctx.viewer.bulk_changes.emit(data_changes)
        if axis_changes:
            self.ctx.viewer.axis_bulk_changes.emit(axis_changes)

    def decrement_selection(self):
        """Decrement selected cells by fixed amount (using appropriate increment per cell type)"""
        if not self.ctx.current_table:
            return

        # Get data decrement from scaling metadata if available
        data_decrement = 1.0
        if self.ctx.rom_definition and self.ctx.current_table.scaling:
            scaling = self.ctx.rom_definition.get_scaling(self.ctx.current_table.scaling)
            if scaling and scaling.inc:
                data_decrement = scaling.inc

        # Create axis operation that uses axis-specific increment
        def axis_decrement_op(value: float, axis_type_str: str) -> float:
            axis_inc = self._get_axis_increment(axis_type_str)
            return value - axis_inc

        # Apply operation
        data_changes, axis_changes = self.apply_bulk_operation(
            lambda v: v - data_decrement,
            f"Decrement",
            axis_operation_fn=axis_decrement_op
        )

        # Emit signals
        if data_changes:
            self.ctx.viewer.bulk_changes.emit(data_changes)
        if axis_changes:
            self.ctx.viewer.axis_bulk_changes.emit(axis_changes)

    def add_to_selection(self):
        """Add custom value to selected cells (dialog)"""
        logger.debug("add_to_selection() called")
        if not self.ctx.current_table:
            logger.debug("No current table, returning")
            return

        logger.debug("Creating AddValueDialog")
        dialog = AddValueDialog(self.ctx.viewer)
        logger.debug("Showing dialog")
        result = dialog.exec()
        logger.debug(f"Dialog result: {result}")
        if result == QDialog.Accepted:
            value = dialog.get_value()

            # Apply operation
            data_changes, axis_changes = self.apply_bulk_operation(
                lambda v: v + value,
                f"Add {value}"
            )

            # Emit signals
            if data_changes:
                self.ctx.viewer.bulk_changes.emit(data_changes)
            if axis_changes:
                self.ctx.viewer.axis_bulk_changes.emit(axis_changes)

    def multiply_selection(self):
        """Multiply selected cells by factor (dialog)"""
        logger.debug("multiply_selection() called")
        if not self.ctx.current_table:
            logger.debug("No current table, returning")
            return

        logger.debug("Creating MultiplyDialog")
        dialog = MultiplyDialog(self.ctx.viewer)
        logger.debug("Showing dialog")
        result = dialog.exec()
        logger.debug(f"Dialog result: {result}")
        if result == QDialog.Accepted:
            factor = dialog.get_factor()

            # Apply operation
            data_changes, axis_changes = self.apply_bulk_operation(
                lambda v: v * factor,
                f"Multiply by {factor}"
            )

            # Emit signals
            if data_changes:
                self.ctx.viewer.bulk_changes.emit(data_changes)
            if axis_changes:
                self.ctx.viewer.axis_bulk_changes.emit(axis_changes)

    def set_value_selection(self):
        """Set all selected cells to value (dialog)"""
        if not self.ctx.current_table:
            return

        # Count selected cells for preview
        selected = self.ctx.table_widget.selectedRanges()
        if not selected:
            return

        cell_count = sum(
            (r.bottomRow() - r.topRow() + 1) * (r.rightColumn() - r.leftColumn() + 1)
            for r in selected
        )

        dialog = SetValueDialog(cell_count, self.ctx.viewer)
        if dialog.exec() == QDialog.Accepted:
            value = dialog.get_value()

            # Apply operation
            data_changes, axis_changes = self.apply_bulk_operation(
                lambda v: value,
                f"Set to {value}"
            )

            # Emit signals
            if data_changes:
                self.ctx.viewer.bulk_changes.emit(data_changes)
            if axis_changes:
                self.ctx.viewer.axis_bulk_changes.emit(axis_changes)

    def select_all_data(self):
        """Select all data cells (excluding axes)"""
        if not self.ctx.current_table:
            return

        table_type = self.ctx.current_table.type

        if table_type == TableType.ONE_D:
            # Single cell at (0, 0)
            selection = QTableWidgetSelectionRange(0, 0, 0, 0)
            self.ctx.table_widget.setRangeSelected(selection, True)
            self.ctx.table_widget.setCurrentCell(0, 0)

        elif table_type == TableType.TWO_D:
            # Select value column (skip Y axis in column 0)
            num_rows = self.ctx.table_widget.rowCount()
            if num_rows > 0:
                # Column 1 is the value column
                selection = QTableWidgetSelectionRange(0, 1, num_rows - 1, 1)
                self.ctx.table_widget.setRangeSelected(selection, True)
                self.ctx.table_widget.setCurrentCell(0, 1)

        elif table_type == TableType.THREE_D:
            # Select data region (ECUFlash layout: data starts at row 2, col 1)
            # Row 0: Axis labels, Row 1: X-axis values
            # Col 0: Y-axis values
            num_rows = self.ctx.table_widget.rowCount()
            num_cols = self.ctx.table_widget.columnCount()

            if num_rows > 2 and num_cols > 1:
                # Data starts at row 2, col 1
                selection = QTableWidgetSelectionRange(2, 1, num_rows - 1, num_cols - 1)
                self.ctx.table_widget.setRangeSelected(selection, True)
                self.ctx.table_widget.setCurrentCell(2, 1)

    def smooth_selection(self):
        """
        Apply light smoothing to selected data cells.

        Uses weighted neighbor averaging to reduce jagged transitions.
        Only affects data cells (not axis cells).
        Works best on 3D tables but also works on 2D tables.
        """
        if self.ctx.read_only or not self.ctx.current_table or not self.ctx.current_data:
            return

        # Only 2D and 3D tables benefit from smoothing
        table_type = self.ctx.current_table.type
        if table_type == TableType.ONE_D:
            logger.debug("Smoothing not applicable to 1D tables")
            return

        # Get selected ranges
        selected = self.ctx.table_widget.selectedRanges()
        if not selected:
            logger.debug("Smooth: No selection")
            return

        # Collect selected data cells with their coordinates
        values = self.ctx.current_data['values']
        selected_cells = []

        for sel_range in selected:
            for row in range(sel_range.topRow(), sel_range.bottomRow() + 1):
                for col in range(sel_range.leftColumn(), sel_range.rightColumn() + 1):
                    item = self.ctx.table_widget.item(row, col)
                    if not item:
                        continue

                    data_indices = item.data(Qt.UserRole)
                    if data_indices is None:
                        continue

                    # Only process data cells (not axis cells)
                    if isinstance(data_indices[0], str):
                        continue  # Skip axis cells

                    data_row, data_col = data_indices
                    selected_cells.append((row, col, data_row, data_col))

        if not selected_cells:
            logger.debug("Smooth: No data cells in selection")
            return

        # Light smoothing factor (0.15 = 15% blend toward neighbor average)
        # This is intentionally light - user can apply multiple times
        blend_factor = 0.15

        # Calculate smoothed values first (don't modify while iterating)
        smoothed_values = {}

        for ui_row, ui_col, data_row, data_col in selected_cells:
            if values.ndim == 1:
                # 2D table (1D array) - average with adjacent values
                neighbors = []
                if data_row > 0:
                    neighbors.append(float(values[data_row - 1]))
                if data_row < len(values) - 1:
                    neighbors.append(float(values[data_row + 1]))

                if neighbors:
                    current = float(values[data_row])
                    neighbor_avg = sum(neighbors) / len(neighbors)
                    smoothed = current + blend_factor * (neighbor_avg - current)
                    smoothed_values[(data_row, data_col)] = smoothed
            else:
                # 3D table (2D array) - average with all 8 neighbors
                neighbors = []
                rows, cols = values.shape

                for dr in [-1, 0, 1]:
                    for dc in [-1, 0, 1]:
                        if dr == 0 and dc == 0:
                            continue  # Skip self
                        nr, nc = data_row + dr, data_col + dc
                        if 0 <= nr < rows and 0 <= nc < cols:
                            neighbors.append(float(values[nr, nc]))

                if neighbors:
                    current = float(values[data_row, data_col])
                    neighbor_avg = sum(neighbors) / len(neighbors)
                    smoothed = current + blend_factor * (neighbor_avg - current)
                    smoothed_values[(data_row, data_col)] = smoothed

        if not smoothed_values:
            return

        # Apply smoothed values and track changes
        data_changes = []

        with frozen_table_updates(self.ctx.table_widget):
            for ui_row, ui_col, data_row, data_col in selected_cells:
                if (data_row, data_col) not in smoothed_values:
                    continue

                new_value = smoothed_values[(data_row, data_col)]

                if values.ndim == 1:
                    old_value = float(values[data_row])
                else:
                    old_value = float(values[data_row, data_col])

                # Skip if no significant change
                if abs(new_value - old_value) < 1e-10:
                    continue

                # Convert to raw values
                old_raw = self.edit.display_to_raw(old_value)
                new_raw = self.edit.display_to_raw(new_value)
                if old_raw is None or new_raw is None:
                    continue

                # Update internal data
                if values.ndim == 1:
                    self.ctx.current_data['values'][data_row] = new_value
                else:
                    self.ctx.current_data['values'][data_row, data_col] = new_value

                # Update cell display
                item = self.ctx.table_widget.item(ui_row, ui_col)
                if item:
                    self.ctx.editing_in_progress = True
                    try:
                        value_fmt = self.display.get_value_format()
                        item.setText(self.display.format_value(new_value, value_fmt))
                        color = self.display.get_cell_color(new_value, self.ctx.current_data['values'], data_row, data_col)
                        item.setBackground(QBrush(color))
                    finally:
                        self.ctx.editing_in_progress = False

                    # Record change
                    data_changes.append((data_row, data_col, old_value, new_value, old_raw, new_raw))

            # Emit signal for all changes
            if data_changes:
                self.ctx.viewer.bulk_changes.emit(data_changes)
                logger.debug(f"Smoothed {len(data_changes)} cell(s)")
