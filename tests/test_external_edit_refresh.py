"""Regression tests — external (compare-copy / MCP) edits refresh an open window.

`_apply_external_cell_edits` always refreshed an already-open table viewer
window after writing the ROM, but `_apply_external_axis_edits` did not — a
compare-copy whose axes differed left an open destination window showing stale
axis values (ROM bytes, undo, and borders were already correct). The axis path
now mirrors the cell path's viewer refresh.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from PySide6.QtWidgets import QApplication

from main import MainWindow

_app = QApplication.instance() or QApplication([])


def _table():
    return SimpleNamespace(name="Fuel", address="0x1000")


def _external_edit_fake(window):
    return SimpleNamespace(
        table_undo_manager=MagicMock(),
        change_tracker=MagicMock(),
        _write_to_rom_and_mark_modified=MagicMock(return_value=True),
        _find_table_window=lambda key: window,
    )


def test_external_axis_edits_refresh_open_window():
    window = SimpleNamespace(viewer=MagicMock())
    fake = _external_edit_fake(window)
    document = MagicMock()

    changes = [("x_axis", 0, 1.0, 2.0, 1, 2), ("y_axis", 3, 5.0, 6.0, 5, 6)]
    MainWindow._apply_external_axis_edits(
        fake, document, _table(), changes, "Compare Copy Axis", rom_path="rom"
    )

    # The open window's axis cells are updated with the new display values,
    # inside a bulk update (single repaint).
    window.viewer.begin_bulk_update.assert_called_once()
    window.viewer.end_bulk_update.assert_called_once()
    assert window.viewer.update_axis_cell_value.call_args_list == [
        (("x_axis", 0, 2.0),),
        (("y_axis", 3, 6.0),),
    ]
    # Borders still marked on the document's edit state (single owner).
    assert document.edit_state.mark_axis_modified.call_count == 2


def test_external_axis_edits_with_no_open_window_is_safe():
    fake = _external_edit_fake(window=None)
    document = MagicMock()
    MainWindow._apply_external_axis_edits(
        fake,
        document,
        _table(),
        [("x_axis", 0, 1.0, 2.0, 1, 2)],
        "Compare Copy Axis",
        rom_path="rom",
    )
    document.edit_state.mark_axis_modified.assert_called_once()


def test_external_cell_edits_still_refresh_open_window():
    window = SimpleNamespace(viewer=MagicMock())
    fake = _external_edit_fake(window)
    document = MagicMock()

    changes = [(0, 1, 1.0, 2.0, 1, 2)]
    MainWindow._apply_external_cell_edits(
        fake, document, _table(), changes, "Compare Copy", rom_path="rom"
    )

    window.viewer.begin_bulk_update.assert_called_once()
    window.viewer.end_bulk_update.assert_called_once()
    window.viewer.update_cell_value.assert_called_once_with(0, 1, 2.0)
