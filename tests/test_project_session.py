"""
Tests for project-aware session save/restore and recent files.

Validates that:
- _handle_close() saves project tabs as "project:<path>" and standalone ROMs as plain paths
- _restore_session() dispatches project: entries to open_project_path()
- _restore_session() detects ROM files inside project folders (legacy data)
- MainWindow.closeEvent delegates to _handle_close (not shadowed by QWidget)
- open_recent_file() dispatches project: entries and legacy ROM-in-project correctly
"""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

from src.core.project_manager import ProjectManager
from src.ui.session_mixin import SessionMixin
from src.ui.recent_files_mixin import RecentFilesMixin


# ---------------------------------------------------------------------------
# Helpers: lightweight host objects that satisfy mixin dependencies
# ---------------------------------------------------------------------------

def _make_document(rom_path, project_path=None, modified=False):
    """Create a mock RomDocument with the attributes mixins check."""
    doc = MagicMock()
    doc.rom_path = rom_path
    doc.project_path = project_path
    doc.file_name = Path(rom_path).name
    doc.is_modified.return_value = modified
    return doc


class _SessionHost(SessionMixin):
    """Minimal host satisfying SessionMixin dependencies for unit tests."""

    def __init__(self, documents=None, session_files=None, projects_enabled=True):
        self.projects_enabled = projects_enabled
        self.settings = MagicMock()
        self.settings.get_session_files.return_value = session_files or []
        self.tab_widget = MagicMock()
        docs = documents or []
        self.tab_widget.count.return_value = len(docs)
        self.tab_widget.widget.side_effect = lambda i: docs[i]
        self.open_project_path = MagicMock()
        self._open_rom_file = MagicMock()
        self.statusBar = MagicMock()


class _RecentHost(RecentFilesMixin):
    """Minimal host satisfying RecentFilesMixin dependencies for unit tests."""

    def __init__(self, projects_enabled=True):
        self.projects_enabled = projects_enabled
        self.settings = MagicMock()
        self.file_menu = MagicMock()
        self.recent_files_actions = []
        self.recent_files_separator = MagicMock()
        self.open_project_path = MagicMock()
        self._open_rom_file = MagicMock()


# ===========================================================================
# _handle_close  (session save)
# ===========================================================================

class TestHandleClose:
    """Test _handle_close saves the correct session entries."""

    def test_project_tab_saved_with_prefix(self):
        doc = _make_document(
            rom_path=r"C:\proj\v1_LF9VEB_working.bin",
            project_path=r"C:\proj",
        )
        host = _SessionHost(documents=[doc])
        event = MagicMock()

        host._handle_close(event)

        host.settings.set_session_files.assert_called_once_with(
            [r"project:C:\proj"]
        )
        event.accept.assert_called_once()

    def test_standalone_rom_saved_as_plain_path(self):
        doc = _make_document(rom_path=r"C:\roms\stock.bin")
        host = _SessionHost(documents=[doc])
        event = MagicMock()

        host._handle_close(event)

        host.settings.set_session_files.assert_called_once_with(
            [r"C:\roms\stock.bin"]
        )

    def test_mixed_tabs(self):
        proj = _make_document(
            rom_path=r"C:\proj\v1_LF9VEB_working.bin",
            project_path=r"C:\proj",
        )
        rom = _make_document(rom_path=r"C:\roms\stock.bin")
        host = _SessionHost(documents=[proj, rom])
        event = MagicMock()

        host._handle_close(event)

        host.settings.set_session_files.assert_called_once_with(
            [r"project:C:\proj", r"C:\roms\stock.bin"]
        )

    def test_no_tabs_saves_empty_list(self):
        host = _SessionHost(documents=[])
        event = MagicMock()

        host._handle_close(event)

        host.settings.set_session_files.assert_called_once_with([])

    def test_document_without_project_path_attr(self):
        """Legacy document objects that lack project_path should save rom_path."""
        doc = MagicMock(spec=["rom_path", "is_modified", "file_name"])
        doc.rom_path = r"C:\old\legacy.bin"
        doc.is_modified.return_value = False
        host = _SessionHost(documents=[doc])
        event = MagicMock()

        host._handle_close(event)

        host.settings.set_session_files.assert_called_once_with(
            [r"C:\old\legacy.bin"]
        )


# ===========================================================================
# _restore_session
# ===========================================================================

class TestRestoreSession:
    """Test _restore_session dispatches entries to the correct opener."""

    def test_project_entry_calls_open_project_path(self, tmp_path):
        proj_dir = tmp_path / "myproj"
        proj_dir.mkdir()
        (proj_dir / "project.json").write_text("{}")

        host = _SessionHost(session_files=[f"project:{proj_dir}"])
        host._restore_session()

        host.open_project_path.assert_called_once_with(str(proj_dir))
        host._open_rom_file.assert_not_called()

    def test_standalone_rom_calls_open_rom_file(self, tmp_path):
        rom_dir = tmp_path / "roms"
        rom_dir.mkdir()
        rom_file = rom_dir / "stock.bin"
        rom_file.write_bytes(b"\x00" * 10)

        host = _SessionHost(session_files=[str(rom_file)])
        host._restore_session()

        host._open_rom_file.assert_called_once_with(str(rom_file))
        host.open_project_path.assert_not_called()

    def test_legacy_rom_in_project_opens_as_project(self, tmp_path):
        """ROM file inside a project folder should be opened as a project."""
        proj_dir = tmp_path / "myproj"
        proj_dir.mkdir()
        (proj_dir / "project.json").write_text("{}")
        rom_file = proj_dir / "modified.bin"
        rom_file.write_bytes(b"\x00" * 10)

        host = _SessionHost(session_files=[str(rom_file)])
        host._restore_session()

        host.open_project_path.assert_called_once_with(str(proj_dir))
        host._open_rom_file.assert_not_called()

    def test_missing_project_folder_skipped(self, tmp_path):
        missing = tmp_path / "gone"
        host = _SessionHost(session_files=[f"project:{missing}"])
        host._restore_session()

        host.open_project_path.assert_not_called()
        host._open_rom_file.assert_not_called()

    def test_missing_rom_file_skipped(self, tmp_path):
        missing = tmp_path / "gone.bin"
        host = _SessionHost(session_files=[str(missing)])
        host._restore_session()

        host.open_project_path.assert_not_called()
        host._open_rom_file.assert_not_called()

    def test_empty_session_does_nothing(self):
        host = _SessionHost(session_files=[])
        host._restore_session()

        host.open_project_path.assert_not_called()
        host._open_rom_file.assert_not_called()

    def test_mixed_session(self, tmp_path):
        """Multiple entries: project, standalone ROM, legacy ROM-in-project."""
        proj_dir = tmp_path / "proj"
        proj_dir.mkdir()
        (proj_dir / "project.json").write_text("{}")
        legacy_rom = proj_dir / "v1_LF9VEB_working.bin"
        legacy_rom.write_bytes(b"\x00" * 10)

        rom_dir = tmp_path / "roms"
        rom_dir.mkdir()
        standalone = rom_dir / "stock.bin"
        standalone.write_bytes(b"\x00" * 10)

        host = _SessionHost(session_files=[
            f"project:{proj_dir}",
            str(standalone),
            str(legacy_rom),
        ])
        host._restore_session()

        assert host.open_project_path.call_count == 2
        host.open_project_path.assert_any_call(str(proj_dir))
        host._open_rom_file.assert_called_once_with(str(standalone))

    def test_error_in_one_entry_does_not_block_others(self, tmp_path):
        """An exception restoring one entry should not prevent restoring the rest."""
        rom_dir = tmp_path / "roms"
        rom_dir.mkdir()
        good_rom = rom_dir / "good.bin"
        good_rom.write_bytes(b"\x00" * 10)

        proj_dir = tmp_path / "badproj"
        proj_dir.mkdir()
        (proj_dir / "project.json").write_text("{}")

        host = _SessionHost(session_files=[
            f"project:{proj_dir}",
            str(good_rom),
        ])
        # First call raises, second should still proceed
        host.open_project_path.side_effect = [Exception("boom"), None]

        host._restore_session()

        host._open_rom_file.assert_called_once_with(str(good_rom))


# ===========================================================================
# MainWindow.closeEvent  (MRO verification)
# ===========================================================================

class TestCloseEventMRO:
    """Verify MainWindow.closeEvent delegates to _handle_close, not QWidget's."""

    def test_close_event_defined_on_main_window(self):
        """closeEvent must be defined directly on MainWindow to override QWidget."""
        from main import MainWindow
        assert "closeEvent" in MainWindow.__dict__, (
            "MainWindow must define closeEvent directly — "
            "mixin methods are shadowed by QWidget's C++ slot"
        )

    def test_close_event_not_from_qwidget(self):
        """The first closeEvent in MRO should be MainWindow's, not QWidget's."""
        from main import MainWindow
        for cls in MainWindow.__mro__:
            if "closeEvent" in cls.__dict__:
                assert cls.__name__ == "MainWindow", (
                    f"closeEvent resolved to {cls.__name__}, expected MainWindow"
                )
                break


# ===========================================================================
# open_recent_file  (recent files dispatch)
# ===========================================================================

class TestOpenRecentFile:
    """Test open_recent_file dispatches project vs ROM entries."""

    def test_project_entry_opens_project(self, tmp_path):
        proj_dir = tmp_path / "myproj"
        proj_dir.mkdir()
        (proj_dir / "project.json").write_text("{}")

        host = _RecentHost()
        host.open_recent_file(f"project:{proj_dir}")

        host.open_project_path.assert_called_once_with(str(proj_dir))
        host._open_rom_file.assert_not_called()

    def test_standalone_rom_opens_rom(self, tmp_path):
        rom_dir = tmp_path / "roms"
        rom_dir.mkdir()
        rom_file = rom_dir / "stock.bin"
        rom_file.write_bytes(b"\x00" * 10)

        host = _RecentHost()
        host.open_recent_file(str(rom_file))

        host._open_rom_file.assert_called_once_with(str(rom_file))
        host.open_project_path.assert_not_called()

    def test_legacy_rom_in_project_opens_project(self, tmp_path):
        """Legacy recent entry: ROM inside project folder → open as project."""
        proj_dir = tmp_path / "myproj"
        proj_dir.mkdir()
        (proj_dir / "project.json").write_text("{}")
        rom_file = proj_dir / "modified.bin"
        rom_file.write_bytes(b"\x00" * 10)

        host = _RecentHost()
        host.open_recent_file(str(rom_file))

        host.open_project_path.assert_called_once_with(str(proj_dir))
        host._open_rom_file.assert_not_called()


# ===========================================================================
# is_project_folder detection
# ===========================================================================

class TestProjectFolderDetection:
    """Test ProjectManager.is_project_folder on various paths."""

    def test_folder_with_project_json(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "project.json").write_text("{}")
        assert ProjectManager.is_project_folder(str(proj))

    def test_folder_without_project_json(self, tmp_path):
        folder = tmp_path / "plain"
        folder.mkdir()
        assert not ProjectManager.is_project_folder(str(folder))

    def test_nonexistent_folder(self, tmp_path):
        assert not ProjectManager.is_project_folder(str(tmp_path / "nope"))
