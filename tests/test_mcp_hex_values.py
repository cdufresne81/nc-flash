"""MCP/API table edits honor the table's display format (#146).

Tire Size Correction (and the CEL Flash Pattern settings) display as hex
('%08x'). The read endpoint returned that hex text without saying so, and the
edit endpoint parsed values with float(), i.e. as decimal: reading
' 2054517' and writing it back stored 0x001F5975 instead of 0x02054517.

These tests drive the real read/edit handlers against the sample ROM and its
definition, through a JSON round trip like the HTTP bridge, and check the
bytes that land in the ROM.
"""

import json
import shutil
import struct
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QApplication

from main import MainWindow
from src.core.definition_parser import load_definition
from src.core.rom_reader import RomReader
from src.ui.mcp_mixin import McpMixin
from src.utils.formatting import parse_api_value, value_format_name

_app = QApplication.instance() or QApplication([])

TIRE_ADDR = 0xF3E84
COIL_DWELL = "Ignition Coil Dwell | Speed Limiter Active "  # 1D float, %0.2f
COIL_ADDR = 0xBB8CC


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
    fake._write_to_rom_and_mark_modified = (
        lambda *a, **k: MainWindow._write_to_rom_and_mark_modified(fake, *a, **k)
    )
    fake._apply_external_cell_edits = (
        lambda *a, **k: MainWindow._apply_external_cell_edits(fake, *a, **k)
    )
    fake._rollback_failed_cell_writes = (
        lambda *a, **k: MainWindow._rollback_failed_cell_writes(fake, *a, **k)
    )
    tire = next(
        t
        for t in definition.tables
        if t.name.startswith("Tire Size Correction") and t.address_int == TIRE_ADDR
    )
    return SimpleNamespace(fake=fake, reader=reader, rom=str(rom_copy), tire=tire)


def _wire(result):
    """Simulate the HTTP/JSON bridge."""
    return json.loads(json.dumps(result))


def _read(bench, table_name):
    req = {"rom_path": bench.rom, "table_name": table_name}
    return _wire(McpMixin._api_read_table(bench.fake, req))


def _edit(bench, table_name, value):
    req = _wire(
        {
            "rom_path": bench.rom,
            "table_name": table_name,
            "cells": [{"row": 0, "col": 0, "value": value}],
        }
    )
    return _wire(McpMixin._api_edit_table(bench.fake, req))


def _bytes(bench, addr, n=4):
    return bytes(bench.reader.rom_data[addr : addr + n])


def _set_tire(bench, raw: bytes):
    bench.reader.rom_data[TIRE_ADDR : TIRE_ADDR + 4] = raw


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


# --------------------------------------------------------------------------
# Round trip: read then write the same value leaves the bytes unchanged
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["02054517", "001f5975", "ffffffff", "0000000a"])
def test_tire_size_text_round_trip_is_byte_identical(bench, raw):
    _set_tire(bench, bytes.fromhex(raw))
    before = bytes(bench.reader.rom_data)

    text = _read(bench, bench.tire.name)["values"][0]
    result = _edit(bench, bench.tire.name, text)

    assert result["success"] is True, result
    assert bytes(bench.reader.rom_data) == before


def test_tire_size_numeric_round_trip_is_byte_identical(bench):
    _set_tire(bench, bytes.fromhex("02054517"))
    before = bytes(bench.reader.rom_data)

    number = _read(bench, bench.tire.name)["numeric_values"][0]
    assert _edit(bench, bench.tire.name, number)["success"] is True
    assert bytes(bench.reader.rom_data) == before


def test_decimal_table_numeric_round_trip_is_byte_identical(bench):
    """Full-precision numbers round-trip a float table exactly (the '%0.2f'
    display text alone would round it)."""
    before = bytes(bench.reader.rom_data)
    number = _read(bench, COIL_DWELL)["numeric_values"][0]
    assert _edit(bench, COIL_DWELL, number)["success"] is True
    assert bytes(bench.reader.rom_data) == before


# --------------------------------------------------------------------------
# Writes land the right bytes
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value", ["2054517", " 2054517", "02054517", "0x2054517", "0X02054517"]
)
def test_hex_text_edit_lands_hex_bytes(bench, value):
    assert _edit(bench, bench.tire.name, value)["success"] is True
    assert _bytes(bench, TIRE_ADDR) == bytes.fromhex("02054517")


def test_json_number_on_hex_table_is_the_numeric_value(bench):
    assert _edit(bench, bench.tire.name, 0x02054517)["success"] is True
    assert _bytes(bench, TIRE_ADDR) == bytes.fromhex("02054517")


@pytest.mark.parametrize("value", [3.5, "3.5", "3,5", 3])
def test_decimal_table_edit_still_works(bench, value):
    assert _edit(bench, COIL_DWELL, value)["success"] is True
    assert _bytes(bench, COIL_ADDR) == struct.pack(
        ">f", float(str(value).replace(",", "."))
    )


# --------------------------------------------------------------------------
# Garbage is rejected, nothing is written
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value", ["xyz", "", "-5", "1.5", "0x", "2054517g", True, [1], {"v": 1}]
)
def test_hex_table_rejects_garbage(bench, value):
    before = bytes(bench.reader.rom_data)
    result = _edit(bench, bench.tire.name, value)
    assert result["success"] is False
    assert "hex" in result["error"]
    assert bytes(bench.reader.rom_data) == before


@pytest.mark.parametrize("value", ["abc", "nan", "inf", "1_0", "0x10", "", False])
def test_decimal_table_rejects_garbage(bench, value):
    before = bytes(bench.reader.rom_data)
    result = _edit(bench, COIL_DWELL, value)
    assert result["success"] is False
    assert "Invalid value" in result["error"]
    assert bytes(bench.reader.rom_data) == before


def test_hex_value_too_large_for_storage_is_rejected(bench):
    before = bytes(bench.reader.rom_data)
    result = _edit(bench, bench.tire.name, "1ffffffff")  # 33 bits into uint32
    assert result["success"] is False
    assert bytes(bench.reader.rom_data) == before


def test_one_bad_cell_rejects_the_whole_batch(bench):
    """A batch is never half-applied: the good cell is not written either."""
    before = bytes(bench.reader.rom_data)
    req = {
        "rom_path": bench.rom,
        "table_name": COIL_DWELL,
        "cells": [
            {"row": 0, "col": 0, "value": 4.0},
            {"row": 0, "col": 0, "value": "garbage"},
        ],
    }
    result = McpMixin._api_edit_table(bench.fake, req)
    assert result["success"] is False
    assert result["error"].startswith("No cells written")
    assert bytes(bench.reader.rom_data) == before


# --------------------------------------------------------------------------
# Pure parsing rule
# --------------------------------------------------------------------------


def test_parse_api_value_rule():
    assert value_format_name("08x") == "hex"
    assert value_format_name(".2f") == "decimal"
    # Numbers are numbers, whatever the format.
    assert parse_api_value(10, "08x") == 10.0
    assert parse_api_value(10.5, ".2f") == 10.5
    # Strings follow the format.
    assert parse_api_value("10", "08x") == 16.0
    assert parse_api_value("10", ".2f") == 10.0
    # Rejected.
    for bad in (None, True, False, float("nan"), float("inf"), 10**400, [1]):
        assert parse_api_value(bad, ".2f") is None
