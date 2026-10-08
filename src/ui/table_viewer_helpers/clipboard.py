"""
Table Clipboard Helper

Handles copy/paste operations for TableViewer.
"""

import csv
import json
import logging
import math
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Tuple

from PySide6.QtCore import QMimeData, Qt, QUrl
from PySide6.QtGui import QBrush, QDesktopServices
from PySide6.QtWidgets import QApplication

from ...utils.formatting import parse_cell_text
from .context import TableViewerContext, frozen_table_updates

if TYPE_CHECKING:
    from .display import TableDisplayHelper
    from .editing import TableEditHelper

logger = logging.getLogger(__name__)

# Exact cell values (JSON grid of floats / null) placed next to text/plain.
CELLS_MIME = "application/x-ncflash-cells"


class TableClipboardHelper:
    """Helper class for clipboard operations"""

    def __init__(
        self,
        ctx: TableViewerContext,
        display: "TableDisplayHelper",
        edit: "TableEditHelper",
    ):
        self.ctx = ctx
        self.display = display
        self.edit = edit

    def copy_selection(self):
        """Copy selected cells to clipboard as tab-separated values"""
        selected = self.ctx.table_widget.selectedRanges()
        if not selected:
            return

        # Get the bounding rectangle of selection
        min_row = min(r.topRow() for r in selected)
        max_row = max(r.bottomRow() for r in selected)
        min_col = min(r.leftColumn() for r in selected)
        max_col = max(r.rightColumn() for r in selected)

        self._put_on_clipboard(min_row, max_row, min_col, max_col)
        logger.debug(
            f"Copied {max_row - min_row + 1}x{max_col - min_col + 1} cells to clipboard"
        )

    def _put_on_clipboard(self, min_row, max_row, min_col, max_col):
        """Put a block of cells on the clipboard, twice over.

        text/plain carries the displayed text (for Excel and other apps).
        CELLS_MIME carries the exact stored values: the displayed text is
        rounded to the scaling format, so pasting it back would silently
        change the ROM (e.g. a 0.0625 breakpoint shown as 0.062).
        """
        rows_text = []
        rows_exact = []
        for row in range(min_row, max_row + 1):
            row_values = []
            row_exact = []
            for col in range(min_col, max_col + 1):
                item = self.ctx.table_widget.item(row, col)
                row_values.append(item.text() if item else "")
                row_exact.append(
                    self._stored_value(item.data(Qt.UserRole)) if item else None
                )
            rows_text.append("\t".join(row_values))
            rows_exact.append(row_exact)

        mime = QMimeData()
        mime.setText("\n".join(rows_text))
        mime.setData(CELLS_MIME, json.dumps(rows_exact).encode("utf-8"))
        QApplication.clipboard().setMimeData(mime)

    def _stored_value(self, coords) -> Optional[float]:
        """Exact stored display value behind a cell's Qt.UserRole coords."""
        if coords is None:
            return None
        if isinstance(coords[0], str):
            axis_data = self.ctx.current_data.get(coords[0])
            return None if axis_data is None else float(axis_data[coords[1]])
        values = self.ctx.current_data["values"]
        if values.ndim == 1:
            return float(values[coords[0]])
        return float(values[coords[0], coords[1]])

    @staticmethod
    def _read_clipboard() -> Tuple[Optional[list], bool]:
        """Return (rows, exact).

        exact=True: rows of floats/None from an NC Flash copy (CELLS_MIME).
        exact=False: rows of tab-separated text cells from any other source.
        """
        mime = QApplication.clipboard().mimeData()
        if mime is not None and mime.hasFormat(CELLS_MIME):
            try:
                rows = json.loads(bytes(mime.data(CELLS_MIME)).decode("utf-8"))
                if isinstance(rows, list) and all(isinstance(r, list) for r in rows):
                    return rows, True
            except (ValueError, UnicodeDecodeError):
                pass
            logger.warning("Ignoring unreadable NC Flash clipboard data")

        text = QApplication.clipboard().text()
        if not text:
            return None, False
        # Strip only line breaks: a leading tab is the empty corner cell of a
        # copied 3D table, and stripping it shifts the whole X-axis row left.
        lines = text.strip("\r\n").split("\n")
        return [line.rstrip("\r").split("\t") for line in lines], False

    def _pasted_value(self, source, exact: bool, item, coords) -> Optional[float]:
        """Value to paste into one cell, or None to leave the cell alone."""
        if exact:
            # Exact value from an NC Flash copy (None = no data at the source)
            if isinstance(source, bool) or not isinstance(source, (int, float)):
                return None
            try:
                value = float(source)
            except OverflowError:
                return None
            return value if math.isfinite(value) else None

        # Parse in the cell's own display format (hex cells take hex) - skip
        # anything that is not a finite number
        fmt = self.display.get_cell_format(coords)
        value = parse_cell_text(source, fmt)
        # Text that reads as the number the cell already shows ("0" for
        # "0.00", e.g. back from Excel) changes nothing: the text is rounded,
        # so applying it would overwrite the exact stored value.
        if value is not None and value == parse_cell_text(item.text(), fmt):
            return None
        return value

    def paste_selection(self):
        """Paste clipboard content into selected cells"""
        if self.ctx.read_only:
            return

        rows_data, exact = self._read_clipboard()
        if not rows_data:
            return

        # Get current selection start
        selected = self.ctx.table_widget.selectedRanges()
        if not selected:
            return

        start_row = min(r.topRow() for r in selected)
        start_col = min(r.leftColumn() for r in selected)

        # Paste values
        changes_made = []
        axis_changes = []

        with frozen_table_updates(self.ctx.table_widget):
            for row_offset, row_values in enumerate(rows_data):
                for col_offset, source in enumerate(row_values):
                    target_row = start_row + row_offset
                    target_col = start_col + col_offset

                    # Check bounds
                    if target_row >= self.ctx.table_widget.rowCount():
                        continue
                    if target_col >= self.ctx.table_widget.columnCount():
                        continue

                    item = self.ctx.table_widget.item(target_row, target_col)
                    if not item:
                        continue

                    data_indices = item.data(Qt.UserRole)
                    if data_indices is None:
                        continue  # No data behind this cell

                    new_value = self._pasted_value(source, exact, item, data_indices)
                    if new_value is None:
                        continue

                    # Axis cell: ('x_axis' | 'y_axis', index)
                    if isinstance(data_indices[0], str):
                        change = self.edit.apply_axis_value(
                            item, data_indices[0], data_indices[1], new_value
                        )
                        if change is not None:
                            axis_changes.append(change)
                        continue

                    data_row, data_col = data_indices

                    # Get old value
                    values = self.ctx.current_data["values"]
                    if values.ndim == 1:
                        old_value = float(values[data_row])
                    else:
                        old_value = float(values[data_row, data_col])
                    new_value = self.edit.fit_to_array(values, new_value)

                    # Skip if no change
                    if abs(new_value - old_value) < 1e-10:
                        continue

                    # Note: intentionally no scaling min/max clamp here. The
                    # XML-declared min/max is unreliable (some definitions use
                    # min=0/max=0 as placeholders, and sibling tables legitimately
                    # hold raw bytes outside the stated range). display_to_raw
                    # below is the real safety net — it rejects values that
                    # cannot be encoded in the storage type.

                    # Convert to raw values
                    old_raw = self.edit.display_to_raw(old_value)
                    new_raw = self.edit.display_to_raw(new_value)
                    if old_raw is None or new_raw is None:
                        continue

                    # Update the internal data
                    if values.ndim == 1:
                        self.ctx.current_data["values"][data_row] = new_value
                    else:
                        self.ctx.current_data["values"][data_row, data_col] = new_value

                    # Update cell display
                    self.ctx.editing_in_progress = True
                    try:
                        value_fmt = self.display.get_value_format()
                        item.setText(self.display.format_value(new_value, value_fmt))
                        color = self.display.get_cell_color(
                            new_value,
                            self.ctx.current_data["values"],
                            data_row,
                            data_col,
                        )
                        item.setBackground(QBrush(color))
                    finally:
                        self.ctx.editing_in_progress = False

                        # Record change for signaling
                        changes_made.append(
                            (data_row, data_col, old_value, new_value, old_raw, new_raw)
                        )

            # Emit single bulk signal for atomic undo (matches operations.py pattern)
            if changes_made:
                self.ctx.viewer.bulk_changes.emit(self.ctx.current_table, changes_made)
            if axis_changes:
                self.ctx.viewer.axis_bulk_changes.emit(
                    self.ctx.current_table, axis_changes
                )
            if changes_made or axis_changes:
                logger.debug(
                    f"Pasted {len(changes_made)} cell(s), "
                    f"{len(axis_changes)} axis cell(s)"
                )

    def copy_table_to_clipboard(self):
        """Copy entire table to clipboard as tab-separated values (for Excel)"""
        if not self.ctx.table_widget:
            return

        row_count = self.ctx.table_widget.rowCount()
        col_count = self.ctx.table_widget.columnCount()

        if row_count == 0 or col_count == 0:
            return

        self._put_on_clipboard(0, row_count - 1, 0, col_count - 1)

        table_name = self.ctx.current_table.name if self.ctx.current_table else "table"
        logger.info(
            f"Copied entire table '{table_name}' ({row_count}x{col_count}) to clipboard"
        )

    def export_to_csv(self, rom_path: str = None):
        """
        Export table to CSV file and open with default application

        Args:
            rom_path: Path to ROM file (used to determine export directory)
        """
        if not self.ctx.table_widget or not self.ctx.current_table:
            return

        # Determine export directory
        from ...utils.settings import get_settings

        configured_dir = get_settings().get_export_directory()
        if configured_dir:
            export_dir = Path(configured_dir)
        elif rom_path:
            export_dir = Path(rom_path).parent / "export"
        else:
            export_dir = Path.cwd() / "export"

        # Create export directory if it doesn't exist
        export_dir.mkdir(parents=True, exist_ok=True)

        # Build filename: romid_tablename.csv
        rom_id = "unknown"
        if self.ctx.rom_definition and self.ctx.rom_definition.romid:
            rom_id = self.ctx.rom_definition.romid.xmlid or "unknown"

        # Sanitize table name for filename
        table_name = self.ctx.current_table.name
        safe_table_name = "".join(
            c if c.isalnum() or c in (" ", "-", "_") else "_" for c in table_name
        )
        safe_table_name = safe_table_name.replace(" ", "_")

        filename = f"{rom_id}_{safe_table_name}.csv"
        filepath = export_dir / filename

        # Write CSV file
        row_count = self.ctx.table_widget.rowCount()
        col_count = self.ctx.table_widget.columnCount()

        try:
            with open(filepath, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                for row in range(row_count):
                    row_values = []
                    for col in range(col_count):
                        item = self.ctx.table_widget.item(row, col)
                        if item:
                            row_values.append(item.text())
                        else:
                            row_values.append("")
                    writer.writerow(row_values)

            logger.info(f"Exported table to: {filepath}")

            # Open with default application
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(filepath)))

        except Exception as e:
            logger.error(f"Failed to export table to CSV: {e}")
