"""
Setup Wizard

First-run wizard to configure essential application settings.
Asks for the RomDrop installation folder and derives romdrop.exe
and metadata/ paths from it.
"""

from pathlib import Path
from PySide6.QtWidgets import (
    QWizard,
    QWizardPage,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QLineEdit,
    QFileDialog,
    QMessageBox,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

from ..utils.settings import get_settings


class SetupWizard(QWizard):
    """First-run setup wizard for configuring RomDrop paths"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("NC ROM Editor - First Run Setup")
        self.setMinimumSize(600, 400)
        self.setModal(True)
        self.setWizardStyle(QWizard.ModernStyle)
        self.settings = get_settings()

        self.romdrop_page = RomDropFolderPage()
        self.confirm_page = ConfirmPathsPage()

        self.addPage(self.romdrop_page)
        self.addPage(self.confirm_page)

        self.currentIdChanged.connect(self._on_page_changed)

    def _on_page_changed(self, page_id):
        """Derive paths when moving to confirm page"""
        if page_id == 1:
            romdrop_folder = self.field("romdrop_folder")
            if romdrop_folder:
                folder = Path(romdrop_folder)
                exe_path = str(folder / "romdrop.exe")
                metadata_path = str(folder / "metadata")

                if not self.confirm_page.exe_edit.text():
                    self.confirm_page.exe_edit.setText(exe_path)
                if not self.confirm_page.metadata_edit.text():
                    self.confirm_page.metadata_edit.setText(metadata_path)

                self.confirm_page._validate_paths()

    def accept(self):
        """Save paths on wizard completion"""
        exe_path = self.confirm_page.exe_edit.text().strip()
        metadata_path = self.confirm_page.metadata_edit.text().strip()

        if exe_path:
            self.settings.set_romdrop_executable_path(exe_path)
        if metadata_path:
            self.settings.set_metadata_directory(metadata_path)

        super().accept()


class RomDropFolderPage(QWizardPage):
    """Step 1: Select RomDrop installation folder"""

    def __init__(self):
        super().__init__()
        self.setTitle("Select RomDrop Folder")
        self.setSubTitle(
            "Browse to your RomDrop installation folder.\n"
            "This folder should contain romdrop.exe and a metadata/ subfolder."
        )

        layout = QVBoxLayout()
        self.setLayout(layout)

        layout.addSpacing(10)

        # Folder input
        path_layout = QHBoxLayout()
        path_label = QLabel("RomDrop Folder:")
        path_layout.addWidget(path_label)

        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText("Path to RomDrop installation folder...")
        path_layout.addWidget(self.folder_edit)

        browse_button = QPushButton("Browse...")
        browse_button.clicked.connect(self._browse)
        path_layout.addWidget(browse_button)

        layout.addLayout(path_layout)

        # Help text
        help_label = QLabel("Example: C:/Projets/MiataNC/romdrop_rev_21053000")
        help_label.setStyleSheet("color: gray; font-size: 10px; font-style: italic;")
        help_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(help_label)

        layout.addSpacing(20)

        # Status label
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        layout.addStretch()

        self.registerField("romdrop_folder*", self.folder_edit)
        self.folder_edit.textChanged.connect(self._on_path_changed)

    def _browse(self):
        """Open directory browser for RomDrop folder"""
        current_path = self.folder_edit.text()
        if not current_path:
            current_path = str(Path.home())

        directory = QFileDialog.getExistingDirectory(
            self,
            "Select RomDrop Folder",
            current_path,
            QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks,
        )

        if directory:
            self.folder_edit.setText(directory)

    def _on_path_changed(self, path):
        """Validate folder contents as user types"""
        if not path:
            self.status_label.setText("")
            return

        folder = Path(path)
        if not folder.exists() or not folder.is_dir():
            self.status_label.setText(
                "<span style='color: #cc4444;'>Folder does not exist</span>"
            )
            return

        has_exe = (folder / "romdrop.exe").exists()
        has_metadata = (folder / "metadata").is_dir()

        parts = []
        if has_exe:
            parts.append("<span style='color: #44aa44;'>romdrop.exe found</span>")
        else:
            parts.append("<span style='color: #cc4444;'>romdrop.exe not found</span>")

        if has_metadata:
            xml_count = len(list((folder / "metadata").glob("*.xml")))
            parts.append(
                f"<span style='color: #44aa44;'>metadata/ found ({xml_count} XML files)</span>"
            )
        else:
            parts.append(
                "<span style='color: #cc4444;'>metadata/ subfolder not found</span>"
            )

        self.status_label.setText("<br>".join(parts))

    def validatePage(self):
        """Validate the selected folder"""
        path = self.folder_edit.text().strip()

        if not path:
            QMessageBox.warning(
                self,
                "Path Required",
                "Please select a RomDrop folder to continue.",
            )
            return False

        folder = Path(path)
        if not folder.exists() or not folder.is_dir():
            QMessageBox.warning(
                self,
                "Folder Not Found",
                f"The specified folder does not exist:\n{path}\n\n"
                "Please select a valid folder.",
            )
            return False

        has_exe = (folder / "romdrop.exe").exists()
        has_metadata = (folder / "metadata").is_dir()

        if not has_exe and not has_metadata:
            response = QMessageBox.question(
                self,
                "Not a RomDrop Folder",
                f"This folder doesn't appear to contain romdrop.exe or a metadata/ subfolder:\n{path}\n\n"
                "Do you want to continue anyway? You can manually set the paths on the next page.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            return response == QMessageBox.Yes

        return True


class ConfirmPathsPage(QWizardPage):
    """Step 2: Confirm derived paths"""

    def __init__(self):
        super().__init__()
        self.setTitle("Confirm Paths")
        self.setSubTitle(
            "Verify the paths below. You can edit them if your layout differs."
        )

        layout = QVBoxLayout()
        self.setLayout(layout)

        layout.addSpacing(10)

        # RomDrop executable
        exe_label = QLabel("RomDrop Executable:")
        exe_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(exe_label)

        exe_layout = QHBoxLayout()
        self.exe_edit = QLineEdit()
        self.exe_edit.setPlaceholderText("Path to romdrop.exe...")
        self.exe_edit.textChanged.connect(self._validate_paths)
        exe_layout.addWidget(self.exe_edit)

        exe_browse = QPushButton("Browse...")
        exe_browse.clicked.connect(self._browse_exe)
        exe_layout.addWidget(exe_browse)

        layout.addLayout(exe_layout)

        self.exe_status = QLabel("")
        self.exe_status.setStyleSheet("font-size: 11px;")
        layout.addWidget(self.exe_status)

        layout.addSpacing(15)

        # Metadata directory
        metadata_label = QLabel("Metadata Directory:")
        metadata_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(metadata_label)

        metadata_layout = QHBoxLayout()
        self.metadata_edit = QLineEdit()
        self.metadata_edit.setPlaceholderText("Path to metadata directory...")
        self.metadata_edit.textChanged.connect(self._validate_paths)
        metadata_layout.addWidget(self.metadata_edit)

        metadata_browse = QPushButton("Browse...")
        metadata_browse.clicked.connect(self._browse_metadata)
        metadata_layout.addWidget(metadata_browse)

        layout.addLayout(metadata_layout)

        self.metadata_status = QLabel("")
        self.metadata_status.setStyleSheet("font-size: 11px;")
        layout.addWidget(self.metadata_status)

        layout.addStretch()

    def _browse_exe(self):
        """Browse for romdrop.exe"""
        current_path = self.exe_edit.text()
        if not current_path:
            current_path = str(Path.home())

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select RomDrop Executable",
            current_path,
            "Executable Files (*.exe);;All Files (*)",
        )
        if file_path:
            self.exe_edit.setText(file_path)

    def _browse_metadata(self):
        """Browse for metadata directory"""
        current_path = self.metadata_edit.text()
        if not current_path:
            current_path = str(Path.home())

        directory = QFileDialog.getExistingDirectory(
            self,
            "Select Metadata Directory",
            current_path,
            QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks,
        )
        if directory:
            self.metadata_edit.setText(directory)

    def _validate_paths(self):
        """Update status indicators for both paths"""
        # Validate executable
        exe_path = self.exe_edit.text().strip()
        if not exe_path:
            self.exe_status.setText("")
        elif Path(exe_path).exists() and Path(exe_path).is_file():
            self.exe_status.setText(
                "<span style='color: #44aa44;'>&#10004; File exists</span>"
            )
        else:
            self.exe_status.setText(
                "<span style='color: #cc4444;'>&#10008; File not found</span>"
            )

        # Validate metadata directory
        metadata_path = self.metadata_edit.text().strip()
        if not metadata_path:
            self.metadata_status.setText("")
        elif Path(metadata_path).exists() and Path(metadata_path).is_dir():
            xml_count = len(list(Path(metadata_path).glob("*.xml")))
            if xml_count > 0:
                self.metadata_status.setText(
                    f"<span style='color: #44aa44;'>&#10004; Directory exists ({xml_count} XML files)</span>"
                )
            else:
                self.metadata_status.setText(
                    "<span style='color: #cc8800;'>&#9888; Directory exists but contains no XML files</span>"
                )
        else:
            self.metadata_status.setText(
                "<span style='color: #cc4444;'>&#10008; Directory not found</span>"
            )

        self.completeChanged.emit()

    def isComplete(self):
        """Page is complete when metadata directory exists with XML files"""
        metadata_path = self.metadata_edit.text().strip()
        if not metadata_path:
            return False

        metadata_dir = Path(metadata_path)
        if not metadata_dir.exists() or not metadata_dir.is_dir():
            return False

        xml_files = list(metadata_dir.glob("*.xml"))
        return len(xml_files) > 0

    def validatePage(self):
        """Final validation before accepting"""
        metadata_path = self.metadata_edit.text().strip()

        if not metadata_path:
            QMessageBox.warning(
                self,
                "Path Required",
                "Please provide a metadata directory path.",
            )
            return False

        metadata_dir = Path(metadata_path)
        if not metadata_dir.exists() or not metadata_dir.is_dir():
            QMessageBox.warning(
                self,
                "Directory Not Found",
                f"The metadata directory does not exist:\n{metadata_path}\n\n"
                "Please select a valid directory containing ROM metadata XML files.",
            )
            return False

        xml_files = list(metadata_dir.glob("*.xml"))
        if not xml_files:
            response = QMessageBox.question(
                self,
                "No XML Files Found",
                f"No XML files found in:\n{metadata_path}\n\n"
                "ROM metadata files should be XML files.\n\n"
                "Do you want to use this directory anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            return response == QMessageBox.Yes

        return True
