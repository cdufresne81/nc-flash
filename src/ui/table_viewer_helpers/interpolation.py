"""
Table Interpolation Helper

Handles interpolation algorithms for TableViewer.
"""

import logging
from typing import TYPE_CHECKING, List, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush
from PySide6.QtWidgets import QMessageBox

from ...core.rom_definition import TableType, AxisType
from ...core.rom_reader import ScalingConverter
from .context import TableViewerContext

if TYPE_CHECKING:
    from .display import TableDisplayHelper

logger = logging.getLogger(__name__)


class TableInterpolationHelper:
    """Helper class for interpolation operations"""

    def __init__(self, ctx: TableViewerContext, display: 'TableDisplayHelper'):
        self.ctx = ctx
        self.display = display

    def interpolate_vertical(self):
        """Fill gaps vertically with linear interpolation (V key)"""
        logger.debug("interpolate_vertical() called")

        if not self.ctx.current_table or not self.ctx.current_data:
            logger.debug("No current table or data")
            return

        selected_ranges = self.ctx.table_widget.selectedRanges()
        if not selected_ranges:
            logger.debug("No selection")
            return

        logger.debug(f"Processing {len(selected_ranges)} selection range(s)")

        all_changes = []
        axis_changes = []

        for sel_range in selected_ranges:
            logger.debug(f"Selection range: rows {sel_range.topRow()}-{sel_range.bottomRow()}, cols {sel_range.leftColumn()}-{sel_range.rightColumn()}")

            # For each column in the selection
            for col in range(sel_range.leftColumn(), sel_range.rightColumn() + 1):
                # Collect cells in this column (both data and axis)
                cells = []
                is_axis_column = None  # Will be set to axis type if all cells are axis cells

                for row in range(sel_range.topRow(), sel_range.bottomRow() + 1):
                    item = self.ctx.table_widget.item(row, col)
                    if item and item.data(Qt.UserRole) is not None:
                        coords = item.data(Qt.UserRole)
                        # Check if this is an axis cell
                        if isinstance(coords[0], str):
                            # Axis cell: ('y_axis', index) - only Y axis can be vertical
                            if coords[0] == 'y_axis':
                                if is_axis_column is None:
                                    is_axis_column = 'y_axis'
                                elif is_axis_column != 'y_axis':
                                    continue  # Mixed types, skip
                                try:
                                    value = float(item.text())
                                    cells.append((row, value, coords))
                                except ValueError:
                                    continue
                        else:
                            # Data cell: (data_row, data_col)
                            if is_axis_column is None:
                                is_axis_column = False
                            elif is_axis_column != False:
                                continue  # Mixed types, skip
                            try:
                                value = float(item.text())
                                cells.append((row, value, coords))
                            except ValueError:
                                continue

                logger.debug(f"Column {col}: found {len(cells)} cells (axis={is_axis_column})")

                # Need at least 2 cells to interpolate
                if len(cells) < 2:
                    logger.debug(f"Column {col}: skipping (need at least 2 cells)")
                    continue

                # Get first and last values
                first_row, first_val, first_coords = cells[0]
                last_row, last_val, last_coords = cells[-1]

                logger.debug(f"Column {col}: first cell at row {first_row} = {first_val}, last cell at row {last_row} = {last_val}")

                # If first and last are the same row, can't interpolate vertically
                if last_row == first_row:
                    logger.debug(f"Column {col}: skipping (first and last on same row)")
                    continue

                # If only 2 adjacent cells, nothing between them to interpolate
                if len(cells) == 2 and last_row - first_row == 1:
                    logger.debug(f"Column {col}: skipping (only 2 adjacent cells, nothing between)")
                    continue

                # Interpolate ALL cells between first and last
                cells_interpolated = 0
                for row in range(first_row + 1, last_row):
                    # Get the cell at this position
                    item = self.ctx.table_widget.item(row, col)
                    if not item or item.data(Qt.UserRole) is None:
                        logger.debug(f"Column {col}, row {row}: skipping (not a data cell)")
                        continue

                    coords = item.data(Qt.UserRole)

                    # Get current value
                    try:
                        old_val = float(item.text())
                    except ValueError:
                        logger.debug(f"Column {col}, row {row}: skipping (can't parse value)")
                        continue

                    # Linear interpolation based on position
                    t = (row - first_row) / (last_row - first_row)
                    new_val = first_val + t * (last_val - first_val)

                    if abs(new_val - old_val) > 1e-9:  # Only if changed
                        if is_axis_column == 'y_axis':
                            # Handle Y-axis cell interpolation
                            axis_type = AxisType.Y_AXIS
                            axis_table = self.ctx.current_table.get_axis(axis_type)
                            if not axis_table:
                                continue

                            axis_key = 'y_axis'
                            data_idx = coords[1]
                            old_raw = float(self.ctx.current_data[axis_key][data_idx])

                            # Convert to raw and back
                            scaling = self.ctx.rom_definition.get_scaling(axis_table.scaling)
                            if scaling:
                                converter = ScalingConverter(scaling)
                                new_raw = converter.from_display(new_val)
                                new_val = converter.to_display(new_raw)

                                # Update display and data
                                self.ctx.editing_in_progress = True
                                try:
                                    axis_fmt = self.display.get_axis_format(axis_type)
                                    item.setText(self.display.format_value(new_val, axis_fmt))
                                    self.ctx.current_data[axis_key][data_idx] = new_val
                                    color = self.display.get_axis_color(new_val, self.ctx.current_data[axis_key])
                                    item.setBackground(QBrush(color))
                                finally:
                                    self.ctx.editing_in_progress = False

                                # Record axis change
                                change_tuple = (axis_key, data_idx,
                                              float(old_val), float(new_val), float(old_raw), float(new_raw))
                                axis_changes.append(change_tuple)
                                cells_interpolated += 1
                        else:
                            # Handle data cell interpolation
                            # Get old raw value - use correct indexing based on array dimensions
                            values = self.ctx.current_data['values']
                            if values.ndim == 2:
                                # 3D table: 2D values array, use (row, col) indexing
                                old_raw = values[coords[0], coords[1]]
                            else:
                                # 1D/2D table: 1D values array, use single index
                                old_raw = values[coords[0]]

                            # Convert to raw and back to ensure consistency
                            scaling = self.ctx.rom_definition.get_scaling(self.ctx.current_table.scaling)
                            if scaling:
                                converter = ScalingConverter(scaling)
                                new_raw = converter.from_display(new_val)
                                new_val = converter.to_display(new_raw)

                                # Update display and data (disable cell_changed signal during bulk operation)
                                self.ctx.editing_in_progress = True
                                try:
                                    value_fmt = self.display.get_value_format()
                                    item.setText(self.display.format_value(new_val, value_fmt))
                                    # Update display value in values array - use correct indexing based on array dimensions
                                    if values.ndim == 2:
                                        # 3D table: 2D values array, use (row, col) indexing
                                        values[coords[0], coords[1]] = new_val
                                    else:
                                        # 1D/2D table: 1D values array, use single index
                                        values[coords[0]] = new_val
                                    # Update cell color
                                    color = self.display.get_cell_color(new_val, values,
                                                                coords[0], coords[1] if values.ndim == 2 else 0)
                                    item.setBackground(QBrush(color))
                                finally:
                                    self.ctx.editing_in_progress = False

                                # Convert numpy types to Python native types for undo/redo
                                change_tuple = (coords[0], coords[1] if len(coords) > 1 else 0,
                                              float(old_val), float(new_val), float(old_raw), float(new_raw))
                                all_changes.append(change_tuple)
                                cells_interpolated += 1

                logger.debug(f"Column {col}: interpolated {cells_interpolated} cells")

        if all_changes:
            logger.info(f"Vertical interpolation: updated {len(all_changes)} data cells")
            self.ctx.viewer.bulk_changes.emit(all_changes)

        if axis_changes:
            logger.info(f"Vertical interpolation: updated {len(axis_changes)} axis cells")
            self.ctx.viewer.axis_bulk_changes.emit(axis_changes)

        if not all_changes and not axis_changes:
            logger.debug("Vertical interpolation: no changes made")

    def interpolate_horizontal(self):
        """Fill gaps horizontally with linear interpolation (H key)"""
        logger.debug("interpolate_horizontal() called")

        if not self.ctx.current_table or not self.ctx.current_data:
            logger.debug("No current table or data")
            return

        selected_ranges = self.ctx.table_widget.selectedRanges()
        if not selected_ranges:
            logger.debug("No selection")
            return

        logger.debug(f"Processing {len(selected_ranges)} selection range(s)")

        all_changes = []
        axis_changes = []

        for sel_range in selected_ranges:
            logger.debug(f"Selection range: rows {sel_range.topRow()}-{sel_range.bottomRow()}, cols {sel_range.leftColumn()}-{sel_range.rightColumn()}")

            # For each row in the selection
            for row in range(sel_range.topRow(), sel_range.bottomRow() + 1):
                # Collect cells in this row (both data and axis)
                cells = []
                is_axis_row = None  # Will be set to axis type if all cells are axis cells

                for col in range(sel_range.leftColumn(), sel_range.rightColumn() + 1):
                    item = self.ctx.table_widget.item(row, col)
                    if item and item.data(Qt.UserRole) is not None:
                        coords = item.data(Qt.UserRole)
                        # Check if this is an axis cell
                        if isinstance(coords[0], str):
                            # Axis cell: ('x_axis', index) - only X axis can be horizontal
                            if coords[0] == 'x_axis':
                                if is_axis_row is None:
                                    is_axis_row = 'x_axis'
                                elif is_axis_row != 'x_axis':
                                    continue  # Mixed types, skip
                                try:
                                    value = float(item.text())
                                    cells.append((col, value, coords))
                                except ValueError:
                                    continue
                        else:
                            # Data cell: (data_row, data_col)
                            if is_axis_row is None:
                                is_axis_row = False
                            elif is_axis_row != False:
                                continue  # Mixed types, skip
                            try:
                                value = float(item.text())
                                cells.append((col, value, coords))
                            except ValueError:
                                continue

                logger.debug(f"Row {row}: found {len(cells)} cells (axis={is_axis_row})")

                # Need at least 2 cells to interpolate
                if len(cells) < 2:
                    logger.debug(f"Row {row}: skipping (need at least 2 cells)")
                    continue

                # Get first and last values
                first_col, first_val, first_coords = cells[0]
                last_col, last_val, last_coords = cells[-1]

                logger.debug(f"Row {row}: first cell at col {first_col} = {first_val}, last cell at col {last_col} = {last_val}")

                # If first and last are the same column, can't interpolate horizontally
                if last_col == first_col:
                    logger.debug(f"Row {row}: skipping (first and last on same column)")
                    continue

                # If only 2 adjacent cells, nothing between them to interpolate
                if len(cells) == 2 and last_col - first_col == 1:
                    logger.debug(f"Row {row}: skipping (only 2 adjacent cells, nothing between)")
                    continue

                # Interpolate ALL cells between first and last
                cells_interpolated = 0
                for col in range(first_col + 1, last_col):
                    # Get the cell at this position
                    item = self.ctx.table_widget.item(row, col)
                    if not item or item.data(Qt.UserRole) is None:
                        logger.debug(f"Row {row}, col {col}: skipping (not a data cell)")
                        continue

                    coords = item.data(Qt.UserRole)

                    # Get current value
                    try:
                        old_val = float(item.text())
                    except ValueError:
                        logger.debug(f"Row {row}, col {col}: skipping (can't parse value)")
                        continue

                    # Linear interpolation based on position
                    t = (col - first_col) / (last_col - first_col)
                    new_val = first_val + t * (last_val - first_val)

                    if abs(new_val - old_val) > 1e-9:  # Only if changed
                        if is_axis_row == 'x_axis':
                            # Handle X-axis cell interpolation
                            axis_type = AxisType.X_AXIS
                            axis_table = self.ctx.current_table.get_axis(axis_type)
                            if not axis_table:
                                continue

                            axis_key = 'x_axis'
                            data_idx = coords[1]
                            old_raw = float(self.ctx.current_data[axis_key][data_idx])

                            # Convert to raw and back
                            scaling = self.ctx.rom_definition.get_scaling(axis_table.scaling)
                            if scaling:
                                converter = ScalingConverter(scaling)
                                new_raw = converter.from_display(new_val)
                                new_val = converter.to_display(new_raw)

                                # Update display and data
                                self.ctx.editing_in_progress = True
                                try:
                                    axis_fmt = self.display.get_axis_format(axis_type)
                                    item.setText(self.display.format_value(new_val, axis_fmt))
                                    self.ctx.current_data[axis_key][data_idx] = new_val
                                    color = self.display.get_axis_color(new_val, self.ctx.current_data[axis_key])
                                    item.setBackground(QBrush(color))
                                finally:
                                    self.ctx.editing_in_progress = False

                                # Record axis change
                                change_tuple = (axis_key, data_idx,
                                              float(old_val), float(new_val), float(old_raw), float(new_raw))
                                axis_changes.append(change_tuple)
                                cells_interpolated += 1
                        else:
                            # Handle data cell interpolation
                            # Get old raw value - use correct indexing based on array dimensions
                            values = self.ctx.current_data['values']
                            if values.ndim == 2:
                                # 3D table: 2D values array, use (row, col) indexing
                                old_raw = values[coords[0], coords[1]]
                            else:
                                # 1D/2D table: 1D values array, use single index
                                old_raw = values[coords[0]]

                            # Convert to raw and back to ensure consistency
                            scaling = self.ctx.rom_definition.get_scaling(self.ctx.current_table.scaling)
                            if scaling:
                                converter = ScalingConverter(scaling)
                                new_raw = converter.from_display(new_val)
                                new_val = converter.to_display(new_raw)

                                # Update display and data (disable cell_changed signal during bulk operation)
                                self.ctx.editing_in_progress = True
                                try:
                                    value_fmt = self.display.get_value_format()
                                    item.setText(self.display.format_value(new_val, value_fmt))
                                    # Update display value in values array - use correct indexing based on array dimensions
                                    if values.ndim == 2:
                                        # 3D table: 2D values array, use (row, col) indexing
                                        values[coords[0], coords[1]] = new_val
                                    else:
                                        # 1D/2D table: 1D values array, use single index
                                        values[coords[0]] = new_val
                                    # Update cell color
                                    color = self.display.get_cell_color(new_val, values,
                                                                coords[0], coords[1] if values.ndim == 2 else 0)
                                    item.setBackground(QBrush(color))
                                finally:
                                    self.ctx.editing_in_progress = False

                                # Convert numpy types to Python native types for undo/redo
                                change_tuple = (coords[0], coords[1] if len(coords) > 1 else 0,
                                              float(old_val), float(new_val), float(old_raw), float(new_raw))
                                all_changes.append(change_tuple)
                                cells_interpolated += 1

                logger.debug(f"Row {row}: interpolated {cells_interpolated} cells")

        if all_changes:
            logger.info(f"Horizontal interpolation: updated {len(all_changes)} data cells")
            self.ctx.viewer.bulk_changes.emit(all_changes)

        if axis_changes:
            logger.info(f"Horizontal interpolation: updated {len(axis_changes)} axis cells")
            self.ctx.viewer.axis_bulk_changes.emit(axis_changes)

        if not all_changes and not axis_changes:
            logger.debug("Horizontal interpolation: no changes made")

    def interpolate_2d(self):
        """2D bilinear interpolation for 3D tables (B key)"""
        logger.debug("interpolate_2d() called")

        if not self.ctx.current_table or not self.ctx.current_data:
            logger.debug("No current table or data")
            return

        # Only works for 3D tables
        if self.ctx.current_table.type != TableType.THREE_D:
            logger.debug("Not a 3D table")
            QMessageBox.warning(
                self.ctx.viewer,
                "Invalid Operation",
                "2D interpolation only works on 3D tables"
            )
            return

        selected_ranges = self.ctx.table_widget.selectedRanges()
        if not selected_ranges:
            logger.debug("No selection")
            return

        logger.debug(f"Processing {len(selected_ranges)} selection range(s)")

        all_changes = []

        for sel_range in selected_ranges:
            top_row = sel_range.topRow()
            bottom_row = sel_range.bottomRow()
            left_col = sel_range.leftColumn()
            right_col = sel_range.rightColumn()

            logger.debug(f"Selection range: rows {top_row}-{bottom_row}, cols {left_col}-{right_col}")

            # Need at least 2x2 selection
            if (bottom_row - top_row < 1) or (right_col - left_col < 1):
                logger.debug("Selection too small (need at least 2x2)")
                continue

            # Get corner values
            def get_corner_value(row, col):
                item = self.ctx.table_widget.item(row, col)
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

            logger.debug(f"Corner values: TL={v00}, TR={v10}, BL={v01}, BR={v11}")

            # All corners must have values
            if None in (v00, v10, v01, v11):
                logger.debug("Missing corner value(s), skipping this selection")
                continue

            # Apply bilinear interpolation to all cells in the selection
            for row in range(top_row, bottom_row + 1):
                for col in range(left_col, right_col + 1):
                    item = self.ctx.table_widget.item(row, col)
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
                        # Get old raw value - unpack coords for proper numpy indexing
                        if len(coords) == 2:
                            old_raw = self.ctx.current_data['values'][coords[0], coords[1]]
                        else:
                            old_raw = self.ctx.current_data['values'][coords[0]]

                        # Convert to raw and back to ensure consistency
                        scaling = self.ctx.rom_definition.get_scaling(self.ctx.current_table.scaling)
                        if scaling:
                            converter = ScalingConverter(scaling)
                            new_raw = converter.from_display(new_val)
                            new_val = converter.to_display(new_raw)

                            # Update display and data (disable cell_changed signal during bulk operation)
                            self.ctx.editing_in_progress = True
                            try:
                                value_fmt = self.display.get_value_format()
                                item.setText(self.display.format_value(new_val, value_fmt))
                                # Update display value in values array - unpack coords for proper numpy indexing
                                if len(coords) == 2:
                                    self.ctx.current_data['values'][coords[0], coords[1]] = new_val
                                else:
                                    self.ctx.current_data['values'][coords[0]] = new_val
                                # Update cell color
                                color = self.display.get_cell_color(new_val, self.ctx.current_data['values'],
                                                            coords[0], coords[1] if len(coords) > 1 else 0)
                                item.setBackground(QBrush(color))
                            finally:
                                self.ctx.editing_in_progress = False

                            # Convert numpy types to Python native types for undo/redo
                            change_tuple = (coords[0], coords[1] if len(coords) > 1 else 0,
                                          float(old_val), float(new_val), float(old_raw), float(new_raw))
                            all_changes.append(change_tuple)

        if all_changes:
            logger.info(f"2D bilinear interpolation: updated {len(all_changes)} cells")
            self.ctx.viewer.bulk_changes.emit(all_changes)
        else:
            logger.debug("2D bilinear interpolation: no changes made")
