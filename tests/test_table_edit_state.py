"""Unit tests for TableEditState (Phase 3, finding C1).

The pure per-ROM edit-state store: modified-cell borders (cells + axis) and
capture-once original snapshots. No Qt, no MainWindow — just the storage
contract that RomDocument owns and the TableViewer delegates to.
"""

import numpy as np

from src.core.table_edit_state import TableEditState

# --- cell borders -----------------------------------------------------------


def test_cell_mark_unmark_is():
    st = TableEditState()
    assert not st.is_cell_modified("0x100", 1, 2)
    st.mark_cell_modified("0x100", 1, 2)
    assert st.is_cell_modified("0x100", 1, 2)
    st.unmark_cell("0x100", 1, 2)
    assert not st.is_cell_modified("0x100", 1, 2)


def test_unmark_missing_is_safe():
    st = TableEditState()
    # No KeyError when the table/axis was never marked.
    st.unmark_cell("0x100", 0, 0)
    st.unmark_axis("0x100", "x_axis", 0)


def test_bulk_mark_cells():
    st = TableEditState()
    st.mark_cells_modified("0x100", [(0, 0), (1, 1), (2, 2)])
    assert st.is_cell_modified("0x100", 0, 0)
    assert st.is_cell_modified("0x100", 2, 2)
    assert not st.is_cell_modified("0x100", 0, 1)


# --- axis borders -----------------------------------------------------------


def test_axis_mark_unmark_is():
    st = TableEditState()
    st.mark_axis_modified("0x100", "x_axis", 3)
    assert st.is_axis_modified("0x100", "x_axis", 3)
    st.unmark_axis("0x100", "x_axis", 3)
    assert not st.is_axis_modified("0x100", "x_axis", 3)


def test_axis_key_isolates_x_from_y_and_from_cells():
    st = TableEditState()
    st.mark_axis_modified("0x100", "x_axis", 3)
    # Different axis type is a different key.
    assert not st.is_axis_modified("0x100", "y_axis", 3)
    # A cell at (3, 0) must not collide with x_axis index 3.
    st.mark_cell_modified("0x100", 3, 0)
    assert st.is_axis_modified("0x100", "x_axis", 3)
    assert st.is_cell_modified("0x100", 3, 0)


# --- capture-once originals -------------------------------------------------


def test_capture_once_is_deep_and_immutable():
    st = TableEditState()
    src = {
        "values": np.array([1.0, 2.0, 3.0]),
        "x_axis": np.array([10.0, 20.0]),
        "y_axis": None,
    }
    st.capture_originals("0x100", src)
    orig = st.get_original("0x100")
    assert np.array_equal(orig["values"], [1.0, 2.0, 3.0])
    assert np.array_equal(orig["x_axis"], [10.0, 20.0])
    assert orig["y_axis"] is None

    # Deep copy: mutating the live source does not change the snapshot.
    src["values"][0] = 999.0
    assert st.get_original("0x100")["values"][0] == 1.0

    # Capture-once: a second call with different data is a no-op.
    st.capture_originals("0x100", {"values": np.array([5.0, 5.0, 5.0])})
    assert st.get_original("0x100")["values"][0] == 1.0


def test_get_original_missing_returns_none():
    assert TableEditState().get_original("0xdead") is None


def test_reset_baseline_clears_borders_and_originals():
    st = TableEditState()
    st.mark_cell_modified("0x100", 0, 0)
    st.mark_axis_modified("0x100", "x_axis", 1)
    st.capture_originals("0x100", {"values": np.array([1.0])})
    st.reset_baseline()
    assert not st.is_cell_modified("0x100", 0, 0)
    assert not st.is_axis_modified("0x100", "x_axis", 1)
    assert st.get_original("0x100") is None
