"""
Recent Files Mixin for MainWindow

Handles recent files menu management: updating the menu, opening recent files,
and clearing the recent files list.

This is a mixin class — it has no __init__ and relies on MainWindow providing:
- self.settings (Settings instance)
- self.file_menu (QMenu)
- self.recent_files_actions (list)
- self.recent_files_separator (QAction separator)
- self._open_rom_file(path) method
- self.open_project_path(path) method
"""

from pathlib import Path

from PySide6.QtWidgets import QMessageBox

from src.utils.logging_config import get_logger
from src.core.project_manager import ProjectManager

logger = get_logger(__name__)


class RecentFilesMixin:
    """Mixin providing recent files menu functionality for MainWindow."""

    def update_recent_files_menu(self):
        """Update the recent files menu with current list"""
        # Remove and delete existing recent file actions to prevent QAction/lambda leaks
        for action in self.recent_files_actions:
            self.file_menu.removeAction(action)
            action.deleteLater()
        self.recent_files_actions.clear()

        # Get recent files from settings
        recent_files = self.settings.get_recent_files()

        if recent_files:
            # Add each recent file
            for i, entry in enumerate(recent_files, 1):
                # Detect project entries (project:<path>) vs standalone ROM paths
                if entry.startswith("project:"):
                    project_path = entry[len("project:") :]
                    folder_name = Path(project_path).name
                    action_text = f"{i}. [P] {folder_name}"
                    status_text = project_path
                elif ProjectManager.is_project_folder(str(Path(entry).parent)):
                    # Legacy entry: ROM file inside a project folder
                    folder_name = Path(entry).parent.name
                    action_text = f"{i}. [P] {folder_name}"
                    status_text = str(Path(entry).parent)
                else:
                    action_text = f"{i}. {Path(entry).name}"
                    status_text = entry

                action = self.file_menu.addAction(action_text)
                action.setData(
                    entry
                )  # Store full entry (with project: prefix if applicable)
                action.setStatusTip(status_text)  # Show full path in status bar
                action.triggered.connect(
                    lambda checked=False, path=entry: self.open_recent_file(path)
                )

                # Insert before the separator
                self.file_menu.insertAction(self.recent_files_separator, action)
                self.recent_files_actions.append(action)

            # Add "Clear Recent Files" option
            clear_action = self.file_menu.addAction("Clear Recent Files")
            clear_action.triggered.connect(self.clear_recent_files)
            self.file_menu.insertAction(self.recent_files_separator, clear_action)
            self.recent_files_actions.append(clear_action)

    def open_recent_file(self, entry: str):
        """
        Open a ROM file or project from recent files list

        Args:
            entry: Full path to ROM file, or "project:<path>" for projects
        """
        if entry.startswith("project:"):
            project_path = entry[len("project:") :]
            if not Path(project_path).exists():
                QMessageBox.warning(
                    self,
                    "Project Not Found",
                    f"The project folder no longer exists:\n{project_path}\n\n"
                    "It will be removed from recent files.",
                )
                self._remove_recent_entry(entry)
                return
            self.open_project_path(project_path)
        else:
            path = Path(entry)
            if not path.exists():
                QMessageBox.warning(
                    self,
                    "File Not Found",
                    f"The file no longer exists:\n{entry}\n\n"
                    "It will be removed from recent files.",
                )
                self._remove_recent_entry(entry)
                return
            # Check if ROM lives inside a project folder (legacy entry)
            if ProjectManager.is_project_folder(str(path.parent)):
                self.open_project_path(str(path.parent))
            else:
                self._open_rom_file(entry)

    def _remove_recent_entry(self, entry: str):
        """Remove an entry from the recent files list and refresh the menu."""
        recent = self.settings.get_recent_files()
        if entry in recent:
            recent.remove(entry)
            self.settings.settings.setValue("recent_files", recent)
            self.settings.settings.sync()
            self.update_recent_files_menu()

    def clear_recent_files(self):
        """Clear the recent files list"""
        self.settings.clear_recent_files()
        self.update_recent_files_menu()
        logger.info("Recent files list cleared")
