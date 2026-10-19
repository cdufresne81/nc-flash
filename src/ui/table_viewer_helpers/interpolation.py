"""
Table Interpolation Helper

Handles interpolation algorithms for TableViewer.
"""

import logging
import math
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush
from PySide6.QtWidgets import QMessageBox

from ...core.rom_definition import TableType, AxisType
from ...core.rom_reader import ScalingConverter
from ...utils.formatting import (
    _get_format_precision,
    get_scaling_format,
    get_axis_format,
)
from ...utils.settings import get_settings
from .context import TableViewerContext, frozen_table_updates

if TYPE_CHECKING:
    from .display import TableDisplayHelper
    from .editing import TableEditHelper

logger = logging.getLogger(__name__)


def compute_interpolated_1d_values(
    first_pos,
    first_val,
    last_pos,
    last_val,
    auto_round=False,
    precision=2,
):
    """Compute linearly interpolated values for all integer positions between anchors.

    Args:
        first_pos: position of the first anchor.
        first_val: value at the first anchor.
        last_pos: position of the last anchor.
        last_val: value at the last anchor.
        auto_round: whether to round results to ``precision`` decimals.
        precision: number of decimal places for rounding.

    Returns:
        dict mapping position to interpolated value (excludes anchors).
    """
    if last_pos <= first_pos + 1:
        return {}

    result = {}
    for pos in range(first_pos + 1, last_pos):
        t = (pos - first_pos) / (last_pos - first_pos)
        new_val = first_val + t * (last_val - first_val)
        if auto_round:
            new_val = round(new_val, precision)
        result[pos] = new_val

    return result


def compute_interpolated_2d_values(
    v00,
    v10,
    v01,
    v11,
    rows,
    cols,
    auto_round=False,
    precision=2,
):
    """Compute bilinearly interpolated values over a grid from four corners.

    Args:
        v00: top-left corner value.
        v10: top-right corner value.
        v01: bottom-left corner value.
        v11: bottom-right corner value.
        rows: number of rows in the grid.
        cols: number of columns in the grid.
        auto_round: whether to round results to ``precision`` decimals.
        precision: number of decimal places for rounding.

    Returns:
        dict mapping (row, col) to interpolated value.
    """
    result = {}
    for row in range(rows):
        for col in range(cols):
            ty = row / (rows - 1) if rows > 1 else 0.0
            tx = col / (cols - 1) if cols > 1 else 0.0

            new_val = (
                (1 - tx) * (1 - ty) * v00
                + tx * (1 - ty) * v10
                + (1 - tx) * ty * v01
                + tx * ty * v11
            )

            if auto_round:
                new_val = round(new_val, precision)

            result[(row, col)] = new_val

    return result


class TableInterpolationHelper:
    """Helper class for interpolation operations"""

    def __init__(
        self,
        ctx: TableViewerContext,
        display: "TableDisplayHelper",
        edit: "TableEditHelper",
    ):
        self.ctx = ctx
        self.display = display
        self.edit = edit

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

        # Pre-compute formats and precisions for auto-round
        auto_round = get_settings().get_auto_round()
        if auto_round:
            data_fmt = get_scaling_format(
                self.ctx.rom_definition, self.ctx.current_table.scaling
            )
            axis_fmt_str = get_axis_format(
                self.ctx.rom_definition, self.ctx.current_table, axis_type
            )
            data_precision = _get_format_precision(data_fmt)
            axis_precision = _get_format_precision(axis_fmt_str)

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

                    # Pre-compute all interpolated values
                    interp_precision = (
                        (axis_precision if is_axis_line == axis_key else data_precision)
                        if auto_round
                        else 2
                    )
                    interp_values = compute_interpolated_1d_values(
                        first_pos,
                        first_val,
                        last_pos,
                        last_val,
                        auto_round=auto_round,
                        precision=interp_precision,
                    )

                    # Apply interpolated values to widget cells
                    cells_interpolated = 0
                    for pos, new_val in interp_values.items():
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

                        if abs(new_val - old_val) > 1e-9:
                            if is_axis_line == axis_key:
                                changed = self._apply_axis_interpolation(
                                    item,
                                    coords,
                                    axis_type,
                                    axis_key,
                                    new_val,
                                    axis_changes,
                                )
                            else:
                                changed = self._apply_data_interpolation(
                                    item,
                                    coords,
                                    new_val,
                                    all_changes,
                                )
                            cells_interpolated += int(changed)

                    logger.debug(
                        f"{line_label} {line_idx}: interpolated {cells_interpolated} cells"
                    )

            if all_changes:
                logger.debug(
                    f"{direction_label} interpolation: updated {len(all_changes)} data cells"
                )
                self.ctx.viewer.bulk_changes.emit(self.ctx.current_table, all_changes)

            if axis_changes:
                logger.info(
                    f"{direction_label} interpolation: updated {len(axis_changes)} axis cells"
                )
                self.ctx.viewer.axis_bulk_changes.emit(
                    self.ctx.current_table, axis_changes
                )

        if not all_changes and not axis_changes:
            logger.debug(f"{direction_label} interpolation: no changes made")

    def _stored_values(self, array, old_val, new_val, scaling, to_raw):
        """Raw/display values an interpolated cell will hold, or None to skip it.

        ``to_raw`` is the edit helper's display->raw converter, which returns
        None for a failed conversion (e.g. a stored inf under 100/(100+x)) or
        a raw value the storage type can't hold. A skipped cell is left
        completely alone: not redrawn, not stored, not recorded, so screen,
        memory and ROM stay in step.

        current_data holds DISPLAY values, so the old raw value goes back
        through the scaling like the new one (recording the display value as
        raw made undo/rollback write wrong bytes, issue 145).

        Returns:
            (old_raw, new_val, new_raw) or None.
        """
        old_raw = to_raw(old_val)
        new_raw = to_raw(new_val)
        if old_raw is None or new_raw is None or not math.isfinite(new_raw):
            return None
        try:
            new_val = float(ScalingConverter(scaling).to_display(new_raw))
        except Exception as e:
            logger.error(f"Error converting interpolated value to display: {e}")
            return None
        # Integer-dtype (identity-scaled) arrays truncate on assignment while
        # the ROM writer rounds; store what the ROM will hold.
        new_val = self.edit.fit_to_array(array, new_val)
        if not math.isfinite(new_val) or abs(new_val - old_val) < 1e-10:
            return None
        return float(old_raw), new_val, float(new_raw)

    def _apply_axis_interpolation(
        self, item, coords, axis_type, axis_key, new_val, axis_changes
    ) -> bool:
        """Apply an interpolated value to an axis cell and record the change.

        Returns True if the cell changed, False if it was skipped.
        """
        axis_table = self.ctx.current_table.get_axis(axis_type)
        if not axis_table:
            return False

        scaling = self.ctx.rom_definition.get_scaling(axis_table.scaling)
        if not scaling:
            return False

        axis_data = self.ctx.current_data[axis_key]
        data_idx = coords[1]
        old_val = float(axis_data[data_idx])
        stored = self._stored_values(
            axis_data,
            old_val,
            new_val,
            scaling,
            lambda v: self.edit._axis_display_to_raw(v, axis_table),
        )
        if stored is None:
            logger.debug(f"{axis_key}[{data_idx}]: skipping (value can't be stored)")
            return False
        old_raw, new_val, new_raw = stored

        self.ctx.editing_in_progress = True
        try:
            axis_fmt = self.display.get_axis_format(axis_type)
            item.setText(self.display.format_value(new_val, axis_fmt))
            axis_data[data_idx] = new_val
            color = self.display.get_axis_color(new_val, axis_data, axis_type)
            item.setBackground(QBrush(color))
        finally:
            self.ctx.editing_in_progress = False

        axis_changes.append((axis_key, data_idx, old_val, new_val, old_raw, new_raw))
        return True

    def _apply_data_interpolation(self, item, coords, new_val, all_changes) -> bool:
        """Apply an interpolated value to a data cell and record the change.

        Returns True if the cell changed, False if it was skipped.
        """
        values = self.ctx.current_data["values"]

        scaling = self.ctx.rom_definition.get_scaling(self.ctx.current_table.scaling)
        if not scaling:
            return False

        if values.ndim == 2:
            index = (coords[0], coords[1])
        else:
            index = coords[0]
        old_val = float(values[index])
        stored = self._stored_values(
            values, old_val, new_val, scaling, self.edit.display_to_raw
        )
        if stored is None:
            logger.debug(f"Cell {coords}: skipping (value can't be stored)")
            return False
        old_raw, new_val, new_raw = stored

        self.ctx.editing_in_progress = True
        try:
            value_fmt = self.display.get_value_format()
            item.setText(self.display.format_value(new_val, value_fmt))
            values[index] = new_val
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
                old_val,
                new_val,
                old_raw,
                new_raw,
            )
        )
        return True

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

        # Pre-compute format and precision for auto-round
        auto_round = get_settings().get_auto_round()
        if auto_round:
            data_fmt = get_scaling_format(
                self.ctx.rom_definition, self.ctx.current_table.scaling
            )
            data_precision = _get_format_precision(data_fmt)

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

                # Pre-compute all interpolated values
                grid_rows = bottom_row - top_row + 1
                grid_cols = right_col - left_col + 1
                interp_values = compute_interpolated_2d_values(
                    v00,
                    v10,
                    v01,
                    v11,
                    grid_rows,
                    grid_cols,
                    auto_round=auto_round,
                    precision=data_precision if auto_round else 2,
                )

                # Apply interpolated values to widget cells
                for row in range(top_row, bottom_row + 1):
                    for col in range(left_col, right_col + 1):
                        item = self.ctx.table_widget.item(row, col)
                        if not item or item.data(Qt.UserRole) is None:
                            continue

                        coords = item.data(Qt.UserRole)
                        new_val = interp_values[(row - top_row, col - left_col)]

                        try:
                            old_val = float(item.text())
                        except ValueError:
                            continue

                        if abs(new_val - old_val) > 1e-9:
                            self._apply_data_interpolation(
                                item,
                                coords,
                                new_val,
                                all_changes,
                            )

                if all_changes:
                    logger.debug(
                        f"2D bilinear interpolation: updated {len(all_changes)} cells"
                    )
                    self.ctx.viewer.bulk_changes.emit(
                        self.ctx.current_table, all_changes
                    )
                else:
                    logger.debug("2D bilinear interpolation: no changes made")
