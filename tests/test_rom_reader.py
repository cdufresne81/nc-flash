"""
Unit tests for ROM reader module
"""

import pytest
import numpy as np
from pathlib import Path
from src.core.rom_reader import RomReader, ScalingConverter
from src.core.rom_definition import Scaling, TableType
from src.core.definition_parser import load_definition
from src.core.exceptions import (
    RomFileNotFoundError,
    ScalingNotFoundError,
    ScalingConversionError,
)


class TestScalingConverter:
    """Test ScalingConverter class"""

    def test_init_with_scaling(self):
        """Test ScalingConverter initialization"""
        scaling = Scaling(
            name="test",
            units="rpm",
            toexpr="x*2",
            frexpr="x/2",
            format="%0.2f",
            min=0.0,
            max=100.0,
            inc=1.0,
            storagetype="float",
            endian="big",
        )
        converter = ScalingConverter(scaling)

        assert converter.scaling == scaling

    def test_to_display_single_value(self):
        """Test converting single raw value to display"""
        scaling = Scaling(
            name="test",
            units="",
            toexpr="x*2",
            frexpr="x/2",
            format="%0.2f",
            min=0.0,
            max=100.0,
            inc=1.0,
            storagetype="float",
            endian="big",
        )
        converter = ScalingConverter(scaling)

        result = converter.to_display(10.0)
        assert result == 20.0

    def test_to_display_array(self):
        """Test converting array of values to display"""
        scaling = Scaling(
            name="test",
            units="",
            toexpr="x+10",
            frexpr="x-10",
            format="%0.2f",
            min=0.0,
            max=100.0,
            inc=1.0,
            storagetype="float",
            endian="big",
        )
        converter = ScalingConverter(scaling)

        raw_values = np.array([0.0, 5.0, 10.0])
        result = converter.to_display(raw_values)

        np.testing.assert_array_equal(result, np.array([10.0, 15.0, 20.0]))

    def test_from_display_single_value(self):
        """Test converting single display value back to raw"""
        scaling = Scaling(
            name="test",
            units="",
            toexpr="x*2",
            frexpr="x/2",
            format="%0.2f",
            min=0.0,
            max=100.0,
            inc=1.0,
            storagetype="float",
            endian="big",
        )
        converter = ScalingConverter(scaling)

        result = converter.from_display(20.0)
        assert result == 10.0

    def test_from_display_array(self):
        """Test converting array of display values back to raw"""
        scaling = Scaling(
            name="test",
            units="",
            toexpr="x+10",
            frexpr="x-10",
            format="%0.2f",
            min=0.0,
            max=100.0,
            inc=1.0,
            storagetype="float",
            endian="big",
        )
        converter = ScalingConverter(scaling)

        display_values = np.array([10.0, 15.0, 20.0])
        result = converter.from_display(display_values)

        np.testing.assert_array_equal(result, np.array([0.0, 5.0, 10.0]))

    def test_identity_conversion(self):
        """Test conversion with identity expression (x)"""
        scaling = Scaling(
            name="test",
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
        converter = ScalingConverter(scaling)

        value = 42.0
        assert converter.to_display(value) == value
        assert converter.from_display(value) == value

    def test_caret_exponentiation_conversion(self):
        """Test that ^ is converted to ** for exponentiation (calculator-style expressions)"""
        # This expression uses ^ for power, common in ROM definition XML files
        scaling = Scaling(
            name="temp_conversion",
            units="°F",
            toexpr="(180-(1.42*x)+(0.00765*x^2)-(0.0000163*x^3))",
            frexpr="x",  # Simplified for test
            format="%0.1f",
            min=-40.0,
            max=300.0,
            inc=1.0,
            storagetype="uint8",
            endian="big",
        )
        converter = ScalingConverter(scaling)

        # Test that it doesn't raise bitwise_xor error
        # With x=100: 180 - 142 + 76.5 - 16.3 = 98.2
        result = converter.to_display(100.0)
        expected = 180 - (1.42 * 100) + (0.00765 * 100**2) - (0.0000163 * 100**3)
        assert abs(result - expected) < 0.01

    def test_caret_in_array_conversion(self):
        """Test ^ conversion works with numpy arrays"""
        scaling = Scaling(
            name="polynomial",
            units="",
            toexpr="x^2 + 2*x + 1",
            frexpr="x",
            format="%0.2f",
            min=0.0,
            max=100.0,
            inc=1.0,
            storagetype="float",
            endian="big",
        )
        converter = ScalingConverter(scaling)

        raw_values = np.array([0.0, 1.0, 2.0, 3.0])
        result = converter.to_display(raw_values)

        # (x^2 + 2x + 1) = (x+1)^2
        expected = np.array([1.0, 4.0, 9.0, 16.0])
        np.testing.assert_array_almost_equal(result, expected)


class TestRomReaderInitialization:
    """Test RomReader initialization"""

    def test_init_with_valid_rom(self, sample_rom_path, sample_xml_path):
        """Test initialization with valid ROM and definition"""
        definition = load_definition(str(sample_xml_path))
        reader = RomReader(str(sample_rom_path), definition)

        assert reader is not None
        assert reader.rom_path == sample_rom_path
        assert reader.definition == definition
        assert reader.rom_data is not None
        assert len(reader.rom_data) > 0

    def test_init_with_nonexistent_rom(self, sample_xml_path):
        """Test initialization with non-existent ROM file"""
        definition = load_definition(str(sample_xml_path))

        with pytest.raises(RomFileNotFoundError):
            RomReader("nonexistent.bin", definition)

    def test_rom_data_loaded(self, sample_rom_path, sample_xml_path):
        """Test that ROM data is loaded into memory"""
        definition = load_definition(str(sample_xml_path))
        reader = RomReader(str(sample_rom_path), definition)

        # ROM should be loaded as bytearray (mutable for in-place writes)
        assert isinstance(reader.rom_data, bytearray)
        assert len(reader.rom_data) > 100000  # ROM files are large


class TestRomIdVerification:
    """Test ROM ID verification"""

    def test_verify_rom_id_success(self, sample_rom_path, sample_xml_path):
        """Test successful ROM ID verification"""
        definition = load_definition(str(sample_xml_path))
        reader = RomReader(str(sample_rom_path), definition)

        assert reader.verify_rom_id() is True

    def test_verify_rom_id_failure(self, tmp_path, sample_xml_path):
        """Test ROM ID verification failure with wrong ROM"""
        # Create a fake ROM file
        fake_rom = tmp_path / "fake.bin"
        fake_rom.write_bytes(b"WRONG_ID" * 100000)

        definition = load_definition(str(sample_xml_path))
        reader = RomReader(str(fake_rom), definition)

        assert reader.verify_rom_id() is False


class TestTableDataReading:
    """Test reading table data from ROM"""

    def test_read_1d_table(self, sample_rom_path, sample_xml_path):
        """Test reading 1D table data"""
        definition = load_definition(str(sample_xml_path))
        reader = RomReader(str(sample_rom_path), definition)

        # Find a 1D table
        table_1d = None
        for table in definition.tables:
            if table.type == TableType.ONE_D and not table.is_axis:
                table_1d = table
                break

        assert table_1d is not None

        data = reader.read_table_data(table_1d)

        assert data is not None
        assert "values" in data
        assert "raw_values" in data
        assert len(data["values"]) == table_1d.elements

    def test_read_2d_table(self, sample_rom_path, sample_xml_path):
        """Test reading 2D table data"""
        definition = load_definition(str(sample_xml_path))
        reader = RomReader(str(sample_rom_path), definition)

        # Find a 2D table
        table_2d = None
        for table in definition.tables:
            if table.type == TableType.TWO_D and not table.is_axis:
                table_2d = table
                break

        assert table_2d is not None

        data = reader.read_table_data(table_2d)

        assert data is not None
        assert "values" in data
        assert "y_axis" in data
        assert len(data["values"]) == table_2d.elements

    def test_read_3d_table(self, sample_rom_path, sample_xml_path):
        """Test reading 3D table data"""
        definition = load_definition(str(sample_xml_path))
        reader = RomReader(str(sample_rom_path), definition)

        # Find a 3D table
        table_3d = None
        for table in definition.tables:
            if table.type == TableType.THREE_D and not table.is_axis:
                table_3d = table
                break

        assert table_3d is not None

        data = reader.read_table_data(table_3d)

        assert data is not None
        assert "values" in data
        assert "x_axis" in data
        assert "y_axis" in data

        # 3D table values should be reshaped to 2D
        if isinstance(data["values"], np.ndarray) and data["values"].ndim == 2:
            x_len = len(data["x_axis"])
            y_len = len(data["y_axis"])
            assert data["values"].shape == (y_len, x_len)

    def test_read_table_raises_error_for_invalid_scaling(
        self, sample_rom_path, sample_xml_path
    ):
        """Test that reading table with invalid scaling raises exception"""
        definition = load_definition(str(sample_xml_path))
        reader = RomReader(str(sample_rom_path), definition)

        # Get a table and change its scaling to invalid name
        table = definition.tables[0]
        table.scaling = "INVALID_SCALING"

        with pytest.raises(ScalingNotFoundError):
            reader.read_table_data(table)


class TestTableDataWriting:
    """Test writing table data to ROM"""

    def test_write_table_data(self, sample_rom_path, sample_xml_path, tmp_path):
        """Test writing modified table data"""
        definition = load_definition(str(sample_xml_path))
        reader = RomReader(str(sample_rom_path), definition)

        # Find a 1D table
        table = None
        for t in definition.tables:
            if t.type == TableType.ONE_D and not t.is_axis:
                table = t
                break

        # Read original data
        original_data = reader.read_table_data(table)
        assert original_data is not None

        # Modify values
        modified_values = original_data["values"] + 1.0

        # Write back (no return value, raises exception on error)
        reader.write_table_data(table, modified_values)

        # Read again and verify change
        new_data = reader.read_table_data(table)
        np.testing.assert_array_almost_equal(
            new_data["values"], modified_values, decimal=2
        )

    def test_write_table_with_2d_array(self, sample_rom_path, sample_xml_path):
        """Test writing 2D array is flattened correctly"""
        definition = load_definition(str(sample_xml_path))
        reader = RomReader(str(sample_rom_path), definition)

        # Find a 3D table
        table = None
        for t in definition.tables:
            if t.type == TableType.THREE_D and not t.is_axis:
                table = t
                break

        data = reader.read_table_data(table)

        # Modify and write back (no return value, raises exception on error)
        if isinstance(data["values"], np.ndarray) and data["values"].ndim == 2:
            modified = data["values"] + 0.5
            reader.write_table_data(table, modified)


class TestRomSaving:
    """Test saving modified ROM files"""

    def test_save_rom_to_new_file(self, sample_rom_path, sample_xml_path, tmp_path):
        """Test saving ROM to a new file"""
        definition = load_definition(str(sample_xml_path))
        reader = RomReader(str(sample_rom_path), definition)

        # Modify some data
        table = definition.tables[0]
        if not table.is_axis:
            data = reader.read_table_data(table)
            if data:
                reader.write_table_data(table, data["values"] + 1.0)

        # Save to new file
        output_path = tmp_path / "modified.bin"
        reader.save_rom(str(output_path))

        assert output_path.exists()
        assert output_path.stat().st_size > 0

    def test_save_rom_preserves_size(self, sample_rom_path, sample_xml_path, tmp_path):
        """Test that saving ROM preserves file size"""
        definition = load_definition(str(sample_xml_path))
        reader = RomReader(str(sample_rom_path), definition)

        original_size = sample_rom_path.stat().st_size

        # Save to new file
        output_path = tmp_path / "same_size.bin"
        reader.save_rom(str(output_path))

        assert output_path.stat().st_size == original_size

    def test_save_rom_default_path(self, sample_rom_path, sample_xml_path, tmp_path):
        """Test saving ROM to default path (overwrite)"""
        # Copy ROM to tmp dir first
        test_rom = tmp_path / "test.bin"
        test_rom.write_bytes(sample_rom_path.read_bytes())

        definition = load_definition(str(sample_xml_path))
        reader = RomReader(str(test_rom), definition)

        # Save without specifying path (should overwrite)
        reader.save_rom()

        assert test_rom.exists()


class TestDataIntegrity:
    """Test data integrity during read/write cycle"""

    def test_read_write_round_trip(self, sample_rom_path, sample_xml_path, tmp_path):
        """Test that data survives read-write-read cycle unchanged"""
        definition = load_definition(str(sample_xml_path))
        reader = RomReader(str(sample_rom_path), definition)

        # Find a simple table
        table = None
        for t in definition.tables:
            if t.type == TableType.ONE_D and not t.is_axis:
                table = t
                break

        # Read original
        original_data = reader.read_table_data(table)

        # Write back unchanged
        reader.write_table_data(table, original_data["values"])

        # Read again
        roundtrip_data = reader.read_table_data(table)

        # Should be identical (allowing for floating point precision)
        np.testing.assert_array_almost_equal(
            original_data["values"], roundtrip_data["values"], decimal=5
        )

    def test_multiple_modifications(self, sample_rom_path, sample_xml_path):
        """Test multiple sequential modifications"""
        definition = load_definition(str(sample_xml_path))
        reader = RomReader(str(sample_rom_path), definition)

        table = None
        for t in definition.tables:
            if t.type == TableType.ONE_D and not t.is_axis:
                table = t
                break

        # Original
        data1 = reader.read_table_data(table)

        # Modify once
        reader.write_table_data(table, data1["values"] + 10.0)
        data2 = reader.read_table_data(table)

        # Modify again
        reader.write_table_data(table, data2["values"] - 5.0)
        data3 = reader.read_table_data(table)

        # Final should be original + 5
        expected = data1["values"] + 5.0
        np.testing.assert_array_almost_equal(data3["values"], expected, decimal=2)
