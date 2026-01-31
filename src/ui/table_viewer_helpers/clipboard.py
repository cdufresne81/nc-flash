"""
Table Clipboard Helper

Handles copy/paste operations for TableViewer.
"""

import logging
import os
import csv
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QBrush, QDesktopServices
from PySide6.QtWidgets import QApplication

from .context import TableViewerContext

if TYPE_CHECKING:
    from .display import TableDisplayHelper
    from .editing import TableEditHelper

logger = logging.getLogger(__name__)


class TableClipboardHelper:
    """Helper class for clipboard operations"""

    def __init__(self, ctx: TableViewerContext, display: 'TableDisplayHelper', edit: 'TableEditHelper'):
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

        # Build tab-separated string
        rows_text = []
        for row in range(min_row, max_row + 1):
            row_values = []
            for col in range(min_col, max_col + 1):
                item = self.ctx.table_widget.item(row, col)
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
        if self.ctx.read_only:
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
        selected = self.ctx.table_widget.selectedRanges()
        if not selected:
            return

        start_row = min(r.topRow() for r in selected)
        start_col = min(r.leftColumn() for r in selected)

        # Paste values
        changes_made = []

        # Disable widget updates during bulk paste to prevent repaints on every cell
        self.ctx.table_widget.setUpdatesEnabled(False)
        try:
            for row_offset, row_values in enumerate(rows_data):
                for col_offset, value_text in enumerate(row_values):
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
                    values = self.ctx.current_data['values']
                    if values.ndim == 1:
                        old_value = float(values[data_row])
                    else:
                        old_value = float(values[data_row, data_col])

                    # Skip if no change
                    if abs(new_value - old_value) < 1e-10:
                        continue

                    # Validate against scaling if available
                    if self.ctx.rom_definition and self.ctx.current_table.scaling:
                        scaling = self.ctx.rom_definition.get_scaling(self.ctx.current_table.scaling)
                        if scaling:
                            if scaling.min is not None and new_value < scaling.min:
                                continue
                            if scaling.max is not None and new_value > scaling.max:
                                continue

                    # Convert to raw values
                    old_raw = self.edit.display_to_raw(old_value)
                    new_raw = self.edit.display_to_raw(new_value)
                    if old_raw is None or new_raw is None:
                        continue

                    # Update the internal data
                    if values.ndim == 1:
                        self.ctx.current_data['values'][data_row] = new_value
                    else:
                        self.ctx.current_data['values'][data_row, data_col] = new_value

                    # Update cell display
                    self.ctx.editing_in_progress = True
                    try:
                        value_fmt = self.display.get_value_format()
                        item.setText(self.display.format_value(new_value, value_fmt))
                        color = self.display.get_cell_color(new_value, self.ctx.current_data['values'], data_row, data_col)
                        item.setBackground(QBrush(color))
                    finally:
                        self.ctx.editing_in_progress = False

                        # Record change for signaling
                        changes_made.append((data_row, data_col, old_value, new_value, old_raw, new_raw))

            # Emit signals for all changes (use address as unique identifier)
            for data_row, data_col, old_value, new_value, old_raw, new_raw in changes_made:
                self.ctx.viewer.cell_changed.emit(
                    self.ctx.current_table.address,
                    data_row, data_col,
                    old_value, new_value,
                    old_raw, new_raw
                )

            if changes_made:
                logger.debug(f"Pasted {len(changes_made)} cell(s)")
        finally:
            # Re-enable updates and trigger a single repaint
            self.ctx.table_widget.setUpdatesEnabled(True)
            self.ctx.table_widget.viewport().update()

    def copy_table_to_clipboard(self):
        """Copy entire table to clipboard as tab-separated values (for Excel)"""
        if not self.ctx.table_widget:
            return

        row_count = self.ctx.table_widget.rowCount()
        col_count = self.ctx.table_widget.columnCount()

        if row_count == 0 or col_count == 0:
            return

        # Build tab-separated string for entire table
        rows_text = []
        for row in range(row_count):
            row_values = []
            for col in range(col_count):
                item = self.ctx.table_widget.item(row, col)
                if item:
                    row_values.append(item.text())
                else:
                    row_values.append("")
            rows_text.append("\t".join(row_values))

        clipboard_text = "\n".join(rows_text)
        QApplication.clipboard().setText(clipboard_text)

        table_name = self.ctx.current_table.name if self.ctx.current_table else "table"
        logger.info(f"Copied entire table '{table_name}' ({row_count}x{col_count}) to clipboard")

    def export_to_csv(self, rom_path: str = None):
        """
        Export table to CSV file and open with default application

        Args:
            rom_path: Path to ROM file (used to determine export directory)
        """
        if not self.ctx.table_widget or not self.ctx.current_table:
            return

        # Determine export directory (next to ROM file, or current directory)
        if rom_path:
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
        safe_table_name = "".join(c if c.isalnum() or c in (' ', '-', '_') else '_' for c in table_name)
        safe_table_name = safe_table_name.replace(' ', '_')

        filename = f"{rom_id}_{safe_table_name}.csv"
        filepath = export_dir / filename

        # Write CSV file
        row_count = self.ctx.table_widget.rowCount()
        col_count = self.ctx.table_widget.columnCount()

        try:
            with open(filepath, 'w', newline='', encoding='utf-8') as f:
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
