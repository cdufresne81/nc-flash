"""
Table Edit Helper

Handles cell editing, validation, and value conversion for TableViewer.
"""

import logging
from typing import TYPE_CHECKING, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush

from ...core.rom_definition import TableType, AxisType
from .context import TableViewerContext

if TYPE_CHECKING:
    from .display import TableDisplayHelper

logger = logging.getLogger(__name__)


class TableEditHelper:
    """Helper class for cell editing operations"""

    def __init__(self, ctx: TableViewerContext, display: 'TableDisplayHelper'):
        self.ctx = ctx
        self.display = display

    def on_cell_changed(self, row: int, col: int):
        """Handle cell value change from user edit"""
        if self.ctx.editing_in_progress or self.ctx.read_only:
            return

        if not self.ctx.current_table or not self.ctx.current_data:
            return

        item = self.ctx.table_widget.item(row, col)
        if not item:
            return

        # Get the data indices from the item
        data_indices = item.data(Qt.UserRole)
        if data_indices is None:
            # No data associated with this cell
            return

        # Check if this is an axis cell or data cell
        if isinstance(data_indices[0], str):
            # This is an axis cell: ('x_axis', index) or ('y_axis', index)
            self._on_axis_cell_changed(row, col, item, data_indices)
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
        values = self.ctx.current_data['values']
        if values.ndim == 1:
            old_value = float(values[data_row])
        else:
            old_value = float(values[data_row, data_col])

        # Skip if no change
        if abs(new_value - old_value) < 1e-10:
            return

        # Convert display values to raw values
        old_raw = self.display_to_raw(old_value)
        new_raw = self.display_to_raw(new_value)

        if old_raw is None or new_raw is None:
            self._revert_cell(row, col, data_row, data_col)
            return

        # Update the internal data
        if values.ndim == 1:
            self.ctx.current_data['values'][data_row] = new_value
        else:
            self.ctx.current_data['values'][data_row, data_col] = new_value

        # Update cell display with proper formatting
        self.ctx.editing_in_progress = True
        try:
            value_fmt = self.display.get_value_format()
            item.setText(self.display.format_value(new_value, value_fmt))

            # Update cell color based on new value
            color = self.display.get_cell_color(new_value, self.ctx.current_data['values'], data_row, data_col)
            item.setBackground(QBrush(color))
        finally:
            self.ctx.editing_in_progress = False

        # Emit the change signal
        self.ctx.viewer.cell_changed.emit(
            self.ctx.current_table.name,
            data_row, data_col,
            old_value, new_value,
            old_raw, new_raw
        )

        logger.debug(f"Cell changed: {self.ctx.current_table.name}[{data_row},{data_col}] {old_value} -> {new_value}")

    def _revert_cell(self, row: int, col: int, data_row: int, data_col: int):
        """Revert cell to its original value"""
        values = self.ctx.current_data['values']
        if values.ndim == 1:
            old_value = values[data_row]
        else:
            old_value = values[data_row, data_col]

        self.ctx.editing_in_progress = True
        try:
            value_fmt = self.display.get_value_format()
            item = self.ctx.table_widget.item(row, col)
            if item:
                item.setText(self.display.format_value(old_value, value_fmt))
        finally:
            self.ctx.editing_in_progress = False

    def display_to_raw(self, display_value: float) -> Optional[float]:
        """Convert display value to raw binary value using scaling"""
        if not self.ctx.rom_definition or not self.ctx.current_table:
            return display_value

        scaling = self.ctx.rom_definition.get_scaling(self.ctx.current_table.scaling)
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
        if not self.ctx.current_table or not self.ctx.current_data:
            return

        # Update internal data
        values = self.ctx.current_data['values']
        if values.ndim == 1:
            values[data_row] = new_value
        else:
            values[data_row, data_col] = new_value

        # Find the UI cell that corresponds to this data cell
        ui_row, ui_col = self.data_to_ui_coords(data_row, data_col)
        if ui_row is None:
            return

        # Update UI
        self.ctx.editing_in_progress = True
        try:
            item = self.ctx.table_widget.item(ui_row, ui_col)
            if item:
                value_fmt = self.display.get_value_format()
                item.setText(self.display.format_value(new_value, value_fmt))
                color = self.display.get_cell_color(new_value, values, data_row, data_col)
                item.setBackground(QBrush(color))
        finally:
            self.ctx.editing_in_progress = False

        # Check if value matches original and remove border if so (smart border removal on undo)
        self.ctx.viewer._check_and_remove_border_if_original(
            self.ctx.current_table.name, data_row, data_col, new_value
        )

    def data_to_ui_coords(self, data_row: int, data_col: int) -> Tuple[Optional[int], Optional[int]]:
        """Convert data coordinates to UI table coordinates"""
        if not self.ctx.current_table:
            return None, None

        table_type = self.ctx.current_table.type
        flipx = self.ctx.current_table.flipx if self.ctx.current_table else False
        flipy = self.ctx.current_table.flipy if self.ctx.current_table else False

        if table_type == TableType.ONE_D:
            return 0, 0
        elif table_type == TableType.TWO_D:
            values = self.ctx.current_data['values']
            num_values = len(values)
            ui_row = (num_values - 1 - data_row) if flipy else data_row
            return ui_row, 1  # Column 1 is the value column (col 0 = Y-axis)
        elif table_type == TableType.THREE_D:
            values = self.ctx.current_data['values']
            rows, cols = values.shape
            ui_row = (rows - 1 - data_row) if flipy else data_row
            ui_col = (cols - 1 - data_col) if flipx else data_col
            return ui_row + 1, ui_col + 1  # +1 for X-axis row, +1 for Y-axis col

        return None, None

    def _on_axis_cell_changed(self, row: int, col: int, item, data_indices: tuple):
        """Handle axis cell value change from user edit"""
        axis_type_str, data_idx = data_indices
        axis_type = AxisType.X_AXIS if axis_type_str == 'x_axis' else AxisType.Y_AXIS

        # Get the axis table for scaling
        axis_table = self.ctx.current_table.get_axis(axis_type)
        if not axis_table:
            return

        # Get the new text value
        new_text = item.text().strip()

        # Try to parse the new value
        try:
            new_value = float(new_text)
        except ValueError:
            # Invalid input - revert to old value
            self._revert_axis_cell(row, col, axis_type, data_idx)
            return

        # Get the old value from current_data
        axis_key = 'x_axis' if axis_type == AxisType.X_AXIS else 'y_axis'
        axis_data = self.ctx.current_data.get(axis_key)
        if axis_data is None:
            return

        old_value = float(axis_data[data_idx])

        # Skip if no change
        if abs(new_value - old_value) < 1e-10:
            return

        # Convert display values to raw values
        old_raw = self._axis_display_to_raw(old_value, axis_table)
        new_raw = self._axis_display_to_raw(new_value, axis_table)

        if old_raw is None or new_raw is None:
            self._revert_axis_cell(row, col, axis_type, data_idx)
            return

        # Update the internal axis data
        self.ctx.current_data[axis_key][data_idx] = new_value

        # Update cell display with proper formatting
        self.ctx.editing_in_progress = True
        try:
            axis_fmt = self.display.get_axis_format(axis_type)
            item.setText(self.display.format_value(new_value, axis_fmt))

            # Update cell color based on new value
            color = self.display.get_axis_color(new_value, self.ctx.current_data[axis_key])
            item.setBackground(QBrush(color))
        finally:
            self.ctx.editing_in_progress = False

        # Emit the axis change signal
        self.ctx.viewer.axis_changed.emit(
            self.ctx.current_table.name,
            axis_type_str,
            data_idx,
            old_value, new_value,
            old_raw, new_raw
        )

        logger.debug(f"Axis changed: {self.ctx.current_table.name}[{axis_type_str}][{data_idx}] {old_value} -> {new_value}")

    def _revert_axis_cell(self, row: int, col: int, axis_type: AxisType, data_idx: int):
        """Revert axis cell to its original value"""
        axis_key = 'x_axis' if axis_type == AxisType.X_AXIS else 'y_axis'
        axis_data = self.ctx.current_data.get(axis_key)
        if axis_data is None:
            return

        old_value = axis_data[data_idx]

        self.ctx.editing_in_progress = True
        try:
            axis_fmt = self.display.get_axis_format(axis_type)
            item = self.ctx.table_widget.item(row, col)
            if item:
                item.setText(self.display.format_value(old_value, axis_fmt))
        finally:
            self.ctx.editing_in_progress = False

    def _axis_display_to_raw(self, display_value: float, axis_table) -> Optional[float]:
        """Convert axis display value to raw binary value using scaling"""
        if not self.ctx.rom_definition or not axis_table:
            return display_value

        scaling = self.ctx.rom_definition.get_scaling(axis_table.scaling)
        if not scaling:
            return display_value

        try:
            from simpleeval import simple_eval
            return simple_eval(scaling.frexpr, names={'x': display_value})
        except Exception as e:
            logger.error(f"Error converting axis to raw: {e}")
            return None

    def update_axis_cell_value(self, axis_type: str, data_idx: int, new_value: float):
        """
        Update an axis cell's value programmatically (for undo/redo)

        Args:
            axis_type: 'x_axis' or 'y_axis'
            data_idx: Index in the axis array
            new_value: New display value
        """
        if not self.ctx.current_table or not self.ctx.current_data:
            return

        axis_key = axis_type
        axis_data = self.ctx.current_data.get(axis_key)
        if axis_data is None:
            return

        # Update internal data
        axis_data[data_idx] = new_value

        # Find the UI cell that corresponds to this axis cell
        ui_row, ui_col = self._axis_data_to_ui_coords(axis_type, data_idx)
        if ui_row is None:
            return

        # Update UI
        self.ctx.editing_in_progress = True
        try:
            item = self.ctx.table_widget.item(ui_row, ui_col)
            if item:
                axis_type_enum = AxisType.X_AXIS if axis_type == 'x_axis' else AxisType.Y_AXIS
                axis_fmt = self.display.get_axis_format(axis_type_enum)
                item.setText(self.display.format_value(new_value, axis_fmt))
                color = self.display.get_axis_color(new_value, axis_data)
                item.setBackground(QBrush(color))
        finally:
            self.ctx.editing_in_progress = False

        # Check if value matches original and remove border if so (smart border removal on undo)
        self.ctx.viewer._check_and_remove_axis_border_if_original(
            self.ctx.current_table.name, axis_type, data_idx, new_value
        )

    def _axis_data_to_ui_coords(self, axis_type: str, data_idx: int) -> Tuple[Optional[int], Optional[int]]:
        """Convert axis data index to UI table coordinates"""
        if not self.ctx.current_table:
            return None, None

        table_type = self.ctx.current_table.type
        flipx = self.ctx.current_table.flipx if self.ctx.current_table else False
        flipy = self.ctx.current_table.flipy if self.ctx.current_table else False

        if table_type == TableType.TWO_D:
            # Y axis is in column 0
            if axis_type == 'y_axis':
                values = self.ctx.current_data['values']
                num_values = len(values)
                ui_row = (num_values - 1 - data_idx) if flipy else data_idx
                return ui_row, 0
            return None, None

        elif table_type == TableType.THREE_D:
            if axis_type == 'x_axis':
                # X axis is in row 0, columns 1+
                x_axis = self.ctx.current_data.get('x_axis')
                if x_axis is not None:
                    cols = len(x_axis)
                    ui_col = (cols - 1 - data_idx) if flipx else data_idx
                    return 0, ui_col + 1
            elif axis_type == 'y_axis':
                # Y axis is in column 0, rows 1+
                y_axis = self.ctx.current_data.get('y_axis')
                if y_axis is not None:
                    rows = len(y_axis)
                    ui_row = (rows - 1 - data_idx) if flipy else data_idx
                    return ui_row + 1, 0
            return None, None

        return None, None
