"""
Table Viewer Window

Displays table data in a separate, independent window.
Allows opening multiple tables simultaneously for comparison.
"""

from PySide6.QtWidgets import QMainWindow, QVBoxLayout, QWidget, QApplication
from PySide6.QtCore import Qt

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
    """

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

        # Create central widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        layout = QVBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)
        central_widget.setLayout(layout)

        # Create table viewer widget
        self.viewer = TableViewer(rom_definition)
        layout.addWidget(self.viewer)

        # Display the table data
        self.viewer.display_table(table, data)

        # Auto-size window to fit content
        self._auto_size_window()

    def _auto_size_window(self):
        """Auto-size window to fit table content"""
        table_widget = self.viewer.table_widget

        # Calculate content width
        content_width = 0
        for col in range(table_widget.columnCount()):
            content_width += table_widget.columnWidth(col)

        # Add vertical header width if visible
        if table_widget.verticalHeader().isVisible():
            content_width += table_widget.verticalHeader().width()

        # Add scroll bar width margin
        content_width += 20

        # Calculate content height
        content_height = 0
        for row in range(table_widget.rowCount()):
            content_height += table_widget.rowHeight(row)

        # Add horizontal header height
        content_height += table_widget.horizontalHeader().height()

        # Add info label height
        content_height += self.viewer.info_label.sizeHint().height()

        # Add margins and padding
        content_height += 40  # Layout margins + extra padding

        # Get screen size to limit window size
        screen = QApplication.primaryScreen()
        if screen:
            screen_geometry = screen.availableGeometry()
            max_width = int(screen_geometry.width() * 0.9)
            max_height = int(screen_geometry.height() * 0.9)
        else:
            max_width = 1600
            max_height = 900

        # Apply size limits
        min_width = 200
        min_height = 150
        final_width = max(min_width, min(content_width, max_width))
        final_height = max(min_height, min(content_height, max_height))

        self.resize(final_width, final_height)

    def closeEvent(self, event):
        """Handle window close event"""
        # Clean up if needed
        event.accept()
