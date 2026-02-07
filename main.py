#!/usr/bin/env python3
"""
NC ROM Editor - Main Application Entry Point

An open-source ROM editor for NC Miata ECUs
"""

import sys
import logging
from pathlib import Path
from datetime import datetime
from PySide6.QtWidgets import (
    QDialog,
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QSplitter,
    QFileDialog,
    QMessageBox,
    QLabel,
    QTabWidget
)
from PySide6.QtCore import Qt

from src.utils.logging_config import setup_logging, get_logger
from src.utils.settings import get_settings
from src.utils.constants import (
    APP_NAME, APP_VERSION_STRING, APP_DESCRIPTION,
    MAIN_WINDOW_X, MAIN_WINDOW_Y, MAIN_WINDOW_WIDTH, MAIN_WINDOW_HEIGHT,
    MAIN_SPLITTER_LEFT, MAIN_SPLITTER_RIGHT
)
from src.core.definition_parser import load_definition
from src.core.rom_reader import RomReader
from src.core.rom_detector import RomDetector
from src.core.exceptions import (
    DefinitionError,
    RomFileError,
    DetectionError,
    ScalingNotFoundError,
    RomReadError
)
from src.ui.table_browser import TableBrowser
from src.ui.table_viewer_window import TableViewerWindow
from src.ui.log_console import LogConsole
from src.ui.settings_dialog import SettingsDialog
from src.ui.setup_wizard import SetupWizard
from src.ui.rom_document import RomDocument
from src.ui.project_wizard import ProjectWizard
from src.ui.commit_dialog import CommitDialog
from src.ui.history_viewer import HistoryViewer
from src.core.project_manager import ProjectManager
from src.core.change_tracker import ChangeTracker
from src.core.table_undo_manager import TableUndoManager
from src.core.version_models import CellChange, AxisChange

logger = get_logger(__name__)


def handle_rom_operation_error(parent, operation: str, exception: Exception):
    """
    Handle common ROM operation errors with consistent logging and user feedback

    Args:
        parent: Parent widget for message box
        operation: Description of operation that failed (e.g., "open ROM file")
        exception: The exception that was raised
    """
    error_msg = f"Failed to {operation}:\n{str(exception)}"
    logger.error(error_msg.replace('\n', ' '))
    QMessageBox.critical(parent, "Error", error_msg)


class MainWindow(QMainWindow):
    """Main application window"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setGeometry(MAIN_WINDOW_X, MAIN_WINDOW_Y, MAIN_WINDOW_WIDTH, MAIN_WINDOW_HEIGHT)

        logger.info("Initializing NC ROM Editor")

        # Track open table viewer windows
        self.open_table_windows = []

        # Get application settings
        self.settings = get_settings()

        # Track modified cells across all ROMs (persists when tables are closed/reopened)
        # Structure: {rom_path: {table_name: {(data_row, data_col), ...}, "table_name:x_axis": {idx, ...}}}
        self.modified_cells = {}

        # Store original table values when first loaded (for smart border removal on undo)
        # Structure: {rom_path: {table_name: {"values": np.array, "x_axis": np.array, "y_axis": np.array}}}
        self.original_table_values = {}

        # Project management
        self.project_manager = ProjectManager()
        self.change_tracker = ChangeTracker()
        self.change_tracker.add_change_callback(self._on_changes_updated)

        # Per-table undo/redo manager (uses Qt's QUndoGroup pattern)
        self.table_undo_manager = TableUndoManager()
        self.table_undo_manager.set_callbacks(
            apply_cell=self._apply_cell_change_from_undo,
            apply_axis=self._apply_axis_change_from_undo,
            update_pending=self._update_pending_from_undo,
            begin_bulk_update=self._begin_bulk_update,
            end_bulk_update=self._end_bulk_update,
        )

        # Check if definitions directory is configured and valid
        if not self.check_definitions_directory():
            # Show setup wizard on first run or if definitions directory is invalid
            if not self.show_setup_wizard():
                # User cancelled setup, exit application
                logger.warning("Setup cancelled by user, exiting application")
                QMessageBox.critical(
                    self,
                    "Setup Required",
                    f"{APP_NAME} requires a definitions directory to function.\n"
                    "Application will now exit."
                )
                sys.exit(1)

        # ROM detector for automatic XML matching
        try:
            definitions_dir = self.settings.get_definitions_directory()
            self.rom_detector = RomDetector(definitions_dir)
            logger.info(f"ROM detector initialized successfully (definitions: {definitions_dir})")
        except DetectionError as e:
            logger.error(f"Failed to initialize ROM detector: {e}")
            QMessageBox.critical(
                self,
                "Initialization Error",
                f"Failed to initialize ROM detector:\n{str(e)}"
            )
            self.rom_detector = None
        except Exception as e:
            logger.error(f"Unexpected error initializing ROM detector: {e}")
            QMessageBox.critical(
                self,
                "Initialization Error",
                f"Failed to initialize ROM detector:\n{str(e)}"
            )
            self.rom_detector = None

        # Initialize UI
        self.init_ui()
        self.init_menu()

        # Log startup message
        self.log_startup_message()

        # Restore previous session
        self._restore_session()

    def check_definitions_directory(self) -> bool:
        """
        Check if definitions directory is configured and valid

        Returns:
            bool: True if valid, False if needs configuration
        """
        definitions_dir = self.settings.get_definitions_directory()

        # Check if path exists
        definitions_path = Path(definitions_dir)
        if not definitions_path.exists() or not definitions_path.is_dir():
            logger.warning(f"Definitions directory does not exist: {definitions_dir}")
            return False

        # Check if directory contains at least one XML file
        xml_files = list(definitions_path.glob("*.xml"))
        if not xml_files:
            logger.warning(f"No XML files found in definitions directory: {definitions_dir}")
            return False

        return True

    def show_setup_wizard(self) -> bool:
        """
        Show the setup wizard for first-run configuration

        Returns:
            bool: True if setup completed, False if cancelled
        """
        wizard = SetupWizard(self)
        result = wizard.exec()

        if result == QDialog.Accepted:
            logger.info("Setup wizard completed successfully")
            return True
        else:
            logger.info("Setup wizard cancelled")
            return False

    def init_ui(self):
        """Initialize the user interface"""
        # Central widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        layout = QHBoxLayout()
        central_widget.setLayout(layout)

        # Main splitter (tabs on left, activity log on right)
        main_splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(main_splitter)

        # Tab widget for multiple ROM documents
        self.tab_widget = QTabWidget()
        self.tab_widget.setTabsClosable(True)
        self.tab_widget.setMovable(True)
        self.tab_widget.tabCloseRequested.connect(self.close_tab)
        self.tab_widget.currentChanged.connect(self.on_tab_changed)
        main_splitter.addWidget(self.tab_widget)

        # Shared activity log on the right (always visible)
        self.log_console = LogConsole()
        main_splitter.addWidget(self.log_console)

        # Set initial splitter sizes (30% tabs, 70% log)
        # Matches longest table name width on left, rest for activity log
        main_splitter.setSizes([MAIN_SPLITTER_LEFT, MAIN_SPLITTER_RIGHT])

    def init_menu(self):
        """Initialize the menu bar"""
        menubar = self.menuBar()

        # File menu
        self.file_menu = menubar.addMenu("File")

        # Project section
        new_project_action = self.file_menu.addAction("New Project...")
        new_project_action.triggered.connect(self.new_project)

        open_project_action = self.file_menu.addAction("Open Project...")
        open_project_action.triggered.connect(self.open_project)

        self.file_menu.addSeparator()

        open_action = self.file_menu.addAction("Open ROM...")
        open_action.triggered.connect(self.open_rom)

        save_action = self.file_menu.addAction("Save ROM")
        save_action.triggered.connect(self.save_rom)

        save_as_action = self.file_menu.addAction("Save ROM As...")
        save_as_action.triggered.connect(self.save_rom_as)

        self.file_menu.addSeparator()

        # Commit (for projects)
        self.commit_action = self.file_menu.addAction("Commit Changes...")
        self.commit_action.setShortcut("Ctrl+S")
        self.commit_action.triggered.connect(self.commit_changes)
        self.commit_action.setEnabled(False)

        self.file_menu.addSeparator()

        close_tab_action = self.file_menu.addAction("Close Tab")
        close_tab_action.triggered.connect(self.close_current_tab)

        self.file_menu.addSeparator()

        # Recent files section (will be populated dynamically)
        self.recent_files_separator = self.file_menu.addSeparator()
        self.recent_files_actions = []
        self.update_recent_files_menu()

        self.file_menu.addSeparator()

        exit_action = self.file_menu.addAction("Exit")
        exit_action.triggered.connect(self.close)

        # Edit menu
        edit_menu = menubar.addMenu("Edit")

        # Use QUndoGroup's createUndoAction/createRedoAction for per-table undo/redo
        # These actions automatically enable/disable based on active stack state
        self.undo_action = self.table_undo_manager.undo_group.createUndoAction(self, "Undo")
        self.undo_action.setShortcut("Ctrl+Z")
        edit_menu.addAction(self.undo_action)

        self.redo_action = self.table_undo_manager.undo_group.createRedoAction(self, "Redo")
        self.redo_action.setShortcut("Ctrl+Y")
        edit_menu.addAction(self.redo_action)

        edit_menu.addSeparator()

        settings_action = edit_menu.addAction("Settings...")
        settings_action.triggered.connect(self.show_settings)

        # View menu
        view_menu = menubar.addMenu("View")

        history_action = view_menu.addAction("Commit History...")
        history_action.triggered.connect(self.show_history)

        # Help menu
        help_menu = menubar.addMenu("Help")

        about_action = help_menu.addAction("About")
        about_action.triggered.connect(self.show_about)

    def update_recent_files_menu(self):
        """Update the recent files menu with current list"""
        # Remove existing recent file actions
        for action in self.recent_files_actions:
            self.file_menu.removeAction(action)
        self.recent_files_actions.clear()

        # Get recent files from settings
        recent_files = self.settings.get_recent_files()

        if recent_files:
            # Add each recent file
            for i, file_path in enumerate(recent_files, 1):
                # Show just the filename, but store full path
                file_name = Path(file_path).name
                action_text = f"{i}. {file_name}"

                action = self.file_menu.addAction(action_text)
                action.setData(file_path)  # Store full path in action data
                action.setStatusTip(file_path)  # Show full path in status bar
                action.triggered.connect(lambda checked=False, path=file_path: self.open_recent_file(path))

                # Insert before the separator
                self.file_menu.insertAction(self.recent_files_separator, action)
                self.recent_files_actions.append(action)

            # Add "Clear Recent Files" option
            clear_action = self.file_menu.addAction("Clear Recent Files")
            clear_action.triggered.connect(self.clear_recent_files)
            self.file_menu.insertAction(self.recent_files_separator, clear_action)
            self.recent_files_actions.append(clear_action)

    def open_recent_file(self, file_path: str):
        """
        Open a ROM file from recent files list

        Args:
            file_path: Full path to ROM file
        """
        if not Path(file_path).exists():
            QMessageBox.warning(
                self,
                "File Not Found",
                f"The file no longer exists:\n{file_path}\n\n"
                "It will be removed from recent files."
            )
            # Remove from recent files
            recent = self.settings.get_recent_files()
            if file_path in recent:
                recent.remove(file_path)
                self.settings.settings.setValue("recent_files", recent)
                self.settings.settings.sync()
                self.update_recent_files_menu()
            return

        # Open the file (reuse existing logic by calling the internal open method)
        self._open_rom_file(file_path)

    def clear_recent_files(self):
        """Clear the recent files list"""
        self.settings.clear_recent_files()
        self.update_recent_files_menu()
        logger.info("Recent files list cleared")

    def update_window_title(self):
        """Update window title based on tab count"""
        if self.tab_widget.count() == 0:
            self.setWindowTitle(APP_NAME)

    def get_current_document(self):
        """
        Get the currently active ROM document

        Returns:
            RomDocument or None: Current document or None if no tabs
        """
        current_index = self.tab_widget.currentIndex()
        if current_index >= 0:
            return self.tab_widget.widget(current_index)
        return None

    def _find_document_by_rom_path(self, rom_path):
        """Find the RomDocument tab that owns the given ROM file path."""
        if not rom_path:
            return None
        # Use Path comparison to handle slash normalization on Windows
        # (QFileDialog returns forward slashes, Path uses backslashes)
        from pathlib import Path as _Path
        target = _Path(rom_path)
        for i in range(self.tab_widget.count()):
            doc = self.tab_widget.widget(i)
            if hasattr(doc, 'rom_path') and _Path(doc.rom_path) == target:
                return doc
        logger.warning(f"No document found for rom_path={rom_path}")
        return None

    def close_tab(self, index: int):
        """
        Close a ROM tab

        Args:
            index: Tab index to close
        """
        if index < 0 or index >= self.tab_widget.count():
            return

        document = self.tab_widget.widget(index)
        if document and document.is_modified():
            response = QMessageBox.question(
                self,
                "Unsaved Changes",
                f"'{document.file_name}' has unsaved changes.\n\nDo you want to save before closing?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save
            )

            if response == QMessageBox.Cancel:
                return
            elif response == QMessageBox.Save:
                document.save()

        # Clean up all state tied to this ROM before removing the tab
        if document:
            # Use rom_reader.rom_path (Path) for consistent comparison
            # (document.rom_path is str, window.rom_path is Path)
            rom_path = document.rom_reader.rom_path if hasattr(document, 'rom_reader') and document.rom_reader else None

            # Close all open table windows belonging to this ROM
            windows_to_close = [w for w in self.open_table_windows
                                if w.rom_path == rom_path]
            for window in windows_to_close:
                window.close()

            # Collect all table addresses from this ROM's definition
            table_addresses = set()
            if hasattr(document, 'rom_reader') and document.rom_reader:
                definition = document.rom_reader.definition
                if definition:
                    for table in definition.tables:
                        table_addresses.add(table.address)

            # Remove undo stacks for this ROM's tables
            self.table_undo_manager.remove_stacks_for_addresses(table_addresses)

            # Clear pending changes for this ROM's tables
            self.change_tracker.clear_pending_for_addresses(table_addresses)

            # Clear per-ROM tracking dicts
            if rom_path:
                self.modified_cells.pop(rom_path, None)
                self.original_table_values.pop(rom_path, None)

        # Remove the tab and schedule widget cleanup
        self.tab_widget.removeTab(index)
        if document:
            document.deleteLater()
        self.update_window_title()

        logger.info(f"Closed ROM tab: {document.file_name if document else 'unknown'}")

    def close_current_tab(self):
        """Close the currently active tab"""
        current_index = self.tab_widget.currentIndex()
        if current_index >= 0:
            self.close_tab(current_index)

    def on_tab_changed(self, index: int):
        """Handle tab change"""
        if index >= 0:
            document = self.tab_widget.widget(index)
            if document:
                self.setWindowTitle(f"{APP_NAME} - {document.get_tab_title()}")
                logger.info(f"Switched to ROM: {document.file_name}")
        else:
            self.update_window_title()

    def open_rom(self):
        """Open a ROM file via file dialog"""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Open ROM File",
            "",
            "ROM Files (*.bin *.rom);;All Files (*)"
        )

        if file_path:
            self._open_rom_file(file_path)

    def _open_rom_file(self, file_path: str):
        """
        Open a ROM file from a given path

        Args:
            file_path: Full path to ROM file
        """
        try:
            logger.info(f"Opening ROM file: {file_path}")
            self.statusBar().showMessage(f"Detecting ROM ID...")

            # Detect ROM ID and find matching XML definition
            if not self.rom_detector:
                logger.error("ROM detector not initialized")
                QMessageBox.critical(
                    self,
                    "Error",
                    "ROM detector not initialized. Cannot auto-detect ROM type."
                )
                return

            rom_id, xml_path = self.rom_detector.detect_rom_id(file_path)

            if not rom_id or not xml_path:
                logger.warning(f"No matching ROM definition found for {file_path}")
                QMessageBox.critical(
                    self,
                    "Unknown ROM",
                    "Could not identify ROM type. No matching definition found.\n\n"
                    "Supported ROM IDs:\n" +
                    "\n".join([f"  - {info['xmlid']} ({info['make']} {info['model']})"
                               for info in self.rom_detector.get_definitions_summary()])
                )
                return

            # Load the matching definition
            logger.info(f"Detected ROM ID: {rom_id}")
            self.statusBar().showMessage(f"Detected ROM ID: {rom_id}, loading definition...")
            rom_definition = load_definition(xml_path)

            # Create ROM reader
            self.statusBar().showMessage(f"Loading ROM data...")
            rom_reader = RomReader(file_path, rom_definition)

            # Verify ROM ID (should always pass now, but kept as sanity check)
            if not rom_reader.verify_rom_id():
                logger.warning("ROM ID verification failed after auto-detection")
                QMessageBox.warning(
                    self,
                    "ROM ID Warning",
                    f"ROM ID verification failed. This should not happen after auto-detection.\n"
                    f"Expected: {rom_definition.romid.internalidstring}\n"
                    f"This may indicate a detection bug."
                )

            # Create ROM document widget
            rom_document = RomDocument(file_path, rom_definition, rom_reader, self)
            rom_document.table_selected.connect(self.on_table_selected)
            rom_document.modified_changed.connect(lambda modified, doc=rom_document: self._update_tab_title(doc))

            # Add as new tab
            file_name = Path(file_path).name
            tab_index = self.tab_widget.addTab(rom_document, file_name)
            self.tab_widget.setTabToolTip(tab_index, file_path)
            self.tab_widget.setCurrentIndex(tab_index)

            # Add to recent files list
            self.settings.add_recent_file(file_path)
            self.update_recent_files_menu()

            # Log to console
            logger.info(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            logger.info(f"ROM LOADED: {file_name}")
            logger.info(f"  ROM ID: {rom_id}")
            logger.info(f"  Definition: {rom_definition.romid.xmlid}")
            logger.info(f"  Make/Model: {rom_definition.romid.make} {rom_definition.romid.model}")
            logger.info(f"  Tables: {len(rom_definition.tables)}")
            logger.info(f"  Size: {len(rom_reader.rom_data):,} bytes")
            logger.info(f"  Tab: {tab_index + 1}")
            logger.info(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

            self.statusBar().showMessage(
                f"Loaded: {file_name} - {rom_definition.romid.xmlid} "
                f"({len(rom_definition.tables)} tables)"
            )

        except (DetectionError, RomFileError, DefinitionError, Exception) as e:
            handle_rom_operation_error(self, "open ROM file", e)

    def save_rom(self):
        """Save the current ROM file"""
        document = self.get_current_document()
        if not document:
            QMessageBox.warning(self, "No ROM", "No ROM file is currently loaded.")
            return

        try:
            document.save()
            document.set_modified(False)
            self._update_tab_title(document)

            # Log to console
            logger.info(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            logger.info(f"ROM SAVED: {document.file_name}")
            logger.info(f"  Location: {document.rom_path}")
            logger.info(f"  Size: {len(document.rom_reader.rom_data):,} bytes")
            logger.info(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            logger.info(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

            self.statusBar().showMessage(f"Saved: {document.rom_path}")
            QMessageBox.information(
                self,
                "Success",
                f"ROM saved successfully to:\n{document.rom_path}"
            )
        except (RomFileError, Exception) as e:
            handle_rom_operation_error(self, "save ROM file", e)

    def save_rom_as(self):
        """Save the ROM to a new file"""
        document = self.get_current_document()
        if not document:
            QMessageBox.warning(self, "No ROM", "No ROM file is currently loaded.")
            return

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save ROM File As",
            "",
            "ROM Files (*.bin);;All Files (*)"
        )

        if file_path:
            try:
                document.save(file_path)
                document.set_modified(False)

                # Update tab title with new filename
                self._update_tab_title(document)
                current_index = self.tab_widget.indexOf(document)
                self.tab_widget.setTabToolTip(current_index, file_path)

                # Log to console
                logger.info(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
                logger.info(f"ROM SAVED AS: {document.file_name}")
                logger.info(f"  Location: {file_path}")
                logger.info(f"  Size: {len(document.rom_reader.rom_data):,} bytes")
                logger.info(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                logger.info(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

                self.statusBar().showMessage(f"Saved: {file_path}")
                QMessageBox.information(
                    self,
                    "Success",
                    f"ROM saved successfully to:\n{file_path}"
                )
            except (RomFileError, Exception) as e:
                handle_rom_operation_error(self, "save ROM file", e)

    def on_table_selected(self, table, rom_reader):
        """Handle table selection from browser - opens table in new window"""
        try:
            # Get ROM path for duplicate detection
            rom_path = rom_reader.rom_path

            # Check if this table is already open for this ROM
            # Use address for comparison since table names may not be unique across categories
            for window in self.open_table_windows:
                if window.rom_path == rom_path and window.table.address == table.address:
                    # Window already exists - bring to focus
                    window.raise_()
                    window.activateWindow()
                    logger.info(f"Table already open, bringing to focus: {table.name}")
                    self.statusBar().showMessage(f"Table already open: {table.name}")
                    return

            # Read table data from ROM
            logger.debug(f"User selected table: {table.name}")
            self.statusBar().showMessage(f"Loading table: {table.name}...")
            data = rom_reader.read_table_data(table)

            if data:
                # Store original table values if this is the first time loading this table
                # Use table.address as the key since table names are not unique
                if rom_path not in self.original_table_values:
                    self.original_table_values[rom_path] = {}
                if table.address not in self.original_table_values[rom_path]:
                    import numpy as np
                    # Deep copy the original values
                    self.original_table_values[rom_path][table.address] = {
                        "values": np.copy(data["values"]),
                        "x_axis": np.copy(data["x_axis"]) if data.get("x_axis") is not None else None,
                        "y_axis": np.copy(data["y_axis"]) if data.get("y_axis") is not None else None,
                    }

                # Initialize modified cells tracking for this ROM if needed
                if rom_path not in self.modified_cells:
                    self.modified_cells[rom_path] = {}

                # Create and show new table viewer window
                viewer_window = TableViewerWindow(
                    table, data, rom_reader.definition,
                    rom_path=rom_path, parent=self,
                    modified_cells_dict=self.modified_cells[rom_path],
                    original_values_dict=self.original_table_values[rom_path]
                )

                # Connect cell_changed signal to change tracker
                viewer_window.cell_changed.connect(self._on_table_cell_changed)

                # Connect bulk_changes signal to change tracker
                viewer_window.bulk_changes.connect(self._on_table_bulk_changes)

                # Connect axis_changed signal to change tracker
                viewer_window.axis_changed.connect(self._on_table_axis_changed)

                # Connect axis_bulk_changes signal to change tracker
                viewer_window.axis_bulk_changes.connect(self._on_table_axis_bulk_changes)

                # Connect window focus signal to highlight table in tree and activate undo stack
                viewer_window.window_focused.connect(self._on_table_window_focused)

                viewer_window.show()

                # Track the window (removed in TableViewerWindow.closeEvent)
                self.open_table_windows.append(viewer_window)

                # Log to console
                logger.info(f"Opened table: {table.name} ({table.address})")
                logger.debug(f"  Category: {table.category}")
                logger.debug(f"  Type: {table.type.value}")
                logger.debug(f"  Address: {table.address}")
                logger.debug(f"  Elements: {table.elements}")
                logger.debug(f"  Open windows: {len(self.open_table_windows)}")

                self.statusBar().showMessage(
                    f"Opened: {table.name} ({len(self.open_table_windows)} tables open)"
                )
            else:
                logger.warning(f"No data returned for table: {table.name}")
                QMessageBox.warning(
                    self,
                    "Error",
                    f"Failed to read table data for: {table.name}"
                )

        except (ScalingNotFoundError, RomReadError, Exception) as e:
            handle_rom_operation_error(self, "load table", e)

    def log_startup_message(self):
        """Log application startup message to console"""
        logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        logger.info(f"{APP_NAME} {APP_VERSION_STRING}")
        logger.info(APP_DESCRIPTION)
        logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        logger.info("")
        logger.info("Ready. Open a ROM file to begin.")
        logger.info("")

        if self.rom_detector:
            definitions = self.rom_detector.get_definitions_summary()
            logger.info(f"Available ROM definitions: {len(definitions)}")
            for defn in definitions:
                logger.info(f"  • {defn['xmlid']} - {defn['make']} {defn['model']}")

    def show_settings(self):
        """Show settings dialog"""
        dialog = SettingsDialog(self)
        dialog.settings_changed.connect(self.on_settings_changed)
        dialog.exec()

    def on_settings_changed(self):
        """Handle settings changes"""
        # Reinitialize ROM detector with new definitions path
        try:
            definitions_dir = self.settings.get_definitions_directory()
            self.rom_detector = RomDetector(definitions_dir)
            logger.info(f"ROM detector reinitialized with definitions directory: {definitions_dir}")
            self.statusBar().showMessage(f"Settings updated. Definitions directory: {definitions_dir}")
        except DetectionError as e:
            logger.error(f"Failed to reinitialize ROM detector: {e}")
            QMessageBox.warning(
                self,
                "Settings Error",
                f"Failed to load definitions from new directory:\n{str(e)}\n\n"
                "Please check the definitions directory path in settings."
            )

    def show_about(self):
        """Show about dialog"""
        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            f"{APP_NAME} {APP_VERSION_STRING}\n\n"
            f"{APP_DESCRIPTION}\n\n"
            "Designed to replace EcuFlash for ROM editing tasks.\n"
            "Works with RomDrop for ECU flashing."
        )

    # ========== Project Management Methods ==========

    def new_project(self):
        """Create a new project via wizard"""
        wizard = ProjectWizard(self)
        if wizard.exec() == QDialog.Accepted:
            try:
                # Create the project
                project = self.project_manager.create_project(
                    project_path=wizard.project_location,
                    project_name=wizard.project_name,
                    source_rom_path=wizard.rom_path,
                    rom_definition=wizard.rom_definition,
                    description=wizard.project_description
                )

                # Open the project's working ROM
                self._open_rom_file(project.working_rom_path)

                # Update UI state
                self._update_project_ui()

                logger.info(f"Created project: {project.name}")
                self.statusBar().showMessage(f"Created project: {project.name}")

                QMessageBox.information(
                    self,
                    "Project Created",
                    f"Project '{project.name}' created successfully.\n\n"
                    f"Location: {project.project_path}"
                )

            except Exception as e:
                handle_rom_operation_error(self, "create project", e)

    def open_project(self):
        """Open an existing project"""
        project_path = QFileDialog.getExistingDirectory(
            self,
            "Open Project Folder",
            str(Path.home())
        )

        if not project_path:
            return

        # Check if it's a valid project folder
        if not ProjectManager.is_project_folder(project_path):
            QMessageBox.warning(
                self,
                "Invalid Project",
                "The selected folder is not a valid NC ROM Editor project.\n\n"
                "A project folder must contain a project.json file."
            )
            return

        try:
            # Open the project
            project = self.project_manager.open_project(project_path)

            # Get ROM definition for the project
            rom_id = project.original_rom.rom_id
            xml_path = self.rom_detector.find_definition_by_id(rom_id)

            if xml_path:
                rom_definition = load_definition(xml_path)

                # Create ROM reader for working ROM
                rom_reader = RomReader(project.working_rom_path, rom_definition)

                # Create ROM document widget
                rom_document = RomDocument(
                    project.working_rom_path, rom_definition, rom_reader, self
                )
                rom_document.table_selected.connect(self.on_table_selected)

                # Add as new tab
                tab_title = f"[P] {project.name}"
                tab_index = self.tab_widget.addTab(rom_document, tab_title)
                self.tab_widget.setTabToolTip(tab_index, project.project_path)
                self.tab_widget.setCurrentIndex(tab_index)

                # Update UI state
                self._update_project_ui()

                logger.info(f"Opened project: {project.name}")
                self.statusBar().showMessage(f"Opened project: {project.name}")
            else:
                QMessageBox.warning(
                    self,
                    "Definition Not Found",
                    f"Could not find ROM definition for ID: {rom_id}\n\n"
                    "The project was created with a ROM definition that is no longer available."
                )

        except Exception as e:
            handle_rom_operation_error(self, "open project", e)

    def commit_changes(self):
        """Commit pending changes to the project"""
        if not self.project_manager.is_project_open():
            QMessageBox.warning(
                self,
                "No Project",
                "No project is currently open.\n\n"
                "Use File > New Project to create a project first."
            )
            return

        if not self.change_tracker.has_pending_changes():
            QMessageBox.information(
                self,
                "No Changes",
                "There are no pending changes to commit."
            )
            return

        # Get pending changes
        pending = self.change_tracker.get_pending_changes()

        # Get version info for dialog
        next_version = self.project_manager.get_next_version()
        rom_id = self.project_manager.current_project.original_rom.rom_id
        suggested_suffix = self.project_manager.current_project.last_suffix

        # Show commit dialog with version info
        dialog = CommitDialog(
            pending,
            next_version=next_version,
            rom_id=rom_id,
            suggested_suffix=suggested_suffix,
            parent=self
        )
        if dialog.exec() == QDialog.Accepted:
            try:
                message = dialog.get_commit_message()
                create_snapshot = dialog.get_create_snapshot()
                snapshot_suffix = dialog.get_snapshot_suffix()

                # Save changes to working ROM file first
                document = self.get_current_document()
                if document:
                    document.rom_reader.save_rom()

                # Create commit with version numbering
                commit = self.project_manager.commit_changes(
                    message=message,
                    changes=pending,
                    create_snapshot=create_snapshot,
                    snapshot_suffix=snapshot_suffix
                )

                # Clear pending changes
                self.change_tracker.clear_pending_changes()

                # Update UI
                self._update_project_ui()

                logger.info(f"Committed v{commit.version}: {message[:50]}...")
                self.statusBar().showMessage(f"Saved version {commit.version}")

            except Exception as e:
                handle_rom_operation_error(self, "commit changes", e)

    def show_history(self):
        """Show commit history viewer"""
        if not self.project_manager.is_project_open():
            QMessageBox.information(
                self,
                "No Project",
                "Open a project to view commit history."
            )
            return

        dialog = HistoryViewer(self.project_manager, self)
        dialog.view_table_diff.connect(self._on_view_table_diff)
        dialog.exec()

    def _on_view_table_diff(self, table_name: str, commit):
        """
        Open a table viewer showing changes from a specific commit

        Args:
            table_name: Name of the table to view
            commit: Commit object containing the changes
        """
        document = self.get_current_document()
        if not document:
            return

        # Find the table definition
        table = document.rom_definition.get_table_by_name(table_name)
        if not table:
            QMessageBox.warning(
                self,
                "Table Not Found",
                f"Could not find table: {table_name}"
            )
            return

        try:
            # Load base version data (previous version)
            base_version = commit.version - 1 if commit.version > 0 else 0
            base_rom_data = self.project_manager.load_version_data(base_version)

            if base_rom_data is None:
                QMessageBox.warning(
                    self,
                    "Version Not Found",
                    f"Could not load base version {base_version} data."
                )
                return

            # Create a temporary RomReader to read base version table data
            from src.core.rom_reader import RomReader
            import tempfile
            import os

            # Write base ROM to temp file and read table data
            with tempfile.NamedTemporaryFile(delete=False, suffix='.bin') as tmp:
                tmp.write(base_rom_data)
                tmp_path = tmp.name

            try:
                base_reader = RomReader(tmp_path, document.rom_definition)
                base_data = base_reader.read_table_data(table)
            finally:
                os.unlink(tmp_path)

            # Read current version table data
            current_data = document.rom_reader.read_table_data(table)

            # Open diff viewer
            viewer_window = TableViewerWindow(
                table,
                current_data,
                document.rom_definition,
                rom_path=document.rom_path,
                parent=self,
                diff_mode=True,
                diff_base_data=base_data
            )
            viewer_window.setWindowTitle(f"{table_name} (v{base_version} → v{commit.version})")
            viewer_window.show()

        except Exception as e:
            logger.error(f"Failed to open diff view: {e}")
            QMessageBox.warning(
                self,
                "Error",
                f"Failed to open diff view: {e}"
            )

    # ========== Undo/Redo Callback Methods ==========
    # These are called by TableUndoManager when undo/redo operations occur

    def _find_table_window(self, table_address: str):
        """Find visible table viewer window by address, using cache during bulk ops."""
        # Use cache during bulk operations to avoid per-cell window scans
        cache = getattr(self, '_bulk_window_cache', None)
        if cache is not None:
            if table_address in cache:
                return cache[table_address]
            for window in self.open_table_windows:
                if window.isVisible() and window.table.address == table_address:
                    cache[table_address] = window
                    return window
            cache[table_address] = None
            return None

        # Non-bulk: scan directly
        for window in self.open_table_windows:
            if window.isVisible() and window.table.address == table_address:
                return window
        return None

    def _apply_cell_change_from_undo(self, change: CellChange):
        """
        Apply a cell change to open table viewers and ROM data.
        Called by TableUndoManager during undo/redo operations.
        """
        window = self._find_table_window(change.table_address)
        if window:
            # Update the viewer display
            window.viewer.update_cell_value(
                change.row, change.col, change.new_value
            )

            # Write to the ROM that owns this table (not the active tab)
            document = self._find_document_by_rom_path(window.rom_path)
            if document:
                try:
                    document.rom_reader.write_cell_value(
                        window.table, change.row, change.col, change.new_raw
                    )
                except Exception as e:
                    logger.error(f"Failed to write cell value during undo/redo: {e}")

        logger.debug(f"Applied cell change: {change.table_name}[{change.row},{change.col}]")

    def _apply_axis_change_from_undo(self, change: AxisChange):
        """
        Apply an axis change to open table viewers and ROM data.
        Called by TableUndoManager during undo/redo operations.
        """
        window = self._find_table_window(change.table_address)
        if window:
            # Update the viewer display
            window.viewer.update_axis_cell_value(
                change.axis_type, change.index, change.new_value
            )

            # Write to the ROM that owns this table (not the active tab)
            document = self._find_document_by_rom_path(window.rom_path)
            if document:
                try:
                    document.rom_reader.write_axis_value(
                        window.table, change.axis_type, change.index, change.new_raw
                    )
                except Exception as e:
                    logger.error(f"Failed to write axis value during undo/redo: {e}")

        logger.debug(f"Applied axis change: {change.table_name}[{change.axis_type}][{change.index}]")

    def _update_pending_from_undo(self, change: CellChange, is_undo: bool):
        """
        Update pending changes tracking during undo/redo.
        Called by TableUndoManager to keep change tracker in sync.

        Note: change_tracker._notify_change() fires _on_changes_updated callback,
        which handles _update_project_ui(). No direct call needed here.
        """
        self.change_tracker.update_pending_from_undo(change, is_undo)

    def _begin_bulk_update(self, table_address: str = None):
        """
        Begin bulk update on the table viewer window for the given table.
        Called by undo commands before applying multiple changes for performance.

        Args:
            table_address: Address of table being edited (if None, affects all visible windows)
        """
        self._in_bulk_undo = True  # Defer _update_project_ui calls
        self._bulk_window_cache = {}  # Cache window lookups during bulk
        self._bulk_update_windows = []  # Track which windows we started
        for window in self.open_table_windows:
            if window.isVisible():
                if table_address is None or window.table.address == table_address:
                    window.viewer.begin_bulk_update()
                    self._bulk_update_windows.append(window)

    def _end_bulk_update(self, table_address: str = None):
        """
        End bulk update on table viewer windows.
        Called by undo commands after applying multiple changes.

        Args:
            table_address: Address of table being edited (unused, we use tracked windows)
        """
        # End bulk update on exactly the windows we started (not based on visibility)
        for window in getattr(self, '_bulk_update_windows', []):
            try:
                window.viewer.end_bulk_update()
            except RuntimeError:
                # Window might have been deleted
                pass
        self._bulk_update_windows = []
        self._in_bulk_undo = False
        del self._bulk_window_cache  # Clear window cache
        # Single deferred UI update for all changes in this bulk operation
        self._update_project_ui()

    def _on_changes_updated(self):
        """Called when change tracker state changes (via _notify_change callback)"""
        # During bulk undo, defer UI updates until _end_bulk_update calls it once
        if not getattr(self, '_in_bulk_undo', False):
            self._update_project_ui()

    def _update_project_ui(self):
        """Update UI elements based on project/change state"""
        # Note: undo/redo action enabled state is managed automatically by QUndoGroup

        # Update commit action
        has_project = self.project_manager.is_project_open()
        has_changes = self.change_tracker.has_pending_changes()
        self.commit_action.setEnabled(has_project and has_changes)

        # Update window title
        if has_project:
            project = self.project_manager.current_project
            modified_marker = "*" if has_changes else ""
            self.setWindowTitle(f"{APP_NAME} - {project.name}{modified_marker}")
        elif self.tab_widget.currentIndex() >= 0:
            document = self.get_current_document()
            if document:
                self.setWindowTitle(f"{APP_NAME} - {document.get_tab_title()}")
        else:
            self.setWindowTitle(APP_NAME)

        # Update table browser colors to show modified tables
        self._update_modified_table_colors()

    def _update_modified_table_colors(self):
        """Update table browser to show modified tables in pink"""
        # Get list of modified table addresses from change tracker
        modified_addresses = self.change_tracker.get_modified_table_addresses()

        # Update all open ROM documents' table browsers
        for i in range(self.tab_widget.count()):
            document = self.tab_widget.widget(i)
            if hasattr(document, 'table_browser'):
                document.table_browser.update_modified_tables_by_address(modified_addresses)

    def _on_table_cell_changed(self, table, row: int, col: int,
                               old_value: float, new_value: float,
                               old_raw: float, new_raw: float):
        """Handle cell change from table viewer window"""
        # Record to undo manager (per-table undo/redo)
        self.table_undo_manager.record_cell_change(
            table, row, col,
            old_value, new_value,
            old_raw, new_raw
        )

        # Record to change tracker (for pending changes / commit tracking)
        self.change_tracker.record_pending_change(
            table, row, col,
            old_value, new_value,
            old_raw, new_raw
        )

        # Write to the ROM that owns this table (sender is the TableViewerWindow)
        sender = self.sender()
        rom_path = getattr(sender, 'rom_path', None)
        document = self._find_document_by_rom_path(rom_path) if rom_path else self.get_current_document()
        if document:
            try:
                document.rom_reader.write_cell_value(table, row, col, new_raw)

                if not document.is_modified():
                    document.set_modified(True)
            except Exception as e:
                logger.error(f"Failed to write cell value: {e}")

    def _on_table_bulk_changes(self, table, changes: list, description: str = "Bulk Operation"):
        """Handle bulk changes from table viewer window (data manipulation operations)"""
        if not changes:
            return

        # Record to undo manager (per-table undo/redo)
        self.table_undo_manager.record_bulk_cell_changes(table, changes, description)

        # Record to change tracker (for pending changes / commit tracking)
        self.change_tracker.record_pending_bulk_changes(table, changes)

        # Write to the ROM that owns this table (sender is the TableViewerWindow)
        sender = self.sender()
        rom_path = getattr(sender, 'rom_path', None)
        document = self._find_document_by_rom_path(rom_path) if rom_path else self.get_current_document()
        if document:
            try:
                for row, col, old_value, new_value, old_raw, new_raw in changes:
                    document.rom_reader.write_cell_value(table, row, col, new_raw)

                if not document.is_modified():
                    document.set_modified(True)

                logger.debug(f"Applied bulk changes: {len(changes)} cells in {table.name}")
            except Exception as e:
                logger.error(f"Failed to write bulk changes: {e}")

    def _on_table_axis_changed(self, table, axis_type: str, index: int,
                               old_value: float, new_value: float,
                               old_raw: float, new_raw: float):
        """Handle axis change from table viewer window"""
        # Record to undo manager (per-table undo/redo)
        self.table_undo_manager.record_axis_change(
            table, axis_type, index,
            old_value, new_value,
            old_raw, new_raw
        )

        # Write to the ROM that owns this table (sender is the TableViewerWindow)
        sender = self.sender()
        rom_path = getattr(sender, 'rom_path', None)
        document = self._find_document_by_rom_path(rom_path) if rom_path else self.get_current_document()
        if document:
            try:
                document.rom_reader.write_axis_value(table, axis_type, index, new_raw)

                if not document.is_modified():
                    document.set_modified(True)
            except Exception as e:
                logger.error(f"Failed to write axis value: {e}")

    def _on_table_axis_bulk_changes(self, table, changes: list, description: str = "Axis Bulk Operation"):
        """Handle axis bulk changes from table viewer window (interpolation, etc.)"""
        if not changes:
            return

        # Record to undo manager (per-table undo/redo)
        self.table_undo_manager.record_axis_bulk_changes(table, changes, description)

        # Write to the ROM that owns this table (sender is the TableViewerWindow)
        sender = self.sender()
        rom_path = getattr(sender, 'rom_path', None)
        document = self._find_document_by_rom_path(rom_path) if rom_path else self.get_current_document()
        if document:
            try:
                for axis_type, index, old_value, new_value, old_raw, new_raw in changes:
                    document.rom_reader.write_axis_value(table, axis_type, index, new_raw)

                if not document.is_modified():
                    document.set_modified(True)

                logger.debug(f"Applied axis bulk changes: {len(changes)} cells in {table.name}")
            except Exception as e:
                logger.error(f"Failed to write axis bulk changes: {e}")

    def _on_table_window_focused(self, table_address: str):
        """
        Handle table viewer window gaining focus - highlight corresponding tree item
        and activate the correct undo stack.

        Args:
            table_address: Address of the table that was focused
        """
        # Activate the undo stack for this table (enables per-table undo/redo)
        self.table_undo_manager.set_active_stack(table_address)

        # Find the document containing this table and select it in the tree
        document = self.get_current_document()
        if document and hasattr(document, 'table_browser'):
            document.table_browser.select_table_by_address(table_address)

    def _update_tab_title(self, document):
        """Update tab title to show modified state"""
        tab_index = self.tab_widget.indexOf(document)
        if tab_index >= 0:
            title = document.file_name
            if document.is_modified():
                title = f"*{title}"
            self.tab_widget.setTabText(tab_index, title)

    def _restore_session(self):
        """Restore files from previous session"""
        session_files = self.settings.get_session_files()

        if not session_files:
            return

        logger.info(f"Restoring session: {len(session_files)} file(s)")

        for file_path in session_files:
            if Path(file_path).exists():
                try:
                    self._open_rom_file(file_path)
                except Exception as e:
                    logger.warning(f"Failed to restore session file: {file_path} - {e}")
            else:
                logger.warning(f"Session file no longer exists: {file_path}")

    def closeEvent(self, event):
        """Save session state before closing"""
        # Collect paths of all open ROM documents
        open_files = []
        for i in range(self.tab_widget.count()):
            document = self.tab_widget.widget(i)
            if document and hasattr(document, 'rom_path'):
                open_files.append(document.rom_path)

        # Save to settings
        self.settings.set_session_files(open_files)
        logger.info(f"Session saved: {len(open_files)} file(s)")

        # Accept close event
        event.accept()


def main():
    """Application entry point"""
    # Initialize logging before anything else
    # Default: INFO level to console, optionally to file
    log_file = Path.home() / ".nc-rom-editor" / "nc-rom-editor.log"
    setup_logging(
        level=logging.INFO,
        log_file=str(log_file),
        console=True,
        detailed=False
    )

    logger.info("=" * 60)
    logger.info(f"{APP_NAME} {APP_VERSION_STRING} starting")
    logger.info("=" * 60)

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)

    window = MainWindow()
    window.show()

    logger.info("Application window displayed")
    exit_code = app.exec()
    logger.info(f"Application exiting with code {exit_code}")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
