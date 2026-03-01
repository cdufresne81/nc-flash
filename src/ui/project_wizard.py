"""
Project Creation Wizard

Multi-step wizard for creating a new ROM editing project.
"""

from PySide6.QtWidgets import (
    QWizard,
    QWizardPage,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QTextEdit,
    QPushButton,
    QFileDialog,
    QMessageBox,
    QGroupBox,
)
from pathlib import Path
import logging

from ..core.rom_detector import RomDetector
from ..core.definition_parser import load_definition
from ..utils.settings import get_settings

logger = logging.getLogger(__name__)


class ProjectWizard(QWizard):
    """Wizard for creating a new project from a ROM file"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create New Project")
        self.setMinimumSize(600, 450)
        self.setWizardStyle(QWizard.ModernStyle)

        # Store results
        self.rom_path = None
        self.rom_definition = None
        self.project_name = None
        self.project_description = None
        self.project_location = None

        # Add pages
        self.rom_page = RomSelectionPage()
        self.info_page = ProjectInfoPage()
        self.location_page = ProjectLocationPage()

        self.addPage(self.rom_page)
        self.addPage(self.info_page)
        self.addPage(self.location_page)

        # Connect page changed to update info
        self.currentIdChanged.connect(self._on_page_changed)

    def _on_page_changed(self, page_id):
        """Update pages when navigating"""
        if page_id == 1:  # Info page
            # Suggest project name from ROM filename
            rom_path = self.field("rom_path")
            if rom_path and not self.info_page.name_edit.text():
                filename = Path(rom_path).stem
                self.info_page.name_edit.setText(filename)
        elif page_id == 2:  # Location page
            # Suggest location from configured projects directory
            project_name = self.field("project_name")
            if project_name and not self.location_page.location_edit.text():
                projects_dir = get_settings().get_projects_directory()
                suggested_path = Path(projects_dir) / project_name
                self.location_page.location_edit.setText(str(suggested_path))

    def accept(self):
        """Called when wizard is completed"""
        self.rom_path = self.field("rom_path")
        self.project_name = self.field("project_name")
        self.project_description = self.info_page.desc_edit.toPlainText()
        self.project_location = self.field("project_location")

        # Get ROM definition from ROM page
        self.rom_definition = self.rom_page.rom_definition

        super().accept()


class RomSelectionPage(QWizardPage):
    """Page for selecting source ROM file"""

    def __init__(self):
        super().__init__()
        self.setTitle("Select ROM File")
        self.setSubTitle("Choose the ROM file to create a project from")

        self.rom_definition = None

        layout = QVBoxLayout()
        self.setLayout(layout)

        # ROM path input
        path_layout = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("Path to ROM file (.bin)")
        self.path_edit.textChanged.connect(self._on_path_changed)
        path_layout.addWidget(self.path_edit)

        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse)
        path_layout.addWidget(browse_btn)

        layout.addLayout(path_layout)

        # ROM info display
        info_group = QGroupBox("ROM Information")
        info_layout = QVBoxLayout()
        info_group.setLayout(info_layout)

        self.info_label = QLabel("Select a ROM file to see its information")
        self.info_label.setWordWrap(True)
        info_layout.addWidget(self.info_label)

        layout.addWidget(info_group)
        layout.addStretch()

        # Register field
        self.registerField("rom_path*", self.path_edit)

    def _browse(self):
        """Open file dialog to select ROM"""
        path, _ = QFileDialog.getOpenFileName(
            self, "Select ROM File", "", "ROM Files (*.bin *.rom);;All Files (*)"
        )
        if path:
            self.path_edit.setText(path)

    def _on_path_changed(self, path):
        """Handle path change - try to detect ROM"""
        if not path or not Path(path).exists():
            self.info_label.setText("Select a ROM file to see its information")
            self.rom_definition = None
            return

        try:
            # Try to detect ROM
            metadata_dir = get_settings().get_metadata_directory()
            detector = RomDetector(metadata_dir)
            rom_id, xml_path = detector.detect_rom_id(path)

            if rom_id and xml_path:
                # Load the ROM definition from XML
                rom_def = load_definition(xml_path)
                self.rom_definition = rom_def

                # Display ROM info
                info_text = (
                    f"<b>ROM ID:</b> {rom_def.romid.internalidstring}<br>"
                    f"<b>Make:</b> {rom_def.romid.make}<br>"
                    f"<b>Model:</b> {rom_def.romid.model}<br>"
                    f"<b>Year:</b> {rom_def.romid.year}<br>"
                    f"<b>Tables:</b> {len(rom_def.tables)}<br>"
                    f"<b>Scalings:</b> {len(rom_def.scalings)}"
                )
                self.info_label.setText(info_text)
            else:
                self.info_label.setText(
                    "<span style='color: orange;'>Unknown ROM - no matching definition found.<br>"
                    "You can still create a project, but table data won't be available.</span>"
                )
                self.rom_definition = None

        except Exception as e:
            logger.error(f"Error detecting ROM: {e}")
            self.info_label.setText(f"<span style='color: red;'>Error: {e}</span>")
            self.rom_definition = None

    def validatePage(self):
        """Validate that ROM file exists and is valid"""
        path = self.path_edit.text()
        if not path:
            QMessageBox.warning(self, "Error", "Please select a ROM file")
            return False

        if not Path(path).exists():
            QMessageBox.warning(self, "Error", "ROM file does not exist")
            return False

        if self.rom_definition is None:
            # Allow creating project even without definition
            reply = QMessageBox.question(
                self,
                "Unknown ROM",
                "No ROM definition found for this file.\n\n"
                "You can create a project, but table editing won't be available "
                "until a matching definition is added.\n\n"
                "Continue anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            return reply == QMessageBox.Yes

        return True


class ProjectInfoPage(QWizardPage):
    """Page for project name and description"""

    def __init__(self):
        super().__init__()
        self.setTitle("Project Information")
        self.setSubTitle("Enter a name and description for your project")

        layout = QVBoxLayout()
        self.setLayout(layout)

        # Project name
        layout.addWidget(QLabel("Project Name:"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g., My Performance Tune")
        layout.addWidget(self.name_edit)

        # Description
        layout.addWidget(QLabel("Description (optional):"))
        self.desc_edit = QTextEdit()
        self.desc_edit.setPlaceholderText(
            "Describe the purpose of this project...\n\n"
            "Example: Performance tune for my NC Miata with intake and exhaust"
        )
        self.desc_edit.setMaximumHeight(120)
        layout.addWidget(self.desc_edit)

        layout.addStretch()

        self.registerField("project_name*", self.name_edit)

    def validatePage(self):
        """Validate project name"""
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Error", "Please enter a project name")
            return False

        # Check for invalid characters
        invalid_chars = '<>:"/\\|?*'
        if any(c in name for c in invalid_chars):
            QMessageBox.warning(
                self,
                "Error",
                f"Project name cannot contain these characters: {invalid_chars}",
            )
            return False

        return True


class ProjectLocationPage(QWizardPage):
    """Page for selecting project save location"""

    def __init__(self):
        super().__init__()
        self.setTitle("Project Location")
        self.setSubTitle("Choose where to save the project folder")

        layout = QVBoxLayout()
        self.setLayout(layout)

        # Location input
        layout.addWidget(QLabel("Project Folder:"))

        path_layout = QHBoxLayout()
        self.location_edit = QLineEdit()
        self.location_edit.setPlaceholderText(
            "Path where project folder will be created"
        )
        path_layout.addWidget(self.location_edit)

        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse)
        path_layout.addWidget(browse_btn)

        layout.addLayout(path_layout)

        # Info text
        info_label = QLabel(
            "<i>A new folder will be created at this location containing:</i>\n"
            "- original.bin (pristine copy of your ROM)\n"
            "- modified.bin (working copy with your changes)\n"
            "- project.json (project metadata)\n"
            "- history/ (commit history)"
        )
        info_label.setWordWrap(True)
        info_label.setStyleSheet("color: #666; margin-top: 10px;")
        layout.addWidget(info_label)

        layout.addStretch()

        self.registerField("project_location*", self.location_edit)

    def _browse(self):
        """Open directory dialog"""
        path = QFileDialog.getExistingDirectory(
            self, "Select Project Location", str(Path.home())
        )
        if path:
            # Append project name if available
            project_name = self.wizard().field("project_name")
            if project_name:
                path = str(Path(path) / project_name)
            self.location_edit.setText(path)

    def validatePage(self):
        """Validate project location"""
        location = self.location_edit.text().strip()
        if not location:
            QMessageBox.warning(self, "Error", "Please select a project location")
            return False

        path = Path(location)

        # Check if path already exists
        if path.exists():
            if path.is_file():
                QMessageBox.warning(
                    self, "Error", "Selected location is a file, not a folder"
                )
                return False

            # Check if folder is not empty
            if any(path.iterdir()):
                reply = QMessageBox.question(
                    self,
                    "Folder Exists",
                    "The selected folder already exists and is not empty.\n\n"
                    "Continue anyway? Existing files may be overwritten.",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                return reply == QMessageBox.Yes

        # Check if parent directory exists
        if not path.parent.exists():
            reply = QMessageBox.question(
                self,
                "Create Folders",
                f"The parent folder does not exist:\n{path.parent}\n\n" "Create it?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            return reply == QMessageBox.Yes

        return True
