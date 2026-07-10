"""
ROM Document Widget

Represents a single ROM file with its own table browser and state.
Each ROM is displayed in its own tab.
"""

from pathlib import Path
from PySide6.QtWidgets import QWidget, QHBoxLayout
from PySide6.QtCore import Signal

from .table_browser import TableBrowser
from ..core.rom_definition import RomDefinition, Table
from ..core.rom_reader import RomReader
from ..core.table_edit_state import TableEditState


class RomDocument(QWidget):
    """Widget representing a single open ROM document"""

    # Signal emitted when a table is selected
    table_selected = Signal(Table, object)  # table, rom_reader

    # Signal emitted when modified state changes
    modified_changed = Signal(bool)

    def __init__(
        self,
        rom_path: str,
        rom_definition: RomDefinition,
        rom_reader: RomReader,
        parent=None,
    ):
        super().__init__(parent)
        self.rom_path = rom_path
        self.rom_definition = rom_definition
        self.rom_reader = rom_reader
        self.file_name = Path(rom_path).name
        self._modified = False
        self.project_path = None  # Set by open_project_path() for project tabs
        # Base label shown on this document's tab (without the "*" dirty marker).
        # Standalone ROMs use the file name; project tabs override this with
        # "[P] {name}" in open_project_path(). _update_tab_title() reads it so a
        # modified project tab shows "*[P] name" instead of clobbering the label
        # with the bare filename (B15).
        self.tab_base_title = self.file_name
        # This document is the SINGLE owner of its per-ROM edit state (Phase 3,
        # finding C1): modified-cell borders + capture-once originals. Its open
        # TableViewer(s) share this object by method, never by aliasing a raw
        # dict. State dies with the document — no per-path dicts on MainWindow.
        self.edit_state = TableEditState()
        # Per-ROM tab/window tint (None = default gray), assigned by MainWindow's
        # color allocator.
        self._rom_color = None
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

    def get_color(self):
        """Return this ROM's assigned tint (QColor) or None for default gray."""
        return self._rom_color

    def set_color(self, color):
        """Assign this ROM's tint. None means the default gray."""
        self._rom_color = color

    def save(self, file_path: str = None):
        """
        Save this ROM

        Args:
            file_path: Optional new path for save-as
        """
        save_path = file_path if file_path else self.rom_path
        self.rom_reader.save_rom(save_path)

        # Update path if it changed (Save As)
        if file_path:
            self.rom_path = file_path
            self.file_name = Path(file_path).name
            self.rom_reader.rom_path = Path(file_path)
            # Standalone tabs are labelled by file name; keep the base title in
            # sync after a rename. Project tabs keep their "[P] {name}" label.
            if self.project_path is None:
                self.tab_base_title = self.file_name

        self.set_modified(False)
