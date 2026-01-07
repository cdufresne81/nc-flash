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
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QSplitter,
    QFileDialog,
    QMessageBox,
    QLabel
)
from PySide6.QtCore import Qt

from src.utils.logging_config import setup_logging, get_logger
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

logger = get_logger(__name__)


class MainWindow(QMainWindow):
    """Main application window"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("NC ROM Editor")
        self.setGeometry(100, 100, 1400, 900)

        logger.info("Initializing NC ROM Editor")

        # ROM data
        self.current_rom_path = None
        self.rom_definition = None
        self.rom_reader = None

        # Track open table viewer windows
        self.open_table_windows = []

        # ROM detector for automatic XML matching
        try:
            self.rom_detector = RomDetector("metadata")
            logger.info("ROM detector initialized successfully")
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

    def init_ui(self):
        """Initialize the user interface"""
        # Central widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        layout = QHBoxLayout()
        central_widget.setLayout(layout)

        # Splitter for browser and log console
        splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(splitter)

        # Left: Table browser
        self.table_browser = TableBrowser()
        self.table_browser.table_selected.connect(self.on_table_selected)
        splitter.addWidget(self.table_browser)

        # Right: Log console
        self.log_console = LogConsole()
        splitter.addWidget(self.log_console)

        # Set initial splitter sizes (60% browser, 40% console)
        splitter.setSizes([600, 400])

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

        exit_action = file_menu.addAction("Exit")
        exit_action.triggered.connect(self.close)

        # Help menu
        help_menu = menubar.addMenu("Help")

        about_action = help_menu.addAction("About")
        about_action.triggered.connect(self.show_about)

    def load_definition(self, definition_path: str):
        """
        Load ROM definition file

        Args:
            definition_path: Path to XML definition file
        """
        try:
            logger.info(f"Loading ROM definition from {definition_path}")
            self.statusBar().showMessage("Loading ROM definition...")
            self.rom_definition = load_definition(definition_path)

            # Populate table browser
            self.table_browser.load_definition(self.rom_definition)

            logger.info(f"Loaded definition: {self.rom_definition.romid.xmlid}")
            self.statusBar().showMessage(
                f"Loaded definition: {self.rom_definition.romid.xmlid} "
                f"({len(self.rom_definition.tables)} tables)"
            )
        except DefinitionError as e:
            logger.error(f"Failed to load ROM definition: {e}")
            QMessageBox.critical(
                self,
                "Error Loading Definition",
                f"Failed to load ROM definition:\n{str(e)}"
            )
            self.statusBar().showMessage("Failed to load definition")
        except Exception as e:
            logger.error(f"Unexpected error loading ROM definition: {e}")
            QMessageBox.critical(
                self,
                "Error Loading Definition",
                f"Failed to load ROM definition:\n{str(e)}"
            )
            self.statusBar().showMessage("Failed to load definition")

    def open_rom(self):
        """Open a ROM file"""
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
                self.load_definition(xml_path)

                # Create ROM reader
                self.statusBar().showMessage(f"Loading ROM data...")
                self.rom_reader = RomReader(file_path, self.rom_definition)

                # Verify ROM ID (should always pass now, but kept as sanity check)
                if not self.rom_reader.verify_rom_id():
                    logger.warning("ROM ID verification failed after auto-detection")
                    QMessageBox.warning(
                        self,
                        "ROM ID Warning",
                        f"ROM ID verification failed. This should not happen after auto-detection.\n"
                        f"Expected: {self.rom_definition.romid.internalidstring}\n"
                        f"This may indicate a detection bug."
                    )

                self.current_rom_path = file_path
                file_name = Path(file_path).name
                self.setWindowTitle(f"NC ROM Editor - {file_name} ({rom_id})")

                # Log to console
                logger.info(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
                logger.info(f"ROM LOADED: {file_name}")
                logger.info(f"  ROM ID: {rom_id}")
                logger.info(f"  Definition: {self.rom_definition.romid.xmlid}")
                logger.info(f"  Make/Model: {self.rom_definition.romid.make} {self.rom_definition.romid.model}")
                logger.info(f"  Tables: {len(self.rom_definition.tables)}")
                logger.info(f"  Size: {len(self.rom_reader.rom_data):,} bytes")
                logger.info(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

                self.statusBar().showMessage(
                    f"Loaded: {file_name} - {self.rom_definition.romid.xmlid} "
                    f"({len(self.rom_definition.tables)} tables)"
                )

            except (DetectionError, RomFileError) as e:
                logger.error(f"Failed to open ROM file: {e}")
                QMessageBox.critical(
                    self,
                    "Error",
                    f"Failed to open ROM file:\n{str(e)}"
                )
                self.rom_reader = None
            except Exception as e:
                logger.error(f"Unexpected error opening ROM file: {e}")
                QMessageBox.critical(
                    self,
                    "Error",
                    f"Failed to open ROM file:\n{str(e)}"
                )
                self.rom_reader = None

    def save_rom(self):
        """Save the current ROM file"""
        if not self.rom_reader:
            QMessageBox.warning(self, "No ROM", "No ROM file is currently loaded.")
            return

        if self.current_rom_path:
            self.save_rom_to_path(self.current_rom_path)

    def save_rom_as(self):
        """Save the ROM to a new file"""
        if not self.rom_reader:
            QMessageBox.warning(self, "No ROM", "No ROM file is currently loaded.")
            return

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save ROM File As",
            "",
            "ROM Files (*.bin);;All Files (*)"
        )

        if file_path:
            self.save_rom_to_path(file_path)

    def save_rom_to_path(self, file_path):
        """Save ROM data to specified path"""
        try:
            self.rom_reader.save_rom(file_path)

            # Log to console
            logger.info(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            logger.info(f"ROM SAVED: {Path(file_path).name}")
            logger.info(f"  Location: {file_path}")
            logger.info(f"  Size: {len(self.rom_reader.rom_data):,} bytes")
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

    def on_table_selected(self, table):
        """Handle table selection from browser - opens table in new window"""
        if not self.rom_reader:
            QMessageBox.warning(
                self,
                "No ROM Loaded",
                "Please open a ROM file first."
            )
            return

        try:
            # Read table data from ROM
            logger.debug(f"User selected table: {table.name}")
            self.statusBar().showMessage(f"Loading table: {table.name}...")
            data = self.rom_reader.read_table_data(table)

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
