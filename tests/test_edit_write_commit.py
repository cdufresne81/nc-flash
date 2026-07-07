"""Regression tests for B8 — the ROM write is the commit point.

Interactive cell/axis edits used to record undo + pending BEFORE writing the
ROM, and the write only logged on failure. A rejected write then left the UI,
undo stack, and pending set asserting a value the ROM never took (later shipped
by Save/flash). The handlers now write first and only advance state on success;
on failure they revert the viewer cell and surface the error.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PySide6.QtWidgets import QApplication, QMessageBox

from main import MainWindow
from src.core.exceptions import RomWriteError

_app = QApplication.instance() or QApplication([])


def _table():
    return SimpleNamespace(name="Fuel", address="0x1000")


def _cell_handler_fake(write_ok):
    return SimpleNamespace(
        # Edit signals now carry rom_path; the handler resolves the document
        # from it (no sender-walk). A MagicMock document stands in.
        _resolve_edit_target=lambda rom_path: MagicMock(),
        _write_to_rom_and_mark_modified=MagicMock(return_value=write_ok),
        table_undo_manager=MagicMock(),
        change_tracker=MagicMock(),
        _revert_failed_cell_edit=MagicMock(),
        _revert_failed_axis_edit=MagicMock(),
    )


# --- handler branching ------------------------------------------------------


def test_cell_edit_records_state_only_when_write_succeeds():
    fake = _cell_handler_fake(write_ok=True)
    MainWindow._on_table_cell_changed(fake, "rom", _table(), 0, 0, 1.0, 2.0, 1, 2)
    fake.table_undo_manager.record_cell_change.assert_called_once()
    fake.change_tracker.record_pending_change.assert_called_once()
    fake._revert_failed_cell_edit.assert_not_called()


def test_cell_edit_reverts_and_skips_state_when_write_fails():
    fake = _cell_handler_fake(write_ok=False)
    MainWindow._on_table_cell_changed(fake, "rom", _table(), 0, 0, 1.0, 2.0, 1, 2)
    fake.table_undo_manager.record_cell_change.assert_not_called()
    fake.change_tracker.record_pending_change.assert_not_called()
    fake._revert_failed_cell_edit.assert_called_once()


def test_axis_edit_records_state_only_when_write_succeeds():
    fake = _cell_handler_fake(write_ok=True)
    MainWindow._on_table_axis_changed(fake, "rom", _table(), "x", 0, 1.0, 2.0, 1, 2)
    fake.table_undo_manager.record_axis_change.assert_called_once()
    fake.change_tracker.record_pending_axis_change.assert_called_once()
    fake._revert_failed_axis_edit.assert_not_called()


def test_axis_edit_reverts_and_skips_state_when_write_fails():
    fake = _cell_handler_fake(write_ok=False)
    MainWindow._on_table_axis_changed(fake, "rom", _table(), "x", 0, 1.0, 2.0, 1, 2)
    fake.table_undo_manager.record_axis_change.assert_not_called()
    fake.change_tracker.record_pending_axis_change.assert_not_called()
    fake._revert_failed_axis_edit.assert_called_once()


# --- the write helper's success signal --------------------------------------


def test_write_helper_returns_false_on_rom_write_error():
    doc = MagicMock()
    doc.is_modified.return_value = False

    def boom():
        raise RomWriteError("out of bounds")

    assert (
        MainWindow._write_to_rom_and_mark_modified(object(), doc, boom, "cell") is False
    )
    doc.set_modified.assert_not_called()


def test_write_helper_returns_true_and_marks_modified_on_success():
    doc = MagicMock()
    doc.is_modified.return_value = False
    ok = MainWindow._write_to_rom_and_mark_modified(object(), doc, lambda: None, "cell")
    assert ok is True
    doc.set_modified.assert_called_once_with(True)


def test_write_helper_treats_missing_document_as_noop_success():
    assert (
        MainWindow._write_to_rom_and_mark_modified(object(), None, lambda: None, "x")
        is True
    )


# --- revert helper ----------------------------------------------------------


def test_revert_failed_cell_edit_restores_viewer_and_warns():
    window = SimpleNamespace(viewer=MagicMock())
    fake = SimpleNamespace(_find_table_window=lambda key: window)
    with patch.object(QMessageBox, "critical") as crit:
        MainWindow._revert_failed_cell_edit(fake, "rom", _table(), 3, 4, 9.5)
    window.viewer.update_cell_value.assert_called_once_with(3, 4, 9.5)
    crit.assert_called_once()


# --- bulk handlers (interactive) ---------------------------------------------

CELL_CHANGES = [(0, 0, 1.0, 2.0, 1, 2), (0, 1, 3.0, 4.0, 3, 4)]
AXIS_CHANGES = [("x_axis", 0, 1.0, 2.0, 1, 2), ("y_axis", 1, 3.0, 4.0, 3, 4)]


def _bulk_handler_fake(write_ok):
    return SimpleNamespace(
        _resolve_edit_target=lambda rom_path: MagicMock(),
        _write_to_rom_and_mark_modified=MagicMock(return_value=write_ok),
        table_undo_manager=MagicMock(),
        change_tracker=MagicMock(),
        _revert_failed_bulk_cells=MagicMock(),
        _revert_failed_bulk_axes=MagicMock(),
    )


def test_bulk_edit_records_state_only_when_write_succeeds():
    fake = _bulk_handler_fake(write_ok=True)
    MainWindow._on_table_bulk_changes(fake, "rom", _table(), CELL_CHANGES, "Multiply")
    fake.table_undo_manager.record_bulk_cell_changes.assert_called_once()
    fake.change_tracker.record_pending_bulk_changes.assert_called_once()
    fake._revert_failed_bulk_cells.assert_not_called()


def test_bulk_edit_reverts_and_skips_state_when_write_fails():
    fake = _bulk_handler_fake(write_ok=False)
    MainWindow._on_table_bulk_changes(fake, "rom", _table(), CELL_CHANGES, "Multiply")
    fake.table_undo_manager.record_bulk_cell_changes.assert_not_called()
    fake.change_tracker.record_pending_bulk_changes.assert_not_called()
    fake._revert_failed_bulk_cells.assert_called_once()


def test_axis_bulk_edit_records_state_only_when_write_succeeds():
    fake = _bulk_handler_fake(write_ok=True)
    MainWindow._on_table_axis_bulk_changes(
        fake, "rom", _table(), AXIS_CHANGES, "Interpolate"
    )
    fake.table_undo_manager.record_axis_bulk_changes.assert_called_once()
    fake.change_tracker.record_pending_axis_bulk_changes.assert_called_once()
    fake._revert_failed_bulk_axes.assert_not_called()


def test_axis_bulk_edit_reverts_and_skips_state_when_write_fails():
    fake = _bulk_handler_fake(write_ok=False)
    MainWindow._on_table_axis_bulk_changes(
        fake, "rom", _table(), AXIS_CHANGES, "Interpolate"
    )
    fake.table_undo_manager.record_axis_bulk_changes.assert_not_called()
    fake.change_tracker.record_pending_axis_bulk_changes.assert_not_called()
    fake._revert_failed_bulk_axes.assert_called_once()


# --- external-edit pipeline (compare-copy / MCP) ------------------------------


def _external_fake(write_ok, window=None):
    return SimpleNamespace(
        table_undo_manager=MagicMock(),
        change_tracker=MagicMock(),
        _write_to_rom_and_mark_modified=MagicMock(return_value=write_ok),
        _rollback_failed_cell_writes=MagicMock(),
        _rollback_failed_axis_writes=MagicMock(),
        _find_table_window=lambda key: window,
    )


def test_external_cell_edits_return_true_and_record_on_success():
    fake = _external_fake(write_ok=True)
    document = MagicMock()
    ok = MainWindow._apply_external_cell_edits(
        fake, document, _table(), CELL_CHANGES, "AI edit", rom_path="rom"
    )
    assert ok is True
    fake.table_undo_manager.record_bulk_cell_changes.assert_called_once()
    fake.change_tracker.record_pending_bulk_changes.assert_called_once()
    document.edit_state.mark_cells_modified.assert_called_once()


def test_external_cell_edits_roll_back_and_record_nothing_on_failure():
    fake = _external_fake(write_ok=False)
    document = MagicMock()
    ok = MainWindow._apply_external_cell_edits(
        fake, document, _table(), CELL_CHANGES, "AI edit", rom_path="rom"
    )
    assert ok is False
    fake._rollback_failed_cell_writes.assert_called_once()
    fake.table_undo_manager.record_bulk_cell_changes.assert_not_called()
    fake.change_tracker.record_pending_bulk_changes.assert_not_called()
    document.edit_state.mark_cells_modified.assert_not_called()


def test_external_axis_edits_roll_back_and_record_nothing_on_failure():
    window = SimpleNamespace(viewer=MagicMock())
    fake = _external_fake(write_ok=False, window=window)
    document = MagicMock()
    ok = MainWindow._apply_external_axis_edits(
        fake, document, _table(), AXIS_CHANGES, "Copy Axis", rom_path="rom"
    )
    assert ok is False
    fake._rollback_failed_axis_writes.assert_called_once()
    fake.table_undo_manager.record_axis_bulk_changes.assert_not_called()
    document.edit_state.mark_axis_modified.assert_not_called()
    # The viewer never advanced (external edits update it only after commit).
    window.viewer.update_axis_cell_value.assert_not_called()


def test_external_edits_with_no_changes_are_noop_success():
    fake = _external_fake(write_ok=False)  # write never reached
    assert (
        MainWindow._apply_external_cell_edits(fake, MagicMock(), _table(), [], "x")
        is True
    )
    assert (
        MainWindow._apply_external_axis_edits(fake, MagicMock(), _table(), [], "x")
        is True
    )


# --- rollback helpers ---------------------------------------------------------


def test_rollback_failed_cell_writes_restores_old_raw_and_survives_errors():
    document = MagicMock()
    document.rom_reader.write_cell_value.side_effect = [
        None,
        RomWriteError("out of bounds"),
    ]
    MainWindow._rollback_failed_cell_writes(object(), document, _table(), CELL_CHANGES)
    calls = document.rom_reader.write_cell_value.call_args_list
    assert len(calls) == 2  # second failure swallowed, loop completed
    # Old RAW values written back, not the new ones.
    assert calls[0].args[1:] == (0, 0, 1)
    assert calls[1].args[1:] == (0, 1, 3)


def test_revert_failed_bulk_cells_rolls_back_reverts_viewer_and_warns():
    window = SimpleNamespace(viewer=MagicMock())
    fake = SimpleNamespace(
        _rollback_failed_cell_writes=MagicMock(),
        _find_table_window=lambda key: window,
    )
    with patch.object(QMessageBox, "critical") as crit:
        MainWindow._revert_failed_bulk_cells(
            fake, MagicMock(), "rom", _table(), CELL_CHANGES
        )
    fake._rollback_failed_cell_writes.assert_called_once()
    # Viewer restored to the old DISPLAY values inside a bulk update.
    window.viewer.begin_bulk_update.assert_called_once()
    window.viewer.end_bulk_update.assert_called_once()
    assert window.viewer.update_cell_value.call_args_list == [
        ((0, 0, 1.0),),
        ((0, 1, 3.0),),
    ]
    crit.assert_called_once()
