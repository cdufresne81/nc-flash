"""
Session Mixin for MainWindow

Handles session management: restoring previous session state, saving session
on close, checking for unsaved changes, and settings/about dialogs.

This is a mixin class — it has no __init__ and relies on MainWindow providing:
- self.settings (Settings instance)
- self.rom_detector (RomDetector instance, may be None)
- self.tab_widget (QTabWidget)
- self._open_rom_file(path) method
- self.open_project_path(path) method
- self.statusBar() method
"""

from pathlib import Path

from PySide6.QtWidgets import QMessageBox

from src.utils.logging_config import get_logger
from src.utils.constants import APP_NAME, APP_VERSION_STRING, APP_DESCRIPTION
from src.core.rom_detector import RomDetector
from src.core.project_manager import ProjectManager
from src.core.exceptions import DetectionError, RomEditorError
from src.ui.settings_dialog import SettingsDialog

logger = get_logger(__name__)


class SessionMixin:
    """Mixin providing session management functionality for MainWindow."""

    def _restore_session(self):
        """Restore files from previous session"""
        session_files = self.settings.get_session_files()

        if not session_files:
            return

        logger.info(f"Restoring session: {len(session_files)} file(s)")

        for entry in session_files:
            try:
                if entry.startswith("project:"):
                    # Explicit project tab
                    project_path = entry[len("project:") :]
                    if Path(project_path).exists():
                        self.open_project_path(project_path)
                    else:
                        logger.warning(
                            f"Session project folder no longer exists: {project_path}"
                        )
                else:
                    path = Path(entry)
                    if not path.exists():
                        logger.warning(f"Session file no longer exists: {entry}")
                        continue
                    # Check if this ROM lives inside a project folder (legacy session data)
                    parent = path.parent
                    if ProjectManager.is_project_folder(str(parent)):
                        logger.info(
                            f"Session ROM is inside project folder, restoring as project: {parent}"
                        )
                        self.open_project_path(str(parent))
                    else:
                        self._open_rom_file(entry)
            except RomEditorError as e:
                logger.warning(f"Failed to restore session entry: {entry} - {e}")
            except Exception as e:
                logger.exception(
                    f"Unexpected error restoring session entry: {entry} - {type(e).__name__}: {e}"
                )

    def _handle_close(self, event):
        """Check for unsaved changes across all tabs, then save session state before closing.

        NOTE: This is NOT called closeEvent because QWidget.closeEvent (C++ slot)
        shadows mixin methods in the MRO. MainWindow must define its own closeEvent
        that delegates here.
        """
        # Check each open tab for unsaved changes
        for i in range(self.tab_widget.count()):
            document = self.tab_widget.widget(i)
            if document and document.is_modified():
                response = QMessageBox.question(
                    self,
                    "Unsaved Changes",
                    f"'{document.file_name}' has unsaved changes.\n\nDo you want to save before closing?",
                    QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                    QMessageBox.Save,
                )

                if response == QMessageBox.Cancel:
                    event.ignore()
                    return
                elif response == QMessageBox.Save:
                    document.save()

        # Collect paths of all open ROM documents
        # For project tabs, save as "project:<path>" so restore reopens the project
        open_files = []
        for i in range(self.tab_widget.count()):
            document = self.tab_widget.widget(i)
            if document and hasattr(document, "rom_path"):
                project_path = getattr(document, "project_path", None)
                if project_path:
                    open_files.append(f"project:{project_path}")
                else:
                    open_files.append(document.rom_path)

        # Save to settings
        self.settings.set_session_files(open_files)
        logger.info(f"Session saved: {len(open_files)} file(s)")

        # Delete workspace state file (no ROMs are "open" anymore)
        self._delete_workspace_state()

        # Stop MCP server if running
        self._stop_mcp_server()

        # Accept close event
        event.accept()

    def show_settings(self):
        """Show settings dialog"""
        dialog = SettingsDialog(self)
        dialog.settings_changed.connect(self.on_settings_changed)
        dialog.exec()

    def on_settings_changed(self):
        """Handle settings changes"""
        # Reinitialize ROM detector with new metadata path
        try:
            metadata_dir = self.settings.get_metadata_directory()
            self.rom_detector = RomDetector(metadata_dir)
            logger.info(
                f"ROM detector reinitialized with metadata directory: {metadata_dir}"
            )
            self.statusBar().showMessage(
                f"Settings updated. Metadata directory: {metadata_dir}"
            )
        except DetectionError as e:
            logger.error(f"Failed to reinitialize ROM detector: {e}")
            QMessageBox.warning(
                self,
                "Settings Error",
                f"Failed to load metadata from new directory:\n{str(e)}\n\n"
                "Please check the metadata directory path in settings.",
            )

    def show_about(self):
        """Show about dialog"""
        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            f"{APP_NAME} {APP_VERSION_STRING}\n\n"
            f"{APP_DESCRIPTION}\n\n"
            "Designed to replace EcuFlash for ROM editing tasks.\n"
            "Works with RomDrop for ECU flashing.",
        )
