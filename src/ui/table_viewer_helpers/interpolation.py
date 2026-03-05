"""
Table Interpolation Helper

Handles interpolation algorithms for TableViewer.
"""

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush
from PySide6.QtWidgets import QMessageBox

from ...core.rom_definition import TableType, AxisType
from ...core.rom_reader import ScalingConverter
from .context import TableViewerContext, frozen_table_updates

if TYPE_CHECKING:
    from .display import TableDisplayHelper

logger = logging.getLogger(__name__)


class TableInterpolationHelper:
    """Helper class for interpolation operations"""

    def __init__(self, ctx: TableViewerContext, display: "TableDisplayHelper"):
        self.ctx = ctx
        self.display = display

    def _check_scaling_available(self) -> bool:
        """Check if scaling is available for interpolation. Warn user if not."""
        if not self.ctx.current_table or not self.ctx.rom_definition:
            return False

        scaling_name = self.ctx.current_table.scaling
        if not scaling_name:
            logger.warning("Interpolation skipped: table has no scaling defined")
            QMessageBox.warning(
                self.ctx.viewer,
                "Interpolation Skipped",
                "Interpolation requires a scaling definition, but this table "
                "has no scaling configured. The operation was skipped.",
            )
            return False

        scaling = self.ctx.rom_definition.get_scaling(scaling_name)
        if not scaling:
            logger.warning(
                "Interpolation skipped: scaling '%s' not found", scaling_name
            )
            QMessageBox.warning(
                self.ctx.viewer,
                "Interpolation Skipped",
                f"Interpolation requires a valid scaling definition, but "
                f"scaling '{scaling_name}' was not found. The operation was skipped.",
            )
            return False

        return True

    def interpolate_vertical(self):
        """Fill gaps vertically with linear interpolation (V key)"""
        self._interpolate_1d("vertical")

    def interpolate_horizontal(self):
        """Fill gaps horizontally with linear interpolation (H key)"""
        self._interpolate_1d("horizontal")

    def _interpolate_1d(self, direction: str):
        """Shared implementation for 1D linear interpolation.

        Args:
            direction: "vertical" or "horizontal"
        """
        is_vertical = direction == "vertical"
        axis_key = "y_axis" if is_vertical else "x_axis"
        axis_type = AxisType.Y_AXIS if is_vertical else AxisType.X_AXIS
        line_label = "Column" if is_vertical else "Row"
        direction_label = "Vertical" if is_vertical else "Horizontal"

        logger.debug(f"interpolate_{direction}() called")

        if not self.ctx.current_table or not self.ctx.current_data:
            logger.debug("No current table or data")
            return

        if not self._check_scaling_available():
            return

        selected_ranges = self.ctx.table_widget.selectedRanges()
        if not selected_ranges:
            logger.debug("No selection")
            return

        logger.debug(f"Processing {len(selected_ranges)} selection range(s)")

        all_changes = []
        axis_changes = []

        with frozen_table_updates(self.ctx.table_widget):
            for sel_range in selected_ranges:
                logger.debug(
                    f"Selection range: rows {sel_range.topRow()}-{sel_range.bottomRow()}, "
                    f"cols {sel_range.leftColumn()}-{sel_range.rightColumn()}"
                )

                # Vertical: iterate columns, collect cells per column
                # Horizontal: iterate rows, collect cells per row
                if is_vertical:
                    outer_range = range(
                        sel_range.leftColumn(), sel_range.rightColumn() + 1
                    )
                    inner_range = range(sel_range.topRow(), sel_range.bottomRow() + 1)
                else:
                    outer_range = range(sel_range.topRow(), sel_range.bottomRow() + 1)
                    inner_range = range(
                        sel_range.leftColumn(), sel_range.rightColumn() + 1
                    )

                for line_idx in outer_range:
                    # Collect cells along this line (both data and axis)
                    cells = []
                    is_axis_line = None

                    for pos_idx in inner_range:
                        row = pos_idx if is_vertical else line_idx
                        col = line_idx if is_vertical else pos_idx
                        item = self.ctx.table_widget.item(row, col)
                        if item and item.data(Qt.UserRole) is not None:
                            coords = item.data(Qt.UserRole)
                            if isinstance(coords[0], str):
                                # Axis cell — only matching axis can be interpolated
                                if coords[0] == axis_key:
                                    if is_axis_line is None:
                                        is_axis_line = axis_key
                                    elif is_axis_line != axis_key:
                                        continue
                                    try:
                                        value = float(item.text())
                                        cells.append((pos_idx, value, coords))
                                    except ValueError:
                                        continue
                            else:
                                # Data cell
                                if is_axis_line is None:
                                    is_axis_line = False
                                elif is_axis_line is not False:
                                    continue
                                try:
                                    value = float(item.text())
                                    cells.append((pos_idx, value, coords))
                                except ValueError:
                                    continue

                    logger.debug(
                        f"{line_label} {line_idx}: found {len(cells)} cells (axis={is_axis_line})"
                    )

                    if len(cells) < 2:
                        logger.debug(
                            f"{line_label} {line_idx}: skipping (need at least 2 cells)"
                        )
                        continue

                    first_pos, first_val, first_coords = cells[0]
                    last_pos, last_val, last_coords = cells[-1]

                    logger.debug(
                        f"{line_label} {line_idx}: first cell at pos {first_pos} = {first_val}, "
                        f"last cell at pos {last_pos} = {last_val}"
                    )

                    if last_pos == first_pos:
                        logger.debug(
                            f"{line_label} {line_idx}: skipping (first and last at same position)"
                        )
                        continue

                    if len(cells) == 2 and last_pos - first_pos == 1:
                        logger.debug(
                            f"{line_label} {line_idx}: skipping (only 2 adjacent cells, nothing between)"
                        )
                        continue

                    # Interpolate all cells between first and last
                    cells_interpolated = 0
                    for pos in range(first_pos + 1, last_pos):
                        row = pos if is_vertical else line_idx
                        col = line_idx if is_vertical else pos
                        item = self.ctx.table_widget.item(row, col)
                        if not item or item.data(Qt.UserRole) is None:
                            logger.debug(
                                f"{line_label} {line_idx}, pos {pos}: skipping (not a data cell)"
                            )
                            continue

                        coords = item.data(Qt.UserRole)

                        try:
                            old_val = float(item.text())
                        except ValueError:
                            logger.debug(
                                f"{line_label} {line_idx}, pos {pos}: skipping (can't parse value)"
                            )
                            continue

                        # Linear interpolation
                        t = (pos - first_pos) / (last_pos - first_pos)
                        new_val = first_val + t * (last_val - first_val)

                        if abs(new_val - old_val) > 1e-9:
                            if is_axis_line == axis_key:
                                self._apply_axis_interpolation(
                                    item,
                                    coords,
                                    axis_type,
                                    axis_key,
                                    old_val,
                                    new_val,
                                    axis_changes,
                                )
                                cells_interpolated += 1
                            else:
                                self._apply_data_interpolation(
                                    item,
                                    coords,
                                    old_val,
                                    new_val,
                                    all_changes,
                                )
                                cells_interpolated += 1

                    logger.debug(
                        f"{line_label} {line_idx}: interpolated {cells_interpolated} cells"
                    )

            if all_changes:
                logger.info(
                    f"{direction_label} interpolation: updated {len(all_changes)} data cells"
                )
                self.ctx.viewer.bulk_changes.emit(all_changes)

            if axis_changes:
                logger.info(
                    f"{direction_label} interpolation: updated {len(axis_changes)} axis cells"
                )
                self.ctx.viewer.axis_bulk_changes.emit(axis_changes)

        if not all_changes and not axis_changes:
            logger.debug(f"{direction_label} interpolation: no changes made")

    def _apply_axis_interpolation(
        self, item, coords, axis_type, axis_key, old_val, new_val, axis_changes
    ):
        """Apply an interpolated value to an axis cell and record the change."""
        axis_table = self.ctx.current_table.get_axis(axis_type)
        if not axis_table:
            return

        data_idx = coords[1]
        old_raw = float(self.ctx.current_data[axis_key][data_idx])

        scaling = self.ctx.rom_definition.get_scaling(axis_table.scaling)
        if not scaling:
            return

        converter = ScalingConverter(scaling)
        new_raw = converter.from_display(new_val)
        new_val = converter.to_display(new_raw)

        self.ctx.editing_in_progress = True
        try:
            axis_fmt = self.display.get_axis_format(axis_type)
            item.setText(self.display.format_value(new_val, axis_fmt))
            self.ctx.current_data[axis_key][data_idx] = new_val
            color = self.display.get_axis_color(
                new_val,
                self.ctx.current_data[axis_key],
                axis_type,
            )
            item.setBackground(QBrush(color))
        finally:
            self.ctx.editing_in_progress = False

        axis_changes.append(
            (
                axis_key,
                data_idx,
                float(old_val),
                float(new_val),
                float(old_raw),
                float(new_raw),
            )
        )

    def _apply_data_interpolation(self, item, coords, old_val, new_val, all_changes):
        """Apply an interpolated value to a data cell and record the change."""
        values = self.ctx.current_data["values"]
        if values.ndim == 2:
            old_raw = values[coords[0], coords[1]]
        else:
            old_raw = values[coords[0]]

        scaling = self.ctx.rom_definition.get_scaling(self.ctx.current_table.scaling)
        if not scaling:
            return

        converter = ScalingConverter(scaling)
        new_raw = converter.from_display(new_val)
        new_val = converter.to_display(new_raw)

        self.ctx.editing_in_progress = True
        try:
            value_fmt = self.display.get_value_format()
            item.setText(self.display.format_value(new_val, value_fmt))
            if values.ndim == 2:
                values[coords[0], coords[1]] = new_val
            else:
                values[coords[0]] = new_val
            color = self.display.get_cell_color(
                new_val,
                values,
                coords[0],
                coords[1] if values.ndim == 2 else 0,
            )
            item.setBackground(QBrush(color))
        finally:
            self.ctx.editing_in_progress = False

        all_changes.append(
            (
                coords[0],
                coords[1] if len(coords) > 1 else 0,
                float(old_val),
                float(new_val),
                float(old_raw),
                float(new_raw),
            )
        )

    def interpolate_2d(self):
        """2D bilinear interpolation for 3D tables (B key)"""
        logger.debug("interpolate_2d() called")

        if not self.ctx.current_table or not self.ctx.current_data:
            logger.debug("No current table or data")
            return

        if not self._check_scaling_available():
            return

        # Only works for 3D tables
        if self.ctx.current_table.type != TableType.THREE_D:
            logger.debug("Not a 3D table")
            QMessageBox.warning(
                self.ctx.viewer,
                "Invalid Operation",
                "2D interpolation only works on 3D tables",
            )
            return

        selected_ranges = self.ctx.table_widget.selectedRanges()
        if not selected_ranges:
            logger.debug("No selection")
            return

        logger.debug(f"Processing {len(selected_ranges)} selection range(s)")

        all_changes = []

        with frozen_table_updates(self.ctx.table_widget):
            for sel_range in selected_ranges:
                top_row = sel_range.topRow()
                bottom_row = sel_range.bottomRow()
                left_col = sel_range.leftColumn()
                right_col = sel_range.rightColumn()

                logger.debug(
                    f"Selection range: rows {top_row}-{bottom_row}, cols {left_col}-{right_col}"
                )

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
                v00 = get_corner_value(top_row, left_col)  # Top-left
                v10 = get_corner_value(top_row, right_col)  # Top-right
                v01 = get_corner_value(bottom_row, left_col)  # Bottom-left
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
                            (1 - tx) * (1 - ty) * v00
                            + tx * (1 - ty) * v10
                            + (1 - tx) * ty * v01
                            + tx * ty * v11
                        )

                        try:
                            old_val = float(item.text())
                        except ValueError:
                            continue

                        if abs(new_val - old_val) > 1e-9:
                            self._apply_data_interpolation(
                                item,
                                coords,
                                old_val,
                                new_val,
                                all_changes,
                            )

                if all_changes:
                    logger.info(
                        f"2D bilinear interpolation: updated {len(all_changes)} cells"
                    )
                    self.ctx.viewer.bulk_changes.emit(all_changes)
                else:
                    logger.debug("2D bilinear interpolation: no changes made")
