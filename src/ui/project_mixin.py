"""
Project Mixin for MainWindow

Handles project management operations: creating projects, opening projects,
committing changes, viewing history, and viewing table diffs.

This is a mixin class — it has no __init__ and relies on MainWindow providing:
- self.project_manager (ProjectManager instance)
- self.change_tracker (ChangeTracker instance)
- self.rom_detector (RomDetector instance, may be None)
- self.get_current_document() method
- self._open_rom_file(path) method
- self._update_project_ui() method
- self.open_table_windows (list)
- self.tab_widget (QTabWidget)
- self.statusBar() method
"""

import tempfile
import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QDialog, QMessageBox

from src.utils.logging_config import get_logger
from src.core.definition_parser import load_definition
from src.core.rom_reader import RomReader
from src.core.exceptions import RomEditorError
from src.core.project_manager import ProjectManager
from src.ui.rom_document import RomDocument
from src.ui.project_wizard import ProjectWizard
from src.ui.commit_dialog import CommitDialog
from src.ui.compare_window import CompareWindow
from src.ui.history_viewer import HistoryViewer

logger = get_logger(__name__)


def _handle_rom_operation_error(parent, operation: str, exception: Exception):
    """Handle common ROM operation errors with consistent logging and user feedback"""
    error_msg = f"Failed to {operation}:\n{str(exception)}"
    logger.error(error_msg.replace("\n", " "))
    QMessageBox.critical(parent, "Error", error_msg)


class ProjectMixin:
    """Mixin providing project management functionality for MainWindow."""

    def new_project(self):
        """Create a new project via wizard"""
        wizard = ProjectWizard(self)
        if wizard.exec() == QDialog.Accepted:
            try:
                # Create the project
                project = self.project_manager.create_project(
                    project_path=wizard.project_location,
                    project_name=wizard.project_name,
                    source_rom_path=wizard.rom_path,
                    rom_definition=wizard.rom_definition,
                    description=wizard.project_description,
                )

                # Open the project (gets [P] prefix, color swatch, recent files)
                self.open_project_path(project.project_path)

                QMessageBox.information(
                    self,
                    "Project Created",
                    f"Project '{project.name}' created successfully.\n\n"
                    f"Location: {project.project_path}",
                )

            except RomEditorError as e:
                _handle_rom_operation_error(self, "create project", e)
            except Exception as e:
                logger.exception(
                    f"Unexpected error creating project: {type(e).__name__}: {e}"
                )
                QMessageBox.critical(
                    self,
                    "Error",
                    f"Unexpected error creating project:\n{type(e).__name__}: {e}",
                )

    def open_project_path(self, project_path: str):
        """Open a project from a given path (used by open_file, session restore, recent files)"""
        # Prevent opening the same project twice
        existing = self._find_open_tab(project_path=project_path)
        if existing >= 0:
            self.tab_widget.setCurrentIndex(existing)
            QMessageBox.information(
                self,
                "Already Open",
                f"This project is already open.\n\n{Path(project_path).name}",
            )
            return

        try:
            # Open the project
            project = self.project_manager.open_project(project_path)

            # Get ROM definition for the project
            rom_id = project.original_rom.rom_id
            xml_path = self.rom_detector.find_definition_by_id(rom_id)

            if xml_path:
                rom_definition = load_definition(xml_path)

                # Create ROM reader for working ROM
                rom_reader = RomReader(project.working_rom_path, rom_definition)

                # Create ROM document widget
                rom_document = RomDocument(
                    project.working_rom_path, rom_definition, rom_reader, self
                )
                rom_document.project_path = project.project_path
                rom_document.table_selected.connect(self.on_table_selected)

                # Assign color and add as new tab with swatch
                rom_path = rom_reader.rom_path
                self._assign_rom_color(rom_path)

                tab_title = f"[P] {project.name}"
                tab_index = self.tab_widget.addTab(rom_document, tab_title)
                self.tab_widget.setTabToolTip(tab_index, project.project_path)
                self._create_tab_color_button(rom_path, tab_index)
                self.tab_widget.setCurrentIndex(tab_index)

                # Add to recent files
                self.settings.add_recent_file(f"project:{project.project_path}")
                self.update_recent_files_menu()

                # Update UI state
                self._update_project_ui()
                self._write_workspace_state()

                logger.info(f"Opened project: {project.name}")
                self.statusBar().showMessage(f"Opened project: {project.name}")
            else:
                QMessageBox.warning(
                    self,
                    "Definition Not Found",
                    f"Could not find ROM definition for ID: {rom_id}\n\n"
                    "The project was created with a ROM definition that is no longer available.",
                )

        except RomEditorError as e:
            _handle_rom_operation_error(self, "open project", e)
        except Exception as e:
            logger.exception(
                f"Unexpected error opening project: {type(e).__name__}: {e}"
            )
            QMessageBox.critical(
                self,
                "Error",
                f"Unexpected error opening project:\n{type(e).__name__}: {e}",
            )

    def commit_changes(self):
        """Commit pending changes to the project"""
        if not self.project_manager.is_project_open():
            QMessageBox.warning(
                self,
                "No Project",
                "No project is currently open.\n\n"
                "Use File > New Project to create a project first.",
            )
            return

        if not self.change_tracker.has_pending_changes():
            QMessageBox.information(
                self, "No Changes", "There are no pending changes to commit."
            )
            return

        # Get pending changes
        pending = self.change_tracker.get_pending_changes()

        # Get version info for dialog
        next_version = self.project_manager.get_next_version()
        rom_id = self.project_manager.current_project.original_rom.rom_id

        # Show commit dialog with version info
        dialog = CommitDialog(
            pending,
            next_version=next_version,
            rom_id=rom_id,
            parent=self,
        )
        if dialog.exec() == QDialog.Accepted:
            try:
                message = dialog.get_commit_message()
                version_name = dialog.get_version_name()

                # Save changes to working ROM file first
                document = self.get_current_document()
                if document:
                    document.rom_reader.save_rom()

                # Create commit with version numbering (always creates snapshot)
                commit = self.project_manager.commit_changes(
                    message=message,
                    changes=pending,
                    version_name=version_name,
                )

                # Clear pending changes and modified flag
                self.change_tracker.clear_pending_changes()
                if document:
                    document.set_modified(False)

                # Update UI
                self._update_project_ui()

                logger.info(f"Committed v{commit.version}: {message[:50]}...")
                self.statusBar().showMessage(f"Saved version {commit.version}")

            except RomEditorError as e:
                _handle_rom_operation_error(self, "commit changes", e)
            except Exception as e:
                logger.exception(
                    f"Unexpected error committing changes: {type(e).__name__}: {e}"
                )
                QMessageBox.critical(
                    self,
                    "Error",
                    f"Unexpected error committing changes:\n{type(e).__name__}: {e}",
                )

    def show_history(self):
        """Show commit history viewer"""
        if not self.project_manager.is_project_open():
            QMessageBox.information(
                self, "No Project", "Open a project to view commit history."
            )
            return

        self._history_dialog = HistoryViewer(self.project_manager, self)
        self._history_dialog.view_table_diff.connect(self._on_view_table_diff)
        self._history_dialog.revert_requested.connect(self._on_revert_version)
        self._history_dialog.delete_requested.connect(self._on_delete_version)
        self._history_dialog.exec()
        self._history_dialog = None

    def _on_view_table_diff(self, table_name: str, commit):
        """
        Open a read-only CompareWindow showing changes from a specific commit.

        Uses the base version (commit.version - 1) vs the commit version,
        displayed in the ROM comparison view with copy buttons disabled.
        Only one compare window is open at a time — opening a new one closes the previous.

        Args:
            table_name: Name of the table to view (used for context, all tables shown)
            commit: Commit object containing the changes
        """
        document = self.get_current_document()
        if not document:
            return

        # Close any existing version compare window
        dialog = getattr(self, "_history_dialog", None)
        if dialog:
            old_cmp = getattr(dialog, "_compare_window", None)
            if old_cmp is not None:
                try:
                    old_cmp.close()
                except RuntimeError:
                    pass  # C++ object already deleted
                dialog._compare_window = None

        try:
            # Load base version data (previous version)
            base_version = commit.version - 1 if commit.version > 0 else 0
            base_rom_data = self.project_manager.load_version_data(base_version)
            commit_rom_data = self.project_manager.load_version_data(commit.version)

            if base_rom_data is None:
                QMessageBox.warning(
                    self,
                    "Version Not Found",
                    f"Could not load base version {base_version} data.",
                )
                return

            if commit_rom_data is None:
                QMessageBox.warning(
                    self,
                    "Version Not Found",
                    f"Could not load version {commit.version} data.",
                )
                return

            # Write both versions to temp files for RomReader
            base_tmp = tempfile.NamedTemporaryFile(
                delete=False, suffix=".bin", prefix="base_"
            )
            base_tmp.write(base_rom_data)
            base_tmp.close()

            commit_tmp = tempfile.NamedTemporaryFile(
                delete=False, suffix=".bin", prefix="commit_"
            )
            commit_tmp.write(commit_rom_data)
            commit_tmp.close()

            try:
                base_reader = RomReader(base_tmp.name, document.rom_definition)
                commit_reader = RomReader(commit_tmp.name, document.rom_definition)
            except Exception:
                os.unlink(base_tmp.name)
                os.unlink(commit_tmp.name)
                raise

            # Build version labels
            base_commit = self.project_manager.get_commit_by_version(base_version)
            base_name = (
                base_commit.snapshot_filename
                if base_commit and base_commit.snapshot_filename
                else f"v{base_version}"
            )
            commit_name = (
                commit.snapshot_filename
                if commit.snapshot_filename
                else f"v{commit.version}"
            )

            # Open read-only compare window, parented to the history dialog
            # so it appears on top of the modal dialog
            parent_widget = dialog if dialog else self
            window = CompareWindow(
                base_reader,
                commit_reader,
                document.rom_definition,
                document.rom_definition,
                QColor(100, 100, 100),
                QColor(100, 100, 100),
                base_name,
                commit_name,
                parent=parent_widget,
                readonly=True,
            )

            if not window.has_diffs:
                window.deleteLater()
                QMessageBox.information(
                    self,
                    "No Differences",
                    f"No table differences found between v{base_version} and v{commit.version}.",
                )
                os.unlink(base_tmp.name)
                os.unlink(commit_tmp.name)
                return

            # Clean up temp files when the window closes
            def _cleanup():
                try:
                    os.unlink(base_tmp.name)
                except OSError:
                    pass
                try:
                    os.unlink(commit_tmp.name)
                except OSError:
                    pass

            window.destroyed.connect(_cleanup)

            # Track single instance on the dialog
            if dialog:
                dialog._compare_window = window

            window.show()
            window.raise_()
            window.activateWindow()

        except RomEditorError as e:
            logger.error(f"Failed to open diff view: {e}")
            QMessageBox.warning(self, "Error", f"Failed to open diff view:\n{e}")
        except Exception as e:
            logger.exception(
                f"Unexpected error opening diff view: {type(e).__name__}: {e}"
            )
            QMessageBox.critical(
                self,
                "Error",
                f"Unexpected error opening diff view:\n{type(e).__name__}: {e}",
            )

    def _on_revert_version(self, version: int):
        """Confirm and revert working ROM to a previous version"""
        commit = self.project_manager.get_commit_by_version(version)
        if not commit:
            return

        snapshot_name = commit.snapshot_filename or f"v{version}"
        reply = QMessageBox.question(
            self,
            "Revert to Version",
            f"Revert working ROM to v{version} ({snapshot_name})?\n\n"
            f"This will:\n"
            f"- Overwrite the current working ROM\n"
            f"- Move all newer versions to trash\n\n"
            f"This cannot be easily undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )

        if reply == QMessageBox.Yes:
            try:
                restored = self.project_manager.revert_to_version(version)

                # Reload the ROM in the editor from the overwritten working file
                document = self.get_current_document()
                if document:
                    document.rom_reader._load_rom()

                # Clear pending changes (working ROM now matches the snapshot)
                self.change_tracker.clear_pending_changes()
                self._update_project_ui()

                self.statusBar().showMessage(f"Reverted to {restored}")

            except RomEditorError as e:
                _handle_rom_operation_error(self, "revert version", e)
            except Exception as e:
                logger.exception(f"Unexpected error reverting: {type(e).__name__}: {e}")
                QMessageBox.critical(
                    self,
                    "Error",
                    f"Unexpected error reverting:\n{type(e).__name__}: {e}",
                )

    def _on_delete_version(self, version: int):
        """Confirm and soft-delete a version"""
        commit = self.project_manager.get_commit_by_version(version)
        if not commit:
            return

        snapshot_name = commit.snapshot_filename or f"v{version}"
        reply = QMessageBox.question(
            self,
            "Delete Version",
            f"Delete v{version} ({snapshot_name})?\n\n"
            f"The snapshot file will be moved to _trash/.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )

        if reply == QMessageBox.Yes:
            try:
                self.project_manager.soft_delete_version(version)
                self.statusBar().showMessage(f"Deleted v{version}")

            except RomEditorError as e:
                _handle_rom_operation_error(self, "delete version", e)
            except Exception as e:
                logger.exception(
                    f"Unexpected error deleting version: {type(e).__name__}: {e}"
                )
                QMessageBox.critical(
                    self,
                    "Error",
                    f"Unexpected error deleting version:\n{type(e).__name__}: {e}",
                )
