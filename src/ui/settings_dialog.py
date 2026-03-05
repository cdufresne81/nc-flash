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
    QSpinBox,
    QCheckBox,
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
        self.create_tools_tab()
        self.create_ecu_tab()

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

        # Projects directory setting
        projects_layout = QHBoxLayout()
        self.projects_path_edit = QLineEdit()
        self.projects_path_edit.setPlaceholderText("Path to store ROM projects")
        projects_layout.addWidget(self.projects_path_edit)

        browse_projects_button = QPushButton("Browse...")
        browse_projects_button.clicked.connect(self.browse_projects_directory)
        projects_layout.addWidget(browse_projects_button)

        paths_layout.addRow("Projects Directory:", projects_layout)

        # Add help text for projects
        projects_help = QLabel("Location where ROM tuning projects are stored")
        projects_help.setStyleSheet("color: gray; font-size: 10px;")
        paths_layout.addRow("", projects_help)

        # Export directory setting
        export_layout = QHBoxLayout()
        self.export_path_edit = QLineEdit()
        self.export_path_edit.setPlaceholderText("Folder for CSV exports")
        export_layout.addWidget(self.export_path_edit)

        browse_export_button = QPushButton("Browse...")
        browse_export_button.clicked.connect(self.browse_export_directory)
        export_layout.addWidget(browse_export_button)

        paths_layout.addRow("Export Directory:", export_layout)

        # Add help text for export
        export_help = QLabel("Default folder for CSV exports (Ctrl+E)")
        export_help.setStyleSheet("color: gray; font-size: 10px;")
        paths_layout.addRow("", export_help)

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
        self.colormap_path_edit.setPlaceholderText(
            "Path to .map file (or empty for built-in)"
        )
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

        # Toggle display group
        toggle_group = QGroupBox("Toggle Display")
        toggle_layout = QVBoxLayout()
        toggle_group.setLayout(toggle_layout)

        self.dtc_toggle_checkbox = QCheckBox(
            "Use toggle switches for DTC Activation Flags"
        )
        toggle_layout.addWidget(self.dtc_toggle_checkbox)

        toggle_help = QLabel(
            "When enabled, DTC Activation Flag tables show an ON/OFF toggle\n"
            "instead of a numeric cell (0 = OFF, non-zero = ON)"
        )
        toggle_help.setStyleSheet("color: gray; font-size: 10px;")
        toggle_layout.addWidget(toggle_help)

        layout.addWidget(toggle_group)

        layout.addStretch()

        self.tabs.addTab(tab, "Editor")

    def create_tools_tab(self):
        """Create the Tools settings tab"""
        tab = QWidget()
        layout = QVBoxLayout()
        tab.setLayout(layout)

        # RomDrop group
        romdrop_group = QGroupBox("RomDrop")
        romdrop_layout = QFormLayout()
        romdrop_group.setLayout(romdrop_layout)

        # RomDrop executable path
        romdrop_path_layout = QHBoxLayout()
        self.romdrop_path_edit = QLineEdit()
        self.romdrop_path_edit.setPlaceholderText("Path to romdrop.exe")
        romdrop_path_layout.addWidget(self.romdrop_path_edit)

        browse_romdrop_button = QPushButton("Browse...")
        browse_romdrop_button.clicked.connect(self.browse_romdrop_executable)
        romdrop_path_layout.addWidget(browse_romdrop_button)

        romdrop_layout.addRow("RomDrop executable:", romdrop_path_layout)

        romdrop_help = QLabel("Path to romdrop.exe for one-click ECU flashing")
        romdrop_help.setStyleSheet("color: gray; font-size: 10px;")
        romdrop_layout.addRow("", romdrop_help)

        # Metadata directory setting
        metadata_layout = QHBoxLayout()
        self.metadata_path_edit = QLineEdit()
        self.metadata_path_edit.setPlaceholderText("Path to ROM metadata XML files")
        metadata_layout.addWidget(self.metadata_path_edit)

        browse_metadata_button = QPushButton("Browse...")
        browse_metadata_button.clicked.connect(self.browse_metadata_directory)
        metadata_layout.addWidget(browse_metadata_button)

        romdrop_layout.addRow("Metadata Directory:", metadata_layout)

        metadata_help = QLabel("Location of ROM metadata XML files (e.g., lf9veb.xml)")
        metadata_help.setStyleSheet("color: gray; font-size: 10px;")
        romdrop_layout.addRow("", metadata_help)

        layout.addWidget(romdrop_group)

        # MCP Server group
        mcp_group = QGroupBox("MCP Server (AI Assistant Access)")
        mcp_layout = QVBoxLayout()
        mcp_group.setLayout(mcp_layout)

        self.mcp_auto_start_checkbox = QCheckBox(
            "Start MCP server automatically on app launch"
        )
        mcp_layout.addWidget(self.mcp_auto_start_checkbox)

        mcp_help = QLabel(
            "Enables AI assistants (Claude, ChatGPT, etc.) to read your open ROMs via the Model Context Protocol"
        )
        mcp_help.setStyleSheet("color: gray; font-size: 10px;")
        mcp_help.setWordWrap(True)
        mcp_layout.addWidget(mcp_help)

        layout.addWidget(mcp_group)
        layout.addStretch()

        self.tabs.addTab(tab, "Tools")

    def create_ecu_tab(self):
        """Create the ECU settings tab"""
        tab = QWidget()
        layout = QVBoxLayout()
        tab.setLayout(layout)

        # J2534 group
        j2534_group = QGroupBox("J2534 PassThru Interface")
        j2534_layout = QFormLayout()
        j2534_group.setLayout(j2534_layout)

        # J2534 DLL path
        dll_path_layout = QHBoxLayout()
        self.j2534_dll_edit = QLineEdit()
        self.j2534_dll_edit.setPlaceholderText("op20pt32.dll (auto-detected)")
        dll_path_layout.addWidget(self.j2534_dll_edit)

        browse_dll_button = QPushButton("Browse...")
        browse_dll_button.clicked.connect(self.browse_j2534_dll)
        dll_path_layout.addWidget(browse_dll_button)

        j2534_layout.addRow("J2534 DLL override:", dll_path_layout)

        dll_help = QLabel(
            "Leave empty for Tactrix OpenPort 2.0 (op20pt32.dll is found automatically). "
            "Only set this if you use a different J2534 adapter."
        )
        dll_help.setStyleSheet("color: gray; font-size: 10px;")
        dll_help.setWordWrap(True)
        j2534_layout.addRow("", dll_help)

        # Test connection button
        self.test_j2534_button = QPushButton("Test Connection")
        self.test_j2534_button.clicked.connect(self._test_j2534_connection)
        j2534_layout.addRow("", self.test_j2534_button)

        layout.addWidget(j2534_group)

        # Security module status
        status_group = QGroupBox("Flash Security Module")
        status_layout = QVBoxLayout()
        status_group.setLayout(status_layout)

        from src.ecu.flash_manager import SECURE_MODULE_AVAILABLE

        if SECURE_MODULE_AVAILABLE:
            status_label = QLabel("Installed — flash operations are available")
            status_label.setStyleSheet("color: green; font-weight: bold;")
        else:
            status_label = QLabel(
                "Not installed — flash operations are disabled.\n"
                "Contact the project maintainer for access to the security module."
            )
            status_label.setStyleSheet("color: red;")
            status_label.setWordWrap(True)

        status_layout.addWidget(status_label)
        layout.addWidget(status_group)

        layout.addStretch()
        self.tabs.addTab(tab, "ECU")

    def browse_j2534_dll(self):
        """Open file browser for J2534 DLL"""
        current_path = self.j2534_dll_edit.text()
        if not current_path:
            current_path = str(Path.cwd())

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select J2534 DLL",
            current_path,
            "DLL Files (*.dll);;All Files (*)",
        )

        if file_path:
            self.j2534_dll_edit.setText(file_path)

    def _test_j2534_connection(self):
        """Test J2534 device connection"""
        from PySide6.QtWidgets import QMessageBox
        from src.ecu.constants import DEFAULT_J2534_DLL

        dll_path = self.j2534_dll_edit.text().strip() or DEFAULT_J2534_DLL

        try:
            from src.ecu.j2534 import J2534Device

            device = J2534Device(dll_path)
            device.open()
            device.close()
            QMessageBox.information(
                self, "Connection OK", "J2534 device connected successfully!"
            )
        except Exception as e:
            QMessageBox.warning(
                self, "Connection Failed", f"Could not connect to J2534 device:\n{e}"
            )

    def load_settings(self):
        """Load current settings into the UI"""
        # Load projects directory
        projects_dir = self.settings.get_projects_directory()
        self.projects_path_edit.setText(projects_dir)

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

        # Load export directory
        export_dir = self.settings.get_export_directory()
        self.export_path_edit.setText(export_dir)

        # Load toggle categories setting
        toggle_cats = self.settings.get_toggle_categories()
        self.dtc_toggle_checkbox.setChecked("DTC - Activation Flags" in toggle_cats)

        # Load RomDrop executable path
        romdrop_path = self.settings.get_romdrop_executable_path()
        self.romdrop_path_edit.setText(romdrop_path)

        # Load MCP auto-start setting
        self.mcp_auto_start_checkbox.setChecked(self.settings.get_mcp_auto_start())

        # Load J2534 DLL path
        j2534_path = self.settings.get_j2534_dll_path()
        self.j2534_dll_edit.setText(j2534_path)

    def browse_projects_directory(self):
        """Open directory browser for projects directory"""
        current_path = self.projects_path_edit.text()
        if not current_path:
            current_path = str(Path.cwd())

        directory = QFileDialog.getExistingDirectory(
            self,
            "Select Projects Directory",
            current_path,
            QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks,
        )

        if directory:
            self.projects_path_edit.setText(directory)

    def browse_metadata_directory(self):
        """Open directory browser for metadata directory"""
        current_path = self.metadata_path_edit.text()
        if not current_path:
            current_path = str(Path.cwd())

        directory = QFileDialog.getExistingDirectory(
            self,
            "Select Metadata Directory",
            current_path,
            QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks,
        )

        if directory:
            self.metadata_path_edit.setText(directory)

    def browse_colormap_file(self):
        """Open file browser for color map file"""
        current_path = self.colormap_path_edit.text()
        if not current_path:
            # Default to colormaps directory in project
            current_path = str(Path(__file__).parent.parent.parent / "colormaps")

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Color Map File",
            current_path,
            "Color Map Files (*.map);;All Files (*)",
        )

        if file_path:
            self.colormap_path_edit.setText(file_path)

    def browse_export_directory(self):
        """Open directory browser for export directory"""
        current_path = self.export_path_edit.text()
        if not current_path:
            current_path = str(Path.cwd())

        directory = QFileDialog.getExistingDirectory(
            self,
            "Select Export Directory",
            current_path,
            QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks,
        )

        if directory:
            self.export_path_edit.setText(directory)

    def browse_romdrop_executable(self):
        """Open file browser for RomDrop executable"""
        current_path = self.romdrop_path_edit.text()
        if not current_path:
            current_path = str(Path.cwd())

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select RomDrop Executable",
            current_path,
            "Executable Files (*.exe);;All Files (*)",
        )

        if file_path:
            self.romdrop_path_edit.setText(file_path)

    def apply_settings(self):
        """Apply settings without closing the dialog"""
        # Save projects directory
        projects_dir = self.projects_path_edit.text().strip()
        if projects_dir:
            self.settings.set_projects_directory(projects_dir)

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

        # Save export directory
        export_dir = self.export_path_edit.text().strip()
        self.settings.set_export_directory(export_dir)

        # Save toggle categories
        toggle_cats = []
        if self.dtc_toggle_checkbox.isChecked():
            toggle_cats.append("DTC - Activation Flags")
        self.settings.set_toggle_categories(toggle_cats)

        # Save RomDrop executable path
        romdrop_path = self.romdrop_path_edit.text().strip()
        self.settings.set_romdrop_executable_path(romdrop_path)

        # Save MCP auto-start setting
        self.settings.set_mcp_auto_start(self.mcp_auto_start_checkbox.isChecked())

        # Save J2534 DLL path
        j2534_path = self.j2534_dll_edit.text().strip()
        self.settings.set_j2534_dll_path(j2534_path)

        # Emit signal that settings changed
        self.settings_changed.emit()

    def accept(self):
        """OK button clicked - apply and close"""
        self.apply_settings()
        super().accept()
