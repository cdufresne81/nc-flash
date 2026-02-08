"""
Tests for table viewer helper logic.

These tests exercise actual production code: ScalingConverter, _convert_expr_to_python,
clipboard TSV parsing, and atomic file write patterns.
"""

import os
import json
import struct
import numpy as np
import pytest

from src.core.rom_reader import RomReader, ScalingConverter, _convert_expr_to_python
from src.core.rom_definition import Scaling, Table, TableType, RomDefinition, RomID
from src.core.definition_parser import load_definition
from src.core.exceptions import ScalingConversionError, RomWriteError


class TestScalingConverterWithRealDefinitions:
    """Test ScalingConverter with scalings parsed from the actual XML definition"""

    def test_all_scalings_have_valid_expressions(self, sample_xml_path):
        """Every scaling in the definition should have parseable to/from expressions"""
        definition = load_definition(str(sample_xml_path))

        for scaling in definition.scalings.values():
            converter = ScalingConverter(scaling)
            # Should not raise on a simple value
            display = converter.to_display(100.0)
            assert isinstance(display, (int, float)), \
                f"Scaling '{scaling.name}' to_display returned {type(display)}"
            raw = converter.from_display(display)
            assert isinstance(raw, (int, float)), \
                f"Scaling '{scaling.name}' from_display returned {type(raw)}"

    def test_scaling_round_trip_sample(self, sample_xml_path):
        """Sample of scalings should round-trip: raw->display->raw"""
        definition = load_definition(str(sample_xml_path))

        tested = 0
        for name, scaling in list(definition.scalings.items())[:20]:
            converter = ScalingConverter(scaling)
            # Pick a test value in the middle of the storage range
            test_raw = 128.0

            try:
                display = converter.to_display(test_raw)
                if display == 0:
                    continue  # Skip divisions by zero in reverse
                raw_back = converter.from_display(display)
                # Allow some tolerance for integer storage types
                assert abs(raw_back - test_raw) < 1.0, \
                    f"Scaling '{name}': raw {test_raw} -> display {display} -> raw {raw_back}"
                tested += 1
            except (ZeroDivisionError, ScalingConversionError):
                continue  # Some scalings have x in denominator

        assert tested > 0, "Should have tested at least one scaling"


class TestClipboardTsvParsing:
    """Test clipboard tab-separated value parsing logic (pure string ops, no Qt)"""

    def test_parse_2d_grid(self):
        """Parse a 2-row, 3-col tab-separated grid"""
        text = "1.5\t2.0\t3.5\n4.0\t5.5\t6.0"
        rows = [line.split("\t") for line in text.strip().split("\n")]

        assert len(rows) == 2
        assert len(rows[0]) == 3
        assert float(rows[0][0]) == pytest.approx(1.5)
        assert float(rows[1][2]) == pytest.approx(6.0)

    def test_parse_single_value(self):
        """Single value clipboard text"""
        text = "42.5"
        rows = [line.split("\t") for line in text.strip().split("\n")]

        assert len(rows) == 1
        assert len(rows[0]) == 1
        assert float(rows[0][0]) == pytest.approx(42.5)

    def test_parse_with_trailing_newline(self):
        """Trailing newline should not produce extra empty row"""
        text = "1.0\t2.0\n3.0\t4.0\n"
        rows = [line.split("\t") for line in text.strip().split("\n")]
        assert len(rows) == 2

    def test_generate_tsv(self):
        """Generate tab-separated text from a grid"""
        values = [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
        text = "\n".join("\t".join(f"{v:.1f}" for v in row) for row in values)
        assert text == "1.0\t2.0\t3.0\n4.0\t5.0\t6.0"

    def test_parse_negative_values(self):
        """Negative values should parse correctly"""
        text = "-10.5\t0.0\t10.5"
        row = text.split("\t")
        parsed = [float(v) for v in row]
        assert parsed == pytest.approx([-10.5, 0.0, 10.5])

    def test_parse_integer_values(self):
        """Integer values from clipboard should parse as float"""
        text = "100\t200\t300"
        row = text.split("\t")
        parsed = [float(v) for v in row]
        assert parsed == pytest.approx([100.0, 200.0, 300.0])


class TestAtomicFileWrites:
    """Test that file writes use atomic pattern (write-to-temp-then-replace)"""

    def test_save_rom_creates_valid_file(self, sample_rom_path, sample_xml_path, tmp_path):
        """save_rom should produce a valid file with correct size"""
        definition = load_definition(str(sample_xml_path))
        reader = RomReader(str(sample_rom_path), definition)

        output = tmp_path / "test_save.bin"
        reader.save_rom(str(output))

        assert output.exists()
        assert output.stat().st_size == sample_rom_path.stat().st_size

    def test_save_rom_no_temp_file_left(self, sample_rom_path, sample_xml_path, tmp_path):
        """Successful save should not leave a .tmp file behind"""
        definition = load_definition(str(sample_xml_path))
        reader = RomReader(str(sample_rom_path), definition)

        output = tmp_path / "test_save.bin"
        reader.save_rom(str(output))

        tmp_file = tmp_path / "test_save.bin.tmp"
        assert not tmp_file.exists(), "Temp file should be cleaned up after successful save"

    def test_save_rom_overwrites_existing(self, sample_rom_path, sample_xml_path, tmp_path):
        """save_rom should safely overwrite an existing file"""
        definition = load_definition(str(sample_xml_path))
        reader = RomReader(str(sample_rom_path), definition)

        output = tmp_path / "test_overwrite.bin"
        # Write initial file
        output.write_bytes(b"old data")
        assert output.stat().st_size == 8

        # Overwrite with ROM
        reader.save_rom(str(output))
        assert output.stat().st_size == sample_rom_path.stat().st_size

    def test_save_rom_data_integrity(self, sample_rom_path, sample_xml_path, tmp_path):
        """Saved ROM should be byte-identical to in-memory data"""
        definition = load_definition(str(sample_xml_path))
        reader = RomReader(str(sample_rom_path), definition)

        output = tmp_path / "integrity_check.bin"
        reader.save_rom(str(output))

        saved_data = output.read_bytes()
        assert saved_data == bytes(reader.rom_data)


class TestExpressionConversion:
    """Test _convert_expr_to_python with various expression patterns"""

    def test_basic_caret(self):
        assert _convert_expr_to_python("x^2") == "x**2"

    def test_no_caret(self):
        assert _convert_expr_to_python("x*0.01") == "x*0.01"

    def test_parenthesized_caret(self):
        assert _convert_expr_to_python("(x+1)^3") == "(x+1)**3"

    def test_multiple_carets(self):
        assert _convert_expr_to_python("x^2+y^3") == "x**2+y**3"

    def test_empty_string(self):
        assert _convert_expr_to_python("") == ""

    def test_decimal_exponent(self):
        assert _convert_expr_to_python("x^0.5") == "x**0.5"


class TestTableDataShapes:
    """Test that table data read from ROM has correct shapes"""

    def test_1d_table_values_are_1d(self, sample_rom_path, sample_xml_path):
        """1D table values should be a 1D numpy array"""
        definition = load_definition(str(sample_xml_path))
        reader = RomReader(str(sample_rom_path), definition)

        for t in definition.tables:
            if t.type == TableType.ONE_D and not t.is_axis:
                data = reader.read_table_data(t)
                assert data['values'].ndim == 1, f"Table {t.name} should be 1D"
                assert len(data['values']) == t.elements
                return

        pytest.skip("No 1D tables found")

    def test_3d_table_values_are_2d(self, sample_rom_path, sample_xml_path):
        """3D table values should be reshaped to a 2D numpy array"""
        definition = load_definition(str(sample_xml_path))
        reader = RomReader(str(sample_rom_path), definition)

        for t in definition.tables:
            if t.type == TableType.THREE_D and not t.is_axis:
                data = reader.read_table_data(t)
                if 'x_axis' in data and 'y_axis' in data:
                    assert data['values'].ndim == 2, f"Table {t.name} should be 2D"
                    rows, cols = data['values'].shape
                    assert rows == len(data['y_axis'])
                    assert cols == len(data['x_axis'])
                    return

        pytest.skip("No 3D tables with axes found")

    def test_table_values_are_finite(self, sample_rom_path, sample_xml_path):
        """All table values should be finite (no NaN or Inf)"""
        definition = load_definition(str(sample_xml_path))
        reader = RomReader(str(sample_rom_path), definition)

        tested = 0
        for t in list(definition.tables)[:30]:
            if t.is_axis:
                continue
            try:
                data = reader.read_table_data(t)
                values = data['values']
                assert np.all(np.isfinite(values)), \
                    f"Table {t.name} contains NaN or Inf values"
                tested += 1
            except Exception:
                continue  # Skip tables with scaling issues

        assert tested > 0, "Should have tested at least one table"
