#!/usr/bin/env python3
"""
NC Flash - Main Application Entry Point

An open-source ROM editor for NC Miata ECUs
"""

import json
import os
import sys
import logging
from pathlib import Path
from datetime import datetime
from PySide6.QtWidgets import (
    QDialog,
    QApplication,
    QMainWindow,
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QLabel,
    QSplitter,
    QFileDialog,
    QMessageBox,
    QTabWidget,
    QToolButton,
    QColorDialog,
)
from PySide6.QtCore import Qt, QTimer, QSize
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPen, QPixmap
from src.ui.icons import make_icon

from src.utils.logging_config import setup_logging, get_logger
from src.utils.paths import get_app_root
from src.utils.settings import get_settings
from src.utils.constants import (
    APP_NAME,
    APP_VERSION_STRING,
    APP_DESCRIPTION,
    MAIN_WINDOW_X,
    MAIN_WINDOW_Y,
    MAIN_WINDOW_WIDTH,
    MAIN_WINDOW_HEIGHT,
    MAIN_SPLITTER_LEFT,
    MAIN_SPLITTER_RIGHT,
)
from src.core.definition_parser import load_definition
from src.core.rom_reader import RomReader
from src.core.rom_detector import RomDetector
from src.core.exceptions import (
    DefinitionError,
    RomFileError,
    RomWriteError,
    DetectionError,
    ScalingNotFoundError,
    RomReadError,
)
from src.ui.table_viewer_window import TableViewerWindow
from src.ui.log_console import LogConsole
from src.ui.setup_wizard import SetupWizard
from src.ui.rom_document import RomDocument
from src.core.project_manager import ProjectManager
from src.core.change_tracker import ChangeTracker
from src.core.table_undo_manager import (
    TableUndoManager,
    make_table_key,
    extract_table_address,
    extract_rom_path,
)
from src.core.version_models import CellChange, AxisChange

# Mixin classes — each handles one responsibility group
from src.ui.recent_files_mixin import RecentFilesMixin
from src.ui.project_mixin import ProjectMixin
from src.ui.session_mixin import SessionMixin
from src.ui.mcp_mixin import McpMixin
from src.ui.flash_mixin import FlashMixin

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
    logger.error(error_msg.replace("\n", " "))
    QMessageBox.critical(parent, "Error", error_msg)


class _DropOverlayWidget(QWidget):
    """Translucent overlay shown while dragging files over the main window."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAutoFillBackground(False)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # Semi-transparent background
        painter.fillRect(self.rect(), QBrush(QColor(40, 120, 200, 60)))

        # Dashed border
        pen = QPen(QColor(40, 120, 200, 180), 3, Qt.DashLine)
        painter.setPen(pen)
        margin = 12
        painter.drawRoundedRect(
            margin,
            margin,
            self.width() - 2 * margin,
            self.height() - 2 * margin,
            12,
            12,
        )

        # Centered text
        painter.setPen(QColor(40, 120, 200, 220))
        font = QFont()
        font.setPointSize(18)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(self.rect(), Qt.AlignCenter, "Drop ROM file here")

        painter.end()


class MainWindow(
    QMainWindow, RecentFilesMixin, ProjectMixin, SessionMixin, McpMixin, FlashMixin
):
    """Main application window"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        saved_geometry = get_settings().get_window_geometry()
        if saved_geometry:
            self.restoreGeometry(saved_geometry)
        else:
            self.setGeometry(
                MAIN_WINDOW_X, MAIN_WINDOW_Y, MAIN_WINDOW_WIDTH, MAIN_WINDOW_HEIGHT
            )

        logger.info("Initializing NC Flash")

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
            update_pending_axis=self._update_pending_from_axis_undo,
            begin_bulk_update=self._begin_bulk_update,
            end_bulk_update=self._end_bulk_update,
        )

        # Per-ROM background colors: {rom_path: QColor or None}
        # First ROM gets None (default gray), subsequent ROMs get auto-assigned tints
        self.rom_colors = {}
        self._color_palette = [
            QColor(180, 210, 240),  # soft blue
            QColor(210, 240, 180),  # soft green
            QColor(240, 210, 180),  # soft peach
            QColor(220, 190, 240),  # soft purple
            QColor(240, 230, 180),  # soft yellow
            QColor(180, 235, 220),  # soft teal
            QColor(240, 190, 210),  # soft pink
            QColor(200, 220, 200),  # soft sage
        ]
        self._next_color_index = 0

        # ROM detector initialized in _deferred_init (XML parsing is heavy)
        self.rom_detector = None

        # Singleton comparison window reference
        self.compare_window = None

        # Singleton ECU programming window
        self.ecu_window = None

        # MCP server subprocess
        self._mcp_process = None

        # Command API server (HTTP bridge for MCP → Qt main thread)
        self._command_server = None

        # Initialize UI (lightweight widget creation)
        self.init_ui()
        self.init_menu()
        self._create_toolbar()

        # Enable drag-and-drop for ROM files
        self.setAcceptDrops(True)
        self._drop_overlay = None  # lazily created in dragEnterEvent

        # Defer heavy work to after the window is shown:
        # - metadata directory check + setup wizard (modal dialog)
        # - ROM detector initialization (XML parsing)
        # - startup log message (depends on rom_detector)
        # - session restore (file I/O)
        QTimer.singleShot(0, self._deferred_init)

    def closeEvent(self, event):
        """Override QWidget.closeEvent — delegates to SessionMixin._handle_close.

        Mixin methods named closeEvent are shadowed by QWidget's C++ slot in the MRO,
        so this explicit override is required.
        """
        self._cleanup_ecu_session()
        self._handle_close(event)

    def _deferred_init(self):
        """
        Perform heavy initialization after the window is shown.

        This includes file I/O, modal dialogs, XML parsing, and session restore
        that would otherwise block the constructor and delay window display.
        """
        # Check if metadata directory is configured and valid
        if not self.check_metadata_directory():
            # Show setup wizard on first run or if metadata directory is invalid
            if not self.show_setup_wizard():
                # User cancelled setup, exit application
                logger.warning("Setup cancelled by user, exiting application")
                QMessageBox.critical(
                    self,
                    "Setup Required",
                    f"{APP_NAME} requires a metadata directory to function.\n"
                    "Application will now exit.",
                )
                # Defer exit to the event loop so Qt can clean up properly
                QTimer.singleShot(0, lambda: sys.exit(1))
                return

        # ROM detector for automatic XML matching
        try:
            metadata_dir = self.settings.get_metadata_directory()
            self.rom_detector = RomDetector(metadata_dir)
            logger.info(
                f"ROM detector initialized successfully (metadata: {metadata_dir})"
            )
        except DetectionError as e:
            logger.error(f"Failed to initialize ROM detector: {e}")
            QMessageBox.critical(
                self,
                "Initialization Error",
                f"Failed to initialize ROM detector:\n{str(e)}",
            )
            self.rom_detector = None
        except Exception as e:
            logger.exception(
                f"Unexpected error initializing ROM detector: {type(e).__name__}: {e}"
            )
            QMessageBox.critical(
                self,
                "Initialization Error",
                f"Unexpected error initializing ROM detector:\n{type(e).__name__}: {e}",
            )
            self.rom_detector = None

        # Log startup message (depends on rom_detector)
        self.log_startup_message()

        # Restore previous session (file I/O)
        self._restore_session()

        # Auto-start MCP server if enabled in settings
        if self.settings.get_mcp_auto_start():
            self._start_mcp_server()

    def check_metadata_directory(self) -> bool:
        """
        Check if metadata directory is configured and valid

        Returns:
            bool: True if valid, False if needs configuration
        """
        metadata_dir = self.settings.get_metadata_directory()

        # Check if path exists
        metadata_path = Path(metadata_dir)
        if not metadata_path.exists() or not metadata_path.is_dir():
            logger.warning(f"Metadata directory does not exist: {metadata_dir}")
            return False

        # Check if directory contains at least one XML file
        xml_files = list(metadata_path.glob("*.xml"))
        if not xml_files:
            logger.warning(f"No XML files found in metadata directory: {metadata_dir}")
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
        menubar.setStyleSheet("QMenuBar::item { padding: 2px 6px; }")

        # File menu (Alt+F)
        self.file_menu = menubar.addMenu("&File")

        new_project_action = self.file_menu.addAction("New Project...")
        new_project_action.triggered.connect(self.new_project)

        open_action = self.file_menu.addAction("Open...")
        open_action.setShortcut("Ctrl+O")
        open_action.triggered.connect(self.open_file)

        save_action = self.file_menu.addAction("Save")
        save_action.setShortcut("Ctrl+S")
        save_action.triggered.connect(self._save)

        save_as_action = self.file_menu.addAction("Save ROM As...")
        save_as_action.triggered.connect(self.save_rom_as)

        self.file_menu.addSeparator()

        self.commit_action = self.file_menu.addAction("Commit Changes...")
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

        # Edit menu (Alt+E)
        edit_menu = menubar.addMenu("&Edit")

        # Use QUndoGroup's createUndoAction/createRedoAction for per-table undo/redo
        # These actions automatically enable/disable based on active stack state
        self.undo_action = self.table_undo_manager.undo_group.createUndoAction(
            self, "Undo"
        )
        self.undo_action.setShortcut("Ctrl+Z")
        edit_menu.addAction(self.undo_action)

        self.redo_action = self.table_undo_manager.undo_group.createRedoAction(
            self, "Redo"
        )
        self.redo_action.setShortcut("Ctrl+Y")
        edit_menu.addAction(self.redo_action)

        edit_menu.addSeparator()

        settings_action = edit_menu.addAction("Settings...")
        settings_action.triggered.connect(self.show_settings)

        # View menu (Alt+V)
        view_menu = menubar.addMenu("&View")
        history_action = view_menu.addAction("Commit History...")
        history_action.triggered.connect(self.show_history)

        # Tools menu (Alt+T)
        tools_menu = menubar.addMenu("&Tools")

        self.compare_action = tools_menu.addAction("Compare Open &ROMs...")
        self.compare_action.setShortcut("Ctrl+Shift+D")
        self.compare_action.triggered.connect(self._on_compare_roms)
        self.compare_action.setEnabled(False)

        patch_action = tools_menu.addAction("&Patch ROM...")
        patch_action.triggered.connect(self._on_patch_rom)

        tools_menu.addSeparator()

        self.mcp_action = tools_menu.addAction("&MCP Server")
        self.mcp_action.setCheckable(True)
        self.mcp_action.triggered.connect(self._toggle_mcp_server)

        tools_menu.addSeparator()

        ecu_prog_action = tools_menu.addAction("ECU &Programming...")
        ecu_prog_action.setShortcut("Ctrl+Shift+E")
        ecu_prog_action.triggered.connect(self._on_open_ecu_window)

        # Help menu (Alt+H)
        help_menu = menubar.addMenu("&Help")

        about_action = help_menu.addAction("About")
        about_action.triggered.connect(self.show_about)

    def _create_toolbar(self):
        """Create the main window toolbar with quick-access buttons."""
        tb = self.addToolBar("Main")
        tb.setObjectName("mainToolbar")
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setIconSize(QSize(20, 20))
        tb.setStyleSheet("""
            QToolBar {
                spacing: 1px;
                padding: 1px 4px;
                border: none;
            }
            QToolButton {
                padding: 3px;
                border: 1px solid transparent;
                border-radius: 3px;
            }
            QToolButton:hover {
                background: rgba(128, 128, 128, 0.15);
                border: 1px solid rgba(128, 128, 128, 0.25);
            }
            QToolButton:pressed {
                background: rgba(128, 128, 128, 0.3);
            }
        """)

        act = tb.addAction(self._make_icon("open"), "")
        act.setToolTip("Open  (Ctrl+O)")
        act.triggered.connect(self.open_file)

        act = tb.addAction(self._make_icon("save"), "")
        act.setToolTip("Save  (Ctrl+S)")
        act.triggered.connect(self._save)

        tb.addSeparator()

        act = tb.addAction(self._make_icon("compare"), "")
        act.setToolTip("Compare Open ROMs  (Ctrl+Shift+D)")
        act.triggered.connect(self._on_compare_roms)
        self._toolbar_compare = act

        act = tb.addAction(self._make_icon("history"), "")
        act.setToolTip("Version History")
        act.triggered.connect(self.show_history)
        act.setEnabled(False)
        self._toolbar_history = act

        act = tb.addAction(self._make_icon("flash"), "")
        act.setToolTip("ECU Programming (Ctrl+Shift+E)")
        act.triggered.connect(self._on_open_ecu_window)
        self._toolbar_flash = act

        tb.addSeparator()

        self._toolbar_mcp = tb.addAction(self._make_icon("mcp_off"), "")
        self._toolbar_mcp.setToolTip("MCP Server (off)")
        self._toolbar_mcp.triggered.connect(self._toggle_mcp_server)

        act = tb.addAction(self._make_icon("settings"), "")
        act.setToolTip("Settings")
        act.triggered.connect(self.show_settings)

    def _make_icon(self, name: str) -> QIcon:
        """Create a crisp toolbar icon by name using QPainter."""
        return make_icon(self, name)

    # ========== Tab and Document Management ==========

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
            if hasattr(doc, "rom_path") and _Path(doc.rom_path) == target:
                return doc
        logger.warning(f"No document found for rom_path={rom_path}")
        return None

    def _find_open_tab(self, *, rom_path=None, project_path=None):
        """Find an already-open tab by ROM file path or project path.

        Returns the tab index, or -1 if not found.
        """
        for i in range(self.tab_widget.count()):
            doc = self.tab_widget.widget(i)
            if not hasattr(doc, "rom_path"):
                continue
            if rom_path and Path(doc.rom_path) == Path(rom_path):
                return i
            if (
                project_path
                and getattr(doc, "project_path", None)
                and Path(doc.project_path) == Path(project_path)
            ):
                return i
        return -1

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
                QMessageBox.Save,
            )

            if response == QMessageBox.Cancel:
                return
            elif response == QMessageBox.Save:
                document.save()

        # Clean up all state tied to this ROM before removing the tab
        if document:
            # Use rom_reader.rom_path (Path) for consistent comparison
            # (document.rom_path is str, window.rom_path is Path)
            rom_path = (
                document.rom_reader.rom_path
                if hasattr(document, "rom_reader") and document.rom_reader
                else None
            )

            # Close all open table windows belonging to this ROM
            windows_to_close = [
                w for w in self.open_table_windows if w.rom_path == rom_path
            ]
            for window in windows_to_close:
                window.close()

            # Collect composite keys for this ROM's tables
            table_keys = set()
            if hasattr(document, "rom_reader") and document.rom_reader:
                definition = document.rom_reader.definition
                if definition:
                    for table in definition.tables:
                        table_keys.add(make_table_key(rom_path, table.address))

            # Remove undo stacks for this ROM's tables (composite keys prevent
            # accidentally destroying stacks belonging to other open ROMs)
            self.table_undo_manager.remove_stacks_for_keys(table_keys)

            # Clear pending changes for this ROM's tables
            self.change_tracker.clear_pending_for_keys(table_keys)

            # Clear per-ROM tracking dicts
            if rom_path:
                self.modified_cells.pop(rom_path, None)
                self.original_table_values.pop(rom_path, None)
                self.rom_colors.pop(rom_path, None)

        # Remove the tab and schedule widget cleanup
        self.tab_widget.removeTab(index)
        if document:
            document.deleteLater()
        self.update_window_title()

        logger.info(f"Closed ROM tab: {document.file_name if document else 'unknown'}")
        self._update_compare_action()
        self._write_workspace_state()

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
        self._update_compare_action()

    def _assign_rom_color(self, rom_path):
        """Assign a background color for a newly opened ROM.
        First ROM gets None (default gray), subsequent ROMs get palette colors."""
        if not self.rom_colors:
            # First ROM — keep default gray
            self.rom_colors[rom_path] = None
        else:
            color = self._color_palette[
                self._next_color_index % len(self._color_palette)
            ]
            self.rom_colors[rom_path] = color
            self._next_color_index += 1
        return self.rom_colors[rom_path]

    def _create_tab_color_button(self, rom_path, tab_index):
        """Create a small color swatch button on the left side of a tab."""
        color = self.rom_colors.get(rom_path)
        btn = QToolButton()
        btn.setFixedSize(16, 16)
        btn.setAutoRaise(True)
        self._style_color_button(btn, color)
        btn.clicked.connect(lambda: self._pick_rom_color(rom_path))
        self.tab_widget.tabBar().setTabButton(
            tab_index, self.tab_widget.tabBar().ButtonPosition.LeftSide, btn
        )

    def _style_color_button(self, btn, color):
        """Apply color swatch styling to a tab button."""
        if color:
            btn.setStyleSheet(
                f"QToolButton {{ background-color: {color.name()}; border: 1px solid #888; border-radius: 3px; }}"
                f"QToolButton:hover {{ border: 1px solid #444; }}"
            )
        else:
            # Default gray — use system window color
            btn.setStyleSheet(
                "QToolButton { background-color: palette(window); border: 1px solid #888; border-radius: 3px; }"
                "QToolButton:hover { border: 1px solid #444; }"
            )

    def _pick_rom_color(self, rom_path):
        """Open color picker for a ROM and apply the chosen color."""
        current = self.rom_colors.get(rom_path)
        initial = current if current else self.palette().window().color()
        color = QColorDialog.getColor(initial, self, "Choose ROM color")
        if not color.isValid():
            return
        self.rom_colors[rom_path] = color

        # Update the tab color button
        for i in range(self.tab_widget.count()):
            doc = self.tab_widget.widget(i)
            if (
                doc
                and hasattr(doc, "rom_reader")
                and doc.rom_reader
                and doc.rom_reader.rom_path == rom_path
            ):
                btn = self.tab_widget.tabBar().tabButton(
                    i, self.tab_widget.tabBar().ButtonPosition.LeftSide
                )
                if btn:
                    self._style_color_button(btn, color)
                break

        # Update all open table viewer windows for this ROM
        for window in self.open_table_windows:
            if window.rom_path == rom_path:
                window.set_rom_color(color)

    def _update_tab_title(self, document):
        """Update tab title to show modified state"""
        tab_index = self.tab_widget.indexOf(document)
        if tab_index >= 0:
            title = document.file_name
            if document.is_modified():
                title = f"*{title}"
            self.tab_widget.setTabText(tab_index, title)

    # ========== ROM I/O ==========

    def open_file(self):
        """Open a ROM file or project via file dialog."""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Open ROM File", "", "ROM Files (*.bin *.rom);;All Files (*)"
        )
        if not file_path:
            return

        parent = Path(file_path).parent
        if ProjectManager.is_project_folder(str(parent)):
            self.open_project_path(str(parent))
        else:
            self._open_rom_file(file_path)

    # ------------------------------------------------------------------
    # Drag-and-drop support
    # ------------------------------------------------------------------

    #: File extensions accepted via drag-and-drop (matches File > Open dialog)
    _DROP_EXTENSIONS = {".bin", ".rom"}

    def _get_drop_file_paths(self, mime_data):
        """Extract file paths from drop MIME data, returning only valid ROM files.

        Returns:
            list[str]: Paths with valid ROM extensions, or empty list.
        """
        if not mime_data.hasUrls():
            return []
        paths = []
        for url in mime_data.urls():
            if url.isLocalFile():
                path = url.toLocalFile()
                if Path(path).suffix.lower() in self._DROP_EXTENSIONS:
                    paths.append(path)
        return paths

    def dragEnterEvent(self, event):
        """Accept the drag if it contains at least one valid ROM file."""
        paths = self._get_drop_file_paths(event.mimeData())
        if paths:
            event.acceptProposedAction()
            self._show_drop_overlay()
        else:
            # Check if the user is dragging files with invalid extensions
            if event.mimeData().hasUrls():
                event.ignore()
            else:
                event.ignore()

    def dragMoveEvent(self, event):
        """Continue accepting the drag while over the window."""
        if self._get_drop_file_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        """Remove visual feedback when drag leaves the window."""
        self._hide_drop_overlay()
        event.accept()

    def dropEvent(self, event):
        """Open dropped ROM files."""
        self._hide_drop_overlay()

        paths = self._get_drop_file_paths(event.mimeData())
        if not paths:
            # Files were dropped but none had valid extensions
            if event.mimeData().hasUrls():
                rejected = [
                    url.toLocalFile()
                    for url in event.mimeData().urls()
                    if url.isLocalFile()
                ]
                ext_list = ", ".join(sorted(self._DROP_EXTENSIONS))
                names = "\n".join(Path(p).name for p in rejected[:5])
                if len(rejected) > 5:
                    names += f"\n... and {len(rejected) - 5} more"
                QMessageBox.warning(
                    self,
                    "Unsupported File Type",
                    f"Cannot open the dropped file(s):\n\n{names}\n\n"
                    f"Supported extensions: {ext_list}",
                )
            event.ignore()
            return

        event.acceptProposedAction()
        logger.info(f"Drag-and-drop: opening {len(paths)} file(s)")

        for file_path in paths:
            parent = Path(file_path).parent
            if ProjectManager.is_project_folder(str(parent)):
                self.open_project_path(str(parent))
            else:
                self._open_rom_file(file_path)

    def _show_drop_overlay(self):
        """Show a translucent overlay indicating the drop zone is active."""
        if self._drop_overlay is None:
            self._drop_overlay = _DropOverlayWidget(self)
        self._drop_overlay.setGeometry(self.centralWidget().geometry())
        self._drop_overlay.show()
        self._drop_overlay.raise_()

    def _hide_drop_overlay(self):
        """Hide the drop-zone overlay."""
        if self._drop_overlay is not None:
            self._drop_overlay.hide()

    def _write_workspace_state(self):
        """Write workspace.json listing all open ROMs for MCP server discovery.

        Deletes the file if no ROMs are open. Never raises — this is a
        convenience file and must not crash the app.
        """
        try:
            workspace_path = get_app_root() / "workspace.json"
            if self.tab_widget.count() == 0:
                workspace_path.unlink(missing_ok=True)
                return

            active_index = self.tab_widget.currentIndex()
            active_rom = None
            open_roms = []

            for i in range(self.tab_widget.count()):
                doc = self.tab_widget.widget(i)
                if not hasattr(doc, "rom_path"):
                    continue
                romid = doc.rom_definition.romid
                entry = {
                    "rom_path": doc.rom_path,
                    "file_name": doc.file_name,
                    "xmlid": romid.xmlid,
                    "make": romid.make,
                    "model": romid.model,
                    "year": romid.year,
                    "is_modified": doc.is_modified(),
                }
                open_roms.append(entry)
                if i == active_index:
                    active_rom = doc.rom_path

            if not open_roms:
                workspace_path.unlink(missing_ok=True)
                return

            state = {
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "active_rom": active_rom,
                "open_roms": open_roms,
            }
            # Include command API URL when the bridge server is running
            if self._command_server is not None and self._command_server.is_running:
                state["command_api_url"] = self._command_server.url
            workspace_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except Exception:
            logger.debug("Failed to write workspace.json", exc_info=True)

    def _delete_workspace_state(self):
        """Delete workspace.json (called on app exit)."""
        try:
            (get_app_root() / "workspace.json").unlink(missing_ok=True)
        except Exception:
            logger.debug("Failed to delete workspace.json", exc_info=True)

    # MCP server, command API, and API handlers are in McpMixin (src/ui/mcp_mixin.py)

    def _open_rom_file(self, file_path: str):
        """
        Open a ROM file from a given path

        Args:
            file_path: Full path to ROM file
        """
        # Prevent opening the same ROM twice
        existing = self._find_open_tab(rom_path=file_path)
        if existing >= 0:
            self.tab_widget.setCurrentIndex(existing)
            QMessageBox.information(
                self,
                "Already Open",
                f"This ROM is already open.\n\n{Path(file_path).name}",
            )
            return

        try:
            logger.info(f"Opening ROM file: {file_path}")
            self.statusBar().showMessage(f"Detecting ROM ID...")

            # Detect ROM ID and find matching XML definition
            if not self.rom_detector:
                logger.error("ROM detector not initialized")
                QMessageBox.critical(
                    self,
                    "Error",
                    "ROM detector not initialized. Cannot auto-detect ROM type.",
                )
                return

            rom_id, xml_path = self.rom_detector.detect_rom_id(file_path)

            if not rom_id or not xml_path:
                logger.warning(f"No matching ROM definition found for {file_path}")
                defs = self.rom_detector.get_definitions_summary()
                QMessageBox.critical(
                    self,
                    "Unknown ROM",
                    "Could not identify ROM type. No matching definition found.\n\n"
                    f"{len(defs)} ROM definitions are available. "
                    "Check that the file is a supported ROM image.",
                )
                return

            # Load the matching definition
            logger.info(f"Detected ROM ID: {rom_id}")
            self.statusBar().showMessage(
                f"Detected ROM ID: {rom_id}, loading definition..."
            )
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
                    f"This may indicate a detection bug.",
                )

            # Create ROM document widget
            rom_document = RomDocument(file_path, rom_definition, rom_reader, self)
            rom_document.table_selected.connect(self.on_table_selected)
            rom_document.modified_changed.connect(
                lambda modified, doc=rom_document: self._update_tab_title(doc)
            )

            # Assign a color for this ROM (first ROM = default gray)
            rom_path = rom_reader.rom_path
            self._assign_rom_color(rom_path)

            # Add as new tab with color swatch
            file_name = Path(file_path).name
            tab_index = self.tab_widget.addTab(rom_document, file_name)
            self.tab_widget.setTabToolTip(tab_index, file_path)
            self._create_tab_color_button(rom_path, tab_index)
            self.tab_widget.setCurrentIndex(tab_index)

            # Add to recent files list
            self.settings.add_recent_file(file_path)
            self.update_recent_files_menu()

            # Log to console
            logger.info(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            logger.info(f"ROM LOADED: {file_name}")
            logger.info(f"  ROM ID: {rom_id}")
            logger.info(f"  Definition: {rom_definition.romid.xmlid}")
            logger.info(
                f"  Make/Model: {rom_definition.romid.make} {rom_definition.romid.model}"
            )
            logger.info(f"  Tables: {len(rom_definition.tables)}")
            logger.info(f"  Size: {len(rom_reader.rom_data):,} bytes")
            logger.info(f"  Tab: {tab_index + 1}")
            logger.info(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

            self.statusBar().showMessage(
                f"Loaded: {file_name} - {rom_definition.romid.xmlid} "
                f"({len(rom_definition.tables)} tables)"
            )

            self._update_compare_action()
            self._write_workspace_state()

        except (DetectionError, RomFileError, DefinitionError) as e:
            handle_rom_operation_error(self, "open ROM file", e)
        except Exception as e:
            logger.error(
                f"Unexpected error opening ROM file: {type(e).__name__}: {e}",
                exc_info=True,
            )
            QMessageBox.critical(
                self,
                "Error",
                f"Unexpected error opening ROM file:\n{type(e).__name__}: {e}",
            )

    def _save(self):
        """Unified save: commit if project is open with changes, otherwise save ROM."""
        if (
            self.project_manager.is_project_open()
            and self.change_tracker.has_pending_changes()
        ):
            self.commit_changes()
        else:
            self.save_rom()

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
            self._write_workspace_state()
            QMessageBox.information(
                self, "Success", f"ROM saved successfully to:\n{document.rom_path}"
            )
        except RomFileError as e:
            handle_rom_operation_error(self, "save ROM file", e)
        except Exception as e:
            logger.error(
                f"Unexpected error saving ROM file: {type(e).__name__}: {e}",
                exc_info=True,
            )
            QMessageBox.critical(
                self,
                "Error",
                f"Unexpected error saving ROM file:\n{type(e).__name__}: {e}",
            )

    def save_rom_as(self):
        """Save the ROM to a new file"""
        document = self.get_current_document()
        if not document:
            QMessageBox.warning(self, "No ROM", "No ROM file is currently loaded.")
            return

        file_path, _ = QFileDialog.getSaveFileName(
            self, "Save ROM File As", "", "ROM Files (*.bin);;All Files (*)"
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
                    self, "Success", f"ROM saved successfully to:\n{file_path}"
                )
            except RomFileError as e:
                handle_rom_operation_error(self, "save ROM file", e)
            except Exception as e:
                logger.error(
                    f"Unexpected error saving ROM file: {type(e).__name__}: {e}",
                    exc_info=True,
                )
                QMessageBox.critical(
                    self,
                    "Error",
                    f"Unexpected error saving ROM file:\n{type(e).__name__}: {e}",
                )

    # ========== ROM Comparison ==========

    def _update_compare_action(self):
        """Enable/disable the Compare and Flash actions based on open ROM count."""
        compare_enabled = self.tab_widget.count() >= 2
        self.compare_action.setEnabled(compare_enabled)
        if hasattr(self, "_toolbar_compare"):
            self._toolbar_compare.setEnabled(compare_enabled)

    def apply_compare_copy(
        self,
        dst_reader: "RomReader",
        dst_table: "Table",
        dst_definition: "RomDefinition",
        src_data: dict,
    ):
        """Apply a table copy from the compare window through the full edit pipeline.

        This routes through undo, change tracking, ROM write, and modified
        indicators — identical to a manual paste operation.

        Args:
            dst_reader: RomReader of the destination ROM
            dst_table: Table definition in the destination ROM
            dst_definition: RomDefinition of the destination ROM
            src_data: Source data dict from read_table_data (has 'values', axes)
        """
        from src.core.rom_reader import ScalingConverter
        from src.core.rom_definition import AxisType

        rom_path = dst_reader.rom_path
        document = self._find_document_by_rom_path(rom_path)
        if not document:
            return

        # --- Capture pre-copy originals for border tracking ---
        # Must happen before ROM writes so _check_and_remove_border_if_original
        # uses the true original when undoing (even if the table viewer opens later).
        import numpy as np

        old_data = dst_reader.read_table_data(dst_table)
        if rom_path not in self.original_table_values:
            self.original_table_values[rom_path] = {}
        if dst_table.address not in self.original_table_values[rom_path]:
            self.original_table_values[rom_path][dst_table.address] = {
                "values": np.copy(old_data["values"]),
                "x_axis": (
                    np.copy(old_data["x_axis"])
                    if old_data.get("x_axis") is not None
                    else None
                ),
                "y_axis": (
                    np.copy(old_data["y_axis"])
                    if old_data.get("y_axis") is not None
                    else None
                ),
            }

        # --- Value cells ---
        old_vals = old_data["values"]
        new_vals = src_data["values"]

        scaling = dst_definition.get_scaling(dst_table.scaling)
        converter = ScalingConverter(scaling) if scaling else None

        cell_changes = []
        if old_vals.ndim == 1:
            for i in range(len(old_vals)):
                if old_vals[i] != new_vals[i]:
                    old_raw = (
                        converter.from_display(float(old_vals[i]))
                        if converter
                        else float(old_vals[i])
                    )
                    new_raw = (
                        converter.from_display(float(new_vals[i]))
                        if converter
                        else float(new_vals[i])
                    )
                    cell_changes.append(
                        (
                            i,
                            0,
                            float(old_vals[i]),
                            float(new_vals[i]),
                            float(old_raw),
                            float(new_raw),
                        )
                    )
        else:
            rows, cols = old_vals.shape
            for r in range(rows):
                for c in range(cols):
                    if old_vals[r, c] != new_vals[r, c]:
                        old_raw = (
                            converter.from_display(float(old_vals[r, c]))
                            if converter
                            else float(old_vals[r, c])
                        )
                        new_raw = (
                            converter.from_display(float(new_vals[r, c]))
                            if converter
                            else float(new_vals[r, c])
                        )
                        cell_changes.append(
                            (
                                r,
                                c,
                                float(old_vals[r, c]),
                                float(new_vals[r, c]),
                                float(old_raw),
                                float(new_raw),
                            )
                        )

        if cell_changes:
            desc = f"Compare Copy: {dst_table.name}"
            self.table_undo_manager.record_bulk_cell_changes(
                dst_table, cell_changes, desc, rom_path=rom_path
            )
            self.change_tracker.record_pending_bulk_changes(
                dst_table, cell_changes, rom_path=rom_path
            )

            def write_cells():
                for row, col, _ov, _nv, _or, new_raw in cell_changes:
                    document.rom_reader.write_cell_value(dst_table, row, col, new_raw)

            self._write_to_rom_and_mark_modified(
                document, write_cells, f"compare copy in {dst_table.name}"
            )

        # --- Axis cells ---
        for axis_type, axis_key in [
            (AxisType.Y_AXIS, "y_axis"),
            (AxisType.X_AXIS, "x_axis"),
        ]:
            src_axis = src_data.get(axis_key)
            old_axis = old_data.get(axis_key)
            axis_table = dst_table.get_axis(axis_type)
            if src_axis is None or old_axis is None or axis_table is None:
                continue

            axis_scaling = dst_definition.get_scaling(axis_table.scaling)
            axis_converter = ScalingConverter(axis_scaling) if axis_scaling else None

            axis_changes = []
            for i in range(min(len(old_axis), len(src_axis))):
                if old_axis[i] != src_axis[i]:
                    old_raw = (
                        axis_converter.from_display(float(old_axis[i]))
                        if axis_converter
                        else float(old_axis[i])
                    )
                    new_raw = (
                        axis_converter.from_display(float(src_axis[i]))
                        if axis_converter
                        else float(src_axis[i])
                    )
                    axis_changes.append(
                        (
                            axis_key,
                            i,
                            float(old_axis[i]),
                            float(src_axis[i]),
                            float(old_raw),
                            float(new_raw),
                        )
                    )

            if axis_changes:
                desc = f"Compare Copy Axis: {dst_table.name}"
                self.table_undo_manager.record_axis_bulk_changes(
                    dst_table, axis_changes, desc, rom_path=rom_path
                )
                self.change_tracker.record_pending_axis_bulk_changes(
                    dst_table, axis_changes, rom_path=rom_path
                )

                def write_axes(changes=axis_changes):
                    for ax_type, idx, _ov, _nv, _or, new_raw in changes:
                        document.rom_reader.write_axis_value(
                            dst_table, ax_type, idx, new_raw
                        )

                self._write_to_rom_and_mark_modified(
                    document, write_axes, f"compare copy axis in {dst_table.name}"
                )

        # --- Update modified_cells for cell border highlighting ---
        if rom_path not in self.modified_cells:
            self.modified_cells[rom_path] = {}

        if cell_changes:
            if dst_table.address not in self.modified_cells[rom_path]:
                self.modified_cells[rom_path][dst_table.address] = set()
            for row, col, _ov, _nv, _or, _nr in cell_changes:
                self.modified_cells[rom_path][dst_table.address].add((row, col))

        # Update axis modified tracking
        for axis_type, axis_key in [
            (AxisType.Y_AXIS, "y_axis"),
            (AxisType.X_AXIS, "x_axis"),
        ]:
            ak = f"{dst_table.address}:{axis_key}"
            src_axis = src_data.get(axis_key)
            old_axis = old_data.get(axis_key)
            if src_axis is None or old_axis is None:
                continue
            for i in range(min(len(old_axis), len(src_axis))):
                if old_axis[i] != src_axis[i]:
                    if ak not in self.modified_cells[rom_path]:
                        self.modified_cells[rom_path][ak] = set()
                    self.modified_cells[rom_path][ak].add(i)

        # --- Refresh open table viewer windows showing this table ---
        from src.core.table_undo_manager import make_table_key

        table_key = make_table_key(rom_path, dst_table.address)
        window = self._find_table_window(table_key)
        if window:
            viewer = window.viewer
            viewer.begin_bulk_update()
            try:
                for row, col, _ov, new_val, _or, _nr in cell_changes:
                    viewer.update_cell_value(row, col, new_val)
            finally:
                viewer.end_bulk_update()

        self._update_tab_title(document)

    def _on_compare_roms(self):
        """Open the ROM comparison window."""
        from src.ui.compare_window import CompareWindow

        count = self.tab_widget.count()
        if count < 2:
            QMessageBox.information(
                self, "Compare", "Open at least two ROM files to compare."
            )
            return

        # Close existing compare window
        if self.compare_window is not None:
            self.compare_window.close()
            self.compare_window = None

        if count == 2:
            doc_a = self.tab_widget.widget(0)
            doc_b = self.tab_widget.widget(1)
        else:
            # Let user pick which two ROMs to compare
            rom_names = []
            for i in range(count):
                doc = self.tab_widget.widget(i)
                rom_names.append(doc.file_name)

            from PySide6.QtWidgets import QInputDialog

            name_a, ok = QInputDialog.getItem(
                self, "Compare ROMs", "Select original (base) ROM:", rom_names, 0, False
            )
            if not ok:
                return
            idx_a = rom_names.index(name_a)

            remaining = [n for i, n in enumerate(rom_names) if i != idx_a]
            name_b, ok = QInputDialog.getItem(
                self, "Compare ROMs", "Select modified ROM:", remaining, 0, False
            )
            if not ok:
                return
            idx_b = rom_names.index(name_b)

            doc_a = self.tab_widget.widget(idx_a)
            doc_b = self.tab_widget.widget(idx_b)

        # Get ROM colors
        color_a = self.rom_colors.get(doc_a.rom_reader.rom_path)
        color_b = self.rom_colors.get(doc_b.rom_reader.rom_path)

        cross_def = doc_a.rom_definition.romid.xmlid != doc_b.rom_definition.romid.xmlid
        self.statusBar().showMessage("Computing ROM differences...")

        window = CompareWindow(
            doc_a.rom_reader,
            doc_b.rom_reader,
            doc_a.rom_definition,
            doc_b.rom_definition,
            color_a,
            color_b,
            doc_a.file_name,
            doc_b.file_name,
            parent=self,
        )

        if not window.has_diffs:
            window.deleteLater()
            msg = (
                "No comparable tables found between definitions."
                if cross_def
                else "ROMs are identical \u2014 no differences found."
            )
            QMessageBox.information(self, "Compare", msg)
            self.statusBar().showMessage("No differences found.")
            return

        self.compare_window = window
        window.show()

        n = len(window._modified_tables)
        self.statusBar().showMessage(
            f"Comparing {doc_a.file_name} vs {doc_b.file_name} \u2014 {n} tables differ"
        )
        logger.info(
            f"ROM comparison opened: {doc_a.file_name} vs {doc_b.file_name} ({n} tables differ)"
        )

    def _on_open_ecu_window(self):
        """Open the ECU Programming window (singleton)."""
        from src.ui.ecu_window import ECUProgrammingWindow

        if self.ecu_window is not None:
            self.ecu_window.raise_()
            self.ecu_window.activateWindow()
            return

        window = ECUProgrammingWindow(main_window=self, parent=self)
        window.setAttribute(Qt.WA_DeleteOnClose)
        window.destroyed.connect(lambda: setattr(self, "ecu_window", None))
        self.ecu_window = window
        window.show()

    # ========== Table Selection and Window Management ==========

    def on_table_selected(self, table, rom_reader):
        """Handle table selection from browser - opens table in new window"""
        try:
            # Get ROM path for duplicate detection
            rom_path = rom_reader.rom_path

            # Check if this table is already open for this ROM
            # Use address for comparison since table names may not be unique across categories
            for window in self.open_table_windows:
                if (
                    window.rom_path == rom_path
                    and window.table.address == table.address
                ):
                    # Window already exists - bring to focus
                    window.raise_()
                    window.activateWindow()
                    rom_label = Path(rom_path).stem
                    logger.info(
                        f"[{rom_label}] Table already open, bringing to focus: {table.name}"
                    )
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
                        "x_axis": (
                            np.copy(data["x_axis"])
                            if data.get("x_axis") is not None
                            else None
                        ),
                        "y_axis": (
                            np.copy(data["y_axis"])
                            if data.get("y_axis") is not None
                            else None
                        ),
                    }

                # Initialize modified cells tracking for this ROM if needed
                if rom_path not in self.modified_cells:
                    self.modified_cells[rom_path] = {}

                # Create and show new table viewer window
                viewer_window = TableViewerWindow(
                    table,
                    data,
                    rom_reader.definition,
                    rom_path=rom_path,
                    parent=self,
                    modified_cells_dict=self.modified_cells[rom_path],
                    original_values_dict=self.original_table_values[rom_path],
                    bg_color=self.rom_colors.get(rom_path),
                )

                # Connect cell_changed signal to change tracker
                viewer_window.cell_changed.connect(self._on_table_cell_changed)

                # Connect bulk_changes signal to change tracker
                viewer_window.bulk_changes.connect(self._on_table_bulk_changes)

                # Connect axis_changed signal to change tracker
                viewer_window.axis_changed.connect(self._on_table_axis_changed)

                # Connect axis_bulk_changes signal to change tracker
                viewer_window.axis_bulk_changes.connect(
                    self._on_table_axis_bulk_changes
                )

                # Connect window focus signal to highlight table in tree and activate undo stack
                viewer_window.window_focused.connect(self._on_table_window_focused)

                viewer_window.show()

                # Track the window (removed in TableViewerWindow.closeEvent)
                self.open_table_windows.append(viewer_window)

                # Log to console
                rom_label = Path(rom_path).stem
                logger.info(
                    f"[{rom_label}] Opened table: {table.name} ({table.address})"
                )
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
                    self, "Error", f"Failed to read table data for: {table.name}"
                )

        except (ScalingNotFoundError, RomReadError) as e:
            handle_rom_operation_error(self, "load table", e)
        except Exception as e:
            logger.error(
                f"Unexpected error loading table: {type(e).__name__}: {e}",
                exc_info=True,
            )
            QMessageBox.critical(
                self,
                "Error",
                f"Unexpected error loading table:\n{type(e).__name__}: {e}",
            )

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

    # ========== Undo/Redo Callback Methods ==========
    # These are called by TableUndoManager when undo/redo operations occur

    def _find_table_window(self, table_key: str):
        """Find visible table viewer window by composite key, using cache during bulk ops.

        Args:
            table_key: Composite key (rom_path|table_address) or bare table_address
        """
        rom_path_str = extract_rom_path(table_key)
        table_address = extract_table_address(table_key)

        # Use cache during bulk operations to avoid per-cell window scans
        cache = getattr(self, "_bulk_window_cache", None)
        if cache is not None:
            if table_key in cache:
                return cache[table_key]
            for window in self.open_table_windows:
                if window.isVisible() and window.table.address == table_address:
                    if rom_path_str is None or str(window.rom_path) == rom_path_str:
                        cache[table_key] = window
                        return window
            cache[table_key] = None
            return None

        # Non-bulk: scan directly
        for window in self.open_table_windows:
            if window.isVisible() and window.table.address == table_address:
                if rom_path_str is None or str(window.rom_path) == rom_path_str:
                    return window
        return None

    def _apply_cell_change_from_undo(self, change: CellChange):
        """
        Apply a cell change to open table viewers and ROM data.
        Called by TableUndoManager during undo/redo operations.
        """
        window = self._find_table_window(change.table_key or change.table_address)
        if window:
            # Update the viewer display
            window.viewer.update_cell_value(change.row, change.col, change.new_value)

            # Write to the ROM that owns this table (not the active tab)
            document = self._find_document_by_rom_path(window.rom_path)
            if document:
                try:
                    document.rom_reader.write_cell_value(
                        window.table, change.row, change.col, change.new_raw
                    )
                except RomWriteError as e:
                    logger.error(f"Failed to write cell value during undo/redo: {e}")
                except Exception as e:
                    logger.exception(
                        f"Unexpected error writing cell value during undo/redo: {type(e).__name__}: {e}"
                    )

        logger.debug(
            f"Applied cell change: {change.table_name}[{change.row},{change.col}]"
        )

    def _apply_axis_change_from_undo(self, change: AxisChange):
        """
        Apply an axis change to open table viewers and ROM data.
        Called by TableUndoManager during undo/redo operations.
        """
        window = self._find_table_window(change.table_key or change.table_address)
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
                except RomWriteError as e:
                    logger.error(f"Failed to write axis value during undo/redo: {e}")
                except Exception as e:
                    logger.exception(
                        f"Unexpected error writing axis value during undo/redo: {type(e).__name__}: {e}"
                    )

        logger.debug(
            f"Applied axis change: {change.table_name}[{change.axis_type}][{change.index}]"
        )

    def _update_pending_from_undo(self, change: CellChange, is_undo: bool):
        """
        Update pending changes tracking during cell undo/redo.
        Called by TableUndoManager to keep change tracker in sync.

        Note: change_tracker._notify_change() fires _on_changes_updated callback,
        which handles _update_project_ui(). No direct call needed here.
        """
        self.change_tracker.update_pending_from_undo(change, is_undo)

    def _update_pending_from_axis_undo(self, change, is_undo: bool):
        """
        Update pending changes tracking during axis undo/redo.
        Called by TableUndoManager to keep change tracker in sync.
        """
        self.change_tracker.update_pending_from_axis_undo(change, is_undo)

    def _begin_bulk_update(self, table_key: str = None):
        """
        Begin bulk update on the table viewer window for the given table.
        Called by undo commands before applying multiple changes for performance.

        Args:
            table_key: Composite key (rom_path|table_address) or None for all windows
        """
        self._in_bulk_undo = True  # Defer _update_project_ui calls
        self._bulk_window_cache = {}  # Cache window lookups during bulk
        self._bulk_update_windows = []  # Track which windows we started

        rom_path_str = extract_rom_path(table_key) if table_key else None
        table_address = extract_table_address(table_key) if table_key else None

        for window in self.open_table_windows:
            if window.isVisible():
                if table_key is None or (
                    window.table.address == table_address
                    and (rom_path_str is None or str(window.rom_path) == rom_path_str)
                ):
                    window.viewer.begin_bulk_update()
                    self._bulk_update_windows.append(window)

    def _end_bulk_update(self, table_key: str = None):
        """
        End bulk update on table viewer windows.
        Called by undo commands after applying multiple changes.

        Args:
            table_key: Composite key (unused, we use tracked windows)
        """
        # End bulk update on exactly the windows we started (not based on visibility)
        for window in getattr(self, "_bulk_update_windows", []):
            try:
                window.viewer.end_bulk_update()
            except RuntimeError:
                logger.debug("Window deleted during bulk update end", exc_info=True)
        self._bulk_update_windows = []
        self._in_bulk_undo = False
        del self._bulk_window_cache  # Clear window cache
        # Single deferred UI update for all changes in this bulk operation
        self._update_project_ui()

    # ========== Change Tracking and UI Updates ==========

    def _on_changes_updated(self):
        """Called when change tracker state changes (via _notify_change callback)"""
        # During bulk undo, defer UI updates until _end_bulk_update calls it once
        if not getattr(self, "_in_bulk_undo", False):
            self._update_project_ui()

    def _update_project_ui(self):
        """Update UI elements based on project/change state"""
        # Note: undo/redo action enabled state is managed automatically by QUndoGroup

        # Update commit action and history button
        has_project = self.project_manager.is_project_open()
        has_changes = self.change_tracker.has_pending_changes()
        self.commit_action.setEnabled(has_project and has_changes)
        if hasattr(self, "_toolbar_history"):
            self._toolbar_history.setEnabled(has_project)

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
        """Update table browser to show modified tables in pink (per-ROM filtering)"""
        # Update each ROM document's table browser with only its own modified addresses
        for i in range(self.tab_widget.count()):
            document = self.tab_widget.widget(i)
            if (
                hasattr(document, "table_browser")
                and hasattr(document, "rom_reader")
                and document.rom_reader
            ):
                rom_path = document.rom_reader.rom_path
                modified_addresses = self.change_tracker.get_modified_addresses_for_rom(
                    rom_path
                )
                document.table_browser.update_modified_tables_by_address(
                    modified_addresses
                )

    # ========== Cell/Axis Change Handlers ==========

    def _get_sender_rom_context(self):
        """Get ROM path and document from the signal sender.

        Common setup for all cell/axis change handlers. Returns
        (rom_path, document) where rom_path may be None if not set
        on the sender, and document may be None if not found.
        """
        sender = self.sender()
        rom_path = getattr(sender, "rom_path", None)
        document = (
            self._find_document_by_rom_path(rom_path)
            if rom_path
            else self.get_current_document()
        )
        return rom_path, document

    def _write_to_rom_and_mark_modified(self, document, write_fn, description: str):
        """Execute a ROM write operation with standard error handling.

        Args:
            document: RomDocument to write to (may be None — no-ops safely)
            write_fn: Callable that performs the actual rom_reader write(s)
            description: Human-readable description for error messages
        """
        if not document:
            return
        try:
            write_fn()
            if not document.is_modified():
                document.set_modified(True)
        except RomWriteError as e:
            logger.error(f"Failed to write {description}: {e}")
        except Exception as e:
            logger.exception(
                f"Unexpected error writing {description}: {type(e).__name__}: {e}"
            )

    def _on_table_cell_changed(
        self,
        table,
        row: int,
        col: int,
        old_value: float,
        new_value: float,
        old_raw: float,
        new_raw: float,
    ):
        """Handle cell change from table viewer window"""
        rom_path, document = self._get_sender_rom_context()

        self.table_undo_manager.record_cell_change(
            table, row, col, old_value, new_value, old_raw, new_raw, rom_path=rom_path
        )
        self.change_tracker.record_pending_change(
            table, row, col, old_value, new_value, old_raw, new_raw, rom_path=rom_path
        )
        self._write_to_rom_and_mark_modified(
            document,
            lambda: document.rom_reader.write_cell_value(table, row, col, new_raw),
            f"cell value in {table.name}",
        )

    def _on_table_bulk_changes(
        self, table, changes: list, description: str = "Bulk Operation"
    ):
        """Handle bulk changes from table viewer window (data manipulation operations)"""
        if not changes:
            return

        rom_path, document = self._get_sender_rom_context()

        self.table_undo_manager.record_bulk_cell_changes(
            table, changes, description, rom_path=rom_path
        )
        self.change_tracker.record_pending_bulk_changes(
            table, changes, rom_path=rom_path
        )

        def write_bulk():
            for row, col, old_value, new_value, old_raw, new_raw in changes:
                document.rom_reader.write_cell_value(table, row, col, new_raw)
            logger.debug(f"Applied bulk changes: {len(changes)} cells in {table.name}")

        self._write_to_rom_and_mark_modified(
            document, write_bulk, f"bulk changes in {table.name}"
        )

    def _on_table_axis_changed(
        self,
        table,
        axis_type: str,
        index: int,
        old_value: float,
        new_value: float,
        old_raw: float,
        new_raw: float,
    ):
        """Handle axis change from table viewer window"""
        rom_path, document = self._get_sender_rom_context()

        self.table_undo_manager.record_axis_change(
            table,
            axis_type,
            index,
            old_value,
            new_value,
            old_raw,
            new_raw,
            rom_path=rom_path,
        )
        self.change_tracker.record_pending_axis_change(
            table,
            axis_type,
            index,
            old_value,
            new_value,
            old_raw,
            new_raw,
            rom_path=rom_path,
        )
        self._write_to_rom_and_mark_modified(
            document,
            lambda: document.rom_reader.write_axis_value(
                table, axis_type, index, new_raw
            ),
            f"axis value in {table.name}",
        )

    def _on_table_axis_bulk_changes(
        self, table, changes: list, description: str = "Axis Bulk Operation"
    ):
        """Handle axis bulk changes from table viewer window (interpolation, etc.)"""
        if not changes:
            return

        rom_path, document = self._get_sender_rom_context()

        self.table_undo_manager.record_axis_bulk_changes(
            table, changes, description, rom_path=rom_path
        )
        self.change_tracker.record_pending_axis_bulk_changes(
            table, changes, rom_path=rom_path
        )

        def write_bulk():
            for axis_type, index, old_value, new_value, old_raw, new_raw in changes:
                document.rom_reader.write_axis_value(table, axis_type, index, new_raw)
            logger.debug(
                f"Applied axis bulk changes: {len(changes)} cells in {table.name}"
            )

        self._write_to_rom_and_mark_modified(
            document, write_bulk, f"axis bulk changes in {table.name}"
        )

    def _on_table_window_focused(self, table_key: str):
        """
        Handle table viewer window gaining focus - highlight corresponding tree item
        and activate the correct undo stack.

        Args:
            table_key: Composite key (rom_path|table_address) of the focused table
        """
        # Activate the undo stack for this table (enables per-table undo/redo)
        self.table_undo_manager.set_active_stack(table_key)

        # Find the document containing this table and select it in the tree
        table_address = extract_table_address(table_key)
        document = self.get_current_document()
        if document and hasattr(document, "table_browser"):
            document.table_browser.select_table_by_address(table_address)


def main():
    """Application entry point"""
    # Initialize logging before anything else
    # Default: INFO level to console, optionally to file
    log_file = Path.home() / ".nc-flash" / "nc-flash.log"
    setup_logging(
        level=logging.INFO, log_file=str(log_file), console=True, detailed=False
    )

    # Per-session log in user-writable directory
    # (avoid Path(__file__).parent which is read-only under C:\Program Files)
    from datetime import datetime

    session_log_dir = Path.home() / ".nc-flash" / "logs"
    session_log_dir.mkdir(exist_ok=True)
    session_log_name = datetime.now().strftime("%Y-%m-%d_%H%M%S") + ".log"
    session_handler = logging.FileHandler(
        session_log_dir / session_log_name, encoding="utf-8"
    )
    session_handler.setLevel(logging.INFO)
    session_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    logging.getLogger().addHandler(session_handler)

    logger.info("=" * 60)
    logger.info(f"{APP_NAME} {APP_VERSION_STRING} starting")
    logger.info("=" * 60)

    app = QApplication(sys.argv)
    app.styleHints().setColorScheme(Qt.ColorScheme.Light)
    app.setApplicationName(APP_NAME)

    window = MainWindow()
    window.show()

    logger.info("Application window displayed")
    exit_code = app.exec()
    logger.info(f"Application exiting with code {exit_code}")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
