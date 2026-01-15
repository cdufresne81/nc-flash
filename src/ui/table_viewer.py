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
    QSizePolicy,
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QKeySequence, QShortcut

from ..core.rom_definition import Table, RomDefinition
from ..utils.settings import get_settings
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
        from PySide6.QtGui import QPainter, QFontMetrics
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
        from PySide6.QtCore import QSize
        from PySide6.QtGui import QFontMetrics
        fm = QFontMetrics(self.font())
        # Swap width and height since text is rotated
        return QSize(fm.height() + 4, fm.horizontalAdvance(self.text()) + 10)

    def minimumSizeHint(self):
        """Return minimum size"""
        return self.sizeHint()


class TableViewer(QWidget):
    """Widget for viewing and editing table data with gradient coloring"""

    # Signal emitted when a cell value changes
    # Args: table_name, row, col, old_value, new_value, old_raw, new_raw
    cell_changed = Signal(str, int, int, float, float, float, float)

    # Signal emitted when bulk operation completes (for single undo)
    # Args: list of (row, col, old_value, new_value, old_raw, new_raw) tuples
    bulk_changes = Signal(list)

    # Signal emitted when an axis cell value changes
    # Args: table_name, axis_type ('x_axis' or 'y_axis'), index, old_value, new_value, old_raw, new_raw
    axis_changed = Signal(str, str, int, float, float, float, float)

    # Signal emitted when axis bulk operation completes (for single undo)
    # Args: list of (axis_type, index, old_value, new_value, old_raw, new_raw) tuples
    axis_bulk_changes = Signal(list)

    def __init__(self, rom_definition: RomDefinition = None, parent=None,
                 modified_cells_dict: dict = None, original_values_dict: dict = None,
                 diff_mode: bool = False, diff_base_data: dict = None):
        super().__init__(parent)
        self.rom_definition = rom_definition
        self._editing_in_progress = False
        self._read_only = False
        # Use shared dict from main window (persists across window close/reopen)
        # If not provided, create local dict (for testing/standalone usage)
        self._modified_cells = modified_cells_dict if modified_cells_dict is not None else {}
        self._original_values = original_values_dict if original_values_dict is not None else {}
        # Diff mode for viewing historical changes
        self._diff_mode = diff_mode
        self._diff_base_data = diff_base_data  # Data from previous version to compare against
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
        return self._ctx.current_table if hasattr(self, '_ctx') else None

    @current_table.setter
    def current_table(self, value):
        """Set current table in context"""
        if hasattr(self, '_ctx'):
            self._ctx.current_table = value

    @property
    def current_data(self):
        """Get current data from context"""
        return self._ctx.current_data if hasattr(self, '_ctx') else None

    @current_data.setter
    def current_data(self, value):
        """Set current data in context"""
        if hasattr(self, '_ctx'):
            self._ctx.current_data = value

    def init_ui(self):
        """Initialize the user interface"""
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        self.setLayout(main_layout)

        # Table info label - TEMPORARILY HIDDEN (user request)
        # TODO: May want to restore this label later or make it toggleable
        self.info_label = QLabel("Select a table to view")
        self.info_label.setStyleSheet("font-size: 9px; padding: 1px 2px; background: #f0f0f0;")
        self.info_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.info_label.setVisible(False)  # HIDDEN
        main_layout.addWidget(self.info_label)

        # X-axis label (horizontal, centered above table, initially hidden)
        self.x_axis_label = QLabel("")
        self.x_axis_label.setAlignment(Qt.AlignCenter)
        font = self.x_axis_label.font()
        font.setBold(True)
        font.setPointSize(10)
        self.x_axis_label.setFont(font)
        self.x_axis_label.setVisible(False)  # Hidden by default, shown only for 3D tables
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
        self.y_axis_label.setVisible(False)  # Hidden by default, shown only for 3D tables
        table_layout.addWidget(self.y_axis_label)

        # Table widget for displaying data
        self.table_widget = QTableWidget()
        # Start with ResizeToContents, will be changed to uniform width after data load
        self.table_widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table_widget.verticalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
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
        if hasattr(self, '_display'):
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

        # Apply diff tooltips if in diff mode
        if self._diff_mode and self._diff_base_data is not None:
            self._apply_diff_tooltips()

    def _apply_diff_tooltips(self):
        """Apply tooltips to cells that differ from base version"""
        if not self._diff_mode or self._diff_base_data is None:
            return

        value_fmt = self._display.get_value_format()

        # Iterate through all cells
        for row in range(self.table_widget.rowCount()):
            for col in range(self.table_widget.columnCount()):
                item = self.table_widget.item(row, col)
                if item is None:
                    continue

                # Get data coordinates
                data_coords = item.data(Qt.UserRole)
                if data_coords is None:
                    continue

                # Skip axis cells (stored as tuple with string)
                if isinstance(data_coords[0], str):
                    continue

                data_row, data_col = data_coords

                # Check if cell differs from base
                if self.is_cell_changed_from_base(data_row, data_col):
                    base_value = self.get_base_value(data_row, data_col)
                    if base_value is not None:
                        item.setToolTip(f"Previous: {self._display.format_value(base_value, value_fmt)}")

    def clear(self):
        """Clear the viewer"""
        self._display.clear()

    def _get_value_format(self) -> str:
        """Get the Python format spec for the current table's values."""
        return self._display.get_value_format()

    def _format_value(self, value: float, format_spec: str) -> str:
        """Format a value using the given format spec with error handling."""
        return self._display.format_value(value, format_spec)

    def _get_cell_color(self, value: float, values: np.ndarray,
                        row: int, col: int) -> QColor:
        """Calculate cell background color based on gradient mode."""
        return self._display.get_cell_color(value, values, row, col)

    # Legacy method kept for backwards compatibility
    def _ratio_to_color(self, ratio: float) -> QColor:
        """Convert 0-1 ratio to thermal/rainbow gradient."""
        return self._display.ratio_to_color(ratio)

    def _on_cell_changed(self, row: int, col: int):
        """Handle cell value change from user edit"""
        self._edit.on_cell_changed(row, col)

    def _display_to_raw(self, display_value: float) -> float:
        """Convert display value to raw binary value using scaling"""
        return self._edit.display_to_raw(display_value)

    def update_cell_value(self, data_row: int, data_col: int, new_value: float):
        """Update a cell's value programmatically (for undo/redo)"""
        self._edit.update_cell_value(data_row, data_col, new_value)

    def update_axis_cell_value(self, axis_type: str, data_idx: int, new_value: float):
        """Update an axis cell's value programmatically (for undo/redo)"""
        self._edit.update_axis_cell_value(axis_type, data_idx, new_value)

    def _data_to_ui_coords(self, data_row: int, data_col: int) -> tuple:
        """Convert data coordinates to UI table coordinates"""
        return self._edit.data_to_ui_coords(data_row, data_col)

    def copy_selection(self):
        """Copy selected cells to clipboard as tab-separated values"""
        self._clipboard.copy_selection()

    def paste_selection(self):
        """Paste clipboard content into selected cells"""
        self._clipboard.paste_selection()

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
            axis_key = f"{self.current_table.name}:{axis_type}"
            return axis_key in self._modified_cells and data_idx in self._modified_cells[axis_key]

        # Data cell: (data_row, data_col)
        data_row, data_col = data_indices
        table_name = self.current_table.name

        # Check if this cell is in the modified set
        if table_name in self._modified_cells:
            return (data_row, data_col) in self._modified_cells[table_name]

        return False

    def mark_cell_modified(self, table_name: str, data_row: int, data_col: int):
        """
        Mark a cell as modified

        Args:
            table_name: Name of the table
            data_row: Data row index
            data_col: Data column index
        """
        if table_name not in self._modified_cells:
            self._modified_cells[table_name] = set()

        self._modified_cells[table_name].add((data_row, data_col))

    def mark_axis_cell_modified(self, table_name: str, axis_type: str, data_idx: int):
        """
        Mark an axis cell as modified

        Args:
            table_name: Name of the table
            axis_type: 'x_axis' or 'y_axis'
            data_idx: Index in the axis array
        """
        axis_key = f"{table_name}:{axis_type}"
        if axis_key not in self._modified_cells:
            self._modified_cells[axis_key] = set()

        self._modified_cells[axis_key].add(data_idx)

    def _on_cell_changed_track_modifications(self, table_name: str, data_row: int, data_col: int,
                                             old_value: float, new_value: float,
                                             old_raw: float, new_raw: float):
        """Track cell modifications from cell_changed signal"""
        self.mark_cell_modified(table_name, data_row, data_col)
        # Force repaint to show border
        self.table_widget.viewport().update()

    def _on_bulk_changes_track_modifications(self, changes: list):
        """Track cell modifications from bulk_changes signal"""
        if not self.current_table:
            return

        for change in changes:
            # Bulk changes can have different formats depending on the operation
            # Most operations emit: (row, col, old_value, new_value, old_raw, new_raw)
            if len(change) >= 2:
                data_row, data_col = change[0], change[1]
                self.mark_cell_modified(self.current_table.name, data_row, data_col)

        # Force repaint to show borders
        self.table_widget.viewport().update()

    def _on_axis_changed_track_modifications(self, table_name: str, axis_type: str, data_idx: int,
                                             old_value: float, new_value: float,
                                             old_raw: float, new_raw: float):
        """Track axis cell modifications from axis_changed signal"""
        self.mark_axis_cell_modified(table_name, axis_type, data_idx)
        # Force repaint to show border
        self.table_widget.viewport().update()

    def _on_axis_bulk_changes_track_modifications(self, changes: list):
        """Track axis cell modifications from axis_bulk_changes signal"""
        if not self.current_table:
            return

        for change in changes:
            # Axis bulk changes: (axis_type, index, old_value, new_value, old_raw, new_raw)
            if len(change) >= 2:
                axis_type, data_idx = change[0], change[1]
                self.mark_axis_cell_modified(self.current_table.name, axis_type, data_idx)

        # Force repaint to show borders
        self.table_widget.viewport().update()

    def _check_and_remove_border_if_original(self, table_name: str, data_row: int, data_col: int, current_value: float):
        """
        Sync border with original value: remove if matches, add if differs

        Args:
            table_name: Name of the table
            data_row: Data row index
            data_col: Data column index
            current_value: Current cell value
        """
        if table_name not in self._original_values:
            return

        original_data = self._original_values[table_name]
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
            self.mark_cell_modified(table_name, data_row, data_col)
        else:
            # Value matches original - ensure border is removed
            if table_name in self._modified_cells:
                self._modified_cells[table_name].discard((data_row, data_col))

        # Force repaint to update border
        self.table_widget.viewport().update()

    def _check_and_remove_axis_border_if_original(self, table_name: str, axis_type: str, data_idx: int, current_value: float):
        """
        Sync axis border with original value: remove if matches, add if differs

        Args:
            table_name: Name of the table
            axis_type: 'x_axis' or 'y_axis'
            data_idx: Index in the axis array
            current_value: Current axis cell value
        """
        if table_name not in self._original_values:
            return

        original_data = self._original_values[table_name]
        original_axis = original_data.get(axis_type)
        if original_axis is None or data_idx >= len(original_axis):
            return

        original_value = original_axis[data_idx]

        # Sync border state with original value
        is_modified = abs(current_value - original_value) >= 1e-10

        if is_modified:
            # Value differs from original - ensure border is present
            self.mark_axis_cell_modified(table_name, axis_type, data_idx)
        else:
            # Value matches original - ensure border is removed
            axis_key = f"{table_name}:{axis_type}"
            if axis_key in self._modified_cells:
                self._modified_cells[axis_key].discard(data_idx)

        # Force repaint to update border
        self.table_widget.viewport().update()
