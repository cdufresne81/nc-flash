"""
Settings Dialog

Configuration window for application settings.
"""

from pathlib import Path
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QTabWidget,
    QWidget,
    QLabel,
    QPushButton,
    QDialogButtonBox,
    QLineEdit,
    QFileDialog,
    QGroupBox
)
from PySide6.QtCore import Qt, Signal

from ..utils.settings import get_settings


class SettingsDialog(QDialog):
    """Application settings dialog"""

    # Signal emitted when settings are applied
    settings_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumSize(600, 400)
        self.settings = get_settings()
        self.init_ui()
        self.load_settings()

    def init_ui(self):
        """Initialize the user interface"""
        layout = QVBoxLayout()
        self.setLayout(layout)

        # Tab widget for different settings categories
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        # Create placeholder tabs
        self.create_general_tab()
        self.create_appearance_tab()
        self.create_editor_tab()

        # Dialog buttons (OK, Cancel, Apply)
        button_box = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.Apply
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        button_box.button(QDialogButtonBox.Apply).clicked.connect(self.apply_settings)
        layout.addWidget(button_box)

    def create_general_tab(self):
        """Create the General settings tab"""
        tab = QWidget()
        layout = QVBoxLayout()
        tab.setLayout(layout)

        # Paths group
        paths_group = QGroupBox("Paths")
        paths_layout = QFormLayout()
        paths_group.setLayout(paths_layout)

        # Metadata directory setting
        metadata_layout = QHBoxLayout()
        self.metadata_path_edit = QLineEdit()
        self.metadata_path_edit.setPlaceholderText("Path to ROM definition XML files")
        metadata_layout.addWidget(self.metadata_path_edit)

        browse_button = QPushButton("Browse...")
        browse_button.clicked.connect(self.browse_metadata_directory)
        metadata_layout.addWidget(browse_button)

        paths_layout.addRow("Metadata Directory:", metadata_layout)

        # Add help text
        help_label = QLabel("Location of ROM definition XML files (e.g., lf9veb.xml)")
        help_label.setStyleSheet("color: gray; font-size: 10px;")
        paths_layout.addRow("", help_label)

        layout.addWidget(paths_group)
        layout.addStretch()

        self.tabs.addTab(tab, "General")

    def create_appearance_tab(self):
        """Create the Appearance settings tab"""
        tab = QWidget()
        layout = QVBoxLayout()
        tab.setLayout(layout)

        # Placeholder label
        label = QLabel("Appearance settings will be added here\n(Theme, colors, fonts, etc.)")
        label.setAlignment(Qt.AlignCenter)
        layout.addWidget(label)

        layout.addStretch()

        self.tabs.addTab(tab, "Appearance")

    def create_editor_tab(self):
        """Create the Editor settings tab"""
        tab = QWidget()
        layout = QVBoxLayout()
        tab.setLayout(layout)

        # Placeholder label
        label = QLabel("Table editor settings will be added here")
        label.setAlignment(Qt.AlignCenter)
        layout.addWidget(label)

        layout.addStretch()

        self.tabs.addTab(tab, "Editor")

    def load_settings(self):
        """Load current settings into the UI"""
        # Load metadata directory
        metadata_dir = self.settings.get_metadata_directory()
        self.metadata_path_edit.setText(metadata_dir)

    def browse_metadata_directory(self):
        """Open directory browser for metadata directory"""
        current_path = self.metadata_path_edit.text()
        if not current_path:
            current_path = str(Path.cwd())

        directory = QFileDialog.getExistingDirectory(
            self,
            "Select Metadata Directory",
            current_path,
            QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks
        )

        if directory:
            self.metadata_path_edit.setText(directory)

    def apply_settings(self):
        """Apply settings without closing the dialog"""
        # Save metadata directory
        metadata_dir = self.metadata_path_edit.text().strip()
        if metadata_dir:
            self.settings.set_metadata_directory(metadata_dir)

        # Emit signal that settings changed
        self.settings_changed.emit()

    def accept(self):
        """OK button clicked - apply and close"""
        self.apply_settings()
        super().accept()
