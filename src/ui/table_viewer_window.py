"""
Table Viewer Window

Displays table data in a separate, independent window.
Allows opening multiple tables simultaneously for comparison.
"""

from PySide6.QtWidgets import QMainWindow, QVBoxLayout, QWidget
from PySide6.QtCore import Qt

from .table_viewer import TableViewer
from ..core.rom_definition import Table


class TableViewerWindow(QMainWindow):
    """
    Standalone window for viewing a single table

    Features:
    - Independent window that can be moved/resized
    - Shows table name in window title
    - Contains TableViewer widget for displaying data
    - Can have multiple windows open simultaneously
    """

    def __init__(self, table: Table, data: dict, parent=None):
        """
        Initialize table viewer window

        Args:
            table: Table definition
            data: Table data dictionary from RomReader
            parent: Parent widget (optional)
        """
        super().__init__(parent)

        self.table = table
        self.data = data

        # Set window properties
        self.setWindowTitle(f"{table.name} - NC ROM Editor")
        self.resize(800, 600)

        # Create central widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        layout = QVBoxLayout()
        central_widget.setLayout(layout)

        # Create table viewer widget
        self.viewer = TableViewer()
        layout.addWidget(self.viewer)

        # Display the table data
        self.viewer.display_table(table, data)

        # Make window stay on top initially (user can change this)
        # self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)

    def closeEvent(self, event):
        """Handle window close event"""
        # Clean up if needed
        event.accept()
