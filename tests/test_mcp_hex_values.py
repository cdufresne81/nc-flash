"""MCP/API table edits honor the table's display format (issue 146).

Tire Size Correction (and the CEL Flash Pattern settings) display as hex
('%08x'). The read endpoint returned that hex text without saying so, and the
edit endpoint parsed values with float(), i.e. as decimal: reading
' 2054517' and writing it back stored 0x001F5975 instead of 0x02054517.

These tests drive the real read/edit handlers against the sample ROM and its
definition, through a JSON round trip like the HTTP bridge, and check the
bytes that land in the ROM.
"""

import json
import math
import shutil
import struct
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from main import MainWindow
from src.core.definition_parser import load_definition
from src.core.exceptions import RomWriteError
from src.core.rom_reader import RomReader
from src.ui.mcp_mixin import McpMixin
from src.utils.formatting import (
    parse_api_value,
    parse_numeric_text,
    value_format_name,
)

_app = QApplication.instance() or QApplication([])

TIRE_ADDR = 0xF3E84
COIL_DWELL = "Ignition Coil Dwell | Speed Limiter Active "  # 1D float, %0.2f
COIL_ADDR = 0xBB8CC
LAUNCH_RPM = "Launch Control Activation - RPM Threshold"  # 1D float, %0.0f
LAUNCH_ADDR = 0xF3EA0
BLEND = "Blend Display Format | 0 = Km/h, 1 = Mph"  # 1D uint8, %d
FAN_ECT = "Low Speed Fan Disable - ECT Threshold"  # 2D uint16, lossy, %d
FAN_ADDR = 0xBFA10


@pytest.fixture
def bench(sample_rom_path, sample_xml_path, tmp_path):
    """A fake MainWindow wired to the real edit pipeline on a ROM copy."""
    rom_copy = tmp_path / "lf9veb.bin"
    shutil.copy2(sample_rom_path, rom_copy)
    definition = load_definition(str(sample_xml_path))
    reader = RomReader(str(rom_copy), definition)
    doc = SimpleNamespace(
        rom_definition=definition,
        rom_reader=reader,
        edit_state=MagicMock(),
        set_modified=MagicMock(),
    )
    fake = SimpleNamespace(
        table_undo_manager=MagicMock(),
        change_tracker=MagicMock(),
        _find_document_by_rom_path=lambda p: doc,
        _find_table_window=lambda key: None,
        _update_tab_title=MagicMock(),
        _write_workspace_state=MagicMock(),
    )
    for name in (
        "_write_to_rom_and_mark_modified",
        "_apply_external_cell_edits",
        "_rollback_failed_cell_writes",
    ):
        method = getattr(MainWindow, name)
        setattr(fake, name, lambda *a, _m=method, **k: _m(fake, *a, **k))
    fake._api_format_axis = lambda *a: McpMixin._api_format_axis(fake, *a)
    tire = next(
        t
        for t in definition.tables
        if t.name.startswith("Tire Size Correction") and t.address_int == TIRE_ADDR
    )
    return SimpleNamespace(
        fake=fake,
        doc=doc,
        definition=definition,
        reader=reader,
        rom=str(rom_copy),
        tire=tire,
    )


def _wire(result):
    """Simulate the HTTP/JSON bridge."""
    return json.loads(json.dumps(result))


def _read(bench, table_name):
    req = {"rom_path": bench.rom, "table_name": table_name}
    return _wire(McpMixin._api_read_table(bench.fake, req))


def _edit_cells(bench, table_name, cells):
    req = _wire({"rom_path": bench.rom, "table_name": table_name, "cells": cells})
    return _wire(McpMixin._api_edit_table(bench.fake, req))


def _edit(bench, table_name, value):
    return _edit_cells(bench, table_name, [{"row": 0, "col": 0, "value": value}])


def _bytes(bench, addr, n=4):
    return bytes(bench.reader.rom_data[addr : addr + n])


def _set_tire(bench, raw: bytes):
    bench.reader.rom_data[TIRE_ADDR : TIRE_ADDR + 4] = raw


def _assert_untouched(bench, before):
    """Nothing written, and nothing tracked, marked or flagged modified."""
    assert bytes(bench.reader.rom_data) == before
    bench.doc.set_modified.assert_not_called()
    bench.doc.edit_state.mark_cells_modified.assert_not_called()
    bench.fake.change_tracker.record_pending_bulk_changes.assert_not_called()
    bench.fake.table_undo_manager.record_bulk_cell_changes.assert_not_called()


# --------------------------------------------------------------------------
# Read reports the format
# --------------------------------------------------------------------------


def test_read_hex_table_reports_format_and_numbers(bench):
    _set_tire(bench, bytes.fromhex("02054517"))
    result = _read(bench, bench.tire.name)

    assert result["success"] is True
    assert result["metadata"]["format"] == "hex"
    assert result["metadata"]["format_spec"] == "%08x"
    # Display text unchanged (backward compatible; space-padded) ...
    assert result["values"] == [" 2054517"]
    # ... plus the plain number, same shape.
    assert result["numeric_values"] == [0x02054517]


def test_read_decimal_table_reports_decimal(bench):
    result = _read(bench, COIL_DWELL)
    assert result["metadata"]["format"] == "decimal"
    assert len(result["numeric_values"]) == len(result["values"]) == 1


def test_read_nan_cell_is_null_and_refused_back(bench):
    result = _read(bench, LAUNCH_RPM)  # stock ROM: 0xFFFFFFFF float = NaN
    assert result["numeric_values"] == [None]
    before = bytes(bench.reader.rom_data)
    assert _edit(bench, LAUNCH_RPM, None)["success"] is False
    assert bytes(bench.reader.rom_data) == before


# --------------------------------------------------------------------------
# Round trips: sending back what was read changes nothing
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["02054517", "001f5975", "ffffffff", "0000000a"])
def test_tire_size_text_round_trip_is_byte_identical(bench, raw):
    _set_tire(bench, bytes.fromhex(raw))
    before = bytes(bench.reader.rom_data)

    text = _read(bench, bench.tire.name)["values"][0]
    result = _edit(bench, bench.tire.name, text)

    assert result["success"] is True, result
    assert result["cells_modified"] == 0
    assert bytes(bench.reader.rom_data) == before


def _whole_table_cells(read, key):
    vals = read[key]
    if read["metadata"]["type"] == "3D":
        return [
            {"row": r, "col": c, "value": v}
            for r, row in enumerate(vals)
            for c, v in enumerate(row)
        ]
    return [{"row": r, "col": 0, "value": v} for r, v in enumerate(vals)]


def test_resending_every_table_changes_no_byte(bench):
    """Agents re-send a whole table to change one cell. Sending back the
    rounded "values" text (or, on decimal tables, "numeric_values") must
    leave every lf9veb table byte-identical."""
    before = bytes(bench.reader.rom_data)
    names = {t.name for t in bench.definition.tables if not t.is_axis}
    checked = 0
    for name in sorted(names):
        read = _read(bench, name)
        if not read.get("success") or "numeric_values" not in read:
            continue
        flat = read["numeric_values"]
        if read["metadata"]["type"] == "3D":
            flat = [v for row in flat for v in row]
        if any(v is None for v in flat):
            continue  # blank/NaN cells are refused by design
        keys = ["values"]
        if read["metadata"]["format"] != "hex":
            keys.append("numeric_values")
        for key in keys:
            result = _edit_cells(bench, name, _whole_table_cells(read, key))
            assert result["success"] is True, (name, key, result)
            assert result["cells_modified"] == 0, (name, key)
        checked += 1
    assert checked > 400
    _assert_untouched(bench, before)


# --------------------------------------------------------------------------
# Writes land the right bytes, and report what was stored
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value", ["2054517", " 2054517", "02054517", "0x2054517", "0X02054517"]
)
def test_hex_text_edit_lands_hex_bytes(bench, value):
    result = _edit(bench, bench.tire.name, value)
    assert result["success"] is True
    assert _bytes(bench, TIRE_ADDR) == bytes.fromhex("02054517")
    assert result["cells"] == [
        {
            "row": 0,
            "col": 0,
            "changed": True,
            "value": " 2054517",
            "numeric_value": 0x02054517,
        }
    ]


@pytest.mark.parametrize("value", [2254517, 0x02254517, 2254517.0])
def test_json_number_on_hex_table_is_refused(bench, value):
    """2254517 as a number would store 0x002266B5: a wrong tire size."""
    before = bytes(bench.reader.rom_data)
    result = _edit(bench, bench.tire.name, value)
    assert result["success"] is False
    assert "hex text" in result["error"]
    assert "0x2054517" in result["error"]
    _assert_untouched(bench, before)


@pytest.mark.parametrize("value", [3.5, "3.5", 3, "  3.5 "])
def test_decimal_table_edit_still_works(bench, value):
    result = _edit(bench, COIL_DWELL, value)
    assert result["success"] is True
    assert _bytes(bench, COIL_ADDR) == struct.pack(">f", float(value))
    assert result["cells"][0]["numeric_value"] == float(value)
    assert result["cells"][0]["value"] == f"{float(value):.2f}"


def test_changed_decimal_value_reports_the_rounded_stored_value(bench):
    """Lossy scaling: the response says what the ROM really holds."""
    result = _edit_cells(bench, FAN_ECT, [{"row": 0, "col": 0, "value": 80}])
    assert result["success"] is True
    cell = result["cells"][0]
    assert cell["changed"] is True
    # 80 is stored as uint16 raw 168, the closest count, which reads back
    # as 80.065 ('80'). Before issue 172 the approximate frexpr gave raw 165
    # (80.75).
    assert cell["numeric_value"] == pytest.approx(80.065, abs=0.001)
    assert cell["value"] == "80"
    read = _read(bench, FAN_ECT)
    assert read["numeric_values"][0] == cell["numeric_value"]
    assert read["values"][0] == cell["value"]


def test_launch_rpm_plain_number_text(bench):
    assert _edit(bench, LAUNCH_RPM, "4500")["success"] is True
    assert _bytes(bench, LAUNCH_ADDR) == struct.pack(">f", 4500.0)


def test_unchanged_cells_are_skipped_in_a_batch(bench):
    read = _read(bench, FAN_ECT)
    cells = _whole_table_cells(read, "values")
    cells[3]["value"] = "70"
    result = _edit_cells(bench, FAN_ECT, cells)
    assert result["success"] is True
    assert result["cells_modified"] == 1
    assert [c["changed"] for c in result["cells"]] == [i == 3 for i in range(8)]
    # Only cell 3's two bytes changed.
    after = _bytes(bench, FAN_ADDR, 16)
    stock = bytes.fromhex("00ba" * 8)
    diff = [i for i in range(16) if after[i] != stock[i]]
    assert diff and all(i in (6, 7) for i in diff)


# --------------------------------------------------------------------------
# Refused input: nothing is written
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value", ["xyz", "", "-5", "1.5", "0x", "2054517g", True, [1], {"v": 1}]
)
def test_hex_table_rejects_garbage(bench, value):
    before = bytes(bench.reader.rom_data)
    result = _edit(bench, bench.tire.name, value)
    assert result["success"] is False
    assert "hex" in result["error"]
    _assert_untouched(bench, before)


@pytest.mark.parametrize(
    "value", ["abc", "nan", "inf", "1_0", "0x10", "", False, "٣.5"]
)
def test_decimal_table_rejects_garbage(bench, value):
    before = bytes(bench.reader.rom_data)
    result = _edit(bench, COIL_DWELL, value)
    assert result["success"] is False
    assert "Invalid value" in result["error"]
    _assert_untouched(bench, before)


@pytest.mark.parametrize("value", ["4,500", "4,5", "4.500,0"])
def test_comma_text_is_refused(bench, value):
    """'4,500' used to be refused by float(); it must not become 4.5."""
    before = bytes(bench.reader.rom_data)
    result = _edit(bench, LAUNCH_RPM, value)
    assert result["success"] is False
    assert "comma" in result["error"]
    _assert_untouched(bench, before)


@pytest.mark.parametrize("table", [BLEND, FAN_ECT])
@pytest.mark.parametrize("value", [1.5, "1.5", 0.25])
def test_whole_number_tables_refuse_fractions(bench, table, value):
    before = bytes(bench.reader.rom_data)
    result = _edit(bench, table, value)
    assert result["success"] is False
    assert "whole number" in result["error"]
    _assert_untouched(bench, before)


def test_whole_number_table_takes_whole_number(bench):
    assert _edit(bench, BLEND, 1)["success"] is True
    assert _bytes(bench, 0xF3EA4, 1) == b"\x01"


def test_hex_value_too_large_for_storage_is_refused(bench):
    before = bytes(bench.reader.rom_data)
    result = _edit(bench, bench.tire.name, "1ffffffff")  # 33 bits into uint32
    assert result["success"] is False
    assert "cell (0,0)" in result["error"] and "uint32" in result["error"]
    _assert_untouched(bench, before)


def test_fan_batch_with_overflowing_last_cell_writes_nothing(bench):
    """Reviewer repro: the last cell (300 -> negative raw into uint16) used to
    fail mid-write, and the lossy rollback left 8 bytes changed, untracked."""
    before = bytes(bench.reader.rom_data)
    cells = [{"row": r, "col": 0, "value": 70} for r in range(7)]
    cells.append({"row": 7, "col": 0, "value": 300})
    result = _edit_cells(bench, FAN_ECT, cells)
    assert result["success"] is False
    assert "cell (7,0)" in result["error"] and "uint16" in result["error"]
    _assert_untouched(bench, before)


def test_write_failure_restores_rom_bytes_exactly(bench, monkeypatch):
    """Defence in depth: if the writer still fails mid-batch, the ROM is
    restored from a byte snapshot (the lossy fan scaling would otherwise
    not round-trip through the from_display rollback)."""
    before = bytes(bench.reader.rom_data)
    real_write = bench.reader.write_cell_value

    def flaky_write(table, row, col, raw):
        if row == 7 and raw != 186:  # forward write of the last cell fails
            raise RomWriteError("simulated failure")
        real_write(table, row, col, raw)

    monkeypatch.setattr(bench.reader, "write_cell_value", flaky_write)
    cells = [{"row": r, "col": 0, "value": 70} for r in range(8)]
    result = _edit_cells(bench, FAN_ECT, cells)
    assert result["success"] is False
    assert "no cells written" in result["error"]
    _assert_untouched(bench, before)


def test_one_bad_cell_rejects_the_whole_batch(bench):
    """A batch is never half-applied: the good cell is not written either."""
    before = bytes(bench.reader.rom_data)
    result = _edit_cells(
        bench,
        FAN_ECT,
        [{"row": 0, "col": 0, "value": 70}, {"row": 1, "col": 0, "value": "x"}],
    )
    assert result["success"] is False
    assert result["error"].startswith("No cells written")
    _assert_untouched(bench, before)


@pytest.mark.parametrize(
    "cells",
    [
        [{"row": 0, "col": 0, "value": 70}, {"row": 0, "col": 0, "value": 71}],
        [{"row": "0", "col": 0, "value": 70}],
        [{"row": 0.0, "col": 0, "value": 70}],
        [{"row": 8, "col": 0, "value": 70}],
        ["not a cell"],
    ],
)
def test_bad_cell_addresses_are_refused(bench, cells):
    before = bytes(bench.reader.rom_data)
    assert _edit_cells(bench, FAN_ECT, cells)["success"] is False
    _assert_untouched(bench, before)


THROTTLE_MULT = "Throttle Angle - Conversion Multiplier (pair with f7c7c)"
DWELL_3D = "Ignition Coil Dwell | Speed Limiter Inactive"  # 15x9


@pytest.mark.parametrize(
    "table, cell, needle",
    [
        # Misspelled key: used to write row 0 and report success.
        (FAN_ECT, {"Row": 5, "col": 0, "value": 90}, "unknown key(s) Row"),
        (FAN_ECT, {"row": 5, "col": 0, "value": 90, "note": 1}, "unknown key"),
        (FAN_ECT, {"col": 0, "value": 90}, "missing row"),
        (DWELL_3D, {"row": 2, "value": 3.0}, "missing col"),
        (DWELL_3D, {"value": 3.0}, "missing row and col"),
    ],
)
def test_cell_keys_must_be_exact(bench, table, cell, needle):
    before = bytes(bench.reader.rom_data)
    good = {"row": 1, "col": 0, "value": 80}
    result = _edit_cells(bench, table, [good, cell])
    assert result["success"] is False
    assert needle in result["error"]
    assert "Cell #1" in result["error"]
    _assert_untouched(bench, before)


def test_single_row_table_needs_no_row_or_col(bench):
    result = _edit_cells(bench, bench.tire.name, [{"value": "2054517"}])
    assert result["success"] is True
    assert _bytes(bench, TIRE_ADDR) == bytes.fromhex("02054517")


def test_two_d_table_needs_no_col(bench):
    result = _edit_cells(bench, FAN_ECT, [{"row": 2, "value": 80}])
    assert result["success"] is True
    assert result["cells"][0]["row"] == 2


@pytest.mark.parametrize("value", [0.85, "0.85", "0.850"])
def test_value_packing_to_same_bytes_is_unchanged(bench, value):
    """float32 stores 0.85 as 0.8500000238: sending 0.85 changes no byte, so
    it is no edit (no write, no undo entry, no modified flag)."""
    before = bytes(bench.reader.rom_data)
    result = _edit(bench, THROTTLE_MULT, value)
    assert result["success"] is True
    assert result["cells_modified"] == 0
    assert result["cells"][0]["changed"] is False
    _assert_untouched(bench, before)


def test_cell_write_changes_bytes_matches_writer(bench):
    table = bench.definition.get_table_by_name(THROTTLE_MULT)
    assert bench.reader.cell_write_changes_bytes(table, 0, 0, 0.85) is False
    assert bench.reader.cell_write_changes_bytes(table, 0, 0, 0.86) is True
    fan = bench.definition.get_table_by_name(FAN_ECT)
    assert bench.reader.cell_write_changes_bytes(fan, 0, 0, 186.4) is False
    assert bench.reader.cell_write_changes_bytes(fan, 0, 0, 186.6) is True
    with pytest.raises(RomWriteError):
        bench.reader.cell_write_changes_bytes(fan, 0, 0, -1)


# --------------------------------------------------------------------------
# Pure parsing rule
# --------------------------------------------------------------------------


def test_parse_api_value_rule():
    assert value_format_name("08x") == "hex"
    assert value_format_name(".2f") == "decimal"
    assert parse_api_value(10.5, ".2f") == 10.5
    assert parse_api_value("10", "08x") == 16.0
    assert parse_api_value("10", ".2f") == 10.0
    for bad in (None, True, False, float("nan"), float("inf"), 10**400, [1]):
        with pytest.raises(ValueError):
            parse_api_value(bad, ".2f")
    with pytest.raises(ValueError, match="hex text"):
        parse_api_value(10, "08x")
    with pytest.raises(ValueError, match="comma"):
        parse_api_value("1,5", ".2f")


def test_unicode_digits_are_refused():
    """float() accepts Arabic-Indic digits; a ROM value must not."""
    assert parse_numeric_text("١٢") is None
    assert parse_numeric_text("12") == 12.0
    assert not math.isnan(parse_numeric_text("1e3"))


# --------------------------------------------------------------------------
# Set Value dialog on hex tables (table window)
# --------------------------------------------------------------------------


def test_set_value_dialog_hex_parses_hex_text():
    from src.ui.data_operation_dialogs import SetValueDialog

    dialog = SetValueDialog(1, None, format_spec="8x")
    validator = dialog.value_edit.validator()
    assert validator.validate("205451a", 0)[0] == validator.State.Acceptable
    dialog.value_edit.setText("2054517")
    dialog._on_accept()
    assert dialog.result() == QDialog.Accepted
    assert dialog.get_value() == float(0x02054517)


def test_set_value_dialog_hex_refuses_garbage():
    from src.ui.data_operation_dialogs import SetValueDialog

    dialog = SetValueDialog(1, None, format_spec="8x")
    dialog.value_edit.setText("")
    dialog._on_accept()
    assert dialog.result() != QDialog.Accepted


def test_set_value_dialog_decimal_unchanged():
    from src.ui.data_operation_dialogs import SetValueDialog

    dialog = SetValueDialog(1, None, format_spec=".2f")
    dialog.value_edit.setText("2054517")
    dialog._on_accept()
    assert dialog.get_value() == 2054517.0


def _ops_with_selection(formats):
    """A TableOperationsHelper whose selection covers cells of ``formats``."""
    from src.ui.table_viewer_helpers.operations import TableOperationsHelper

    class _Range:
        def topRow(self):
            return 0

        def bottomRow(self):
            return 0

        def leftColumn(self):
            return 0

        def rightColumn(self):
            return len(formats) - 1

    items = [MagicMock(**{"data.return_value": (0, c)}) for c in range(len(formats))]
    table_widget = MagicMock()
    table_widget.selectedRanges.return_value = [_Range()]
    table_widget.item.side_effect = lambda r, c: items[c]
    ctx = SimpleNamespace(
        current_table=object(), table_widget=table_widget, viewer=None
    )
    display = MagicMock()
    display.get_cell_format.side_effect = lambda coords: formats[coords[1]]
    ops = TableOperationsHelper(ctx, display, MagicMock())
    ops.apply_bulk_operation = MagicMock(return_value=([], []))
    return ops


def test_set_value_on_hex_table_opens_hex_dialog(monkeypatch):
    import src.ui.table_viewer_helpers.operations as ops_mod

    seen = {}

    class FakeDialog:
        def __init__(self, count, parent, format_spec=""):
            seen["format_spec"] = format_spec

        def exec(self):
            return QDialog.Accepted

        def get_value(self):
            return float(0x02054517)

    monkeypatch.setattr(ops_mod, "SetValueDialog", FakeDialog)
    ops = _ops_with_selection(["8x"])
    ops.set_value_selection()

    assert seen["format_spec"] == "8x"
    fn, label = ops.apply_bulk_operation.call_args[0]
    assert fn(0.0) == float(0x02054517)
    assert label == "Set to 0x2054517"


def test_set_value_refuses_mixed_hex_and_decimal_selection(monkeypatch):
    import src.ui.table_viewer_helpers.operations as ops_mod

    info = MagicMock()
    monkeypatch.setattr(ops_mod.QMessageBox, "information", info)
    monkeypatch.setattr(
        ops_mod, "SetValueDialog", MagicMock(side_effect=AssertionError)
    )
    ops = _ops_with_selection(["8x", ".2f"])
    ops.set_value_selection()
    info.assert_called_once()
    ops.apply_bulk_operation.assert_not_called()


def test_add_dialog_notes_decimal_operand_on_hex_table():
    from PySide6.QtWidgets import QLabel

    from src.ui.data_operation_dialogs import (
        HEX_OPERAND_NOTE,
        AddValueDialog,
        MultiplyDialog,
    )

    for cls in (AddValueDialog, MultiplyDialog):
        hex_dialog = cls(None, hex_table=True)
        assert HEX_OPERAND_NOTE in [w.text() for w in hex_dialog.findChildren(QLabel)]
        plain_dialog = cls(None)
        texts = [w.text() for w in plain_dialog.findChildren(QLabel)]
        assert HEX_OPERAND_NOTE not in texts
