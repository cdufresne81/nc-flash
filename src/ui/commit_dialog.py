"""
Commit Dialog

Dialog for entering commit message when saving changes.
"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QTextEdit, QDialogButtonBox, QListWidget,
    QGroupBox, QCheckBox, QLineEdit, QFrame
)
from PySide6.QtCore import Qt
from typing import List

from ..core.version_models import TableChanges


class CommitDialog(QDialog):
    """Dialog for committing changes with a message"""

    def __init__(
        self,
        pending_changes: List[TableChanges],
        next_version: int = 1,
        rom_id: str = "",
        suggested_suffix: str = "",
        parent=None
    ):
        super().__init__(parent)
        self.setWindowTitle("Save Changes")
        self.setMinimumSize(500, 450)

        self.pending_changes = pending_changes
        self.next_version = next_version
        self.rom_id = rom_id
        self.suggested_suffix = suggested_suffix
        self.commit_message = ""
        self.create_snapshot = False
        self.snapshot_suffix = ""

        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout()
        self.setLayout(layout)

        # Version info at top
        version_label = QLabel(f"<b>Creating version {self.next_version}</b>")
        version_label.setStyleSheet("font-size: 14px; padding: 5px;")
        layout.addWidget(version_label)

        # Commit message (prominent, at top)
        msg_group = QGroupBox("Commit Message (required)")
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
        self.message_edit.setMinimumHeight(100)
        msg_layout.addWidget(self.message_edit)

        layout.addWidget(msg_group)

        # Modified tables list (simple, no cell details)
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

        # Snapshot section
        snapshot_group = QGroupBox("ROM Snapshot")
        snapshot_layout = QVBoxLayout()
        snapshot_group.setLayout(snapshot_layout)

        self.snapshot_checkbox = QCheckBox("Save ROM snapshot (recommended for major changes)")
        self.snapshot_checkbox.setToolTip(
            "Creates a full copy of the ROM at this version.\n"
            "Allows you to revert to this exact state later."
        )
        self.snapshot_checkbox.toggled.connect(self._on_snapshot_toggled)
        snapshot_layout.addWidget(self.snapshot_checkbox)

        # Suffix input (only enabled when snapshot checked)
        suffix_layout = QHBoxLayout()
        suffix_label = QLabel("Filename suffix:")
        suffix_layout.addWidget(suffix_label)

        self.suffix_edit = QLineEdit()
        self.suffix_edit.setPlaceholderText("e.g., timing_fix, stage1, fuel_tune")
        self.suffix_edit.setText(self.suggested_suffix)
        self.suffix_edit.setEnabled(False)
        self.suffix_edit.textChanged.connect(self._update_filename_preview)
        suffix_layout.addWidget(self.suffix_edit)

        snapshot_layout.addLayout(suffix_layout)

        # Filename preview
        self.filename_preview = QLabel()
        self.filename_preview.setStyleSheet("color: gray; font-style: italic;")
        self._update_filename_preview()
        snapshot_layout.addWidget(self.filename_preview)

        layout.addWidget(snapshot_group)

        # Spacer
        layout.addStretch()

        # Buttons
        button_box = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        button_box.button(QDialogButtonBox.Ok).setText("Commit")
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def _on_snapshot_toggled(self, checked: bool):
        """Handle snapshot checkbox toggle"""
        self.suffix_edit.setEnabled(checked)
        self._update_filename_preview()

    def _update_filename_preview(self):
        """Update the filename preview label"""
        if self.snapshot_checkbox.isChecked():
            suffix = self.suffix_edit.text().strip()
            if suffix:
                filename = f"v{self.next_version}_{self.rom_id}_{suffix}.bin"
                self.filename_preview.setText(f"File: {filename}")
            else:
                self.filename_preview.setText("Enter a suffix for the filename")
        else:
            self.filename_preview.setText("No snapshot will be created")

    def _on_accept(self):
        """Validate and accept"""
        # Validate message
        message = self.message_edit.toPlainText().strip()
        if not message:
            self.message_edit.setFocus()
            self.message_edit.setStyleSheet("border: 2px solid red;")
            return

        # Validate suffix if snapshot is checked
        if self.snapshot_checkbox.isChecked():
            suffix = self.suffix_edit.text().strip()
            if not suffix:
                self.suffix_edit.setFocus()
                self.suffix_edit.setStyleSheet("border: 2px solid red;")
                return
            self.snapshot_suffix = suffix

        self.commit_message = message
        self.create_snapshot = self.snapshot_checkbox.isChecked()
        self.accept()

    def get_commit_message(self) -> str:
        """Get the commit message"""
        return self.commit_message

    def get_create_snapshot(self) -> bool:
        """Get whether to create a snapshot"""
        return self.create_snapshot

    def get_snapshot_suffix(self) -> str:
        """Get the user-entered snapshot suffix"""
        return self.snapshot_suffix


class QuickCommitDialog(QDialog):
    """Simplified commit dialog for quick saves"""

    def __init__(self, tables_modified: List[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Quick Commit")
        self.setMinimumSize(400, 200)

        self.tables_modified = tables_modified
        self.commit_message = ""

        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout()
        self.setLayout(layout)

        # Tables summary
        tables_str = ", ".join(self.tables_modified[:3])
        if len(self.tables_modified) > 3:
            tables_str += f" (+{len(self.tables_modified) - 3} more)"

        summary = QLabel(f"Modified: <b>{tables_str}</b>")
        summary.setWordWrap(True)
        layout.addWidget(summary)

        # Commit message
        layout.addWidget(QLabel("Commit Message:"))
        self.message_edit = QTextEdit()
        self.message_edit.setPlaceholderText("What did you change?")
        self.message_edit.setMaximumHeight(80)
        layout.addWidget(self.message_edit)

        # Buttons
        button_box = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        button_box.button(QDialogButtonBox.Ok).setText("Commit")
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def _on_accept(self):
        """Validate and accept"""
        message = self.message_edit.toPlainText().strip()
        if not message:
            self.message_edit.setFocus()
            return

        self.commit_message = message
        self.accept()

    def get_commit_message(self) -> str:
        return self.commit_message
