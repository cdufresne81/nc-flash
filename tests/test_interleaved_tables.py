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


def _make_mismatched_reader():
    """ROM whose real 5x3 struct sits at 0x104, with the definition pointing
    at 0x100 where the bytes are NOT a [M][N] header (they read as 64x80).

    Replicates the LF9KT incident: a definition ported from another firmware
    without re-basing addresses points 4 bytes off, so arbitrary data bytes
    get interpreted as dimensions and a 5x3 table renders as an 80x64 grid.
    """
    rom = build_interleaved_rom(
        5,
        3,
        [0x30, 0x38, 0x40, 0x50, 0x60],
        [10, 50, 100],
        [[8, 8, 8, 10, 10], [16, 16, 16, 16, 16], [16, 26, 32, 32, 32]],
        base_offset=0x104,
    )
    # Bytes at the (stale) definition address 0x100: plausible data values
    # that would be misread as M=64, N=80.
    rom[0x100] = 0x40
    rom[0x101] = 0x50
    # Grow the ROM so a 64x80 footprint would fit — the mismatch check, not
    # the bounds check, must be what rejects the read.
    rom.extend(bytearray(64 * 81 + 128))
    scaling = make_scaling()
    definition = RomDefinition(
        romid=make_romid(),
        scalings={"u8": scaling},
        tables=[make_interleaved_table("test_5x3", "100", 5, 3)],
    )
    reader = RomReader.__new__(RomReader)
    reader.rom_data = rom
    reader.definition = definition
    reader.rom_path = "test.bin"
    return reader, definition.tables[0]


class TestInterleavedHeaderMismatch:
    """The ROM [M][N] header must agree with the definition's declared axes.

    Regression for the LF9KT incident: silently trusting the two bytes at a
    stale table address rendered a 5x3 table as an 80x64 grid of garbage.
    A mismatch must fail loudly on every read and write path instead.
    """

    def test_read_mismatched_header_raises(self):
        reader, table = _make_mismatched_reader()
        with pytest.raises(RomReadError, match="reads 64x80"):
            reader.read_table_data(table)

    def test_read_error_names_declared_dims(self):
        """The error must state what the definition declared, for triage."""
        reader, table = _make_mismatched_reader()
        with pytest.raises(RomReadError, match="declares 5x3"):
            reader.read_table_data(table)

    def test_read_corrected_address_succeeds(self):
        """Re-basing the address onto the real header fixes the read."""
        reader, table = _make_mismatched_reader()
        table.address = "104"
        result = reader.read_table_data(table)
        assert result["values"].shape == (3, 5)
        np.testing.assert_array_equal(result["x_axis"], [0x30, 0x38, 0x40, 0x50, 0x60])
        np.testing.assert_array_equal(result["y_axis"], [10, 50, 100])

    def test_write_table_data_mismatched_header_raises(self):
        reader, table = _make_mismatched_reader()
        values = np.zeros((3, 5), dtype=float)
        with pytest.raises(RomWriteError, match="reads 64x80"):
            reader.write_table_data(table, values)

    def test_write_cell_mismatched_header_raises(self):
        reader, table = _make_mismatched_reader()
        with pytest.raises(RomWriteError, match="reads 64x80"):
            reader.write_cell_value(table, row=0, col=0, raw_value=1)

    def test_write_axis_mismatched_header_raises(self):
        reader, table = _make_mismatched_reader()
        with pytest.raises(RomWriteError, match="reads 64x80"):
            reader.write_axis_value(table, "y_axis", 0, raw_value=1)

    def test_read_without_axis_children_uses_rom_header(self):
        """A def with no axis children stays self-describing (no validation
        possible, ROM header wins) — backward compatible."""
        rom = build_interleaved_rom(
            3, 3, [10, 20, 30], [40, 80, 120], [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        )
        scaling = make_scaling()
        table = Table(
            name="headerless",
            address="100",
            elements=9,
            scaling="u8",
            type=TableType.THREE_D,
            layout=TableLayout.INTERLEAVED,
        )
        definition = RomDefinition(
            romid=make_romid(), scalings={"u8": scaling}, tables=[table]
        )
        reader = RomReader.__new__(RomReader)
        reader.rom_data = rom
        reader.definition = definition
        reader.rom_path = "test.bin"
        result = reader.read_table_data(table)
        assert result["values"].shape == (3, 3)

    def test_read_n_only_mismatch_raises(self):
        """N-arm coverage: header 3x4 vs declared 3x3 must raise (a mutant
        dropping the N comparison survived the both-dims-wrong fixture)."""
        rom = build_interleaved_rom(
            3,
            4,
            [10, 20, 30],
            [40, 80, 120, 160],
            [[1, 2, 3], [4, 5, 6], [7, 8, 9], [10, 11, 12]],
        )
        reader, table = _make_reader(rom, 3, 3)
        with pytest.raises(RomReadError, match="reads 3x4"):
            reader.read_table_data(table)

    def test_read_m_only_mismatch_raises(self):
        """M-arm coverage: header 4x3 vs declared 3x3 must raise."""
        rom = build_interleaved_rom(
            4,
            3,
            [10, 20, 30, 40],
            [40, 80, 120],
            [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11, 12]],
        )
        reader, table = _make_reader(rom, 3, 3)
        with pytest.raises(RomReadError, match="reads 4x3"):
            reader.read_table_data(table)

    def test_write_cell_truncated_rom_raises_write_error(self):
        """Header read past ROM end on a WRITE path must raise RomWriteError
        (pins the error_cls parameterization; was a raw IndexError before)."""
        rom = bytearray(0x100 + 1)  # base=0x100, base+1 is out of range
        reader, table = _make_reader(rom, 3, 3)
        with pytest.raises(RomWriteError, match="exceeds ROM size"):
            reader.write_cell_value(table, row=0, col=0, raw_value=1)

    def test_write_mismatch_leaves_rom_unmutated(self):
        """A rejected write must not have modified any ROM byte."""
        reader, table = _make_mismatched_reader()
        snapshot = bytes(reader.rom_data)
        with pytest.raises(RomWriteError):
            reader.write_cell_value(table, row=0, col=0, raw_value=1)
        with pytest.raises(RomWriteError):
            reader.write_axis_value(table, "y_axis", 0, raw_value=1)
        with pytest.raises(RomWriteError):
            reader.write_table_data(table, np.zeros((3, 5), dtype=float))
        assert bytes(reader.rom_data) == snapshot

    def test_x_only_axis_child_still_validates(self):
        """Partial declaration (X child only) validates M and renders the
        undeclared dimension as '?' in the error message."""
        rom = build_interleaved_rom(
            3, 3, [10, 20, 30], [40, 80, 120], [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        )
        scaling = make_scaling()
        x_child = Table(
            name="X Axis",
            address="102",
            elements=5,  # deliberately wrong vs ROM header M=3
            scaling="u8",
            type=TableType.ONE_D,
            axis_type=AxisType.X_AXIS,
        )
        table = Table(
            name="x_only",
            address="100",
            elements=15,
            scaling="u8",
            type=TableType.THREE_D,
            layout=TableLayout.INTERLEAVED,
            children=[x_child],
        )
        definition = RomDefinition(
            romid=make_romid(), scalings={"u8": scaling}, tables=[table]
        )
        reader = RomReader.__new__(RomReader)
        reader.rom_data = rom
        reader.definition = definition
        reader.rom_path = "test.bin"
        with pytest.raises(RomReadError, match=r"declares 5x\?"):
            reader.read_table_data(table)

    def test_elementless_axis_children_stay_self_describing(self):
        """Axis children without an elements attribute (parsed as 0) only
        supply scaling — the table must stay self-describing, not fail a
        0-vs-header comparison."""
        rom = build_interleaved_rom(
            3, 3, [10, 20, 30], [40, 80, 120], [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        )
        reader, table = _make_reader(rom, 3, 3)
        table.x_axis.elements = 0
        table.y_axis.elements = 0
        result = reader.read_table_data(table)
        assert result["values"].shape == (3, 3)

    def test_swapxy_interleaved_rejected(self):
        """swapxy + interleaved is incoherent (read reshapes C-order, write
        flattens F-order) — refuse it outright."""
        rom = build_interleaved_rom(
            3, 3, [10, 20, 30], [40, 80, 120], [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        )
        reader, table = _make_reader(rom, 3, 3)
        table.swapxy = True
        with pytest.raises(RomReadError, match="swapxy"):
            reader.read_table_data(table)

    def test_write_x_axis_ignores_stale_child_address(self):
        """X-axis writes must derive the address from the table base, not the
        child axis address — a stale child address (LF9KT Level_5x5 scenario:
        base re-based, children not) must not redirect the write into a Y
        byte or data cell."""
        rom = build_interleaved_rom(
            3, 3, [10, 20, 30], [40, 80, 120], [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        )
        reader, table = _make_reader(rom, 3, 3)
        # Poison the child X-axis address (points at the row area instead of
        # base+2, as in a half-ported definition).
        table.x_axis.address = format(0x100 + 6, "x")
        reader.write_axis_value(table, "x_axis", 1, raw_value=99)
        result = reader.read_table_data(table)
        assert result["x_axis"][1] == 99
        np.testing.assert_array_equal(result["y_axis"], [40, 80, 120])
        np.testing.assert_array_equal(
            result["values"], [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        )
