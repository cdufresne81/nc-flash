"""
Tests for interleaved 3D table support (TCM-style layout).

Format: [M][N][X_axis: M bytes][Y0 D00..D0M-1][Y1 D10..D1M-1]...
Each row is (M+1) bytes: 1 Y-axis byte followed by M data bytes.
"""

import numpy as np
import pytest
import struct

from src.core.rom_definition import (
    RomDefinition,
    RomID,
    Scaling,
    Table,
    TableType,
    AxisType,
    TableLayout,
)
from src.core.rom_reader import RomReader, ScalingConverter


def make_romid():
    return RomID(
        xmlid="TEST",
        internalidaddress="0",
        internalidstring="TEST",
        ecuid="TEST",
        make="Test",
        model="Test",
        flashmethod="",
        memmodel="SH7055",
        checksummodule="",
    )


def make_scaling(name="u8", storagetype="uint8"):
    return Scaling(
        name=name,
        units="",
        toexpr="x",
        frexpr="x",
        format="%d",
        min=0,
        max=255,
        inc=1,
        storagetype=storagetype,
        endian="big",
    )


def build_interleaved_rom(m, n, x_axis, y_vals, data_grid, base_offset=0x100):
    """Build a ROM byte array containing an interleaved 3D table at base_offset."""
    rom = bytearray(base_offset + 2 + m + n * (m + 1) + 64)  # extra padding
    rom[base_offset] = m
    rom[base_offset + 1] = n
    for i, v in enumerate(x_axis):
        rom[base_offset + 2 + i] = v
    row_start = base_offset + 2 + m
    stride = m + 1
    for r in range(n):
        rom[row_start + r * stride] = y_vals[r]
        for c in range(m):
            rom[row_start + r * stride + 1 + c] = data_grid[r][c]
    return rom


def make_interleaved_table(name, base_hex, m, n, scaling_name="u8"):
    """Create a Table definition for an interleaved 3D table."""
    base = int(base_hex, 16)
    x_axis_child = Table(
        name="X Axis",
        address=hex(base + 2)[2:],
        elements=m,
        scaling=scaling_name,
        type=TableType.ONE_D,
        axis_type=AxisType.X_AXIS,
    )
    y_axis_child = Table(
        name="Y Axis",
        address=hex(base + 2 + m)[2:],
        elements=n,
        scaling=scaling_name,
        type=TableType.ONE_D,
        axis_type=AxisType.Y_AXIS,
    )
    table = Table(
        name=name,
        address=base_hex,
        elements=m * n,
        scaling=scaling_name,
        type=TableType.THREE_D,
        layout=TableLayout.INTERLEAVED,
        children=[x_axis_child, y_axis_child],
    )
    return table


class TestInterleavedRead:
    """Test reading interleaved 3D tables."""

    def test_read_3x3(self):
        """Test reading a small 3x3 interleaved table."""
        m, n = 3, 3
        x_axis = [10, 20, 30]
        y_vals = [40, 80, 120]
        data_grid = [
            [1, 2, 3],
            [4, 5, 6],
            [7, 8, 9],
        ]
        rom = build_interleaved_rom(m, n, x_axis, y_vals, data_grid)

        scaling = make_scaling()
        definition = RomDefinition(
            romid=make_romid(),
            scalings={"u8": scaling},
            tables=[make_interleaved_table("test_3x3", "100", m, n)],
        )

        reader = RomReader.__new__(RomReader)
        reader.rom_data = rom
        reader.definition = definition
        reader.rom_path = "test.bin"

        table = definition.tables[0]
        result = reader.read_table_data(table)

        assert result is not None
        assert "values" in result
        assert "x_axis" in result
        assert "y_axis" in result

        np.testing.assert_array_equal(result["x_axis"], [10, 20, 30])
        np.testing.assert_array_equal(result["y_axis"], [40, 80, 120])
        assert result["values"].shape == (3, 3)
        np.testing.assert_array_equal(result["values"][0], [1, 2, 3])
        np.testing.assert_array_equal(result["values"][1], [4, 5, 6])
        np.testing.assert_array_equal(result["values"][2], [7, 8, 9])

    def test_read_8x8(self):
        """Test reading an 8x8 interleaved table (real TCM size)."""
        m, n = 8, 8
        x_axis = [28, 38, 50, 55, 60, 65, 70, 75]
        y_vals = [40, 80, 120, 160, 200, 216, 240, 255]
        data_grid = [[r * 10 + c for c in range(8)] for r in range(8)]
        rom = build_interleaved_rom(m, n, x_axis, y_vals, data_grid)

        scaling = make_scaling()
        definition = RomDefinition(
            romid=make_romid(),
            scalings={"u8": scaling},
            tables=[make_interleaved_table("test_8x8", "100", m, n)],
        )

        reader = RomReader.__new__(RomReader)
        reader.rom_data = rom
        reader.definition = definition
        reader.rom_path = "test.bin"

        table = definition.tables[0]
        result = reader.read_table_data(table)

        np.testing.assert_array_equal(result["x_axis"], x_axis)
        np.testing.assert_array_equal(result["y_axis"], y_vals)
        assert result["values"].shape == (8, 8)
        # Check specific cells
        assert result["values"][0, 0] == 0  # row 0, col 0
        assert result["values"][0, 7] == 7  # row 0, col 7
        assert result["values"][7, 0] == 70  # row 7, col 0
        assert result["values"][7, 7] == 77  # row 7, col 7


class TestInterleavedWrite:
    """Test writing interleaved 3D tables."""

    def test_write_cell_roundtrip(self):
        """Test that writing a single cell and re-reading gives the same value."""
        m, n = 3, 3
        x_axis = [10, 20, 30]
        y_vals = [40, 80, 120]
        data_grid = [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        rom = build_interleaved_rom(m, n, x_axis, y_vals, data_grid)

        scaling = make_scaling()
        definition = RomDefinition(
            romid=make_romid(),
            scalings={"u8": scaling},
            tables=[make_interleaved_table("test", "100", m, n)],
        )

        reader = RomReader.__new__(RomReader)
        reader.rom_data = rom
        reader.definition = definition
        reader.rom_path = "test.bin"

        table = definition.tables[0]

        # Write value 99 to row 1, col 2
        reader.write_cell_value(table, row=1, col=2, raw_value=99)

        # Re-read and verify
        result = reader.read_table_data(table)
        assert result["values"][1, 2] == 99
        # Other cells unchanged
        assert result["values"][0, 0] == 1
        assert result["values"][2, 2] == 9

    def test_write_table_data_roundtrip(self):
        """Test bulk write of all table data."""
        m, n = 3, 3
        x_axis = [10, 20, 30]
        y_vals = [40, 80, 120]
        data_grid = [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        rom = build_interleaved_rom(m, n, x_axis, y_vals, data_grid)

        scaling = make_scaling()
        definition = RomDefinition(
            romid=make_romid(),
            scalings={"u8": scaling},
            tables=[make_interleaved_table("test", "100", m, n)],
        )

        reader = RomReader.__new__(RomReader)
        reader.rom_data = rom
        reader.definition = definition
        reader.rom_path = "test.bin"

        table = definition.tables[0]

        # Write new values
        new_values = np.array([[10, 20, 30], [40, 50, 60], [70, 80, 90]], dtype=float)
        reader.write_table_data(table, new_values)

        # Re-read and verify
        result = reader.read_table_data(table)
        np.testing.assert_array_equal(result["values"], new_values)
        # Y axis should be unchanged
        np.testing.assert_array_equal(result["y_axis"], y_vals)

    def test_write_y_axis_value(self):
        """Test writing a Y axis value at interleaved offset."""
        m, n = 3, 3
        x_axis = [10, 20, 30]
        y_vals = [40, 80, 120]
        data_grid = [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        rom = build_interleaved_rom(m, n, x_axis, y_vals, data_grid)

        scaling = make_scaling()
        definition = RomDefinition(
            romid=make_romid(),
            scalings={"u8": scaling},
            tables=[make_interleaved_table("test", "100", m, n)],
        )

        reader = RomReader.__new__(RomReader)
        reader.rom_data = rom
        reader.definition = definition
        reader.rom_path = "test.bin"

        table = definition.tables[0]

        # Write Y axis value at index 1 (was 80, change to 90)
        reader.write_axis_value(table, "y_axis", 1, 90)

        # Re-read and verify Y axis changed
        result = reader.read_table_data(table)
        assert result["y_axis"][1] == 90
        # Data should be unchanged
        np.testing.assert_array_equal(result["values"][1], [4, 5, 6])


class TestContiguousUnchanged:
    """Verify contiguous tables still work identically."""

    def test_contiguous_table_has_default_layout(self):
        """Tables without layout attribute should default to contiguous."""
        table = Table(
            name="test",
            address="1000",
            elements=10,
            scaling="u8",
            type=TableType.TWO_D,
        )
        assert table.layout == TableLayout.CONTIGUOUS

    def test_interleaved_enum_values(self):
        """Check enum string values match XML attribute values."""
        assert TableLayout.CONTIGUOUS.value == "contiguous"
        assert TableLayout.INTERLEAVED.value == "interleaved"
