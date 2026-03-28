"""
Table Viewer Widget

Displays table data in a grid view with gradient coloring and axis labels.
Supports cell editing with change tracking.

This class uses composition with helper classes:
- TableDisplayHelper: Rendering and formatting
- TableEditHelper: Cell editing and validation
- TableOperationsHelper: Bulk data operations
- TableInterpolationHelper: Interpolation algorithms
- TableClipboardHelper: Copy/paste operations
"""

import logging

import numpy as np
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QTableWidget,
    QHeaderView,
    QFrame,
)
from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QColor, QKeySequence, QShortcut, QPainter, QFontMetrics

from ..core.rom_definition import Table, TableType, RomDefinition
from ..utils.settings import get_settings
from .widgets.toggle_switch import ToggleSwitch
from .table_viewer_helpers import (
    TableViewerContext,
    TableDisplayHelper,
    TableEditHelper,
    TableOperationsHelper,
    TableInterpolationHelper,
    TableClipboardHelper,
    ModifiedCellDelegate,
)

logger = logging.getLogger(__name__)


class RotatedLabel(QLabel):
    """QLabel with text rotated 90° counter-clockwise"""

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignCenter)

    def paintEvent(self, event):
        """Paint the label with rotated text"""
        painter = QPainter(self)
        painter.rotate(-90)

        # Calculate position after rotation
        # Text will be drawn from bottom-left going upward
        fm = QFontMetrics(self.font())
        text_width = fm.horizontalAdvance(self.text())
        text_height = fm.height()

        # Center the text
        x = -(self.height() + text_width) // 2
        y = (self.width() + text_height) // 2 - fm.descent()

        painter.drawText(x, y, self.text())
        painter.end()

    def sizeHint(self):
        """Return preferred size (width/height swapped for rotated text)"""
        fm = QFontMetrics(self.font())
        # Swap width and height since text is rotated
        return QSize(fm.height() + 4, fm.horizontalAdvance(self.text()) + 10)

    def minimumSizeHint(self):
        """Return minimum size"""
        return self.sizeHint()


class TableViewer(QWidget):
    """Widget for viewing and editing table data with gradient coloring"""

    # Signal emitted when a cell value changes
    # Args: table_address, row, col, old_value, new_value, old_raw, new_raw
    cell_changed = Signal(str, int, int, float, float, float, float)

    # Signal emitted when bulk operation completes (for single undo)
    # Args: list of (row, col, old_value, new_value, old_raw, new_raw) tuples
    bulk_changes = Signal(list)

    # Signal emitted when an axis cell value changes
    # Args: table_address, axis_type ('x_axis' or 'y_axis'), index, old_value, new_value, old_raw, new_raw
    axis_changed = Signal(str, str, int, float, float, float, float)

    # Signal emitted when axis bulk operation completes (for single undo)
    # Args: list of (axis_type, index, old_value, new_value, old_raw, new_raw) tuples
    axis_bulk_changes = Signal(list)

    # Signal emitted when cell is updated programmatically (e.g., undo/redo)
    # Used to notify parent that data has changed and graph should refresh
    data_updated = Signal()

    def __init__(
        self,
        rom_definition: RomDefinition = None,
        parent=None,
        modified_cells_dict: dict = None,
        original_values_dict: dict = None,
        diff_mode: bool = False,
        diff_base_data: dict = None,
    ):
        super().__init__(parent)
        self.rom_definition = rom_definition
        self._editing_in_progress = False
        self._read_only = False
        # Use shared dict from main window (persists across window close/reopen)
        # If not provided, create local dict (for testing/standalone usage)
        self._modified_cells = (
            modified_cells_dict if modified_cells_dict is not None else {}
        )
        self._original_values = (
            original_values_dict if original_values_dict is not None else {}
        )
        # Diff mode for viewing historical changes
        self._diff_mode = diff_mode
        self._diff_base_data = (
            diff_base_data  # Data from previous version to compare against
        )
        self._show_diff_highlights = True  # Toggle for diff highlighting
        self.init_ui()

        # Create context and helpers
        self._ctx = TableViewerContext(
            viewer=self,
            table_widget=self.table_widget,
            rom_definition=rom_definition,
        )
        self._display = TableDisplayHelper(self._ctx)
        self._edit = TableEditHelper(self._ctx, self._display)
        self._ops = TableOperationsHelper(self._ctx, self._display, self._edit)
        self._interp = TableInterpolationHelper(self._ctx, self._display)
        self._clipboard = TableClipboardHelper(self._ctx, self._display, self._edit)

        # Set up delegate for modified cell borders
        self._delegate = ModifiedCellDelegate(self)
        self.table_widget.setItemDelegate(self._delegate)

        # Connect signals to track modifications
        self.cell_changed.connect(self._on_cell_changed_track_modifications)
        self.bulk_changes.connect(self._on_bulk_changes_track_modifications)
        self.axis_changed.connect(self._on_axis_changed_track_modifications)
        self.axis_bulk_changes.connect(self._on_axis_bulk_changes_track_modifications)

    @property
    def current_table(self):
        """Get current table from context"""
        return self._ctx.current_table if hasattr(self, "_ctx") else None

    @current_table.setter
    def current_table(self, value):
        """Set current table in context"""
        if hasattr(self, "_ctx"):
            self._ctx.current_table = value

    @property
    def current_data(self):
        """Get current data from context"""
        return self._ctx.current_data if hasattr(self, "_ctx") else None

    @current_data.setter
    def current_data(self, value):
        """Set current data in context"""
        if hasattr(self, "_ctx"):
            self._ctx.current_data = value

    def init_ui(self):
        """Initialize the user interface"""
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        self.setLayout(main_layout)

        # X-axis label (horizontal, centered above table, initially hidden)
        self.x_axis_label = QLabel("")
        self.x_axis_label.setAlignment(Qt.AlignCenter)
        font = self.x_axis_label.font()
        font.setBold(True)
        font.setPointSize(10)
        self.x_axis_label.setFont(font)
        self.x_axis_label.setVisible(
            False
        )  # Hidden by default, shown only for 3D tables
        main_layout.addWidget(self.x_axis_label)

        # Horizontal layout for Y-axis label and table
        table_layout = QHBoxLayout()
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.setSpacing(2)

        # Y-axis label (rotated 90° counter-clockwise, initially hidden)
        self.y_axis_label = RotatedLabel("")
        font = self.y_axis_label.font()
        font.setBold(True)
        font.setPointSize(10)
        self.y_axis_label.setFont(font)
        self.y_axis_label.setVisible(
            False
        )  # Hidden by default, shown only for 3D tables
        table_layout.addWidget(self.y_axis_label)

        # Table widget for displaying data
        self.table_widget = QTableWidget()
        # Start with ResizeToContents, will be changed to uniform width after data load
        self.table_widget.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeToContents
        )
        self.table_widget.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeToContents
        )
        self.table_widget.verticalHeader().setVisible(False)  # Hide row numbers
        self.table_widget.setShowGrid(True)
        self.table_widget.setGridStyle(Qt.SolidLine)

        # Apply compact styling from settings
        # Note: _apply_table_style is called before helpers are created,
        # so we call it directly here. After init, use self._display.apply_table_style()
        self._apply_table_style_internal()

        # Connect to cell changed signal for editing
        self.table_widget.cellChanged.connect(self._on_cell_changed)

        # Set up copy/paste shortcuts (not in menu, widget-level only)
        copy_shortcut = QShortcut(QKeySequence.Copy, self.table_widget)
        copy_shortcut.activated.connect(self.copy_selection)
        paste_shortcut = QShortcut(QKeySequence.Paste, self.table_widget)
        paste_shortcut.activated.connect(self.paste_selection)

        # Set up select all shortcut (not in menu, widget-level only)
        select_all_shortcut = QShortcut(QKeySequence.SelectAll, self.table_widget)
        select_all_shortcut.activated.connect(self.select_all_data)

        # Note: All data manipulation shortcuts (+, -, *, =, V, H, B) are defined
        # in TableViewerWindow menu to avoid duplicate shortcut conflicts

        table_layout.addWidget(self.table_widget)
        main_layout.addLayout(table_layout)

        # Toggle switch container (for binary ON/OFF categories like DTC flags)
        self._init_toggle_ui(main_layout)

    def _init_toggle_ui(self, parent_layout: QVBoxLayout):
        """Initialize the toggle switch container (hidden by default)"""
        self.toggle_container = QFrame()
        self.toggle_container.setVisible(False)

        toggle_layout = QVBoxLayout()
        toggle_layout.setAlignment(Qt.AlignCenter)
        toggle_layout.setContentsMargins(10, 10, 10, 10)
        toggle_layout.setSpacing(8)
        self.toggle_container.setLayout(toggle_layout)

        # Toggle switch
        self.toggle_switch = ToggleSwitch()
        toggle_switch_row = QHBoxLayout()
        toggle_switch_row.setAlignment(Qt.AlignCenter)
        toggle_switch_row.addWidget(self.toggle_switch)
        toggle_layout.addLayout(toggle_switch_row)

        # Status label ("ON" / "OFF")
        self.toggle_label = QLabel("OFF")
        self.toggle_label.setAlignment(Qt.AlignCenter)
        font = self.toggle_label.font()
        font.setBold(True)
        font.setPointSize(12)
        self.toggle_label.setFont(font)
        toggle_layout.addWidget(self.toggle_label)

        parent_layout.addWidget(self.toggle_container)

        # Track the original non-zero value for restoring on toggle ON
        # (e.g., P1260 stores 3 instead of 1)
        self._toggle_original_nonzero_value = 1.0

        # Connect toggle signal
        self.toggle_switch.toggled.connect(self._on_toggle_changed)

    def _on_toggle_changed(self, checked: bool):
        """Handle toggle switch state change from user interaction"""
        if self._editing_in_progress or self._read_only:
            return
        if not self._ctx.current_table or not self._ctx.current_data:
            return

        values = self._ctx.current_data["values"]
        old_value = float(values[0])

        if checked:
            new_value = (
                self._toggle_original_nonzero_value
                if self._toggle_original_nonzero_value != 0
                else 1.0
            )
        else:
            new_value = 0.0

        if abs(new_value - old_value) < 1e-10:
            return

        # Convert display values to raw
        old_raw = self._edit.display_to_raw(old_value)
        new_raw = self._edit.display_to_raw(new_value)
        if old_raw is None or new_raw is None:
            return

        # Update internal data
        self._ctx.current_data["values"][0] = new_value

        # Update the hidden table item for consistency
        self._editing_in_progress = True
        try:
            item = self.table_widget.item(0, 0)
            if item:
                value_fmt = self._display.get_value_format()
                item.setText(self._display.format_value(new_value, value_fmt))
            self._update_toggle_label(checked)
        finally:
            self._editing_in_progress = False

        # Emit change signal
        self.cell_changed.emit(
            self._ctx.current_table.address,
            0,
            0,
            old_value,
            new_value,
            old_raw,
            new_raw,
        )

        logger.debug(
            f"Toggle changed: {self._ctx.current_table.name} {old_value} -> {new_value}"
        )

    def _update_toggle_label(self, checked: bool):
        """Update the toggle status label text and color"""
        if checked:
            self.toggle_label.setText("ON")
            self.toggle_label.setStyleSheet("color: #4CAF50;")
        else:
            self.toggle_label.setText("OFF")
            self.toggle_label.setStyleSheet("color: #B0B0B0;")

    def set_read_only(self, read_only: bool):
        """Set whether the table is read-only"""
        self._read_only = read_only

    # --- Diff Mode Methods ---

    def is_diff_mode(self) -> bool:
        """Check if viewer is in diff mode"""
        return self._diff_mode

    def is_cell_changed_from_base(self, data_row: int, data_col: int) -> bool:
        """
        Check if a cell value differs from the base version

        Args:
            data_row: Row index in data array
            data_col: Column index in data array

        Returns:
            True if cell value differs from base
        """
        if not self._diff_mode or self._diff_base_data is None:
            return False

        if self.current_data is None:
            return False

        try:
            current_values = self.current_data.get("values")
            base_values = self._diff_base_data.get("values")

            if current_values is None or base_values is None:
                return False

            current_value = current_values[data_row, data_col]
            base_value = base_values[data_row, data_col]

            # Compare with tolerance for floating point
            return abs(current_value - base_value) >= 1e-10
        except (IndexError, TypeError):
            return False

    def get_base_value(self, data_row: int, data_col: int) -> float:
        """
        Get the value from the base version for a cell

        Args:
            data_row: Row index in data array
            data_col: Column index in data array

        Returns:
            Base value, or None if not available
        """
        if self._diff_base_data is None:
            return None

        try:
            base_values = self._diff_base_data.get("values")
            if base_values is not None:
                return base_values[data_row, data_col]
        except (IndexError, TypeError):
            pass
        return None

    def toggle_diff_highlights(self):
        """Toggle visibility of diff highlighting"""
        self._show_diff_highlights = not self._show_diff_highlights
        self.table_widget.viewport().update()

    def show_diff_highlights(self) -> bool:
        """Check if diff highlights should be shown"""
        return self._diff_mode and self._show_diff_highlights

    def _apply_table_style(self):
        """Apply table styling - delegates to helper if available"""
        if hasattr(self, "_display"):
            self._display.apply_table_style()
        else:
            self._apply_table_style_internal()

    def _apply_table_style_internal(self):
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
            QTableWidget::item:selected {{
                background-color: #0078D7;
                color: white;
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
        self._display.display_table(table, data)

        # Select the first data cell so the table is ready for keyboard navigation
        if table.type == TableType.THREE_D:
            self.table_widget.setCurrentCell(1, 1)
        elif table.type == TableType.TWO_D:
            self.table_widget.setCurrentCell(0, 1)
        else:
            self.table_widget.setCurrentCell(0, 0)

        # Apply diff tooltips if in diff mode
        if self._diff_mode and self._diff_base_data is not None:
            self._apply_diff_tooltips()

    def _apply_diff_tooltips(self):
        """Apply tooltips to cells that differ from base version"""
        if not self._diff_mode or self._diff_base_data is None:
            return

        base_values = self._diff_base_data.get("values")
        if base_values is None:
            return

        current_values = self.current_data.get("values") if self.current_data else None
        if current_values is None:
            return

        value_fmt = self._display.get_value_format()

        # Only iterate over cells that exist in the diff base data
        if base_values.ndim == 1:
            rows, cols = len(base_values), 1
        else:
            rows, cols = base_values.shape

        for data_row in range(rows):
            for data_col in range(cols):
                try:
                    if base_values.ndim == 1:
                        base_value = base_values[data_row]
                        current_value = current_values[data_row]
                    else:
                        base_value = base_values[data_row, data_col]
                        current_value = current_values[data_row, data_col]
                except (IndexError, TypeError):
                    continue

                if abs(current_value - base_value) < 1e-10:
                    continue

                # Find the UI cell for these data coordinates
                ui_row, ui_col = self._data_to_ui_coords(data_row, data_col)
                item = self.table_widget.item(ui_row, ui_col)
                if item is not None:
                    item.setToolTip(
                        f"Previous: {self._display.format_value(base_value, value_fmt)}"
                    )

    def clear(self):
        """Clear the viewer"""
        self._display.clear()

    def _on_cell_changed(self, row: int, col: int):
        """Handle cell value change from user edit"""
        self._edit.on_cell_changed(row, col)

    def _display_to_raw(self, display_value: float) -> float:
        """Convert display value to raw binary value using scaling"""
        return self._edit.display_to_raw(display_value)

    def update_cell_value(self, data_row: int, data_col: int, new_value: float):
        """Update a cell's value programmatically (for undo/redo)"""
        self._edit.update_cell_value(data_row, data_col, new_value)
        # Skip per-cell signal during bulk operations (emitted once in end_bulk_update)
        if not hasattr(self, "_bulk_update_state"):
            self.data_updated.emit()

    def update_axis_cell_value(self, axis_type: str, data_idx: int, new_value: float):
        """Update an axis cell's value programmatically (for undo/redo)"""
        self._edit.update_axis_cell_value(axis_type, data_idx, new_value)
        # Skip per-cell signal during bulk operations (emitted once in end_bulk_update)
        if not hasattr(self, "_bulk_update_state"):
            self.data_updated.emit()

    def begin_bulk_update(self):
        """
        Prepare table for bulk cell updates - disables expensive per-cell operations.

        Call this before applying multiple cell updates (e.g., bulk undo/redo).
        Must be paired with end_bulk_update().
        """
        # Save current state
        self._bulk_update_state = {
            "updates_enabled": self.table_widget.updatesEnabled(),
            "signals_blocked": self.table_widget.signalsBlocked(),
        }

        # Disable updates and signals to prevent per-cell repaints
        self.table_widget.setUpdatesEnabled(False)
        self.table_widget.blockSignals(True)

        # Delegate header mode saving and value caching to display helper
        self._display.begin_bulk_update()

    def end_bulk_update(self):
        """
        Complete bulk update - restores state and triggers single repaint.

        Must be called after begin_bulk_update() to restore normal operation.
        Safe to call even if begin_bulk_update() wasn't called - ensures clean state.
        """
        # Restore display helper state (header modes, value cache)
        self._display.end_bulk_update()

        # Restore table widget state
        if hasattr(self, "_bulk_update_state"):
            self.table_widget.blockSignals(
                self._bulk_update_state.get("signals_blocked", False)
            )
            self.table_widget.setUpdatesEnabled(
                self._bulk_update_state.get("updates_enabled", True)
            )
            del self._bulk_update_state
        else:
            # Safety: ensure signals and updates are enabled even if begin wasn't called
            self.table_widget.blockSignals(False)
            self.table_widget.setUpdatesEnabled(True)

        # Single repaint for all changes
        self.table_widget.viewport().update()

        # Single data_updated signal for graph refresh after all bulk changes
        self.data_updated.emit()

    def _data_to_ui_coords(self, data_row: int, data_col: int) -> tuple:
        """Convert data coordinates to UI table coordinates"""
        return self._edit.data_to_ui_coords(data_row, data_col)

    def copy_selection(self):
        """Copy selected cells to clipboard as tab-separated values"""
        self._clipboard.copy_selection()

    def paste_selection(self):
        """Paste clipboard content into selected cells"""
        self._clipboard.paste_selection()

    def copy_table_to_clipboard(self):
        """Copy entire table to clipboard as tab-separated values (for Excel)"""
        self._clipboard.copy_table_to_clipboard()

    def export_to_csv(self, rom_path: str = None):
        """Export table to CSV file and open with default application"""
        self._clipboard.export_to_csv(rom_path)

    def _apply_bulk_operation(self, operation_fn, operation_name: str):
        """Apply an operation to all selected data cells"""
        return self._ops.apply_bulk_operation(operation_fn, operation_name)

    def increment_selection(self):
        """Increment selected cells by fixed amount"""
        self._ops.increment_selection()

    def decrement_selection(self):
        """Decrement selected cells by fixed amount"""
        self._ops.decrement_selection()

    def add_to_selection(self):
        """Add custom value to selected cells (dialog)"""
        self._ops.add_to_selection()

    def multiply_selection(self):
        """Multiply selected cells by factor (dialog)"""
        self._ops.multiply_selection()

    def set_value_selection(self):
        """Set all selected cells to value (dialog)"""
        self._ops.set_value_selection()

    def smooth_selection(self):
        """Apply light smoothing to selected data cells"""
        self._ops.smooth_selection()

    def round_selection(self):
        """Round selected cells one decimal level coarser"""
        self._ops.round_selection()

    def select_all_data(self):
        """Select all data cells (excluding axes)"""
        self._ops.select_all_data()

    def interpolate_vertical(self):
        """Fill gaps vertically with linear interpolation (V key)"""
        self._interp.interpolate_vertical()

    def interpolate_horizontal(self):
        """Fill gaps horizontally with linear interpolation (H key)"""
        self._interp.interpolate_horizontal()

    def interpolate_2d(self):
        """2D bilinear interpolation for 3D tables (B key)"""
        self._interp.interpolate_2d()

    # Modified cell tracking methods

    def is_cell_modified(self, ui_row: int, ui_col: int) -> bool:
        """
        Check if a cell is modified (for delegate painting)

        Args:
            ui_row: UI row coordinate
            ui_col: UI column coordinate

        Returns:
            True if cell has been modified during this session
        """
        if not self.current_table:
            return False

        # Get the item at this position
        item = self.table_widget.item(ui_row, ui_col)
        if not item:
            return False

        # Get data coordinates from item
        data_indices = item.data(Qt.UserRole)
        if data_indices is None:
            return False

        # Check if this is an axis cell
        if isinstance(data_indices[0], str):
            # Axis cells: ('x_axis', index) or ('y_axis', index)
            axis_type, data_idx = data_indices
            axis_key = f"{self.current_table.address}:{axis_type}"
            return (
                axis_key in self._modified_cells
                and data_idx in self._modified_cells[axis_key]
            )

        # Data cell: (data_row, data_col)
        data_row, data_col = data_indices
        table_address = self.current_table.address

        # Check if this cell is in the modified set
        if table_address in self._modified_cells:
            return (data_row, data_col) in self._modified_cells[table_address]

        return False

    def mark_cell_modified(self, table_address: str, data_row: int, data_col: int):
        """
        Mark a cell as modified

        Args:
            table_address: Address of the table (unique identifier)
            data_row: Data row index
            data_col: Data column index
        """
        if table_address not in self._modified_cells:
            self._modified_cells[table_address] = set()

        self._modified_cells[table_address].add((data_row, data_col))

    def mark_axis_cell_modified(
        self, table_address: str, axis_type: str, data_idx: int
    ):
        """
        Mark an axis cell as modified

        Args:
            table_address: Address of the table (unique identifier)
            axis_type: 'x_axis' or 'y_axis'
            data_idx: Index in the axis array
        """
        axis_key = f"{table_address}:{axis_type}"
        if axis_key not in self._modified_cells:
            self._modified_cells[axis_key] = set()

        self._modified_cells[axis_key].add(data_idx)

    def _on_cell_changed_track_modifications(
        self,
        table_address: str,
        data_row: int,
        data_col: int,
        old_value: float,
        new_value: float,
        old_raw: float,
        new_raw: float,
    ):
        """Track cell modifications from cell_changed signal"""
        self.mark_cell_modified(table_address, data_row, data_col)

    def _on_bulk_changes_track_modifications(self, changes: list):
        """Track cell modifications from bulk_changes signal"""
        if not self.current_table:
            return

        for change in changes:
            # Bulk changes can have different formats depending on the operation
            # Most operations emit: (row, col, old_value, new_value, old_raw, new_raw)
            if len(change) >= 2:
                data_row, data_col = change[0], change[1]
                self.mark_cell_modified(self.current_table.address, data_row, data_col)

    def _on_axis_changed_track_modifications(
        self,
        table_address: str,
        axis_type: str,
        data_idx: int,
        old_value: float,
        new_value: float,
        old_raw: float,
        new_raw: float,
    ):
        """Track axis cell modifications from axis_changed signal"""
        self.mark_axis_cell_modified(table_address, axis_type, data_idx)

    def _on_axis_bulk_changes_track_modifications(self, changes: list):
        """Track axis cell modifications from axis_bulk_changes signal"""
        if not self.current_table:
            return

        for change in changes:
            # Axis bulk changes: (axis_type, index, old_value, new_value, old_raw, new_raw)
            if len(change) >= 2:
                axis_type, data_idx = change[0], change[1]
                self.mark_axis_cell_modified(
                    self.current_table.address, axis_type, data_idx
                )

    def _check_and_remove_border_if_original(
        self, table_address: str, data_row: int, data_col: int, current_value: float
    ):
        """
        Sync border with original value: remove if matches, add if differs

        Args:
            table_address: Address of the table (unique identifier)
            data_row: Data row index
            data_col: Data column index
            current_value: Current cell value
        """
        if table_address not in self._original_values:
            return

        original_data = self._original_values[table_address]
        original_values = original_data.get("values")
        if original_values is None:
            return

        # Get original value for this cell
        if original_values.ndim == 1:
            if data_row < len(original_values):
                original_value = original_values[data_row]
            else:
                return
        else:
            rows, cols = original_values.shape
            if data_row < rows and data_col < cols:
                original_value = original_values[data_row, data_col]
            else:
                return

        # Sync border state with original value
        is_modified = abs(current_value - original_value) >= 1e-10

        if is_modified:
            # Value differs from original - ensure border is present
            self.mark_cell_modified(table_address, data_row, data_col)
        else:
            # Value matches original - ensure border is removed
            if table_address in self._modified_cells:
                self._modified_cells[table_address].discard((data_row, data_col))

    def _check_and_remove_axis_border_if_original(
        self, table_address: str, axis_type: str, data_idx: int, current_value: float
    ):
        """
        Sync axis border with original value: remove if matches, add if differs

        Args:
            table_address: Address of the table (unique identifier)
            axis_type: 'x_axis' or 'y_axis'
            data_idx: Index in the axis array
            current_value: Current axis cell value
        """
        if table_address not in self._original_values:
            return

        original_data = self._original_values[table_address]
        original_axis = original_data.get(axis_type)
        if original_axis is None or data_idx >= len(original_axis):
            return

        original_value = original_axis[data_idx]

        # Sync border state with original value
        is_modified = abs(current_value - original_value) >= 1e-10

        if is_modified:
            # Value differs from original - ensure border is present
            self.mark_axis_cell_modified(table_address, axis_type, data_idx)
        else:
            # Value matches original - ensure border is removed
            axis_key = f"{table_address}:{axis_type}"
            if axis_key in self._modified_cells:
                self._modified_cells[axis_key].discard(data_idx)
