"""
Table Operations Helper

Handles bulk data operations for TableViewer.
"""

import logging
import math
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


def selected_cells(ranges) -> List[Tuple[int, int]]:
    """The (row, col) of every selected cell, each once, in selection order.

    Ctrl+click selections are several ranges. Only the cells inside them are
    selected; the rectangle around them is not, so bulk edits must not touch
    the cells in between.
    """
    seen = set()
    cells = []
    for sel_range in ranges:
        for row in range(sel_range.topRow(), sel_range.bottomRow() + 1):
            for col in range(sel_range.leftColumn(), sel_range.rightColumn() + 1):
                if (row, col) not in seen:
                    seen.add((row, col))
                    cells.append((row, col))
    return cells


def compute_smoothed_values(
    values,
    selected_indices,
    blend_factor=0.15,
    auto_round=False,
    precision=2,
):
    """Compute smoothed values using weighted neighbor averaging.

    Args:
        values: numpy array of current values (1D or 2D).
        selected_indices: list of (data_row, data_col) tuples to smooth.
        blend_factor: how much to blend toward neighbor average (0-1).
        auto_round: whether to round results to ``precision`` decimals.
        precision: number of decimal places for rounding.

    Returns:
        dict mapping (data_row, data_col) to smoothed value.
    """
    smoothed = {}

    for data_row, data_col in selected_indices:
        if values.ndim == 1:
            neighbors = []
            if data_row > 0:
                neighbors.append(float(values[data_row - 1]))
            if data_row < len(values) - 1:
                neighbors.append(float(values[data_row + 1]))

            if neighbors:
                current = float(values[data_row])
                neighbor_avg = sum(neighbors) / len(neighbors)
                result = current + blend_factor * (neighbor_avg - current)
                if auto_round:
                    result = round(result, precision)
                smoothed[(data_row, data_col)] = result
        else:
            neighbors = []
            rows, cols = values.shape

            for dr in [-1, 0, 1]:
                for dc in [-1, 0, 1]:
                    if dr == 0 and dc == 0:
                        continue
                    nr, nc = data_row + dr, data_col + dc
                    if 0 <= nr < rows and 0 <= nc < cols:
                        neighbors.append(float(values[nr, nc]))

            if neighbors:
                current = float(values[data_row, data_col])
                neighbor_avg = sum(neighbors) / len(neighbors)
                result = current + blend_factor * (neighbor_avg - current)
                if auto_round:
                    result = round(result, precision)
                smoothed[(data_row, data_col)] = result

    return smoothed


class TableOperationsHelper:
    """Helper class for bulk data operations"""

    def __init__(
        self,
        ctx: TableViewerContext,
        display: "TableDisplayHelper",
        edit: "TableEditHelper",
    ):
        self.ctx = ctx
        self.display = display
        self.edit = edit

    def apply_bulk_operation(
        self,
        operation_fn: Callable[[float], float],
        operation_name: str,
        axis_operation_fn: Callable[[float, str], float] = None,
    ) -> Tuple[List[Tuple], List[Tuple]]:
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
        if (
            self.ctx.read_only
            or not self.ctx.current_table
            or not self.ctx.current_data
        ):
            return [], []

        # Get selected ranges
        selected = self.ctx.table_widget.selectedRanges()
        if not selected:
            logger.debug(f"{operation_name}: No selection")
            return [], []

        # Phase 1: compute and validate every cell's new value without
        # touching memory or the screen. A cell whose result is non-finite
        # (e.g. a multiply that overflows), unchanged or unstorable is
        # skipped here, so nothing below can fail halfway through.
        axis_plans = []  # (item, axis_type, axis_key, data_idx, change)
        data_plans = []  # (row, col, change)
        for row, col in selected_cells(selected):
            item = self.ctx.table_widget.item(row, col)
            if not item:
                continue
            data_indices = item.data(Qt.UserRole)
            if data_indices is None:
                continue  # No data associated with this cell

            if isinstance(data_indices[0], str):
                # Axis cell: ('x_axis', index) or ('y_axis', index)
                plan = self._plan_axis_cell(
                    data_indices, operation_fn, axis_operation_fn
                )
                if plan is not None:
                    axis_plans.append((item,) + plan)
            else:
                change = self._plan_data_cell(data_indices, operation_fn)
                if change is not None:
                    data_plans.append((row, col, change))

        # Phase 2: apply. Every value here is finite and storable.
        data_changes = [change for _row, _col, change in data_plans]
        axis_changes = [plan[-1] for plan in axis_plans]
        if not data_changes and not axis_changes:
            return [], []

        # Cache min/max values before bulk operation to avoid O(n^2) complexity in color calculations
        if "values" in self.ctx.current_data:
            self.display.cache_value_range(self.ctx.current_data["values"])

        # Cache format string to avoid repeated lookups
        value_fmt = self.display.get_value_format()

        with frozen_table_updates(self.ctx.table_widget):
            try:
                for item, axis_type, axis_key, data_idx, change in axis_plans:
                    new_value = change[3]
                    axis_data = self.ctx.current_data[axis_key]
                    axis_data[data_idx] = new_value
                    self.ctx.editing_in_progress = True
                    try:
                        axis_fmt = self.display.get_axis_format(axis_type)
                        item.setText(self.display.format_value(new_value, axis_fmt))
                        color = self.display.get_axis_color(
                            new_value, axis_data, axis_type
                        )
                        item.setBackground(QBrush(color))
                    finally:
                        self.ctx.editing_in_progress = False

                values = self.ctx.current_data.get("values")
                for _row, _col, change in data_plans:
                    data_row, data_col, _old, new_value = change[:4]
                    if values.ndim == 1:
                        values[data_row] = new_value
                    else:
                        values[data_row, data_col] = new_value

                # Batch update: set text and colors for all changed cells at once
                if data_plans:
                    self.display.cache_value_range(values)
                    self.ctx.editing_in_progress = True
                    try:
                        for row, col, change in data_plans:
                            data_row, data_col, _old, new_value = change[:4]
                            item = self.ctx.table_widget.item(row, col)
                            if item:
                                item.setText(
                                    self.display.format_value(new_value, value_fmt)
                                )
                                color = self.display.get_cell_color(
                                    new_value, values, data_row, data_col
                                )
                                item.setBackground(QBrush(color))
                    finally:
                        self.ctx.editing_in_progress = False

                return data_changes, axis_changes
            finally:
                # Clear the cached min/max values
                self.display.clear_value_range_cache()

    def _plan_axis_cell(self, data_indices, operation_fn, axis_operation_fn):
        """Compute one axis cell's change without applying it.

        Returns (axis_type, axis_key, data_idx, change) with change =
        (axis_key, data_idx, old_value, new_value, old_raw, new_raw), or None
        to leave the cell alone.
        """
        axis_key, data_idx = data_indices
        axis_type = AxisType.X_AXIS if axis_key == "x_axis" else AxisType.Y_AXIS
        axis_table = self.ctx.current_table.get_axis(axis_type)
        if not axis_table:
            return None
        axis_data = self.ctx.current_data.get(axis_key)
        if axis_data is None:
            return None

        old_value = float(axis_data[data_idx])
        try:
            if axis_operation_fn:
                new_value = float(axis_operation_fn(old_value, axis_key))
            else:
                new_value = float(operation_fn(old_value))
        except Exception as e:
            logger.warning(
                f"Operation failed for axis cell [{axis_key}][{data_idx}]: {e}"
            )
            return None
        if not math.isfinite(new_value):
            logger.warning(
                f"Skipping axis cell [{axis_key}][{data_idx}]: "
                f"result {new_value} is not a finite number"
            )
            return None
        new_value = self.edit.fit_to_array(axis_data, new_value)
        if abs(new_value - old_value) < 1e-10:
            return None

        old_raw = self.edit._axis_display_to_raw(old_value, axis_table, current=True)
        new_raw = self.edit._axis_display_to_raw(new_value, axis_table)
        if old_raw is None or new_raw is None:
            return None
        change = (
            axis_key,
            data_idx,
            old_value,
            new_value,
            float(old_raw),
            float(new_raw),
        )
        return axis_type, axis_key, data_idx, change

    def _plan_data_cell(self, data_indices, operation_fn):
        """Compute one data cell's change without applying it.

        Returns (data_row, data_col, old_value, new_value, old_raw, new_raw),
        or None to leave the cell alone.
        """
        data_row, data_col = data_indices
        values = self.ctx.current_data["values"]
        if values.ndim == 1:
            old_value = float(values[data_row])
        else:
            old_value = float(values[data_row, data_col])

        try:
            new_value = float(operation_fn(old_value))
        except Exception as e:
            logger.warning(f"Operation failed for cell [{data_row},{data_col}]: {e}")
            return None
        if not math.isfinite(new_value):
            logger.warning(
                f"Skipping cell [{data_row},{data_col}]: "
                f"result {new_value} is not a finite number"
            )
            return None
        new_value = self.edit.fit_to_array(values, new_value)
        if abs(new_value - old_value) < 1e-10:
            return None

        old_raw = self.edit.display_to_raw(old_value, current=True)
        new_raw = self.edit.display_to_raw(new_value)
        if old_raw is None or new_raw is None:
            return None
        return (data_row, data_col, old_value, new_value, old_raw, new_raw)

    def _get_axis_increment(self, axis_type_str: str) -> float:
        """Get the increment value for an axis from its scaling metadata"""
        if not self.ctx.rom_definition or not self.ctx.current_table:
            return 1.0

        axis_type = AxisType.X_AXIS if axis_type_str == "x_axis" else AxisType.Y_AXIS
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
            scaling = self.ctx.rom_definition.get_scaling(
                self.ctx.current_table.scaling
            )
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
            axis_operation_fn=axis_increment_op,
        )

        # Emit signals
        if data_changes:
            self.ctx.viewer.bulk_changes.emit(self.ctx.current_table, data_changes)
        if axis_changes:
            self.ctx.viewer.axis_bulk_changes.emit(self.ctx.current_table, axis_changes)

    def decrement_selection(self):
        """Decrement selected cells by fixed amount (using appropriate increment per cell type)"""
        if not self.ctx.current_table:
            return

        # Get data decrement from scaling metadata if available
        data_decrement = 1.0
        if self.ctx.rom_definition and self.ctx.current_table.scaling:
            scaling = self.ctx.rom_definition.get_scaling(
                self.ctx.current_table.scaling
            )
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
            axis_operation_fn=axis_decrement_op,
        )

        # Emit signals
        if data_changes:
            self.ctx.viewer.bulk_changes.emit(self.ctx.current_table, data_changes)
        if axis_changes:
            self.ctx.viewer.axis_bulk_changes.emit(self.ctx.current_table, axis_changes)

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
                lambda v: v + value, f"Add {value}"
            )

            # Emit signals
            if data_changes:
                self.ctx.viewer.bulk_changes.emit(self.ctx.current_table, data_changes)
            if axis_changes:
                self.ctx.viewer.axis_bulk_changes.emit(
                    self.ctx.current_table, axis_changes
                )

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
                lambda v: v * factor, f"Multiply by {factor}"
            )

            # Emit signals
            if data_changes:
                self.ctx.viewer.bulk_changes.emit(self.ctx.current_table, data_changes)
            if axis_changes:
                self.ctx.viewer.axis_bulk_changes.emit(
                    self.ctx.current_table, axis_changes
                )

    def set_value_selection(self):
        """Set all selected cells to value (dialog)"""
        if not self.ctx.current_table:
            return

        # Count selected cells for preview
        selected = self.ctx.table_widget.selectedRanges()
        if not selected:
            return

        cell_count = len(selected_cells(selected))

        dialog = SetValueDialog(cell_count, self.ctx.viewer)
        if dialog.exec() == QDialog.Accepted:
            value = dialog.get_value()

            # Apply operation
            data_changes, axis_changes = self.apply_bulk_operation(
                lambda v: value, f"Set to {value}"
            )

            # Emit signals
            if data_changes:
                self.ctx.viewer.bulk_changes.emit(self.ctx.current_table, data_changes)
            if axis_changes:
                self.ctx.viewer.axis_bulk_changes.emit(
                    self.ctx.current_table, axis_changes
                )

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
            # Select data region
            # Row 0: X-axis values, Row 1+: data
            # Col 0: Y-axis values
            num_rows = self.ctx.table_widget.rowCount()
            num_cols = self.ctx.table_widget.columnCount()

            if num_rows > 1 and num_cols > 1:
                # Data starts at row 1, col 1
                selection = QTableWidgetSelectionRange(1, 1, num_rows - 1, num_cols - 1)
                self.ctx.table_widget.setRangeSelected(selection, True)
                self.ctx.table_widget.setCurrentCell(1, 1)

    def smooth_selection(self):
        """
        Apply light smoothing to selected data cells.

        Uses weighted neighbor averaging to reduce jagged transitions.
        Only affects data cells (not axis cells).
        Works best on 3D tables but also works on 2D tables.
        """
        if (
            self.ctx.read_only
            or not self.ctx.current_table
            or not self.ctx.current_data
        ):
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
        values = self.ctx.current_data["values"]
        smooth_cells = []

        for row, col in selected_cells(selected):
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
            smooth_cells.append((row, col, data_row, data_col))

        if not smooth_cells:
            logger.debug("Smooth: No data cells in selection")
            return

        # Light smoothing factor (0.15 = 15% blend toward neighbor average)
        # This is intentionally light - user can apply multiple times
        blend_factor = 0.15

        # Check auto-round setting
        from ...utils.settings import get_settings
        from ...utils.formatting import get_scaling_format, _get_format_precision

        auto_round = get_settings().get_auto_round()
        if auto_round:
            data_fmt = get_scaling_format(
                self.ctx.rom_definition, self.ctx.current_table.scaling
            )
            fmt_precision = _get_format_precision(data_fmt)

        # Calculate smoothed values first (don't modify while iterating)
        selected_data_indices = [
            (data_row, data_col) for _, _, data_row, data_col in smooth_cells
        ]
        smoothed_values = compute_smoothed_values(
            values,
            selected_data_indices,
            blend_factor=blend_factor,
            auto_round=auto_round,
            precision=fmt_precision if auto_round else 2,
        )

        if not smoothed_values:
            return

        # Plan every change first (no state touched), then apply. A non-finite
        # result (a NaN neighbour, e.g. an unprogrammed cell) or an
        # unstorable one is skipped.
        plans = []
        for ui_row, ui_col, data_row, data_col in smooth_cells:
            if (data_row, data_col) not in smoothed_values:
                continue

            new_value = smoothed_values[(data_row, data_col)]
            if not math.isfinite(new_value):
                continue

            if values.ndim == 1:
                old_value = float(values[data_row])
            else:
                old_value = float(values[data_row, data_col])
            new_value = self.edit.fit_to_array(values, new_value)

            # Skip if no significant change
            if abs(new_value - old_value) < 1e-10:
                continue

            # Convert to raw values
            old_raw = self.edit.display_to_raw(old_value, current=True)
            new_raw = self.edit.display_to_raw(new_value)
            if old_raw is None or new_raw is None:
                continue
            plans.append(
                (
                    ui_row,
                    ui_col,
                    (data_row, data_col, old_value, new_value, old_raw, new_raw),
                )
            )

        if not plans:
            return

        data_changes = [change for _r, _c, change in plans]
        with frozen_table_updates(self.ctx.table_widget):
            for ui_row, ui_col, change in plans:
                data_row, data_col, _old, new_value = change[:4]
                if values.ndim == 1:
                    values[data_row] = new_value
                else:
                    values[data_row, data_col] = new_value

                item = self.ctx.table_widget.item(ui_row, ui_col)
                if item:
                    self.ctx.editing_in_progress = True
                    try:
                        value_fmt = self.display.get_value_format()
                        item.setText(self.display.format_value(new_value, value_fmt))
                        color = self.display.get_cell_color(
                            new_value, values, data_row, data_col
                        )
                        item.setBackground(QBrush(color))
                    finally:
                        self.ctx.editing_in_progress = False

            # Emit signal for all changes
            self.ctx.viewer.bulk_changes.emit(self.ctx.current_table, data_changes)
            logger.debug(f"Smoothed {len(data_changes)} cell(s)")

    def round_selection(self):
        """Round selected cells one decimal level coarser.

        Uses the scaling format to determine max precision, then detects
        each value's effective precision and rounds to one level less.
        e.g. for %0.2f: 12.11 -> 12.1, 12.10 -> 12.0
        """
        from ...utils.formatting import (
            round_one_level_coarser,
            get_scaling_format,
            get_axis_format,
        )

        if not self.ctx.current_table:
            return

        data_fmt = get_scaling_format(
            self.ctx.rom_definition, self.ctx.current_table.scaling
        )

        def round_data(value: float) -> float:
            return round_one_level_coarser(value, data_fmt)

        def round_axis(value: float, axis_type_str: str) -> float:
            axis_type = (
                AxisType.X_AXIS if axis_type_str == "x_axis" else AxisType.Y_AXIS
            )
            axis_fmt = get_axis_format(
                self.ctx.rom_definition, self.ctx.current_table, axis_type
            )
            return round_one_level_coarser(value, axis_fmt)

        data_changes, axis_changes = self.apply_bulk_operation(
            round_data, "Round", axis_operation_fn=round_axis
        )

        if data_changes:
            self.ctx.viewer.bulk_changes.emit(self.ctx.current_table, data_changes)
        if axis_changes:
            self.ctx.viewer.axis_bulk_changes.emit(self.ctx.current_table, axis_changes)
