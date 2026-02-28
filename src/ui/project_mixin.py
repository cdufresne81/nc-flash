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

from pathlib import Path

from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox

from src.utils.logging_config import get_logger
from src.core.definition_parser import load_definition
from src.core.rom_reader import RomReader
from src.core.exceptions import RomEditorError
from src.core.project_manager import ProjectManager
from src.ui.rom_document import RomDocument
from src.ui.project_wizard import ProjectWizard
from src.ui.commit_dialog import CommitDialog
from src.ui.history_viewer import HistoryViewer
from src.ui.table_viewer_window import TableViewerWindow

logger = get_logger(__name__)


def _handle_rom_operation_error(parent, operation: str, exception: Exception):
    """Handle common ROM operation errors with consistent logging and user feedback"""
    error_msg = f"Failed to {operation}:\n{str(exception)}"
    logger.error(error_msg.replace('\n', ' '))
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
                    description=wizard.project_description
                )

                # Open the project (gets [P] prefix, color swatch, recent files)
                self.open_project_path(project.project_path)

                QMessageBox.information(
                    self,
                    "Project Created",
                    f"Project '{project.name}' created successfully.\n\n"
                    f"Location: {project.project_path}"
                )

            except RomEditorError as e:
                _handle_rom_operation_error(self, "create project", e)
            except Exception as e:
                logger.exception(f"Unexpected error creating project: {type(e).__name__}: {e}")
                QMessageBox.critical(
                    self, "Error",
                    f"Unexpected error creating project:\n{type(e).__name__}: {e}"
                )

    def open_project(self):
        """Open an existing project via folder dialog"""
        project_path = QFileDialog.getExistingDirectory(
            self,
            "Open Project Folder",
            str(Path.home())
        )

        if not project_path:
            return

        # Check if it's a valid project folder
        if not ProjectManager.is_project_folder(project_path):
            QMessageBox.warning(
                self,
                "Invalid Project",
                "The selected folder is not a valid NC ROM Editor project.\n\n"
                "A project folder must contain a project.json file."
            )
            return

        self.open_project_path(project_path)

    def open_project_path(self, project_path: str):
        """Open a project from a given path (used by open_project dialog and session restore)"""
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

                logger.info(f"Opened project: {project.name}")
                self.statusBar().showMessage(f"Opened project: {project.name}")
            else:
                QMessageBox.warning(
                    self,
                    "Definition Not Found",
                    f"Could not find ROM definition for ID: {rom_id}\n\n"
                    "The project was created with a ROM definition that is no longer available."
                )

        except RomEditorError as e:
            _handle_rom_operation_error(self, "open project", e)
        except Exception as e:
            logger.exception(f"Unexpected error opening project: {type(e).__name__}: {e}")
            QMessageBox.critical(
                self, "Error",
                f"Unexpected error opening project:\n{type(e).__name__}: {e}"
            )

    def commit_changes(self):
        """Commit pending changes to the project"""
        if not self.project_manager.is_project_open():
            QMessageBox.warning(
                self,
                "No Project",
                "No project is currently open.\n\n"
                "Use File > New Project to create a project first."
            )
            return

        if not self.change_tracker.has_pending_changes():
            QMessageBox.information(
                self,
                "No Changes",
                "There are no pending changes to commit."
            )
            return

        # Get pending changes
        pending = self.change_tracker.get_pending_changes()

        # Get version info for dialog
        next_version = self.project_manager.get_next_version()
        rom_id = self.project_manager.current_project.original_rom.rom_id
        suggested_suffix = self.project_manager.current_project.last_suffix

        # Show commit dialog with version info
        dialog = CommitDialog(
            pending,
            next_version=next_version,
            rom_id=rom_id,
            suggested_suffix=suggested_suffix,
            parent=self
        )
        if dialog.exec() == QDialog.Accepted:
            try:
                message = dialog.get_commit_message()
                create_snapshot = dialog.get_create_snapshot()
                snapshot_suffix = dialog.get_snapshot_suffix()

                # Save changes to working ROM file first
                document = self.get_current_document()
                if document:
                    document.rom_reader.save_rom()

                # Create commit with version numbering
                commit = self.project_manager.commit_changes(
                    message=message,
                    changes=pending,
                    create_snapshot=create_snapshot,
                    snapshot_suffix=snapshot_suffix
                )

                # Clear pending changes
                self.change_tracker.clear_pending_changes()

                # Update UI
                self._update_project_ui()

                logger.info(f"Committed v{commit.version}: {message[:50]}...")
                self.statusBar().showMessage(f"Saved version {commit.version}")

            except RomEditorError as e:
                _handle_rom_operation_error(self, "commit changes", e)
            except Exception as e:
                logger.exception(f"Unexpected error committing changes: {type(e).__name__}: {e}")
                QMessageBox.critical(
                    self, "Error",
                    f"Unexpected error committing changes:\n{type(e).__name__}: {e}"
                )

    def show_history(self):
        """Show commit history viewer"""
        if not self.project_manager.is_project_open():
            QMessageBox.information(
                self,
                "No Project",
                "Open a project to view commit history."
            )
            return

        dialog = HistoryViewer(self.project_manager, self)
        dialog.view_table_diff.connect(self._on_view_table_diff)
        dialog.exec()

    def _on_view_table_diff(self, table_name: str, commit):
        """
        Open a table viewer showing changes from a specific commit

        Args:
            table_name: Name of the table to view
            commit: Commit object containing the changes
        """
        document = self.get_current_document()
        if not document:
            return

        # Find the table definition
        table = document.rom_definition.get_table_by_name(table_name)
        if not table:
            QMessageBox.warning(
                self,
                "Table Not Found",
                f"Could not find table: {table_name}"
            )
            return

        try:
            # Load base version data (previous version)
            base_version = commit.version - 1 if commit.version > 0 else 0
            base_rom_data = self.project_manager.load_version_data(base_version)

            if base_rom_data is None:
                QMessageBox.warning(
                    self,
                    "Version Not Found",
                    f"Could not load base version {base_version} data."
                )
                return

            # Create a temporary RomReader to read base version table data
            import tempfile
            import os

            # Write base ROM to temp file and read table data
            with tempfile.NamedTemporaryFile(delete=False, suffix='.bin') as tmp:
                tmp.write(base_rom_data)
                tmp_path = tmp.name

            try:
                base_reader = RomReader(tmp_path, document.rom_definition)
                base_data = base_reader.read_table_data(table)
            finally:
                os.unlink(tmp_path)

            # Read current version table data
            current_data = document.rom_reader.read_table_data(table)

            # Open diff viewer
            viewer_window = TableViewerWindow(
                table,
                current_data,
                document.rom_definition,
                rom_path=document.rom_path,
                parent=self,
                diff_mode=True,
                diff_base_data=base_data
            )
            viewer_window.setWindowTitle(f"{table_name} (v{base_version} -> v{commit.version})")
            viewer_window.show()

        except RomEditorError as e:
            logger.error(f"Failed to open diff view: {e}")
            QMessageBox.warning(
                self,
                "Error",
                f"Failed to open diff view:\n{e}"
            )
        except Exception as e:
            logger.exception(f"Unexpected error opening diff view: {type(e).__name__}: {e}")
            QMessageBox.critical(
                self, "Error",
                f"Unexpected error opening diff view:\n{type(e).__name__}: {e}"
            )
