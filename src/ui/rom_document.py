"""
ROM Document Widget

Represents a single ROM file with its own table browser and state.
Each ROM is displayed in its own tab.
"""

from pathlib import Path
from PySide6.QtWidgets import (
    QWidget,
    QHBoxLayout
)
from PySide6.QtCore import Signal

from .table_browser import TableBrowser
from ..core.rom_definition import RomDefinition, Table
from ..core.rom_reader import RomReader


class RomDocument(QWidget):
    """Widget representing a single open ROM document"""

    # Signal emitted when a table is selected
    table_selected = Signal(Table, object)  # table, rom_reader

    # Signal emitted when modified state changes
    modified_changed = Signal(bool)

    def __init__(self, rom_path: str, rom_definition: RomDefinition, rom_reader: RomReader, parent=None):
        super().__init__(parent)
        self.rom_path = rom_path
        self.rom_definition = rom_definition
        self.rom_reader = rom_reader
        self.file_name = Path(rom_path).name
        self._modified = False
        self.init_ui()

    def init_ui(self):
        """Initialize the user interface"""
        layout = QHBoxLayout()
        self.setLayout(layout)

        # Table browser takes full width (log console is now shared at bottom of main window)
        self.table_browser = TableBrowser()
        self.table_browser.load_definition(self.rom_definition)
        self.table_browser.table_selected.connect(self.on_table_selected)
        layout.addWidget(self.table_browser)

    def on_table_selected(self, table: Table):
        """Handle table selection from browser"""
        # Emit signal with both table and this document's rom_reader
        self.table_selected.emit(table, self.rom_reader)

    def get_tab_title(self) -> str:
        """
        Get the title for this document's tab

        Returns:
            str: Tab title (filename + ROM ID)
        """
        return f"{self.file_name} ({self.rom_definition.romid.xmlid})"

    def get_short_title(self) -> str:
        """
        Get short title for tab (just filename)

        Returns:
            str: Short filename
        """
        return self.file_name

    def is_modified(self) -> bool:
        """
        Check if this ROM has been modified

        Returns:
            bool: True if modified
        """
        return self._modified

    def set_modified(self, modified: bool):
        """
        Set the modified state

        Args:
            modified: True if document has unsaved changes
        """
        if self._modified != modified:
            self._modified = modified
            self.modified_changed.emit(modified)

    def save(self, file_path: str = None):
        """
        Save this ROM

        Args:
            file_path: Optional new path for save-as
        """
        save_path = file_path if file_path else self.rom_path
        self.rom_reader.save_rom(save_path)

        # Update path if it changed
        if file_path:
            self.rom_path = file_path
            self.file_name = Path(file_path).name

        self.set_modified(False)
