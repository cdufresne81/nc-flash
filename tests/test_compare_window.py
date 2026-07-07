"""
Tests for CompareWindow diff computation logic.

Tests _compute_diffs and _recompute_entry_diff without full GUI rendering.
Uses real RomReader instances with the sample ROM to verify diff detection.
"""

import copy

import numpy as np
import pytest

from src.core.definition_parser import load_definition
from src.core.rom_definition import (
    RomDefinition,
    RomID,
    Scaling,
    Table,
    TableType,
    AxisType,
)
from src.core.rom_reader import RomReader

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_romid(xmlid="test_rom"):
    return RomID(
        xmlid=xmlid,
        internalidaddress="0x0",
        internalidstring="TEST",
        ecuid="",
        make="",
        model="",
        flashmethod="",
        memmodel="",
        checksummodule="",
    )


def _make_scaling(name="TestScaling"):
    return Scaling(
        name=name,
        units="",
        toexpr="x",
        frexpr="x",
        format="%0.2f",
        min=0.0,
        max=100.0,
        inc=1.0,
        storagetype="float",
        endian="big",
    )


class FakeReader:
    """Minimal object that quacks like RomReader.read_table_data().

    Stores a mapping of table-name -> data-dict so _compute_diffs can call
    reader.read_table_data(table) and get predictable results.
    """

    def __init__(self, table_data: dict):
        self._data = table_data

    def read_table_data(self, table):
        return self._data[table.name]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def scaling():
    return _make_scaling()


@pytest.fixture
def romid():
    return _make_romid()


# ---------------------------------------------------------------------------
# Direct unit tests for _recompute_entry_diff (no window needed)
# ---------------------------------------------------------------------------


class TestRecomputeEntryDiff:
    """Test _recompute_entry_diff as a static-like helper.

    We instantiate CompareWindow indirectly by calling _recompute_entry_diff
    as an unbound method on a manually crafted entry dict.
    """

    @staticmethod
    def _recompute(entry):
        """Call _recompute_entry_diff without a CompareWindow instance.

        The method only touches the entry dict and numpy — no self access
        beyond the method itself — so we can call it directly via the class.
        """
        from src.ui.compare_window import CompareWindow

        CompareWindow._recompute_entry_diff(None, entry)

    def test_identical_values_no_diffs(self):
        values = np.array([[1.0, 2.0], [3.0, 4.0]])
        entry = {
            "data_a": {"values": values.copy()},
            "data_b": {"values": values.copy()},
            "changed_cells": set(),
            "changed_axes": {},
            "change_count": 0,
            "shape_mismatch": False,
        }
        self._recompute(entry)
        assert entry["changed_cells"] == set()
        assert entry["change_count"] == 0
        assert not entry["shape_mismatch"]

    def test_one_changed_cell_detected(self):
        values_a = np.array([[1.0, 2.0], [3.0, 4.0]])
        values_b = np.array([[1.0, 2.0], [3.0, 99.0]])
        entry = {
            "data_a": {"values": values_a},
            "data_b": {"values": values_b},
            "changed_cells": set(),
            "changed_axes": {},
            "change_count": 0,
            "shape_mismatch": False,
        }
        self._recompute(entry)
        assert entry["changed_cells"] == {(1, 1)}
        assert entry["change_count"] == 1

    def test_multiple_changed_cells(self):
        values_a = np.array([[1.0, 2.0], [3.0, 4.0]])
        values_b = np.array([[10.0, 2.0], [30.0, 4.0]])
        entry = {
            "data_a": {"values": values_a},
            "data_b": {"values": values_b},
            "changed_cells": set(),
            "changed_axes": {},
            "change_count": 0,
            "shape_mismatch": False,
        }
        self._recompute(entry)
        assert entry["changed_cells"] == {(0, 0), (1, 0)}
        assert entry["change_count"] == 2

    def test_shape_mismatch(self):
        values_a = np.array([[1.0, 2.0]])
        values_b = np.array([[1.0], [2.0]])
        entry = {
            "data_a": {"values": values_a},
            "data_b": {"values": values_b},
            "changed_cells": set(),
            "changed_axes": {},
            "change_count": 0,
            "shape_mismatch": False,
        }
        self._recompute(entry)
        assert entry["shape_mismatch"] is True
        # All cells from both shapes should be marked
        assert len(entry["changed_cells"]) > 0

    def test_axis_only_change(self):
        values = np.array([[1.0, 2.0]])
        axis_a = np.array([10.0, 20.0])
        axis_b = np.array([10.0, 25.0])
        entry = {
            "data_a": {"values": values.copy(), "x_axis": axis_a},
            "data_b": {"values": values.copy(), "x_axis": axis_b},
            "changed_cells": set(),
            "changed_axes": {},
            "change_count": 0,
            "shape_mismatch": False,
        }
        self._recompute(entry)
        # No data cell changes
        assert entry["changed_cells"] == set()
        # But axis changes detected
        assert "x_axis" in entry["changed_axes"]
        assert 1 in entry["changed_axes"]["x_axis"]
        assert entry["change_count"] == 1

    def test_1d_values_changed(self):
        values_a = np.array([1.0, 2.0, 3.0])
        values_b = np.array([1.0, 99.0, 3.0])
        entry = {
            "data_a": {"values": values_a},
            "data_b": {"values": values_b},
            "changed_cells": set(),
            "changed_axes": {},
            "change_count": 0,
            "shape_mismatch": False,
        }
        self._recompute(entry)
        assert entry["changed_cells"] == {(1, 0)}
        assert entry["change_count"] == 1

    def test_none_data_returns_early(self):
        entry = {
            "data_a": None,
            "data_b": {"values": np.array([1.0])},
            "changed_cells": {(0, 0)},
            "changed_axes": {},
            "change_count": 1,
            "shape_mismatch": False,
        }
        self._recompute(entry)
        # Should return early without modifying
        assert entry["changed_cells"] == {(0, 0)}

    def test_recompute_after_copy_clears_diffs(self):
        """After a copy operation makes both sides identical, diffs should clear."""
        values = np.array([[5.0, 6.0], [7.0, 8.0]])
        entry = {
            "data_a": {"values": np.array([[1.0, 2.0], [3.0, 4.0]])},
            "data_b": {"values": np.array([[5.0, 6.0], [7.0, 8.0]])},
            "changed_cells": set(),
            "changed_axes": {},
            "change_count": 0,
            "shape_mismatch": False,
        }
        # Simulate copy: make both sides identical
        entry["data_a"]["values"] = values.copy()
        self._recompute(entry)
        assert entry["changed_cells"] == set()
        assert entry["change_count"] == 0


# ---------------------------------------------------------------------------
# Integration tests using _compute_diffs with FakeReader
# ---------------------------------------------------------------------------


class TestComputeDiffs:
    """Test _compute_diffs via CompareWindow instantiation with fake readers."""

    def _make_window_diffs(
        self,
        tables_a,
        tables_b,
        reader_data_a,
        reader_data_b,
        xmlid_a="rom_a",
        xmlid_b="rom_b",
    ):
        """Create a CompareWindow and return its _modified_tables list.

        We instantiate CompareWindow, which calls _compute_diffs in __init__.
        If has_diffs is False it returns early without building UI, which
        is fine — we only need _modified_tables.
        """
        from PySide6.QtGui import QColor
        from src.ui.compare_window import CompareWindow

        # Use same xmlid by default so cross-def matching isn't triggered differently
        def_a = RomDefinition(romid=_make_romid(xmlid_a), tables=tables_a)
        def_b = RomDefinition(romid=_make_romid(xmlid_b), tables=tables_b)
        reader_a = FakeReader(reader_data_a)
        reader_b = FakeReader(reader_data_b)

        win = CompareWindow.__new__(CompareWindow)
        # Manually set attributes that __init__ sets before _compute_diffs
        win._reader_a = reader_a
        win._reader_b = reader_b
        win._definition_a = def_a
        win._definition_b = def_b
        win._cross_def = xmlid_a != xmlid_b
        win._modified_tables = []

        win._compute_diffs()
        return win._modified_tables

    def test_identical_roms_no_diffs(self):
        table = Table(
            name="T1",
            address="0x100",
            type=TableType.ONE_D,
            elements=3,
            scaling="s",
        )
        data = {"values": np.array([1.0, 2.0, 3.0])}
        diffs = self._make_window_diffs(
            [table],
            [copy.deepcopy(table)],
            {"T1": data},
            {"T1": {"values": data["values"].copy()}},
        )
        assert len(diffs) == 0

    def test_one_cell_changed(self):
        table = Table(
            name="T1",
            address="0x100",
            type=TableType.ONE_D,
            elements=3,
            scaling="s",
        )
        data_a = {"values": np.array([1.0, 2.0, 3.0])}
        data_b = {"values": np.array([1.0, 99.0, 3.0])}
        diffs = self._make_window_diffs(
            [table], [copy.deepcopy(table)], {"T1": data_a}, {"T1": data_b}
        )
        assert len(diffs) == 1
        assert diffs[0]["changed_cells"] == {(1, 0)}

    def test_one_sided_a_only(self):
        table_a = Table(
            name="OnlyA",
            address="0x200",
            type=TableType.ONE_D,
            elements=2,
            scaling="s",
        )
        data_a = {"values": np.array([10.0, 20.0])}
        diffs = self._make_window_diffs([table_a], [], {"OnlyA": data_a}, {})
        assert len(diffs) == 1
        assert diffs[0]["a_only"] is True
        assert diffs[0]["b_only"] is False
        assert len(diffs[0]["changed_cells"]) == 2

    def test_one_sided_b_only(self):
        table_b = Table(
            name="OnlyB",
            address="0x300",
            type=TableType.ONE_D,
            elements=2,
            scaling="s",
        )
        data_b = {"values": np.array([10.0, 20.0])}
        diffs = self._make_window_diffs([], [table_b], {}, {"OnlyB": data_b})
        assert len(diffs) == 1
        assert diffs[0]["b_only"] is True
        assert diffs[0]["a_only"] is False

    def test_shape_mismatch_3d(self):
        table = Table(
            name="T3D",
            address="0x400",
            type=TableType.THREE_D,
            elements=6,
            scaling="s",
        )
        data_a = {"values": np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])}
        data_b = {"values": np.array([[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]])}
        diffs = self._make_window_diffs(
            [table], [copy.deepcopy(table)], {"T3D": data_a}, {"T3D": data_b}
        )
        assert len(diffs) == 1
        assert diffs[0]["shape_mismatch"] is True

    def test_axis_only_change_detected(self):
        table = Table(
            name="T2D",
            address="0x500",
            type=TableType.TWO_D,
            elements=3,
            scaling="s",
        )
        values = np.array([1.0, 2.0, 3.0])
        data_a = {"values": values.copy(), "y_axis": np.array([10.0, 20.0, 30.0])}
        data_b = {"values": values.copy(), "y_axis": np.array([10.0, 25.0, 30.0])}
        diffs = self._make_window_diffs(
            [table], [copy.deepcopy(table)], {"T2D": data_a}, {"T2D": data_b}
        )
        assert len(diffs) == 1
        assert diffs[0]["changed_cells"] == set()  # No data changes
        assert "y_axis" in diffs[0]["changed_axes"]
        assert 1 in diffs[0]["changed_axes"]["y_axis"]

    def test_cross_definition_comparison(self):
        """Tables matched by name across different xmlids."""
        table_a = Table(
            name="Shared",
            address="0x100",
            type=TableType.ONE_D,
            elements=2,
            scaling="s",
        )
        table_b = Table(
            name="Shared",
            address="0x200",
            type=TableType.ONE_D,
            elements=2,
            scaling="s",
        )
        data_a = {"values": np.array([1.0, 2.0])}
        data_b = {"values": np.array([1.0, 99.0])}
        diffs = self._make_window_diffs(
            [table_a],
            [table_b],
            {"Shared": data_a},
            {"Shared": data_b},
            xmlid_a="rom_x",
            xmlid_b="rom_y",
        )
        assert len(diffs) == 1
        assert diffs[0]["changed_cells"] == {(1, 0)}

    def test_all_nan_one_sided_skipped(self):
        """One-sided table that is entirely NaN should be skipped."""
        table_a = Table(
            name="NanOnly",
            address="0x600",
            type=TableType.ONE_D,
            elements=2,
            scaling="s",
        )
        data_a = {"values": np.array([float("nan"), float("nan")])}
        diffs = self._make_window_diffs([table_a], [], {"NanOnly": data_a}, {})
        assert len(diffs) == 0

    def test_both_nan_skipped(self):
        """Both sides entirely NaN should be skipped."""
        table = Table(
            name="BothNan",
            address="0x700",
            type=TableType.ONE_D,
            elements=2,
            scaling="s",
        )
        nan_data = {"values": np.array([float("nan"), float("nan")])}
        diffs = self._make_window_diffs(
            [table],
            [copy.deepcopy(table)],
            {"BothNan": nan_data},
            {"BothNan": {"values": nan_data["values"].copy()}},
        )
        assert len(diffs) == 0


# ---------------------------------------------------------------------------
# Tests for Copy All eligibility logic
# ---------------------------------------------------------------------------


class TestGetEligibleEntries:
    """Test _get_eligible_entries filtering logic."""

    @staticmethod
    def _get_eligible(entries, direction):
        from src.ui.compare_window import CompareWindow

        win = CompareWindow.__new__(CompareWindow)
        win._modified_tables = entries
        return win._get_eligible_entries(direction)

    def _make_entry(self, **overrides):
        entry = {
            "table_a": "ta",
            "table_b": "tb",
            "name": "Test",
            "category": "Cat",
            "data_a": {"values": np.array([1.0])},
            "data_b": {"values": np.array([2.0])},
            "changed_cells": {(0, 0)},
            "changed_axes": {},
            "change_count": 1,
            "shape_mismatch": False,
            "a_only": False,
            "b_only": False,
        }
        entry.update(overrides)
        return entry

    def test_normal_entry_is_eligible(self):
        entry = self._make_entry()
        eligible = self._get_eligible([entry], "a_to_b")
        assert len(eligible) == 1
        assert eligible[0] == (0, entry)

    def test_shape_mismatch_excluded(self):
        entry = self._make_entry(shape_mismatch=True)
        assert len(self._get_eligible([entry], "a_to_b")) == 0

    def test_a_only_excluded(self):
        entry = self._make_entry(a_only=True, table_b=None)
        assert len(self._get_eligible([entry], "a_to_b")) == 0

    def test_b_only_excluded(self):
        entry = self._make_entry(b_only=True, table_a=None)
        assert len(self._get_eligible([entry], "b_to_a")) == 0

    def test_identical_excluded(self):
        entry = self._make_entry(change_count=0, changed_cells=set())
        assert len(self._get_eligible([entry], "a_to_b")) == 0

    def test_missing_source_data_excluded(self):
        entry = self._make_entry(data_a=None)
        assert len(self._get_eligible([entry], "a_to_b")) == 0

    def test_mixed_entries_filters_correctly(self):
        entries = [
            self._make_entry(name="Good1"),
            self._make_entry(name="ShapeBad", shape_mismatch=True),
            self._make_entry(name="Good2"),
            self._make_entry(name="Identical", change_count=0, changed_cells=set()),
        ]
        eligible = self._get_eligible(entries, "a_to_b")
        assert len(eligible) == 2
        assert eligible[0][1]["name"] == "Good1"
        assert eligible[1][1]["name"] == "Good2"


class TestUpdateSidebarLabels:
    """Test _update_sidebar_labels formatting logic."""

    @staticmethod
    def _update_labels(entries, tree_items):
        from src.ui.compare_window import CompareWindow

        win = CompareWindow.__new__(CompareWindow)
        win._modified_tables = entries
        win._tree_items = tree_items
        win._update_sidebar_labels()

    def test_identical_label(self):
        from PySide6.QtWidgets import QTreeWidgetItem

        entry = {
            "name": "TestTable",
            "change_count": 0,
            "a_only": False,
            "b_only": False,
            "shape_mismatch": False,
        }
        item = QTreeWidgetItem(["old text"])
        self._update_labels([entry], {0: item})
        assert "identical" in item.text(0)

    def test_single_cell_label(self):
        from PySide6.QtWidgets import QTreeWidgetItem

        entry = {
            "name": "TestTable",
            "change_count": 1,
            "a_only": False,
            "b_only": False,
            "shape_mismatch": False,
        }
        item = QTreeWidgetItem(["old text"])
        self._update_labels([entry], {0: item})
        assert "1 cell" in item.text(0)
        assert "cells" not in item.text(0)

    def test_multiple_cells_label(self):
        from PySide6.QtWidgets import QTreeWidgetItem

        entry = {
            "name": "TestTable",
            "change_count": 5,
            "a_only": False,
            "b_only": False,
            "shape_mismatch": False,
        }
        item = QTreeWidgetItem(["old text"])
        self._update_labels([entry], {0: item})
        assert "5 cells" in item.text(0)


class TestCloseEventClearsParentRef:
    """B12 — closeEvent nulls whichever parent ref points at THIS window."""

    @staticmethod
    def _close(parent):
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch
        from src.ui.compare_window import CompareWindow

        fake = SimpleNamespace(
            saveGeometry=MagicMock(return_value=b""),
            deleteLater=MagicMock(),
        )
        fake.parent = lambda: parent
        # Point the parent's ref at this window (the common case).
        for attr in ("compare_window", "_compare_window"):
            if hasattr(parent, attr) and getattr(parent, attr) == "SELF":
                setattr(parent, attr, fake)
        event = MagicMock()
        with patch("src.ui.compare_window.QSettings"):
            CompareWindow.closeEvent(fake, event)
        event.accept.assert_called_once()
        return fake

    def test_nulls_main_window_ref(self):
        from types import SimpleNamespace

        parent = SimpleNamespace(compare_window="SELF")
        self._close(parent)
        assert parent.compare_window is None

    def test_nulls_history_dialog_underscore_ref(self):
        from types import SimpleNamespace

        parent = SimpleNamespace(_compare_window="SELF")
        self._close(parent)
        assert parent._compare_window is None

    def test_leaves_ref_pointing_at_a_different_window(self):
        from types import SimpleNamespace

        other = object()
        parent = SimpleNamespace(compare_window=other)  # not this window
        self._close(parent)
        assert parent.compare_window is other
