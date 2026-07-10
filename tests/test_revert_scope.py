"""Regression tests — reverting a project version must target the PROJECT's ROM.

`_on_revert_version` used to resolve the document via get_current_document():
with a foreign tab active (e.g. a second ROM open for Compare), the revert
reloaded the WRONG document from disk (discarding its unsaved in-memory edits)
while the project document kept stale pre-revert bytes that a later save would
silently write back — undoing the revert. It also cleared pending changes
globally and left undo stacks recorded against pre-revert bytes, which would
write pre-revert values into the reverted ROM if replayed. The fix mirrors the
B3 commit scoping: resolve by working_rom_path, close the ROM's open table
windows, drop its undo stacks, and clear only its pending changes.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PySide6.QtWidgets import QApplication, QMessageBox

from src.ui.project_mixin import ProjectMixin
from src.core.change_tracker import ChangeTracker
from src.core.table_undo_manager import make_table_key

_app = QApplication.instance() or QApplication([])

PROJECT_ROM = r"C:\proj\working.bin"
FOREIGN_ROM = r"C:\roms\other.bin"


def _table(name, address):
    return SimpleNamespace(name=name, address=address)


def _fake_window(rom_path):
    return SimpleNamespace(rom_path=Path(rom_path), close=MagicMock())


def _revert_fixture():
    tracker = ChangeTracker()
    tracker.record_pending_change(
        _table("Fuel", "0x1000"), 0, 0, 1.0, 2.0, 1, 2, rom_path=PROJECT_ROM
    )
    tracker.record_pending_change(
        _table("Boost", "0x2000"), 1, 1, 3.0, 4.0, 3, 4, rom_path=FOREIGN_ROM
    )

    pm = MagicMock()
    pm.get_commit_by_version.return_value = SimpleNamespace(
        version=1, snapshot_filename="v1.bin"
    )
    pm.revert_to_version.return_value = "v1.bin"
    pm.current_project.working_rom_path = PROJECT_ROM

    project_doc = MagicMock()
    project_doc.rom_reader.rom_path = Path(PROJECT_ROM)
    project_doc.rom_reader.definition = SimpleNamespace(
        tables=[_table("Fuel", "0x1000")]
    )

    project_window = _fake_window(PROJECT_ROM)
    foreign_window = _fake_window(FOREIGN_ROM)

    baseline_calls = []
    fake = SimpleNamespace(
        project_manager=pm,
        change_tracker=tracker,
        table_undo_manager=MagicMock(),
        open_table_windows=[project_window, foreign_window],
        _find_document_by_rom_path=lambda p: (
            project_doc if Path(p) == Path(PROJECT_ROM) else None
        ),
        _reset_document_edit_baseline=baseline_calls.append,
        _update_project_ui=lambda: None,
        statusBar=lambda: MagicMock(),
        # Deliberately NO get_current_document: the fix must not consult the
        # active tab. If it did, this test would AttributeError.
    )
    return fake, project_doc, project_window, foreign_window, baseline_calls


def _run_revert(fake):
    with patch(
        "src.ui.project_mixin.QMessageBox.question", return_value=QMessageBox.Yes
    ):
        ProjectMixin._on_revert_version(fake, 1)


def test_revert_reloads_project_document_not_active_tab():
    fake, project_doc, _, _, baseline_calls = _revert_fixture()
    _run_revert(fake)

    fake.project_manager.revert_to_version.assert_called_once_with(1)
    project_doc.rom_reader._load_rom.assert_called_once()
    project_doc.set_modified.assert_called_once_with(False)
    # The reverted bytes are the new baseline (borders cleared, originals
    # re-captured) — same as after a commit.
    assert baseline_calls == [project_doc]


def test_revert_closes_only_project_rom_windows():
    fake, _, project_window, foreign_window, _ = _revert_fixture()
    _run_revert(fake)

    # Open windows for the project ROM show pre-revert values with no reload
    # path — they must close. Foreign ROM windows stay open.
    project_window.close.assert_called_once()
    foreign_window.close.assert_not_called()


def test_revert_clears_only_project_rom_pending_changes():
    fake, _, _, _, _ = _revert_fixture()
    _run_revert(fake)

    tracker = fake.change_tracker
    assert tracker.get_pending_changes_for_rom(PROJECT_ROM) == []
    assert [
        tc.table_name for tc in tracker.get_pending_changes_for_rom(FOREIGN_ROM)
    ] == ["Boost"]


def test_revert_drops_project_rom_undo_stacks():
    fake, _, _, _, _ = _revert_fixture()
    _run_revert(fake)

    expected = {make_table_key(Path(PROJECT_ROM), "0x1000")}
    fake.table_undo_manager.remove_stacks_for_keys.assert_called_once_with(expected)


def test_revert_declined_changes_nothing():
    fake, project_doc, project_window, _, baseline_calls = _revert_fixture()
    with patch(
        "src.ui.project_mixin.QMessageBox.question", return_value=QMessageBox.No
    ):
        ProjectMixin._on_revert_version(fake, 1)

    fake.project_manager.revert_to_version.assert_not_called()
    project_doc.rom_reader._load_rom.assert_not_called()
    project_window.close.assert_not_called()
    assert baseline_calls == []
    assert fake.change_tracker.get_pending_changes_for_rom(PROJECT_ROM)
