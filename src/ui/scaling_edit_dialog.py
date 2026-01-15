"""
Scaling Edit Dialog

Dialog for editing scaling properties (min, max, units, format, increment)
for the currently displayed table.
"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLineEdit, QDoubleSpinBox, QPushButton, QLabel,
    QMessageBox, QCheckBox
)
from PySide6.QtCore import Qt

from ..core.rom_definition import Scaling


class ScalingEditDialog(QDialog):
    """Dialog for editing scaling properties"""

    def __init__(self, scaling: Scaling, scaling_name: str, parent=None):
        """
        Initialize dialog

        Args:
            scaling: The Scaling object to edit
            scaling_name: Name/ID of the scaling (for display)
            parent: Parent widget
        """
        super().__init__(parent)
        self.scaling = scaling
        self.scaling_name = scaling_name

        self.setWindowTitle(f"Edit Scaling: {scaling_name}")
        self.setMinimumWidth(350)

        self._setup_ui()
        self._load_values()

    def _setup_ui(self):
        """Set up the dialog UI"""
        layout = QVBoxLayout(self)

        # Form layout for fields
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)

        # Minimum value
        self.min_enabled = QCheckBox()
        self.min_edit = QLineEdit()
        self.min_edit.setPlaceholderText("No limit")
        min_layout = QHBoxLayout()
        min_layout.addWidget(self.min_enabled)
        min_layout.addWidget(self.min_edit)
        form.addRow("Minimum:", min_layout)
        self.min_enabled.toggled.connect(lambda checked: self.min_edit.setEnabled(checked))

        # Maximum value
        self.max_enabled = QCheckBox()
        self.max_edit = QLineEdit()
        self.max_edit.setPlaceholderText("No limit")
        max_layout = QHBoxLayout()
        max_layout.addWidget(self.max_enabled)
        max_layout.addWidget(self.max_edit)
        form.addRow("Maximum:", max_layout)
        self.max_enabled.toggled.connect(lambda checked: self.max_edit.setEnabled(checked))

        # Units
        self.units_edit = QLineEdit()
        self.units_edit.setPlaceholderText("e.g., %, degrees, RPM")
        form.addRow("Units:", self.units_edit)

        # Format
        self.format_edit = QLineEdit()
        self.format_edit.setPlaceholderText("e.g., %0.2f, %d")
        form.addRow("Format:", self.format_edit)

        # Increment
        self.inc_edit = QLineEdit()
        self.inc_edit.setPlaceholderText("e.g., 0.1, 1, 10")
        form.addRow("Increment:", self.inc_edit)

        layout.addLayout(form)

        # Info label
        info_label = QLabel(
            "<i>Note: Changes are saved to the metadata XML file and take effect immediately.</i>"
        )
        info_label.setWordWrap(True)
        layout.addWidget(info_label)

        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(cancel_btn)

        save_btn = QPushButton("Save")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._save)
        button_layout.addWidget(save_btn)

        layout.addLayout(button_layout)

    def _load_values(self):
        """Load current scaling values into fields"""
        # Min
        if self.scaling.min is not None:
            self.min_enabled.setChecked(True)
            self.min_edit.setText(str(self.scaling.min))
            self.min_edit.setEnabled(True)
        else:
            self.min_enabled.setChecked(False)
            self.min_edit.setEnabled(False)

        # Max
        if self.scaling.max is not None:
            self.max_enabled.setChecked(True)
            self.max_edit.setText(str(self.scaling.max))
            self.max_edit.setEnabled(True)
        else:
            self.max_enabled.setChecked(False)
            self.max_edit.setEnabled(False)

        # Units
        if self.scaling.units:
            self.units_edit.setText(self.scaling.units)

        # Format
        if self.scaling.format:
            self.format_edit.setText(self.scaling.format)

        # Increment
        if self.scaling.inc is not None:
            self.inc_edit.setText(str(self.scaling.inc))

    def _save(self):
        """Validate and accept dialog"""
        # Validate min if enabled
        if self.min_enabled.isChecked() and self.min_edit.text().strip():
            try:
                float(self.min_edit.text().strip())
            except ValueError:
                QMessageBox.warning(self, "Invalid Value", "Minimum must be a valid number.")
                return

        # Validate max if enabled
        if self.max_enabled.isChecked() and self.max_edit.text().strip():
            try:
                float(self.max_edit.text().strip())
            except ValueError:
                QMessageBox.warning(self, "Invalid Value", "Maximum must be a valid number.")
                return

        # Validate increment if provided
        if self.inc_edit.text().strip():
            try:
                float(self.inc_edit.text().strip())
            except ValueError:
                QMessageBox.warning(self, "Invalid Value", "Increment must be a valid number.")
                return

        # Validate format string (basic check)
        fmt = self.format_edit.text().strip()
        if fmt and '%' not in fmt:
            QMessageBox.warning(
                self, "Invalid Format",
                "Format string should contain a % specifier (e.g., %0.2f, %d)."
            )
            return

        self.accept()

    def get_values(self) -> dict:
        """
        Get edited values as a dictionary

        Returns:
            Dict with keys: min, max, units, format, inc
            Values are strings suitable for XML attributes
        """
        result = {}

        # Min
        if self.min_enabled.isChecked() and self.min_edit.text().strip():
            result['min'] = self.min_edit.text().strip()
        else:
            result['min'] = None

        # Max
        if self.max_enabled.isChecked() and self.max_edit.text().strip():
            result['max'] = self.max_edit.text().strip()
        else:
            result['max'] = None

        # Units
        result['units'] = self.units_edit.text().strip()

        # Format
        result['format'] = self.format_edit.text().strip()

        # Increment
        inc_text = self.inc_edit.text().strip()
        result['inc'] = inc_text if inc_text else None

        return result
