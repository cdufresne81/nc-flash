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
from src.core.exceptions import RomReadError, RomWriteError


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


# --- Helper to create a reader from a ROM + table ---
def _make_reader(rom, m, n, scaling_name="u8", storagetype="uint8"):
    scaling = make_scaling(name=scaling_name, storagetype=storagetype)
    definition = RomDefinition(
        romid=make_romid(),
        scalings={scaling_name: scaling},
        tables=[make_interleaved_table("test", "100", m, n, scaling_name=scaling_name)],
    )
    reader = RomReader.__new__(RomReader)
    reader.rom_data = rom
    reader.definition = definition
    reader.rom_path = "test.bin"
    return reader, definition.tables[0]


class TestInterleavedReadValidation:
    """Test bounds checking when reading interleaved 3D tables (#57)."""

    def test_read_m_zero_raises(self):
        """M=0 should raise RomReadError."""
        rom = bytearray(512)
        rom[0x100] = 0  # M = 0
        rom[0x101] = 3  # N = 3
        reader, table = _make_reader(rom, 0, 3)
        # Fix elements to match M*N=0
        table._elements_override = 0
        with pytest.raises(RomReadError, match="M=0"):
            reader.read_table_data(table)

    def test_read_n_zero_raises(self):
        """N=0 should raise RomReadError."""
        rom = bytearray(512)
        rom[0x100] = 3  # M = 3
        rom[0x101] = 0  # N = 0
        reader, table = _make_reader(rom, 3, 0)
        with pytest.raises(RomReadError, match="N=0"):
            reader.read_table_data(table)

    def test_read_exceeds_rom_raises(self):
        """M=255 on a small ROM should raise RomReadError."""
        # ROM only 300 bytes, but M=255, N=3 needs 2+255+3*256 = 1025 bytes from base
        rom = bytearray(300)
        rom[0x100] = 255  # M = 255
        rom[0x101] = 3  # N = 3
        reader, table = _make_reader(rom, 255, 3)
        with pytest.raises(RomReadError, match="exceeds ROM bounds"):
            reader.read_table_data(table)

    def test_read_valid_3x3_no_error(self):
        """A well-formed 3x3 table should read without error (regression guard)."""
        rom = build_interleaved_rom(
            3, 3, [10, 20, 30], [40, 80, 120], [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        )
        reader, table = _make_reader(rom, 3, 3)
        result = reader.read_table_data(table)
        assert result["values"].shape == (3, 3)


class TestInterleavedWriteValidation:
    """Test bounds checking when writing interleaved 3D tables (#58)."""

    def test_write_table_data_exceeds_rom(self):
        """Write to a ROM that's 1 byte too short should raise RomWriteError."""
        m, n = 3, 3
        # Build a correct ROM then truncate by 1 byte
        rom = build_interleaved_rom(
            m, n, [10, 20, 30], [40, 80, 120], [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        )
        rom = bytearray(rom[:-65])  # Remove the 64-byte padding + 1 more
        reader, table = _make_reader(rom, m, n)
        new_values = np.array([[10, 20, 30], [40, 50, 60], [70, 80, 90]], dtype=float)
        with pytest.raises(RomWriteError, match="exceeds ROM bounds"):
            reader.write_table_data(table, new_values)

    def test_write_multibyte_interleaved_raises(self):
        """uint16 interleaved should raise because stride is too small."""
        m, n = 3, 3
        rom = build_interleaved_rom(
            m, n, [10, 20, 30], [40, 80, 120], [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        )
        reader, table = _make_reader(
            rom, m, n, scaling_name="u16", storagetype="uint16"
        )
        new_values = np.array([[10, 20, 30], [40, 50, 60], [70, 80, 90]], dtype=float)
        with pytest.raises(RomWriteError, match="stride"):
            reader.write_table_data(table, new_values)

    def test_write_normal_interleaved_succeeds(self):
        """Standard uint8 interleaved write should still work (regression)."""
        m, n = 3, 3
        rom = build_interleaved_rom(
            m, n, [10, 20, 30], [40, 80, 120], [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        )
        reader, table = _make_reader(rom, m, n)
        new_values = np.array([[10, 20, 30], [40, 50, 60], [70, 80, 90]], dtype=float)
        reader.write_table_data(table, new_values)
        result = reader.read_table_data(table)
        np.testing.assert_array_equal(result["values"], new_values)


class TestCellIndexValidation:
    """Test cell and axis index validation (#60)."""

    def test_write_cell_row_too_large(self):
        """Row beyond table dimensions should raise."""
        rom = build_interleaved_rom(
            3, 3, [10, 20, 30], [40, 80, 120], [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        )
        reader, table = _make_reader(rom, 3, 3)
        with pytest.raises(RomWriteError, match="Row index 5 out of range"):
            reader.write_cell_value(table, row=5, col=0, raw_value=1)

    def test_write_cell_col_too_large(self):
        """Column beyond table dimensions should raise."""
        rom = build_interleaved_rom(
            3, 3, [10, 20, 30], [40, 80, 120], [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        )
        reader, table = _make_reader(rom, 3, 3)
        with pytest.raises(RomWriteError, match="Column index 5 out of range"):
            reader.write_cell_value(table, row=0, col=5, raw_value=1)

    def test_write_cell_negative_row(self):
        """Negative row should raise."""
        rom = build_interleaved_rom(
            3, 3, [10, 20, 30], [40, 80, 120], [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        )
        reader, table = _make_reader(rom, 3, 3)
        with pytest.raises(RomWriteError, match="Row index -1 out of range"):
            reader.write_cell_value(table, row=-1, col=0, raw_value=1)

    def test_write_cell_valid_indices(self):
        """Valid indices should work (regression)."""
        rom = build_interleaved_rom(
            3, 3, [10, 20, 30], [40, 80, 120], [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        )
        reader, table = _make_reader(rom, 3, 3)
        reader.write_cell_value(table, row=2, col=2, raw_value=99)
        result = reader.read_table_data(table)
        assert result["values"][2, 2] == 99

    def test_write_axis_index_too_large(self):
        """Axis index beyond length should raise."""
        rom = build_interleaved_rom(
            3, 3, [10, 20, 30], [40, 80, 120], [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        )
        reader, table = _make_reader(rom, 3, 3)
        with pytest.raises(RomWriteError, match="Axis index 5 out of range"):
            reader.write_axis_value(table, "y_axis", 5, raw_value=1)

    def test_write_axis_negative_index(self):
        """Negative axis index should raise."""
        rom = build_interleaved_rom(
            3, 3, [10, 20, 30], [40, 80, 120], [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        )
        reader, table = _make_reader(rom, 3, 3)
        with pytest.raises(RomWriteError, match="Axis index -1 out of range"):
            reader.write_axis_value(table, "x_axis", -1, raw_value=1)


class TestIntegerOverflowValidation:
    """Test integer overflow validation in struct.pack (#59)."""

    def test_write_cell_uint8_overflow(self):
        """Value 256 in uint8 cell should raise RomWriteError."""
        rom = build_interleaved_rom(
            3, 3, [10, 20, 30], [40, 80, 120], [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        )
        reader, table = _make_reader(rom, 3, 3)
        with pytest.raises(RomWriteError, match="out of range"):
            reader.write_cell_value(table, row=0, col=0, raw_value=256)

    def test_write_cell_int8_underflow(self):
        """Value -129 in int8 cell should raise RomWriteError."""
        rom = build_interleaved_rom(
            3, 3, [10, 20, 30], [40, 80, 120], [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        )
        reader, table = _make_reader(rom, 3, 3, scaling_name="i8", storagetype="int8")
        with pytest.raises(RomWriteError, match="out of range"):
            reader.write_cell_value(table, row=0, col=0, raw_value=-129)

    def test_write_cell_valid_uint8(self):
        """Value 200 in uint8 should succeed."""
        rom = build_interleaved_rom(
            3, 3, [10, 20, 30], [40, 80, 120], [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        )
        reader, table = _make_reader(rom, 3, 3)
        reader.write_cell_value(table, row=0, col=0, raw_value=200)
        result = reader.read_table_data(table)
        assert result["values"][0, 0] == 200

    def test_write_axis_uint8_overflow(self):
        """Value 300 in uint8 axis should raise RomWriteError."""
        rom = build_interleaved_rom(
            3, 3, [10, 20, 30], [40, 80, 120], [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        )
        reader, table = _make_reader(rom, 3, 3)
        with pytest.raises(RomWriteError, match="out of range"):
            reader.write_axis_value(table, "x_axis", 0, raw_value=300)

    def test_write_table_data_uint8_overflow(self):
        """Bulk write with one value out of range should raise RomWriteError."""
        rom = build_interleaved_rom(
            3, 3, [10, 20, 30], [40, 80, 120], [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        )
        reader, table = _make_reader(rom, 3, 3)
        bad_values = np.array([[1, 2, 3], [4, 500, 6], [7, 8, 9]], dtype=float)
        with pytest.raises(RomWriteError, match="out of range"):
            reader.write_table_data(table, bad_values)
