"""
Tests for the transfer guard: a compare copy never writes an erased (all-0xFF)
source over feature settings (nc-flash-re feature release, requirements R2).
"""

import struct
from unittest.mock import MagicMock

import numpy as np
import pytest

from src.core.rom_definition import (
    RomDefinition,
    RomID,
    Scaling,
    Table,
    TableLayout,
    TableType,
)
from src.core.transfer_guard import (
    FEATURE_CATEGORIES,
    keeps_destination,
    table_data_span,
)

ENABLE_ADDR = 0x100
RPM_ADDR = 0x104


def _definition():
    romid = RomID(
        xmlid="TEST",
        internalidaddress="0x0",
        internalidstring="TEST",
        ecuid="",
        make="",
        model="",
        flashmethod="",
        memmodel="",
        checksummodule="",
    )
    defn = RomDefinition(romid=romid)
    for name, storage in (("u8", "uint8"), ("f32", "float")):
        defn.scalings[name] = Scaling(
            name=name,
            units="",
            toexpr="x",
            frexpr="x",
            format="%0.2f",
            min=0.0,
            max=10000.0,
            inc=1.0,
            storagetype=storage,
            endian="big",
        )
    return defn


def _table(name, address, scaling, category, elements=1):
    return Table(
        name=name,
        address=hex(address),
        elements=elements,
        scaling=scaling,
        type=TableType.ONE_D,
        category=category,
    )


def _rom(enable=None, rpm=None):
    """A small ROM image: feature area erased unless values are given."""
    rom = bytearray(b"\x00" * 0x100 + b"\xff" * 0x100)
    if enable is not None:
        rom[ENABLE_ADDR] = enable
    if rpm is not None:
        rom[RPM_ADDR : RPM_ADDR + 4] = struct.pack(">f", rpm)
    return rom


@pytest.fixture
def defn():
    return _definition()


@pytest.fixture
def enable_table():
    return _table(
        "Shift Light - Enable", ENABLE_ADDR, "u8", "Instrument Cluster - Shift Light"
    )


@pytest.fixture
def rpm_table():
    return _table(
        "Shift Light - RPM On", RPM_ADDR, "f32", "Instrument Cluster - Shift Light"
    )


class TestKeepsDestination:
    def test_erased_source_over_release_settings_is_kept(
        self, defn, enable_table, rpm_table
    ):
        old = _rom()  # an old ROM without the features
        release = _rom(enable=0, rpm=6800.0)
        for t in (enable_table, rpm_table):
            assert keeps_destination(t, defn, old, t, defn, release)

    def test_real_source_settings_are_copied(self, defn, rpm_table):
        other_release = _rom(enable=0, rpm=7000.0)
        release = _rom(enable=0, rpm=6800.0)
        assert not keeps_destination(
            rpm_table, defn, other_release, rpm_table, defn, release
        )

    def test_erased_destination_is_not_guarded(self, defn, rpm_table):
        # Both erased: nothing to protect (and nothing differs anyway).
        assert not keeps_destination(rpm_table, defn, _rom(), rpm_table, defn, _rom())

    def test_partly_erased_source_is_copied(self, defn):
        table = _table(
            "Shift Light - Stages",
            ENABLE_ADDR,
            "u8",
            "Instrument Cluster - Shift Light",
            elements=4,
        )
        src = _rom(enable=1)  # first byte set, the other 3 still 0xFF
        dst = _rom(enable=0)
        assert not keeps_destination(table, defn, src, table, defn, dst)

    def test_other_categories_are_not_guarded(self, defn):
        table = _table("Fuel Map", RPM_ADDR, "f32", "Fuel - Base")
        assert not keeps_destination(table, defn, _rom(), table, defn, _rom(rpm=1.0))

    def test_destination_category_is_enough(self, defn, rpm_table):
        src_table = _table(rpm_table.name, RPM_ADDR, "f32", "Uncategorized")
        assert keeps_destination(
            src_table, defn, _rom(), rpm_table, defn, _rom(rpm=6800.0)
        )

    def test_out_of_range_table_is_not_guarded(self, defn):
        table = _table(
            "Shift Light - Far", 0x10000, "f32", "Instrument Cluster - Shift Light"
        )
        assert not keeps_destination(table, defn, _rom(), table, defn, _rom())

    def test_all_spec_categories_listed(self):
        assert FEATURE_CATEGORIES == {
            "DBW - AutoBlip",
            "Engine Limiter - Boost Cut",
            "Instrument Cluster - Shift Light",
            "Engine Limiter - Cold Engine",
            "Instrument Cluster - Speedometer",
        }


class TestTableDataSpan:
    def test_span_uses_storage_size(self, defn, rpm_table):
        assert table_data_span(rpm_table, defn) == (RPM_ADDR, 4)

    def test_unknown_scaling_has_no_span(self, defn):
        assert table_data_span(_table("X", 0, "missing", "Fuel"), defn) is None

    def test_interleaved_has_no_span(self, defn, rpm_table):
        rpm_table.layout = TableLayout.INTERLEAVED
        assert table_data_span(rpm_table, defn) is None


# ---------------------------------------------------------------------------
# Compare window: single copy refused, Copy All keeps feature tables
# ---------------------------------------------------------------------------


class _Reader:
    def __init__(self, rom):
        self.rom_data = rom

    def read_table_data(self, table):
        return {"values": np.array([0.0])}


def _window(defn, rom_a, rom_b, entries):
    from src.ui.compare_window import CompareWindow

    win = CompareWindow.__new__(CompareWindow)
    win._definition_a = win._definition_b = defn
    win._reader_a, win._reader_b = _Reader(rom_a), _Reader(rom_b)
    win._name_a, win._name_b = "old.bin", "release.bin"
    win._modified_tables = entries
    win._readonly = False
    return win


def _entry(table):
    return {
        "table_a": table,
        "table_b": table,
        "name": table.name,
        "category": table.category,
        "data_a": {"values": np.array([np.nan])},
        "data_b": {"values": np.array([6800.0])},
        "changed_cells": {(0, 0)},
        "changed_axes": {},
        "change_count": 1,
        "shape_mismatch": False,
        "a_only": False,
        "b_only": False,
    }


class TestCompareWindowGuard:
    def test_single_copy_is_refused(self, qapp, monkeypatch, defn, rpm_table):
        import src.ui.compare_window as cw

        info = MagicMock()
        monkeypatch.setattr(cw.QMessageBox, "information", info)
        win = _window(defn, _rom(), _rom(enable=0, rpm=6800.0), [_entry(rpm_table)])
        win._current_index = 0
        main = MagicMock()
        win.parent = lambda: main
        win.statusBar = MagicMock()

        win._copy_table("a_to_b")

        main.apply_compare_copy.assert_not_called()
        info.assert_called_once()
        assert "was not copied" in info.call_args[0][2]

    def test_copy_all_keeps_feature_tables_and_copies_the_rest(
        self, qapp, monkeypatch, defn, rpm_table
    ):
        import src.ui.compare_window as cw

        fuel = _table("Fuel Map", 0x10, "f32", "Fuel - Base")
        rom_a = _rom()
        rom_a[0x10:0x14] = struct.pack(">f", 1.5)
        win = _window(
            defn, rom_a, _rom(enable=0, rpm=6800.0), [_entry(rpm_table), _entry(fuel)]
        )
        question = MagicMock(return_value=cw.QMessageBox.Yes)
        monkeypatch.setattr(cw.QMessageBox, "question", question)
        progress = MagicMock()
        progress.return_value.wasCanceled.return_value = False
        monkeypatch.setattr(cw, "QProgressDialog", progress)
        main = MagicMock()
        win.parent = lambda: main
        win.statusBar = MagicMock()
        win._current_index = -1
        win._tree_items = {}
        win._copy_all_a_to_b_btn = None
        win._recompute_entry_diff = MagicMock()

        win._copy_all_tables("a_to_b")

        copied = [c.args[1].name for c in main.apply_compare_copy.call_args_list]
        assert copied == ["Fuel Map"]
        text = question.call_args[0][2]
        assert "Copy 1 table" in text
        assert "Shift Light - RPM On" in text

    def test_copy_all_with_only_feature_tables_copies_nothing(
        self, qapp, monkeypatch, defn, rpm_table
    ):
        import src.ui.compare_window as cw

        info = MagicMock()
        question = MagicMock()
        monkeypatch.setattr(cw.QMessageBox, "information", info)
        monkeypatch.setattr(cw.QMessageBox, "question", question)
        win = _window(defn, _rom(), _rom(enable=0, rpm=6800.0), [_entry(rpm_table)])
        main = MagicMock()
        win.parent = lambda: main

        win._copy_all_tables("a_to_b")

        question.assert_not_called()
        main.apply_compare_copy.assert_not_called()
        assert "will be kept" in info.call_args[0][2]
