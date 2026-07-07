"""Regression tests for B3 — a commit must be scoped to the project's ROM.

The ChangeTracker is global across every open ROM. Committing used to snapshot
the ACTIVE tab and fold ALL open ROMs' pending edits into the project, so a
second ROM open for Compare would pollute the project history (or the project's
own edits could be omitted from the snapshot). The fix scopes both the save and
the pending set to the project ROM only.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PySide6.QtWidgets import QApplication, QDialog

from src.ui.project_mixin import ProjectMixin
from src.core.change_tracker import ChangeTracker

_app = QApplication.instance() or QApplication([])

PROJECT_ROM = r"C:\proj\working.bin"
FOREIGN_ROM = r"C:\roms\other.bin"


def _table(name, address):
    return SimpleNamespace(name=name, address=address)


def _seed_two_roms():
    """Tracker with pending edits on the project ROM and a foreign ROM."""
    tracker = ChangeTracker()
    tracker.record_pending_change(
        _table("Fuel", "0x1000"), 0, 0, 1.0, 2.0, 1, 2, rom_path=PROJECT_ROM
    )
    tracker.record_pending_change(
        _table("Boost", "0x2000"), 1, 1, 3.0, 4.0, 3, 4, rom_path=FOREIGN_ROM
    )
    return tracker


# --- ChangeTracker per-ROM accessors (the B3 mechanism) --------------------


def test_get_pending_changes_for_rom_isolates_by_rom():
    tracker = _seed_two_roms()
    project = tracker.get_pending_changes_for_rom(PROJECT_ROM)
    assert [tc.table_name for tc in project] == ["Fuel"]
    foreign = tracker.get_pending_changes_for_rom(FOREIGN_ROM)
    assert [tc.table_name for tc in foreign] == ["Boost"]


def test_clear_pending_for_rom_leaves_other_rom_intact():
    tracker = _seed_two_roms()
    tracker.clear_pending_for_rom(PROJECT_ROM)
    assert tracker.get_pending_changes_for_rom(PROJECT_ROM) == []
    assert [
        tc.table_name for tc in tracker.get_pending_changes_for_rom(FOREIGN_ROM)
    ] == ["Boost"]
    assert tracker.has_pending_changes() is True  # foreign edits survive


def test_per_rom_filter_normalizes_path_separators():
    tracker = _seed_two_roms()
    # Forward-slash form of the same path (QFileDialog vs Path) must still match.
    assert tracker.get_pending_changes_for_rom("C:/proj/working.bin")


# --- commit_changes end-to-end (B3) ----------------------------------------


def test_commit_only_touches_project_rom():
    tracker = _seed_two_roms()

    pm = MagicMock()
    pm.is_project_open.return_value = True
    pm.current_project.working_rom_path = PROJECT_ROM
    pm.current_project.original_rom.rom_id = "LF9VEB"
    pm.current_project.name = "MyProj"
    pm.get_next_version.return_value = 1
    pm.commit_changes.return_value = SimpleNamespace(version=1)

    project_doc = MagicMock()
    project_doc.rom_reader.rom_path = Path(PROJECT_ROM)

    fake = SimpleNamespace(
        project_manager=pm,
        change_tracker=tracker,
        _find_document_by_rom_path=lambda p: project_doc,
        _reset_document_edit_baseline=lambda doc: None,
        _update_project_ui=lambda: None,
        statusBar=lambda: MagicMock(),
        # Deliberately NO get_current_document: the fix must not consult the
        # active tab. If it did, this test would AttributeError.
    )

    with patch("src.ui.project_mixin.CommitDialog") as MockDialog:
        dlg = MockDialog.return_value
        dlg.exec.return_value = QDialog.Accepted
        dlg.get_commit_message.return_value = "tune fuel"
        dlg.get_version_name.return_value = "v1"
        ProjectMixin.commit_changes(fake)

    # Only the project ROM's Fuel change was committed — not the foreign Boost.
    committed = pm.commit_changes.call_args.kwargs["changes"]
    assert [tc.table_name for tc in committed] == ["Fuel"]

    # The PROJECT ROM's working file was flushed and its dirty flag cleared.
    project_doc.rom_reader.save_rom.assert_called_once()
    project_doc.set_modified.assert_called_once_with(False)

    # The foreign ROM keeps its uncommitted edits; the project ROM is cleared.
    assert tracker.get_pending_changes_for_rom(FOREIGN_ROM)
    assert tracker.get_pending_changes_for_rom(PROJECT_ROM) == []
