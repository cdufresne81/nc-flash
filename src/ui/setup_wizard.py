"""
Setup Wizard

First-run wizard to configure essential application settings.
"""

from pathlib import Path
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QLineEdit,
    QFileDialog,
    QDialogButtonBox,
    QMessageBox
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

from ..utils.settings import get_settings


class SetupWizard(QDialog):
    """First-run setup wizard for configuring metadata directory"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("NC ROM Editor - First Run Setup")
        self.setMinimumSize(600, 300)
        self.setModal(True)
        self.settings = get_settings()
        self.init_ui()

    def init_ui(self):
        """Initialize the user interface"""
        layout = QVBoxLayout()
        self.setLayout(layout)

        # Welcome header
        header_label = QLabel("Welcome to NC ROM Editor!")
        header_font = QFont()
        header_font.setPointSize(14)
        header_font.setBold(True)
        header_label.setFont(header_font)
        header_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(header_label)

        layout.addSpacing(20)

        # Description
        desc_label = QLabel(
            "Before you begin, please configure the location of your ROM definition files.\n\n"
            "This is typically the 'metadata' directory from your RomDrop installation,\n"
            "which contains XML files that define ROM structures (e.g., lf9veb.xml)."
        )
        desc_label.setWordWrap(True)
        desc_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(desc_label)

        layout.addSpacing(20)

        # Metadata directory input
        path_layout = QHBoxLayout()

        path_label = QLabel("Metadata Directory:")
        path_layout.addWidget(path_label)

        self.metadata_path_edit = QLineEdit()
        self.metadata_path_edit.setPlaceholderText("Path to ROM definition XML files...")
        path_layout.addWidget(self.metadata_path_edit)

        browse_button = QPushButton("Browse...")
        browse_button.clicked.connect(self.browse_metadata_directory)
        path_layout.addWidget(browse_button)

        layout.addLayout(path_layout)

        # Help text
        help_label = QLabel(
            "Example: C:/Projets/MiataNC/romdrop_rev_21053000/metadata"
        )
        help_label.setStyleSheet("color: gray; font-size: 10px; font-style: italic;")
        help_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(help_label)

        layout.addStretch()

        # Buttons
        button_box = QDialogButtonBox()
        self.ok_button = button_box.addButton("Continue", QDialogButtonBox.AcceptRole)
        cancel_button = button_box.addButton("Cancel", QDialogButtonBox.RejectRole)

        self.ok_button.clicked.connect(self.validate_and_accept)
        cancel_button.clicked.connect(self.reject)

        layout.addWidget(button_box)

        # Set focus to text field
        self.metadata_path_edit.setFocus()

    def browse_metadata_directory(self):
        """Open directory browser for metadata directory"""
        current_path = self.metadata_path_edit.text()
        if not current_path:
            current_path = str(Path.home())

        directory = QFileDialog.getExistingDirectory(
            self,
            "Select Metadata Directory",
            current_path,
            QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks
        )

        if directory:
            self.metadata_path_edit.setText(directory)

    def validate_and_accept(self):
        """Validate the metadata directory before accepting"""
        metadata_dir = self.metadata_path_edit.text().strip()

        if not metadata_dir:
            QMessageBox.warning(
                self,
                "Path Required",
                "Please select a metadata directory to continue."
            )
            return

        metadata_path = Path(metadata_dir)

        if not metadata_path.exists():
            QMessageBox.warning(
                self,
                "Directory Not Found",
                f"The specified directory does not exist:\n{metadata_dir}\n\n"
                "Please select a valid directory."
            )
            return

        if not metadata_path.is_dir():
            QMessageBox.warning(
                self,
                "Invalid Path",
                f"The specified path is not a directory:\n{metadata_dir}\n\n"
                "Please select a valid directory."
            )
            return

        # Check if directory contains any XML files
        xml_files = list(metadata_path.glob("*.xml"))
        if not xml_files:
            response = QMessageBox.question(
                self,
                "No XML Files Found",
                f"No XML files found in:\n{metadata_dir}\n\n"
                "ROM definition files should be XML files.\n\n"
                "Do you want to use this directory anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if response == QMessageBox.No:
                return

        # Save the metadata directory
        self.settings.set_metadata_directory(metadata_dir)

        # Accept the dialog
        self.accept()

    def reject(self):
        """Handle cancel button"""
        response = QMessageBox.question(
            self,
            "Exit Setup",
            "You need to configure the metadata directory to use NC ROM Editor.\n\n"
            "Are you sure you want to exit?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )

        if response == QMessageBox.Yes:
            super().reject()
