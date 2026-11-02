"""
Tables that share a name (different addresses) must never shadow each other.

Every NC definition reuses a few table names. A name-keyed dict keeps only the
last copy, so the compare tool reported ROMs as matching while a hidden copy
differed, and by-name API/MCP reads and writes could land on the wrong copy.
"""

import shutil
import struct
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from src.core.definition_parser import load_definition
from src.core.exceptions import AmbiguousTableNameError
from src.core.rom_definition import RomDefinition, RomID, Table, TableType
from src.core.rom_reader import RomReader
from src.core.table_lookup import (
    find_match,
    match_tables,
    parse_address,
    split_qualified_name,
)
from src.ui.mcp_mixin import McpMixin

EXAMPLES = Path(__file__).parent.parent / "examples"
LF9VEB_XML = EXAMPLES / "metadata" / "lf9veb.xml"
LF9VEB_BIN = EXAMPLES / "lf9veb.bin"

# Three copies of this 1D float table sit at 0xCDC0C, 0xCDC10, 0xCDC14.
TRIPLE = "After Start Enrichment A - Rate to Normal Fueling  | Engine ON, 90 < ECT < 103, IAT < 55"
TRIPLE_ADDRS = [0xCDC0C, 0xCDC10, 0xCDC14]
UNIQUE = "KR Accumulator - Exit Delay"


def _romid(xmlid="test"):
    return RomID(
        xmlid=xmlid,
        internalidaddress="0",
        internalidstring="T",
        ecuid="",
        make="",
        model="",
        flashmethod="",
        memmodel="",
        checksummodule="",
    )


def _t(name, address, category="Cat"):
    return Table(
        name=name,
        address=address,
        elements=1,
        scaling="s",
        type=TableType.ONE_D,
        category=category,
    )


@pytest.fixture(scope="module")
def lf9veb():
    return load_definition(str(LF9VEB_XML))


@pytest.fixture
def rom_pair(lf9veb, tmp_path):
    """Two readers over byte-identical copies of the sample ROM."""
    path_a = tmp_path / "a.bin"
    path_b = tmp_path / "b.bin"
    shutil.copy(LF9VEB_BIN, path_a)
    shutil.copy(LF9VEB_BIN, path_b)
    return RomReader(str(path_a), lf9veb), RomReader(str(path_b), lf9veb)


def _poke_float(reader, address, value):
    reader.rom_data[address : address + 4] = struct.pack(">f", value)


def _peek_float(reader, address):
    return struct.unpack(">f", bytes(reader.rom_data[address : address + 4]))[0]


# ---------------------------------------------------------------------------
# RomDefinition.get_table_by_name
# ---------------------------------------------------------------------------


class TestNameLookup:
    def test_sample_definition_has_duplicate_names(self, lf9veb):
        counts = {}
        for t in lf9veb.tables:
            counts[t.name] = counts.get(t.name, 0) + 1
        dups = {n: c for n, c in counts.items() if c > 1}
        assert len(dups) == 3
        assert sum(dups.values()) == 8
        assert [t.address_int for t in lf9veb.get_tables_by_name(TRIPLE)] == (
            TRIPLE_ADDRS
        )

    def test_unique_name_unchanged(self, lf9veb):
        table = lf9veb.get_table_by_name(UNIQUE)
        assert table is not None and table.name == UNIQUE
        assert lf9veb.display_name(table) == UNIQUE
        assert lf9veb.get_table_by_name("No Such Table") is None

    def test_ambiguous_name_refused_with_addresses(self, lf9veb):
        with pytest.raises(AmbiguousTableNameError) as exc:
            lf9veb.get_table_by_name(TRIPLE)
        msg = str(exc.value)
        for addr in TRIPLE_ADDRS:
            assert f"0x{addr:X}" in msg
        assert len(exc.value.tables) == 3

    @pytest.mark.parametrize("addr", TRIPLE_ADDRS)
    def test_address_argument_selects_copy(self, lf9veb, addr):
        for form in (addr, f"0x{addr:X}", f"{addr:x}", f"0x{addr:x}"):
            assert lf9veb.get_table_by_name(TRIPLE, address=form).address_int == addr

    @pytest.mark.parametrize("addr", TRIPLE_ADDRS)
    def test_qualified_name_selects_copy(self, lf9veb, addr):
        table = lf9veb.get_table_by_name(f"{TRIPLE} @ 0x{addr:X}")
        assert table.address_int == addr
        assert lf9veb.display_name(table) == f"{TRIPLE.strip()} @ 0x{addr:X}"
        # The display name round-trips through the lookup.
        assert lf9veb.get_table_by_name(lf9veb.display_name(table)) is table

    def test_wrong_address_is_not_found(self, lf9veb):
        assert lf9veb.get_table_by_name(TRIPLE, address=0x1234) is None
        assert lf9veb.get_table_by_name(f"{TRIPLE} @ 0x1234") is None
        unique = lf9veb.get_table_by_name(UNIQUE)
        assert lf9veb.get_table_by_name(UNIQUE, address=unique.address) is unique
        assert lf9veb.get_table_by_name(UNIQUE, address=0x1234) is None

    def test_conflicting_suffix_and_address_is_not_found(self, lf9veb):
        name = f"{TRIPLE} @ 0x{TRIPLE_ADDRS[0]:X}"
        assert lf9veb.get_table_by_name(name, address=TRIPLE_ADDRS[1]) is None

    def test_names_containing_at_sign_still_work(self, lf9veb):
        name = "Injected Fuel (%) To Intake Pool | RPM @ 900"
        assert lf9veb.get_table_by_name(name).name == name
        assert split_qualified_name(name) == (name, None)

    def test_bad_address_raises_value_error(self, lf9veb):
        with pytest.raises(ValueError):
            lf9veb.get_table_by_name(TRIPLE, address="zz")
        with pytest.raises(ValueError):
            parse_address("")

    def test_trailing_whitespace_name_still_found(self):
        defn = RomDefinition(romid=_romid(), tables=[_t("Padded ", "100")])
        assert defn.get_table_by_name("Padded").address == "100"
        assert defn.get_table_by_name("Padded ").address == "100"


# ---------------------------------------------------------------------------
# match_tables / find_match
# ---------------------------------------------------------------------------


class TestMatchTables:
    def test_same_definition_keeps_every_copy(self, lf9veb):
        matches = match_tables(lf9veb.tables, lf9veb.tables)
        assert len(matches) == len(lf9veb.tables)
        assert all(m.table_a is m.table_b for m in matches)
        labels = [m.label for m in matches]
        assert len(set(labels)) == len(labels)
        for addr in TRIPLE_ADDRS:
            assert f"{TRIPLE} @ 0x{addr:X}" in labels
        assert UNIQUE in labels

    def test_cross_definition_pairs_duplicates_by_order(self):
        a = [_t("Dup", "100"), _t("Dup", "200"), _t("Solo", "300")]
        b = [_t("Dup", "1100"), _t("Dup", "1200"), _t("Dup", "1300"), _t("Solo", "9")]
        matches = match_tables(a, b)
        dup = [m for m in matches if m.name == "Dup"]
        assert [(m.table_a and m.table_a.address, m.table_b.address) for m in dup] == [
            ("100", "1100"),
            ("200", "1200"),
            (None, "1300"),
        ]
        assert dup[0].label == "Dup @ 0x100 / 0x1100"
        assert dup[2].label == "Dup @ 0x1300"
        # 2 copies vs 3: the pairing is a guess, so none may be copied.
        assert all(m.uncertain and not m.copy_safe for m in dup)
        solo = [m for m in matches if m.name == "Solo"]
        assert len(solo) == 1 and solo[0].label == "Solo"
        assert solo[0].copy_safe

    def test_same_address_pairs_first(self):
        """Reviewer repro: B inserts a copy in the middle. A's 0xF8348 must
        pair with B's 0xF8348, not with B's new 2nd copy."""
        a = [_t("Dup", "cdc0c"), _t("Dup", "f8348")]
        b = [_t("Dup", "cdc00"), _t("Dup", "cdc04"), _t("Dup", "f8348")]
        pairs = [
            (m.table_a and m.table_a.address, m.table_b and m.table_b.address)
            for m in match_tables(a, b)
        ]
        assert pairs == [("cdc0c", "cdc00"), ("f8348", "f8348"), (None, "cdc04")]
        assert all(m.uncertain for m in match_tables(a, b))

    def test_equal_counts_same_address_reordered(self):
        a = [_t("Dup", "100"), _t("Dup", "200")]
        b = [_t("Dup", "200"), _t("Dup", "100")]
        matches = match_tables(a, b)
        assert [(m.table_a.address, m.table_b.address) for m in matches] == [
            ("100", "100"),
            ("200", "200"),
        ]
        assert all(m.copy_safe for m in matches)

    def test_equal_counts_in_order_pair_as_before(self):
        a = [_t("Dup", "100"), _t("Dup", "200")]
        b = [_t("Dup", "1100"), _t("Dup", "1200")]
        matches = match_tables(a, b)
        assert [(m.table_a.address, m.table_b.address) for m in matches] == [
            ("100", "1100"),
            ("200", "1200"),
        ]
        assert all(m.copy_safe for m in matches)

    def test_find_match_refuses_ambiguous_and_accepts_address(self):
        a = [_t("Dup", "100"), _t("Dup", "200")]
        matches = match_tables(a, a)
        match, err = find_match(matches, "Dup")
        assert match is None and "ambiguous" in err and "0x200" in err
        assert find_match(matches, "Dup", "0x200")[0].table_a.address == "200"
        assert find_match(matches, "Dup @ 0x100")[0].table_a.address == "100"
        assert find_match(matches, "Dup", "0x999")[0] is None


# ---------------------------------------------------------------------------
# Compare window: a change in ANY copy must be reported
# ---------------------------------------------------------------------------


def _window_diffs(reader_a, reader_b, defn):
    from src.ui.compare_window import CompareWindow

    win = CompareWindow.__new__(CompareWindow)
    win._reader_a = reader_a
    win._reader_b = reader_b
    win._definition_a = defn
    win._definition_b = defn
    win._cross_def = False
    win._modified_tables = []
    win._compute_diffs()
    return win._modified_tables


class TestCompareWindowDuplicates:
    def test_identical_roms_no_diffs(self, lf9veb, rom_pair):
        assert _window_diffs(*rom_pair, lf9veb) == []

    @pytest.mark.parametrize("which", [0, 1, 2], ids=["first", "middle", "last"])
    def test_change_in_one_copy_is_reported(self, lf9veb, rom_pair, which):
        reader_a, reader_b = rom_pair
        addr = TRIPLE_ADDRS[which]
        _poke_float(reader_b, addr, _peek_float(reader_a, addr) + 7.0)

        diffs = _window_diffs(reader_a, reader_b, lf9veb)

        assert len(diffs) == 1
        entry = diffs[0]
        assert entry["table_a"].address_int == addr
        assert entry["name"] == f"{TRIPLE} @ 0x{addr:X}"
        assert entry["change_count"] == 1

    def test_each_changed_copy_gets_its_own_row(self, lf9veb, rom_pair):
        reader_a, reader_b = rom_pair
        for addr in TRIPLE_ADDRS:
            _poke_float(reader_b, addr, _peek_float(reader_a, addr) + 1.0)
        diffs = _window_diffs(reader_a, reader_b, lf9veb)
        assert sorted(d["table_a"].address_int for d in diffs) == TRIPLE_ADDRS
        assert len({d["name"] for d in diffs}) == 3


class _AddrReader:
    """Fake reader keyed by table address (names are not unique)."""

    def __init__(self, values_by_addr):
        self._v = values_by_addr

    def read_table_data(self, table):
        return {"values": np.array([self._v[table.address]])}


def _cross_def_window(tables_a, tables_b, vals_a, vals_b):
    from src.ui.compare_window import CompareWindow

    win = CompareWindow.__new__(CompareWindow)
    win._reader_a = _AddrReader(vals_a)
    win._reader_b = _AddrReader(vals_b)
    win._definition_a = RomDefinition(romid=_romid("x"), tables=tables_a)
    win._definition_b = RomDefinition(romid=_romid("y"), tables=tables_b)
    win._cross_def = True
    win._readonly = False
    win._name_a, win._name_b = "a.bin", "b.bin"
    win._modified_tables = []
    win._compute_diffs()
    return win


class _Button:
    def __init__(self):
        self.enabled = None

    def setEnabled(self, value):
        self.enabled = bool(value)


class TestCompareCopyGuard:
    def test_count_mismatch_shows_all_copies_but_blocks_copy(self):
        a = [_t("Dup", "cdc0c"), _t("Dup", "f8348")]
        b = [_t("Dup", "cdc00"), _t("Dup", "cdc04"), _t("Dup", "f8348")]
        win = _cross_def_window(
            a,
            b,
            {"cdc0c": 1.0, "f8348": 2.0},
            {"cdc00": 5.0, "cdc04": 6.0, "f8348": 7.0},
        )
        rows = win._modified_tables
        # Every copy on each side is still shown as a difference.
        assert {r["table_a"].address for r in rows if r["table_a"]} == {
            "cdc0c",
            "f8348",
        }
        assert {r["table_b"].address for r in rows if r["table_b"]} == {
            "cdc00",
            "cdc04",
            "f8348",
        }
        paired = [r for r in rows if r["table_a"] and r["table_b"]]
        assert paired and all(r["pairing_uncertain"] for r in paired)

        # Copy All skips them in both directions.
        assert win._get_eligible_entries("a_to_b") == []
        assert win._get_eligible_entries("b_to_a") == []

        # Single-table Copy buttons are disabled for each of them.
        win._copy_a_to_b_btn, win._copy_b_to_a_btn = _Button(), _Button()
        for i, row in enumerate(rows):
            win._current_index = i
            win._update_copy_buttons()
            assert win._copy_a_to_b_btn.enabled is False
            assert win._copy_b_to_a_btn.enabled is False

    def test_copy_table_refuses_uncertain_pair(self, monkeypatch):
        # If the guard ever regresses, the copy path's error dialog must not
        # block the test run.
        monkeypatch.setattr(
            "src.ui.compare_window.QMessageBox.warning", lambda *a, **k: None
        )
        a = [_t("Dup", "100"), _t("Dup", "200")]
        b = [_t("Dup", "100"), _t("Dup", "200"), _t("Dup", "300")]
        win = _cross_def_window(
            a, b, {"100": 1.0, "200": 2.0}, {"100": 9.0, "200": 2.5, "300": 3.0}
        )
        writes = []
        win.parent = lambda: SimpleNamespace(
            apply_compare_copy=lambda *args: writes.append(args)
        )
        for i, row in enumerate(win._modified_tables):
            win._current_index = i
            win._copy_table("a_to_b")
            win._copy_table("b_to_a")
        assert writes == []

    def test_equal_counts_still_copyable(self):
        a = [_t("Dup", "100"), _t("Dup", "200")]
        b = [_t("Dup", "1100"), _t("Dup", "1200")]
        win = _cross_def_window(
            a, b, {"100": 1.0, "200": 2.0}, {"1100": 9.0, "1200": 8.0}
        )
        assert not any(r["pairing_uncertain"] for r in win._modified_tables)
        assert len(win._get_eligible_entries("a_to_b")) == 2


# ---------------------------------------------------------------------------
# MCP / API handlers
# ---------------------------------------------------------------------------


def _doc(reader, defn):
    return SimpleNamespace(
        rom_reader=reader,
        rom_definition=defn,
        edit_state=SimpleNamespace(capture_originals=lambda *a: None),
    )


def _fake_app(docs):
    """Just enough of MainWindow for the MCP handlers under test."""

    def apply_edits(document, table, changes, desc, rom_path=None):
        for row, col, _ov, _nv, _or, new_raw in changes:
            document.rom_reader.write_cell_value(table, row, col, new_raw)
        return True

    return SimpleNamespace(
        _find_document_by_rom_path=lambda p: docs.get(p),
        _apply_external_cell_edits=apply_edits,
        table_undo_manager=SimpleNamespace(set_active_stack=lambda key: None),
        _update_tab_title=lambda doc: None,
        _write_workspace_state=lambda: None,
    )


class TestMcpDuplicates:
    def test_read_unique_name_unchanged(self, lf9veb, rom_pair):
        app = _fake_app({"a": _doc(rom_pair[0], lf9veb)})
        result = McpMixin._api_read_table(app, {"rom_path": "a", "table_name": UNIQUE})
        assert result["success"] is True
        assert result["metadata"]["name"] == UNIQUE

    def test_read_ambiguous_refused(self, lf9veb, rom_pair):
        app = _fake_app({"a": _doc(rom_pair[0], lf9veb)})
        result = McpMixin._api_read_table(app, {"rom_path": "a", "table_name": TRIPLE})
        assert result["success"] is False
        assert "ambiguous" in result["error"]
        for addr in TRIPLE_ADDRS:
            assert f"0x{addr:X}" in result["error"]

    @pytest.mark.parametrize("which", [0, 1, 2])
    def test_read_by_address_and_qualified_name(self, lf9veb, rom_pair, which):
        reader = rom_pair[0]
        addr = TRIPLE_ADDRS[which]
        _poke_float(reader, addr, 12.5 + which)
        app = _fake_app({"a": _doc(reader, lf9veb)})
        for request in (
            {"table_name": TRIPLE, "address": f"0x{addr:X}"},
            {"table_name": f"{TRIPLE} @ 0x{addr:X}"},
        ):
            result = McpMixin._api_read_table(app, {"rom_path": "a", **request})
            assert result["success"] is True, result
            assert int(result["metadata"]["address"], 16) == addr
            assert float(result["values"][0]) == pytest.approx(12.5 + which)

    def test_write_ambiguous_refused_and_rom_untouched(self, lf9veb, rom_pair):
        reader = rom_pair[0]
        before = bytes(reader.rom_data)
        app = _fake_app({"a": _doc(reader, lf9veb)})
        result = McpMixin._api_edit_table(
            app,
            {
                "rom_path": "a",
                "table_name": TRIPLE,
                "cells": [{"row": 0, "col": 0, "value": 3.0}],
            },
        )
        assert result["success"] is False and "ambiguous" in result["error"]
        assert bytes(reader.rom_data) == before

    @pytest.mark.parametrize("which", [0, 1, 2])
    @pytest.mark.parametrize("qualified", [False, True])
    def test_write_by_address_lands_on_that_copy(
        self, lf9veb, rom_pair, which, qualified
    ):
        reader = rom_pair[0]
        addr = TRIPLE_ADDRS[which]
        before = bytes(reader.rom_data)
        request = {"rom_path": "a", "cells": [{"row": 0, "col": 0, "value": 42.0}]}
        if qualified:
            request["table_name"] = f"{TRIPLE} @ 0x{addr:X}"
        else:
            request["table_name"] = TRIPLE
            request["address"] = f"{addr:x}"
        app = _fake_app({"a": _doc(reader, lf9veb)})

        result = McpMixin._api_edit_table(app, request)

        assert result["success"] is True, result
        assert result["cells_modified"] == 1
        after = bytes(reader.rom_data)
        assert after[addr : addr + 4] == struct.pack(">f", 42.0)
        changed = [i for i in range(len(after)) if after[i] != before[i]]
        assert changed and all(addr <= i < addr + 4 for i in changed)

    def test_write_unique_name_unchanged(self, lf9veb, rom_pair):
        reader = rom_pair[0]
        table = lf9veb.get_table_by_name(UNIQUE)
        old = reader.read_table_data(table)["values"].flat[0]
        app = _fake_app({"a": _doc(reader, lf9veb)})
        result = McpMixin._api_edit_table(
            app,
            {
                "rom_path": "a",
                "table_name": UNIQUE,
                "cells": [{"row": 0, "col": 0, "value": float(old) + 1}],
            },
        )
        assert result["success"] is True
        new = reader.read_table_data(table)["values"].flat[0]
        assert new == pytest.approx(float(old) + 1)

    def test_statistics_ambiguous_refused_and_address_works(self, lf9veb, rom_pair):
        app = _fake_app({"a": _doc(rom_pair[0], lf9veb)})
        bad = McpMixin._api_table_statistics(
            app, {"rom_path": "a", "table_name": TRIPLE}
        )
        assert bad["success"] is False and "ambiguous" in bad["error"]
        good = McpMixin._api_table_statistics(
            app, {"rom_path": "a", "table_name": TRIPLE, "address": "0xCDC10"}
        )
        assert good["success"] is True

    @pytest.mark.parametrize("which", [0, 1, 2], ids=["first", "middle", "last"])
    def test_compare_all_reports_change_in_any_copy(self, lf9veb, rom_pair, which):
        reader_a, reader_b = rom_pair
        addr = TRIPLE_ADDRS[which]
        _poke_float(reader_b, addr, _peek_float(reader_a, addr) + 7.0)
        doc_a, doc_b = _doc(reader_a, lf9veb), _doc(reader_b, lf9veb)

        result = McpMixin._api_compare_all_tables(None, doc_a, doc_b)

        assert result["success"] is True
        names = [t["name"] for t in result["changed_tables"]]
        assert names == [f"{TRIPLE} @ 0x{addr:X}"]
        non_axis = sum(1 for t in lf9veb.tables if not t.is_axis)
        assert result["summary"]["total_common_tables"] == non_axis

    def test_compare_single_ambiguous_refused_and_address_works(self, lf9veb, rom_pair):
        reader_a, reader_b = rom_pair
        addr = TRIPLE_ADDRS[0]
        _poke_float(reader_b, addr, _peek_float(reader_a, addr) + 7.0)
        doc_a, doc_b = _doc(reader_a, lf9veb), _doc(reader_b, lf9veb)

        bad = McpMixin._api_compare_single_table(None, doc_a, doc_b, TRIPLE)
        assert bad["success"] is False and "ambiguous" in bad["error"]

        first = McpMixin._api_compare_single_table(
            None, doc_a, doc_b, TRIPLE, f"0x{addr:X}"
        )
        assert first["success"] is True and first["changed_cells"] == 1
        other = McpMixin._api_compare_single_table(
            None, doc_a, doc_b, f"{TRIPLE} @ 0x{TRIPLE_ADDRS[1]:X}"
        )
        assert other["success"] is True and other["changed_cells"] == 0

        unique = McpMixin._api_compare_single_table(None, doc_a, doc_b, UNIQUE)
        assert unique["success"] is True and unique["changed_cells"] == 0

    def test_list_tables_gives_unique_lookup_names(self, lf9veb, rom_pair):
        app = SimpleNamespace(
            _find_document_by_rom_path=lambda p: _doc(rom_pair[0], lf9veb),
        )
        app._api_table_summary = lambda t, d: McpMixin._api_table_summary(app, t, d)
        result = McpMixin._api_list_tables(app, {"rom_path": "a"})
        lookup = [t["lookup_name"] for t in result["tables"]]
        assert len(set(lookup)) == len(lookup)
        assert UNIQUE in lookup
        for name in lookup:
            assert lf9veb.get_table_by_name(name) is not None


def test_mcp_context_forwards_address(monkeypatch):
    from src.mcp.rom_context import RomContext

    ctx = RomContext()
    sent = []
    monkeypatch.setattr(
        ctx, "_post_to_app", lambda p: (sent.append(p), {"success": True})[1]
    )
    ctx.read_table("r.bin", "T", address="0xCDC10")
    ctx.write_table("r.bin", "T", [], address="0xCDC10")
    ctx.get_table_statistics("r.bin", "T", address="0xCDC10")
    ctx.read_live_table("r.bin", "T", address="0xCDC10")
    ctx.compare_tables("a.bin", "b.bin", "T", address="0xCDC10")
    assert [p["address"] for p in sent] == ["0xCDC10"] * 5
    sent.clear()
    ctx.read_table("r.bin", "T")
    ctx.write_table("r.bin", "T", [])
    assert all("address" not in p for p in sent)
