"""Select All (Ctrl+A) selects every data cell, top-left included.

It used to select the range, then move the current cell to the top-left
cell with Qt's default selection command. With Ctrl still held from Ctrl+A
that toggled the top-left cell out of the selection, so Set Value skipped
it; with no modifier (Edit menu) it cleared the selection down to that one
cell. The held-Ctrl case can't be faked from a test, but both come from the
same call, and the no-modifier case is checked here.
"""

from PySide6.QtWidgets import QTableWidgetSelectionRange

from src.ui.table_viewer_helpers.operations import selected_cells
from tests.test_bulk_edit_nonfinite import N, _table_3d


def test_select_all_selects_every_data_cell(tmp_path):
    viewer = _table_3d(tmp_path, "uint8").viewer
    tw = viewer.table_widget
    # A stray earlier selection on an axis cell is replaced, not extended.
    tw.setRangeSelected(QTableWidgetSelectionRange(0, 2, 0, 2), True)

    viewer.select_all_data()

    cells = set(selected_cells(tw.selectedRanges()))
    expected = {(r, c) for r in range(1, N + 1) for c in range(1, N + 1)}
    assert cells == expected
    assert (tw.currentRow(), tw.currentColumn()) == (1, 1)
