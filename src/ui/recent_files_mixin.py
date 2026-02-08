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
"""

from pathlib import Path

from PySide6.QtWidgets import QMessageBox

from src.utils.logging_config import get_logger

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
            for i, file_path in enumerate(recent_files, 1):
                # Show just the filename, but store full path
                file_name = Path(file_path).name
                action_text = f"{i}. {file_name}"

                action = self.file_menu.addAction(action_text)
                action.setData(file_path)  # Store full path in action data
                action.setStatusTip(file_path)  # Show full path in status bar
                action.triggered.connect(lambda checked=False, path=file_path: self.open_recent_file(path))

                # Insert before the separator
                self.file_menu.insertAction(self.recent_files_separator, action)
                self.recent_files_actions.append(action)

            # Add "Clear Recent Files" option
            clear_action = self.file_menu.addAction("Clear Recent Files")
            clear_action.triggered.connect(self.clear_recent_files)
            self.file_menu.insertAction(self.recent_files_separator, clear_action)
            self.recent_files_actions.append(clear_action)

    def open_recent_file(self, file_path: str):
        """
        Open a ROM file from recent files list

        Args:
            file_path: Full path to ROM file
        """
        if not Path(file_path).exists():
            QMessageBox.warning(
                self,
                "File Not Found",
                f"The file no longer exists:\n{file_path}\n\n"
                "It will be removed from recent files."
            )
            # Remove from recent files
            recent = self.settings.get_recent_files()
            if file_path in recent:
                recent.remove(file_path)
                self.settings.settings.setValue("recent_files", recent)
                self.settings.settings.sync()
                self.update_recent_files_menu()
            return

        # Open the file (reuse existing logic by calling the internal open method)
        self._open_rom_file(file_path)

    def clear_recent_files(self):
        """Clear the recent files list"""
        self.settings.clear_recent_files()
        self.update_recent_files_menu()
        logger.info("Recent files list cleared")
