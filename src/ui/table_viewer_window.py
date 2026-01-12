"""
Table Viewer Window

Displays table data in a separate, independent window.
Allows opening multiple tables simultaneously for comparison.
"""

from PySide6.QtWidgets import QMainWindow, QVBoxLayout, QWidget, QApplication
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut

from ..utils.constants import APP_NAME
from .table_viewer import TableViewer
from ..core.rom_definition import Table, RomDefinition


class TableViewerWindow(QMainWindow):
    """
    Standalone window for viewing a single table

    Features:
    - Independent window that can be moved/resized
    - Shows table name in window title
    - Contains TableViewer widget for displaying data
    - Can have multiple windows open simultaneously
    - Editable cells with change tracking
    """

    # Forward cell_changed signal from viewer
    # Args: table, row, col, old_value, new_value, old_raw, new_raw
    cell_changed = Signal(Table, int, int, float, float, float, float)

    # Forward bulk_changes signal from viewer
    # Args: table, list of (row, col, old_value, new_value, old_raw, new_raw) tuples
    bulk_changes = Signal(Table, list)

    # Forward axis_changed signal from viewer
    # Args: table, axis_type ('x_axis' or 'y_axis'), index, old_value, new_value, old_raw, new_raw
    axis_changed = Signal(Table, str, int, float, float, float, float)

    # Forward axis_bulk_changes signal from viewer
    # Args: table, list of (axis_type, index, old_value, new_value, old_raw, new_raw) tuples
    axis_bulk_changes = Signal(Table, list)

    # Signals for undo/redo requests (forwarded to main window)
    undo_requested = Signal()
    redo_requested = Signal()

    def __init__(self, table: Table, data: dict, rom_definition: RomDefinition,
                 rom_path: str = None, parent=None,
                 modified_cells_dict: dict = None, original_values_dict: dict = None):
        """
        Initialize table viewer window

        Args:
            table: Table definition
            data: Table data dictionary from RomReader
            rom_definition: ROM definition containing scalings
            rom_path: Path to ROM file (for identifying duplicates)
            parent: Parent widget (optional)
            modified_cells_dict: Shared dict for tracking modified cells (persists across window instances)
            original_values_dict: Shared dict with original table values (for smart border removal)
        """
        super().__init__(parent)

        self.table = table
        self.data = data
        self.rom_definition = rom_definition
        self.rom_path = rom_path

        # Set window properties
        self.setWindowTitle(f"{table.name} - {APP_NAME}")

        # Create central widget with minimal margins
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        central_widget.setLayout(layout)

        # Create table viewer widget with shared tracking dicts
        self.viewer = TableViewer(
            rom_definition,
            modified_cells_dict=modified_cells_dict,
            original_values_dict=original_values_dict
        )
        layout.addWidget(self.viewer)

        # Connect cell_changed signal
        self.viewer.cell_changed.connect(self._on_cell_changed)

        # Connect bulk_changes signal
        self.viewer.bulk_changes.connect(self._on_bulk_changes)

        # Connect axis_changed signal
        self.viewer.axis_changed.connect(self._on_axis_changed)

        # Connect axis_bulk_changes signal
        self.viewer.axis_bulk_changes.connect(self._on_axis_bulk_changes)

        # Set up undo/redo shortcuts for this window
        undo_shortcut = QShortcut(QKeySequence.Undo, self)
        undo_shortcut.activated.connect(self.undo_requested.emit)
        redo_shortcut = QShortcut(QKeySequence.Redo, self)
        redo_shortcut.activated.connect(self.redo_requested.emit)

        # Set up Esc key to close window
        close_shortcut = QShortcut(QKeySequence(Qt.Key_Escape), self)
        close_shortcut.activated.connect(self.close)

        # Create menu bar
        self._create_menu_bar()

        # Display the table data
        self.viewer.display_table(table, data)

        # Auto-size window to fit content
        self._auto_size_window()

    def _create_menu_bar(self):
        """Create menu bar with Edit menu"""
        menubar = self.menuBar()

        # Edit menu
        edit_menu = menubar.addMenu("Edit")

        increment_action = edit_menu.addAction("Increment")
        increment_action.setShortcut("+")
        increment_action.triggered.connect(self.viewer.increment_selection)

        decrement_action = edit_menu.addAction("Decrement")
        decrement_action.setShortcut("-")
        decrement_action.triggered.connect(self.viewer.decrement_selection)

        edit_menu.addSeparator()

        add_action = edit_menu.addAction("Add to Data...")
        add_action.triggered.connect(self.viewer.add_to_selection)

        multiply_action = edit_menu.addAction("Multiply Data...")
        multiply_action.setShortcut("*")
        multiply_action.triggered.connect(self.viewer.multiply_selection)

        set_action = edit_menu.addAction("Set Value...")
        set_action.setShortcut("=")
        set_action.triggered.connect(self.viewer.set_value_selection)

        edit_menu.addSeparator()

        interp_v_action = edit_menu.addAction("Interpolate Vertically")
        interp_v_action.setShortcut("V")
        interp_v_action.triggered.connect(self.viewer.interpolate_vertical)

        interp_h_action = edit_menu.addAction("Interpolate Horizontally")
        interp_h_action.setShortcut("H")
        interp_h_action.triggered.connect(self.viewer.interpolate_horizontal)

        interp_2d_action = edit_menu.addAction("Interpolate 2D")
        interp_2d_action.setShortcut("B")
        interp_2d_action.triggered.connect(self.viewer.interpolate_2d)

    def _on_cell_changed(self, table_name: str, row: int, col: int,
                         old_value: float, new_value: float,
                         old_raw: float, new_raw: float):
        """Forward cell change signal with table object"""
        self.cell_changed.emit(
            self.table, row, col, old_value, new_value, old_raw, new_raw
        )

    def _on_bulk_changes(self, changes: list):
        """Forward bulk changes signal with table object"""
        self.bulk_changes.emit(self.table, changes)

    def _on_axis_changed(self, table_name: str, axis_type: str, index: int,
                         old_value: float, new_value: float,
                         old_raw: float, new_raw: float):
        """Forward axis change signal with table object"""
        self.axis_changed.emit(
            self.table, axis_type, index, old_value, new_value, old_raw, new_raw
        )

    def _on_axis_bulk_changes(self, changes: list):
        """Forward axis bulk changes signal with table object"""
        self.axis_bulk_changes.emit(self.table, changes)

    def _auto_size_window(self):
        """Auto-size window to fit table content - compact like ECUFlash"""
        table_widget = self.viewer.table_widget

        # Calculate content width
        content_width = 0
        for col in range(table_widget.columnCount()):
            content_width += table_widget.columnWidth(col)

        # Add vertical header width if visible
        if table_widget.verticalHeader().isVisible():
            content_width += table_widget.verticalHeader().width()

        # Minimal margin for scrollbar/border
        content_width += 4

        # Calculate content height
        content_height = 0
        for row in range(table_widget.rowCount()):
            content_height += table_widget.rowHeight(row)

        # Add horizontal header height if visible
        if table_widget.horizontalHeader().isVisible():
            content_height += table_widget.horizontalHeader().height()

        # Add info label height (TEMPORARILY HIDDEN - not adding height)
        # content_height += self.viewer.info_label.sizeHint().height()

        # Add menu bar height
        if self.menuBar():
            content_height += self.menuBar().height()

        # Add window title bar (approximately)
        content_height += 30

        # Get screen size to limit window size
        screen = QApplication.primaryScreen()
        if screen:
            screen_geometry = screen.availableGeometry()
            max_width = int(screen_geometry.width() * 0.9)
            max_height = int(screen_geometry.height() * 0.9)
        else:
            max_width = 1600
            max_height = 900

        # Apply size limits (allow small tables to be very compact)
        min_width = 80
        min_height = 60
        final_width = max(min_width, min(content_width, max_width))
        final_height = max(min_height, min(content_height, max_height))

        self.resize(final_width, final_height)

    def closeEvent(self, event):
        """Handle window close event"""
        # Clean up if needed
        event.accept()
