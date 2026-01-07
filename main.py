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
from src.ui.table_viewer import TableViewer
from src.ui.table_viewer_window import TableViewerWindow
from src.ui.log_console import LogConsole
from src.ui.settings_dialog import SettingsDialog
from src.ui.setup_wizard import SetupWizard
from src.ui.rom_document import RomDocument

logger = get_logger(__name__)


class MainWindow(QMainWindow):
    """Main application window"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("NC ROM Editor")
        self.setGeometry(100, 100, 1400, 900)

        logger.info("Initializing NC ROM Editor")

        # Track open table viewer windows
        self.open_table_windows = []

        # Get application settings
        self.settings = get_settings()

        # Check if metadata directory is configured and valid
        if not self.check_metadata_directory():
            # Show setup wizard on first run or if metadata directory is invalid
            if not self.show_setup_wizard():
                # User cancelled setup, exit application
                logger.warning("Setup cancelled by user, exiting application")
                QMessageBox.critical(
                    self,
                    "Setup Required",
                    "NC ROM Editor requires a metadata directory to function.\n"
                    "Application will now exit."
                )
                sys.exit(1)

        # ROM detector for automatic XML matching
        try:
            metadata_dir = self.settings.get_metadata_directory()
            self.rom_detector = RomDetector(metadata_dir)
            logger.info(f"ROM detector initialized successfully (metadata: {metadata_dir})")
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
        main_splitter.setSizes([420, 980])

    def init_menu(self):
        """Initialize the menu bar"""
        menubar = self.menuBar()

        # File menu
        file_menu = menubar.addMenu("File")

        open_action = file_menu.addAction("Open ROM...")
        open_action.triggered.connect(self.open_rom)

        save_action = file_menu.addAction("Save ROM")
        save_action.triggered.connect(self.save_rom)

        save_as_action = file_menu.addAction("Save ROM As...")
        save_as_action.triggered.connect(self.save_rom_as)

        file_menu.addSeparator()

        close_tab_action = file_menu.addAction("Close Tab")
        close_tab_action.triggered.connect(self.close_current_tab)

        file_menu.addSeparator()

        exit_action = file_menu.addAction("Exit")
        exit_action.triggered.connect(self.close)

        # Edit menu
        edit_menu = menubar.addMenu("Edit")

        settings_action = edit_menu.addAction("Settings...")
        settings_action.triggered.connect(self.show_settings)

        # Help menu
        help_menu = menubar.addMenu("Help")

        about_action = help_menu.addAction("About")
        about_action.triggered.connect(self.show_about)

    def update_no_roms_visibility(self):
        """Update visibility - placeholder for future use"""
        # Just update window title based on tab count
        if self.tab_widget.count() == 0:
            self.setWindowTitle("NC ROM Editor")
        pass

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
                # TODO: Implement save before close
                pass

        # Remove the tab
        self.tab_widget.removeTab(index)
        self.update_no_roms_visibility()

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
                self.setWindowTitle(f"NC ROM Editor - {document.get_tab_title()}")
                logger.info(f"Switched to ROM: {document.file_name}")
        else:
            self.setWindowTitle("NC ROM Editor")
            self.update_no_roms_visibility()

    def open_rom(self):
        """Open a ROM file in a new tab"""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Open ROM File",
            "",
            "ROM Files (*.bin *.rom);;All Files (*)"
        )

        if file_path:
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

                # Add as new tab
                file_name = Path(file_path).name
                tab_index = self.tab_widget.addTab(rom_document, file_name)
                self.tab_widget.setTabToolTip(tab_index, file_path)
                self.tab_widget.setCurrentIndex(tab_index)

                # Update visibility
                self.update_no_roms_visibility()

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

            except (DetectionError, RomFileError, DefinitionError) as e:
                logger.error(f"Failed to open ROM file: {e}")
                QMessageBox.critical(
                    self,
                    "Error",
                    f"Failed to open ROM file:\n{str(e)}"
                )
            except Exception as e:
                logger.error(f"Unexpected error opening ROM file: {e}")
                QMessageBox.critical(
                    self,
                    "Error",
                    f"Failed to open ROM file:\n{str(e)}"
                )

    def save_rom(self):
        """Save the current ROM file"""
        document = self.get_current_document()
        if not document:
            QMessageBox.warning(self, "No ROM", "No ROM file is currently loaded.")
            return

        try:
            document.save()

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
        except RomFileError as e:
            logger.error(f"Failed to save ROM file: {e}")
            QMessageBox.critical(
                self,
                "Error",
                f"Failed to save ROM file:\n{str(e)}"
            )
        except Exception as e:
            logger.error(f"Unexpected error saving ROM file: {e}")
            QMessageBox.critical(
                self,
                "Error",
                f"Failed to save ROM file:\n{str(e)}"
            )

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

                # Update tab title with new filename
                current_index = self.tab_widget.currentIndex()
                self.tab_widget.setTabText(current_index, document.file_name)
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
            except RomFileError as e:
                logger.error(f"Failed to save ROM file: {e}")
                QMessageBox.critical(
                    self,
                    "Error",
                    f"Failed to save ROM file:\n{str(e)}"
                )
            except Exception as e:
                logger.error(f"Unexpected error saving ROM file: {e}")
                QMessageBox.critical(
                    self,
                    "Error",
                    f"Failed to save ROM file:\n{str(e)}"
                )

    def on_table_selected(self, table, rom_reader):
        """Handle table selection from browser - opens table in new window"""
        try:
            # Read table data from ROM
            logger.debug(f"User selected table: {table.name}")
            self.statusBar().showMessage(f"Loading table: {table.name}...")
            data = rom_reader.read_table_data(table)

            if data:
                # Create and show new table viewer window
                viewer_window = TableViewerWindow(table, data, parent=self)
                viewer_window.show()

                # Track the window
                self.open_table_windows.append(viewer_window)

                # Clean up closed windows from the list
                self.open_table_windows = [w for w in self.open_table_windows if w.isVisible()]

                # Log to console
                logger.info(f"Opened table: {table.name}")
                logger.info(f"  Category: {table.category}")
                logger.info(f"  Type: {table.type.value}")
                logger.info(f"  Address: {table.address}")
                logger.info(f"  Elements: {table.elements}")
                logger.info(f"  Open windows: {len(self.open_table_windows)}")

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

        except (ScalingNotFoundError, RomReadError) as e:
            logger.error(f"Failed to load table {table.name}: {e}")
            QMessageBox.critical(
                self,
                "Error",
                f"Failed to load table:\n{str(e)}"
            )
        except Exception as e:
            logger.error(f"Unexpected error loading table {table.name}: {e}")
            QMessageBox.critical(
                self,
                "Error",
                f"Failed to load table:\n{str(e)}"
            )

    def log_startup_message(self):
        """Log application startup message to console"""
        logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        logger.info("NC ROM Editor v0.1.0")
        logger.info("An open-source ROM editor for NC Miata ECUs")
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
        # Reinitialize ROM detector with new metadata path
        try:
            metadata_dir = self.settings.get_metadata_directory()
            self.rom_detector = RomDetector(metadata_dir)
            logger.info(f"ROM detector reinitialized with metadata directory: {metadata_dir}")
            self.statusBar().showMessage(f"Settings updated. Metadata directory: {metadata_dir}")
        except DetectionError as e:
            logger.error(f"Failed to reinitialize ROM detector: {e}")
            QMessageBox.warning(
                self,
                "Settings Error",
                f"Failed to load metadata from new directory:\n{str(e)}\n\n"
                "Please check the metadata directory path in settings."
            )

    def show_about(self):
        """Show about dialog"""
        QMessageBox.about(
            self,
            "About NC ROM Editor",
            "NC ROM Editor v0.1.0\n\n"
            "An open-source ROM editor for NC Miata ECUs\n\n"
            "Designed to replace EcuFlash for ROM editing tasks.\n"
            "Works with RomDrop for ECU flashing."
        )


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
    logger.info("NC ROM Editor v0.1.0 starting")
    logger.info("=" * 60)

    app = QApplication(sys.argv)
    app.setApplicationName("NC ROM Editor")

    window = MainWindow()
    window.show()

    logger.info("Application window displayed")
    exit_code = app.exec()
    logger.info(f"Application exiting with code {exit_code}")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
