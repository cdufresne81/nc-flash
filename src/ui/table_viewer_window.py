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

    # Signals for undo/redo requests (forwarded to main window)
    undo_requested = Signal()
    redo_requested = Signal()

    def __init__(self, table: Table, data: dict, rom_definition: RomDefinition,
                 rom_path: str = None, parent=None):
        """
        Initialize table viewer window

        Args:
            table: Table definition
            data: Table data dictionary from RomReader
            rom_definition: ROM definition containing scalings
            rom_path: Path to ROM file (for identifying duplicates)
            parent: Parent widget (optional)
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

        # Create table viewer widget
        self.viewer = TableViewer(rom_definition)
        layout.addWidget(self.viewer)

        # Connect cell_changed signal
        self.viewer.cell_changed.connect(self._on_cell_changed)

        # Connect bulk_changes signal
        self.viewer.bulk_changes.connect(self._on_bulk_changes)

        # Set up undo/redo shortcuts for this window
        undo_shortcut = QShortcut(QKeySequence.Undo, self)
        undo_shortcut.activated.connect(self.undo_requested.emit)
        redo_shortcut = QShortcut(QKeySequence.Redo, self)
        redo_shortcut.activated.connect(self.redo_requested.emit)

        # Display the table data
        self.viewer.display_table(table, data)

        # Auto-size window to fit content
        self._auto_size_window()

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

        # Add info label height
        content_height += self.viewer.info_label.sizeHint().height()

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
