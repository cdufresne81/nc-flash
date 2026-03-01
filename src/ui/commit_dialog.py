"""
Commit Dialog

Dialog for entering commit details when saving a version.
Every commit creates a named ROM snapshot.
"""

import re

from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QTextEdit,
    QDialogButtonBox,
    QListWidget,
    QGroupBox,
    QLineEdit,
)
from typing import List

from ..core.version_models import TableChanges


class CommitDialog(QDialog):
    """Dialog for committing changes with a mandatory version name"""

    def __init__(
        self,
        pending_changes: List[TableChanges],
        next_version: int = 1,
        rom_id: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Save Version")
        self.setMinimumSize(500, 450)

        self.pending_changes = pending_changes
        self.next_version = next_version
        self.rom_id = rom_id
        self._version_name = ""
        self._commit_message = ""

        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout()
        self.setLayout(layout)

        # Version info at top
        version_label = QLabel(f"<b>Creating version {self.next_version}</b>")
        version_label.setStyleSheet("font-size: 14px; padding: 5px;")
        layout.addWidget(version_label)

        # Version name (required)
        name_group = QGroupBox("Version Name (required)")
        name_layout = QVBoxLayout()
        name_group.setLayout(name_layout)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g., egr_delete, stage1, fuel_wot_richen")
        self.name_edit.textChanged.connect(self._update_filename_preview)
        name_layout.addWidget(self.name_edit)

        # Filename preview
        self.filename_preview = QLabel()
        self.filename_preview.setStyleSheet("color: gray; font-style: italic;")
        name_layout.addWidget(self.filename_preview)

        layout.addWidget(name_group)

        # Commit message (optional)
        msg_group = QGroupBox("Description (optional)")
        msg_layout = QVBoxLayout()
        msg_group.setLayout(msg_layout)

        self.message_edit = QTextEdit()
        self.message_edit.setPlaceholderText(
            "Describe what you changed and why...\n\n"
            "Examples:\n"
            "- Increased fuel enrichment at high RPM for safer WOT\n"
            "- Adjusted timing for 91 octane fuel\n"
            "- Disabled closed-loop fuel correction"
        )
        self.message_edit.setMinimumHeight(80)
        msg_layout.addWidget(self.message_edit)

        layout.addWidget(msg_group)

        # Modified tables list (read-only, informational)
        tables_group = QGroupBox(f"Modified Tables ({len(self.pending_changes)})")
        tables_layout = QVBoxLayout()
        tables_group.setLayout(tables_layout)

        self.tables_list = QListWidget()
        self.tables_list.setMaximumHeight(100)
        for table_change in self.pending_changes:
            cell_count = len(table_change.cell_changes)
            self.tables_list.addItem(f"{table_change.table_name} ({cell_count} cells)")

        tables_layout.addWidget(self.tables_list)
        layout.addWidget(tables_group)

        # Spacer
        layout.addStretch()

        # Buttons
        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok_button = button_box.button(QDialogButtonBox.Ok)
        self.ok_button.setText("Commit")
        self.ok_button.setEnabled(False)
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

        # Set initial preview text (must be after ok_button is created)
        self._update_filename_preview()

    @staticmethod
    def _sanitize_name(text: str) -> str:
        """Sanitize version name: lowercase, spaces to underscores, strip special chars"""
        text = text.strip().lower()
        text = text.replace(" ", "_")
        text = re.sub(r"[^a-z0-9_]", "", text)
        return text

    def _update_filename_preview(self):
        """Update the filename preview label and OK button state"""
        raw = self.name_edit.text()
        sanitized = self._sanitize_name(raw)

        if sanitized:
            filename = f"v{self.next_version}_{self.rom_id}_{sanitized}.bin"
            self.filename_preview.setText(f"File: {filename}")
            self.ok_button.setEnabled(True)
        else:
            self.filename_preview.setText("Enter a version name")
            self.ok_button.setEnabled(False)

    def _on_accept(self):
        """Validate and accept"""
        sanitized = self._sanitize_name(self.name_edit.text())
        if not sanitized:
            self.name_edit.setFocus()
            self.name_edit.setStyleSheet("border: 2px solid red;")
            return

        self._version_name = sanitized
        self._commit_message = self.message_edit.toPlainText().strip()
        self.accept()

    def get_version_name(self) -> str:
        """Get the sanitized version name"""
        return self._version_name

    def get_commit_message(self) -> str:
        """Get the commit message"""
        return self._commit_message
