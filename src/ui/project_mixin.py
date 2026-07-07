"""
Project Mixin for MainWindow

Handles project management operations: creating projects, opening projects,
committing changes, viewing history, and viewing table diffs.

This is a mixin class — it has no __init__ and relies on MainWindow providing:
- self.project_manager (ProjectManager instance)
- self.change_tracker (ChangeTracker instance)
- self.rom_detector (RomDetector instance, may be None)
- self.get_current_document() method
- self._find_document_by_rom_path(path) method
- self._reset_document_edit_baseline(document) method
- self._open_rom_file(path) method
- self._update_project_ui() method
- self.table_undo_manager (TableUndoManager instance)
- self.open_table_windows (list)
- self.tab_bar (QTabBar)
- self.rom_stack (QStackedWidget)
- self.statusBar() method
"""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QDialog, QMessageBox

from src.utils.logging_config import get_logger
from src.core.definition_parser import load_definition
from src.core.rom_reader import RomReader
from src.core.exceptions import RomEditorError
from src.core.project_manager import ProjectManager
from src.core.table_undo_manager import make_table_key
from src.ui.rom_document import RomDocument
from src.ui.project_wizard import ProjectWizard
from src.ui.commit_dialog import CommitDialog
from src.ui.compare_window import CompareWindow
from src.ui.history_viewer import HistoryViewer
from src.ui.error_helpers import handle_rom_operation_error

logger = get_logger(__name__)


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
                handle_rom_operation_error(self, "create project", e)
            except Exception as e:
                logger.exception(
                    f"Unexpected error creating project: {type(e).__name__}: {e}"
                )
                QMessageBox.critical(
                    self,
                    "Error",
                    f"Unexpected error creating project:\n{type(e).__name__}: {e}",
                )

    def _ensure_single_project(self, new_project_path, *, prompt: bool) -> bool:
        """Enforce one-project-at-a-time before opening new_project_path.

        The ProjectManager is a singleton (one current_project), so a second
        project would rebind it and let the first project's tab commit into the
        second's history. Returns True to proceed, False to abort. (B4)
        """
        if not self.project_manager.is_project_open():
            return True
        current = self.project_manager.current_project
        if current is None or Path(current.project_path) == Path(new_project_path):
            return True  # no other project open (same-project handled by caller)

        if not prompt:
            # Session restore / programmatic: keep the already-open project.
            # Surface the skip (status bar, no modal at startup) — otherwise the
            # entry silently vanishes from the session file on the next close.
            logger.warning(
                "A project is already open; skipping additional project "
                f"(one project at a time): {new_project_path}"
            )
            self.statusBar().showMessage(
                "Only one project can be open at a time — skipped "
                f"{Path(new_project_path).name} (reopen it via File > Recent)",
                10000,
            )
            return False

        reply = QMessageBox.question(
            self,
            "Close Current Project?",
            "Only one project can be open at a time.\n\n"
            f"Opening this project will close the current project "
            f"'{current.name}'. Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return False

        # Close the current project's tab (handles its unsaved-changes prompt
        # and per-ROM state cleanup). If the user cancels that prompt, the tab
        # stays open — abort the switch so we never rebind the manager.
        idx = self._find_open_tab(project_path=current.project_path)
        if idx >= 0:
            self.close_tab(idx)
            if self._find_open_tab(project_path=current.project_path) >= 0:
                return False
        self.project_manager.close_project()
        return True

    def open_project_path(self, project_path: str, *, prompt_on_switch: bool = True):
        """Open a project from a given path (used by open_file, session restore, recent files).

        prompt_on_switch: when True (interactive open), opening a *different*
        project prompts to close the currently-open one first — only one project
        is open at a time so a stale tab can't commit into another project's
        history (B4). Session restore passes False: a second, different project
        is skipped rather than prompting mid-startup.
        """
        # Prevent opening the same project twice
        existing = self._find_open_tab(project_path=project_path)
        if existing >= 0:
            self.tab_bar.setCurrentIndex(existing)
            if prompt_on_switch:
                QMessageBox.information(
                    self,
                    "Already Open",
                    f"This project is already open.\n\n{Path(project_path).name}",
                )
            return

        # One project at a time (B4).
        if not self._ensure_single_project(project_path, prompt=prompt_on_switch):
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
                # Show the "*" dirty marker on the project tab when it is edited,
                # matching standalone ROM tabs (B15).
                rom_document.modified_changed.connect(
                    lambda modified, doc=rom_document: self._update_tab_title(doc)
                )

                # Assign color and add as new tab with swatch
                self._assign_rom_color(rom_document)

                tab_title = f"[P] {project.name}"
                rom_document.tab_base_title = tab_title
                tab_index = self.tab_bar.addTab(tab_title)
                self.rom_stack.addWidget(rom_document)
                self.tab_bar.setTabToolTip(tab_index, project.project_path)
                self._create_tab_color_button(rom_document, tab_index)
                self.tab_bar.setCurrentIndex(tab_index)

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
            handle_rom_operation_error(self, "open project", e)
        except Exception as e:
            logger.exception(
                f"Unexpected error opening project: {type(e).__name__}: {e}"
            )
            QMessageBox.critical(
                self,
                "Error",
                f"Unexpected error opening project:\n{type(e).__name__}: {e}",
            )
        finally:
            # A failure AFTER open_project() bound the manager (missing
            # definition XML, load_definition/RomReader error) must not leave it
            # bound with no tab: the B4 one-project guard gates on the manager,
            # so a phantom binding would silently block every subsequent project
            # open (and session restore) against a project the user can't see.
            current = self.project_manager.current_project
            if (
                current is not None
                and Path(current.project_path) == Path(project_path)
                and self._find_open_tab(project_path=current.project_path) < 0
            ):
                logger.warning(
                    f"Project bound without a tab after failed open; unbinding: "
                    f"{project_path}"
                )
                self.project_manager.close_project()

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

        # A commit is scoped to the PROJECT's own ROM. Other ROMs open in
        # parallel tabs (e.g. for Compare) have independent pending edits in the
        # global ChangeTracker that must NOT be folded into this project's
        # history or working file (B3). Resolve the project document and filter
        # to its changes rather than trusting the active tab.
        project_rom_path = self.project_manager.current_project.working_rom_path
        document = self._find_document_by_rom_path(project_rom_path)
        if document is None:
            QMessageBox.warning(
                self,
                "Project ROM Not Open",
                "The project's ROM tab is not open, so there is nothing to commit.",
            )
            return

        rom_path = document.rom_reader.rom_path  # canonical Path for filtering
        pending = self.change_tracker.get_pending_changes_for_rom(rom_path)
        if not pending:
            QMessageBox.information(
                self, "No Changes", "There are no pending changes to commit."
            )
            return

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

                # Flush the PROJECT ROM's edits to its working file (not the
                # active tab, which may be a different ROM) so the snapshot
                # captures exactly this project's changes (B3).
                document.rom_reader.save_rom()

                # Create commit with version numbering (always creates snapshot)
                commit = self.project_manager.commit_changes(
                    message=message,
                    changes=pending,
                    version_name=version_name,
                )

                # Clear only THIS ROM's pending changes + dirty flag; foreign
                # tabs keep their uncommitted edits (B3).
                self.change_tracker.clear_pending_for_rom(rom_path)
                document.set_modified(False)

                # The committed values are the new baseline: drop this ROM's
                # modified-cell borders and re-snapshot originals so future edits
                # diff from the committed bytes (C3). Previously borders lingered
                # until the tab was closed or Save-As'd.
                self._reset_document_edit_baseline(document)

                # Update UI
                self._update_project_ui()

                logger.info(f"Committed v{commit.version}: {message[:50]}...")
                self.statusBar().showMessage(f"Saved version {commit.version}")

            except RomEditorError as e:
                handle_rom_operation_error(self, "commit changes", e)
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
            base_version = commit.version - 1 if commit.version > 0 else 0
            base_path = self.project_manager.get_snapshot_path(base_version)
            commit_path = self.project_manager.get_snapshot_path(commit.version)

            if base_path is None:
                QMessageBox.warning(
                    self,
                    "Version Not Found",
                    f"Snapshot not found for version {base_version}.",
                )
                return

            if commit_path is None:
                QMessageBox.warning(
                    self,
                    "Version Not Found",
                    f"Snapshot not found for version {commit.version}.",
                )
                return

            base_reader = RomReader(str(base_path), document.rom_definition)
            commit_reader = RomReader(str(commit_path), document.rom_definition)

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
                return

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

                # A revert is scoped to the PROJECT's own ROM — the active tab
                # may be a different ROM open for Compare. Resolving via
                # get_current_document() used to reload the WRONG document
                # (discarding its unsaved edits) while the project document
                # kept stale pre-revert bytes in memory that a later save
                # would silently write back (B3-style scoping, as commit does).
                project_rom_path = self.project_manager.current_project.working_rom_path
                document = self._find_document_by_rom_path(project_rom_path)
                if document:
                    rom_path = document.rom_reader.rom_path

                    # Open table windows show pre-revert values and have no
                    # reload path — close them rather than display stale data.
                    for window in [
                        w for w in self.open_table_windows if w.rom_path == rom_path
                    ]:
                        window.close()

                    # Reload the reverted bytes from the overwritten working file
                    document.rom_reader._load_rom()

                    # Undo stacks recorded against pre-revert bytes would write
                    # pre-revert values into the reverted ROM if replayed —
                    # drop them (mirrors tab close).
                    definition = document.rom_reader.definition
                    if definition:
                        table_keys = {
                            make_table_key(rom_path, table.address)
                            for table in definition.tables
                        }
                        self.table_undo_manager.remove_stacks_for_keys(table_keys)

                    # Clear only THIS ROM's pending changes, dirty flag, and
                    # modified-cell borders; foreign tabs keep their edits.
                    self.change_tracker.clear_pending_for_rom(rom_path)
                    document.set_modified(False)
                    self._reset_document_edit_baseline(document)

                self._update_project_ui()

                self.statusBar().showMessage(f"Reverted to {restored}")

            except RomEditorError as e:
                handle_rom_operation_error(self, "revert version", e)
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
                handle_rom_operation_error(self, "delete version", e)
            except Exception as e:
                logger.exception(
                    f"Unexpected error deleting version: {type(e).__name__}: {e}"
                )
                QMessageBox.critical(
                    self,
                    "Error",
                    f"Unexpected error deleting version:\n{type(e).__name__}: {e}",
                )
