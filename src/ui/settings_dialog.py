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
    QGroupBox,
    QComboBox,
    QSpinBox
)
from PySide6.QtCore import Qt, Signal

from ..utils.settings import get_settings
from ..utils.colormap import reload_colormap


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

        # Table display group
        table_group = QGroupBox("Table Display")
        table_layout = QFormLayout()
        table_group.setLayout(table_layout)

        # Font size setting
        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(6, 16)
        self.font_size_spin.setSuffix(" px")
        table_layout.addRow("Table font size:", self.font_size_spin)

        # Gradient mode setting
        self.gradient_mode_combo = QComboBox()
        self.gradient_mode_combo.addItem("Min/Max of table", "minmax")
        self.gradient_mode_combo.addItem("Relative to neighbors", "neighbors")
        table_layout.addRow("Cell gradient coloring:", self.gradient_mode_combo)

        layout.addWidget(table_group)

        # Color map group
        colormap_group = QGroupBox("Color Map")
        colormap_layout = QFormLayout()
        colormap_group.setLayout(colormap_layout)

        # Color map file selection
        colormap_file_layout = QHBoxLayout()
        self.colormap_path_edit = QLineEdit()
        self.colormap_path_edit.setPlaceholderText("Path to .map file (or empty for built-in)")
        colormap_file_layout.addWidget(self.colormap_path_edit)

        browse_colormap_button = QPushButton("Browse...")
        browse_colormap_button.clicked.connect(self.browse_colormap_file)
        colormap_file_layout.addWidget(browse_colormap_button)

        colormap_layout.addRow("Color map file:", colormap_file_layout)

        # Help text for colormap
        colormap_help = QLabel("256-entry RGB color map file (.map format)")
        colormap_help.setStyleSheet("color: gray; font-size: 10px;")
        colormap_layout.addRow("", colormap_help)

        layout.addWidget(colormap_group)

        # Help text
        help_label = QLabel("Note: Changes take effect on newly opened tables")
        help_label.setStyleSheet("color: gray; font-size: 10px;")
        layout.addWidget(help_label)

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

        # Load gradient mode
        gradient_mode = self.settings.get_gradient_mode()
        index = self.gradient_mode_combo.findData(gradient_mode)
        if index >= 0:
            self.gradient_mode_combo.setCurrentIndex(index)

        # Load font size
        font_size = self.settings.get_table_font_size()
        self.font_size_spin.setValue(font_size)

        # Load colormap path
        colormap_path = self.settings.get_colormap_path()
        self.colormap_path_edit.setText(colormap_path)

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

    def browse_colormap_file(self):
        """Open file browser for color map file"""
        current_path = self.colormap_path_edit.text()
        if not current_path:
            # Default to colormap directory in project
            current_path = str(Path(__file__).parent.parent.parent / "colormap")

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Color Map File",
            current_path,
            "Color Map Files (*.map);;All Files (*)"
        )

        if file_path:
            self.colormap_path_edit.setText(file_path)

    def apply_settings(self):
        """Apply settings without closing the dialog"""
        # Save metadata directory
        metadata_dir = self.metadata_path_edit.text().strip()
        if metadata_dir:
            self.settings.set_metadata_directory(metadata_dir)

        # Save gradient mode
        gradient_mode = self.gradient_mode_combo.currentData()
        self.settings.set_gradient_mode(gradient_mode)

        # Save font size
        font_size = self.font_size_spin.value()
        self.settings.set_table_font_size(font_size)

        # Save colormap path and reload
        colormap_path = self.colormap_path_edit.text().strip()
        self.settings.set_colormap_path(colormap_path)
        reload_colormap()

        # Emit signal that settings changed
        self.settings_changed.emit()

    def accept(self):
        """OK button clicked - apply and close"""
        self.apply_settings()
        super().accept()
