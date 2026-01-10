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
)

logger = logging.getLogger(__name__)


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

    def __init__(self, rom_definition: RomDefinition = None, parent=None):
        super().__init__(parent)
        self.rom_definition = rom_definition
        self._editing_in_progress = False
        self._read_only = False
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
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.setLayout(layout)

        # Table info label - compact, allow truncation
        self.info_label = QLabel("Select a table to view")
        self.info_label.setStyleSheet("font-size: 9px; padding: 1px 2px; background: #f0f0f0;")
        self.info_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        layout.addWidget(self.info_label)

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

        layout.addWidget(self.table_widget)

    def set_read_only(self, read_only: bool):
        """Set whether the table is read-only"""
        self._read_only = read_only

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
