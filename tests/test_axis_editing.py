"""
Tests for axis editing, swapxy data integrity, and axis read/write round-trips.

These tests exercise actual production code from src/core/rom_reader.py
and src/core/rom_definition.py.
"""

import numpy as np
import pytest

from src.core.rom_reader import RomReader, ScalingConverter
from src.core.rom_definition import Table, Scaling, TableType, AxisType
from src.core.definition_parser import load_definition


class TestSwapxyRoundTrip:
    """Test that swapxy tables survive read->write->read without data corruption"""

    def test_swapxy_3d_table_round_trip(self, sample_rom_path, sample_xml_path):
        """Read a swapxy 3D table, write it back unchanged, verify data matches"""
        definition = load_definition(str(sample_xml_path))
        reader = RomReader(str(sample_rom_path), definition)

        swapxy_table = None
        for t in definition.tables:
            if t.type == TableType.THREE_D and not t.is_axis and t.swapxy:
                swapxy_table = t
                break

        if swapxy_table is None:
            pytest.skip("No swapxy 3D tables found in definition")

        original = reader.read_table_data(swapxy_table)
        assert original is not None
        assert 'values' in original
        assert original['values'].ndim == 2

        original_values = original['values'].copy()

        # Write back unchanged
        reader.write_table_data(swapxy_table, original_values)

        # Read again - must be identical
        after_write = reader.read_table_data(swapxy_table)
        np.testing.assert_array_almost_equal(
            original_values, after_write['values'], decimal=5,
            err_msg="swapxy table data corrupted during write->read round-trip"
        )

    def test_non_swapxy_3d_table_round_trip(self, sample_rom_path, sample_xml_path):
        """Read a non-swapxy 3D table, write it back unchanged, verify data matches"""
        definition = load_definition(str(sample_xml_path))
        reader = RomReader(str(sample_rom_path), definition)

        table = None
        for t in definition.tables:
            if t.type == TableType.THREE_D and not t.is_axis and not t.swapxy:
                table = t
                break

        if table is None:
            pytest.skip("No non-swapxy 3D tables found in definition")

        original = reader.read_table_data(table)
        assert original is not None
        assert 'values' in original
        assert original['values'].ndim == 2

        original_values = original['values'].copy()
        reader.write_table_data(table, original_values)
        after_write = reader.read_table_data(table)

        np.testing.assert_array_almost_equal(
            original_values, after_write['values'], decimal=5,
            err_msg="3D table data corrupted during write->read round-trip"
        )

    def test_swapxy_flatten_order_matches_reshape_order(self):
        """Verify that flatten(order='F') reverses reshape(order='F')"""
        flat_data = np.array([1, 2, 3, 4, 5, 6], dtype=float)

        # Read path: reshape with F order (swapxy=True)
        reshaped = flat_data.reshape((2, 3), order='F')
        expected = np.array([[1, 3, 5], [2, 4, 6]], dtype=float)
        np.testing.assert_array_equal(reshaped, expected)

        # Write path: flatten with F order must recover original
        reflattened = reshaped.flatten(order='F')
        np.testing.assert_array_equal(reflattened, flat_data)

    def test_swapxy_flatten_c_order_would_corrupt(self):
        """Demonstrate that C order flatten on F-order-reshaped data is wrong"""
        flat_data = np.array([1, 2, 3, 4, 5, 6], dtype=float)
        reshaped = flat_data.reshape((2, 3), order='F')

        wrong_flatten = reshaped.flatten(order='C')
        assert not np.array_equal(wrong_flatten, flat_data), \
            "C-order and F-order flatten should differ for non-trivial arrays"

    def test_2d_table_round_trip(self, sample_rom_path, sample_xml_path):
        """2D tables should also survive read->write->read"""
        definition = load_definition(str(sample_xml_path))
        reader = RomReader(str(sample_rom_path), definition)

        table = None
        for t in definition.tables:
            if t.type == TableType.TWO_D and not t.is_axis:
                table = t
                break

        if table is None:
            pytest.skip("No 2D tables found in definition")

        original = reader.read_table_data(table)
        assert original is not None
        original_values = original['values'].copy()

        reader.write_table_data(table, original_values)
        after_write = reader.read_table_data(table)

        np.testing.assert_array_almost_equal(
            original_values, after_write['values'], decimal=5
        )


class TestAxisReadWriteIntegrity:
    """Test axis value read/write integrity with actual ROM data"""

    def test_3d_table_has_both_axes(self, sample_rom_path, sample_xml_path):
        """3D table data should include x_axis and y_axis arrays"""
        definition = load_definition(str(sample_xml_path))
        reader = RomReader(str(sample_rom_path), definition)

        table = None
        for t in definition.tables:
            if t.type == TableType.THREE_D and not t.is_axis:
                table = t
                break

        if table is None:
            pytest.skip("No 3D tables found in definition")

        data = reader.read_table_data(table)
        assert 'x_axis' in data, "3D table should have x_axis data"
        assert 'y_axis' in data, "3D table should have y_axis data"
        assert data['x_axis'].ndim == 1
        assert data['y_axis'].ndim == 1

        x_axis_table = table.get_axis(AxisType.X_AXIS)
        y_axis_table = table.get_axis(AxisType.Y_AXIS)
        if x_axis_table:
            assert len(data['x_axis']) == x_axis_table.elements
        if y_axis_table:
            assert len(data['y_axis']) == y_axis_table.elements

    def test_single_cell_write_round_trip(self, sample_rom_path, sample_xml_path):
        """Write a single cell value and read it back"""
        definition = load_definition(str(sample_xml_path))
        reader = RomReader(str(sample_rom_path), definition)

        table = None
        for t in definition.tables:
            if t.type == TableType.ONE_D and not t.is_axis:
                table = t
                break

        if table is None:
            pytest.skip("No 1D tables found")

        data = reader.read_table_data(table)
        original_val = data['values'][0]

        new_val = original_val + 1.0
        scaling = definition.get_scaling(table.scaling)
        converter = ScalingConverter(scaling)
        new_raw = converter.from_display(new_val)
        reader.write_cell_value(table, 0, 0, new_raw)

        data2 = reader.read_table_data(table)
        np.testing.assert_almost_equal(data2['values'][0], new_val, decimal=2)

        # Restore original
        orig_raw = converter.from_display(original_val)
        reader.write_cell_value(table, 0, 0, orig_raw)

    def test_3d_table_values_shape_matches_axes(self, sample_rom_path, sample_xml_path):
        """3D table values array shape should be (y_len, x_len)"""
        definition = load_definition(str(sample_xml_path))
        reader = RomReader(str(sample_rom_path), definition)

        for t in definition.tables:
            if t.type == TableType.THREE_D and not t.is_axis:
                data = reader.read_table_data(t)
                if 'x_axis' in data and 'y_axis' in data and data['values'].ndim == 2:
                    assert data['values'].shape == (len(data['y_axis']), len(data['x_axis'])), \
                        f"Table {t.name}: values shape {data['values'].shape} doesn't match axes"
                    return  # Found and tested one

        pytest.skip("No 3D tables with complete axes found")


class TestScalingConverterRoundTrip:
    """Test that ScalingConverter conversions are reversible"""

    def test_linear_scaling_round_trip(self):
        """Linear scaling (x*factor) should round-trip cleanly"""
        scaling = Scaling(
            name="test_linear", units="V", toexpr="x*0.001",
            frexpr="x/0.001", format="%.3f", min=0, max=5.0,
            inc=0.001, storagetype="uint16", endian="big"
        )
        converter = ScalingConverter(scaling)

        raw = np.array([0, 1000, 2500, 5000], dtype=float)
        display = converter.to_display(raw)
        raw_back = converter.from_display(display)

        np.testing.assert_array_almost_equal(raw, raw_back, decimal=5)

    def test_offset_scaling_round_trip(self):
        """Offset scaling (x*factor+offset) should round-trip cleanly"""
        scaling = Scaling(
            name="test_offset", units="degC", toexpr="x*0.01-40",
            frexpr="(x+40)/0.01", format="%.1f", min=-40, max=120,
            inc=0.01, storagetype="uint16", endian="big"
        )
        converter = ScalingConverter(scaling)

        raw = np.array([4000, 6000, 8000, 16000], dtype=float)
        display = converter.to_display(raw)
        raw_back = converter.from_display(display)

        np.testing.assert_array_almost_equal(raw, raw_back, decimal=3)

    def test_scalar_round_trip(self):
        """Single scalar values should also round-trip"""
        scaling = Scaling(
            name="test_scalar", units="RPM", toexpr="x*0.25",
            frexpr="x/0.25", format="%.0f", min=0, max=10000,
            inc=50, storagetype="uint16", endian="big"
        )
        converter = ScalingConverter(scaling)

        raw_val = 4000.0
        display_val = converter.to_display(raw_val)
        assert display_val == pytest.approx(1000.0)
        raw_back = converter.from_display(display_val)
        assert raw_back == pytest.approx(raw_val)

    def test_exponentiation_scaling(self):
        """Expressions with ^ (converted to **) should work"""
        scaling = Scaling(
            name="test_power", units="kPa", toexpr="x**2*0.01",
            frexpr="(x/0.01)**0.5", format="%.2f", min=0, max=500,
            inc=1, storagetype="uint16", endian="big"
        )
        converter = ScalingConverter(scaling)

        raw_val = 100.0
        display_val = converter.to_display(raw_val)
        assert display_val == pytest.approx(100.0)  # 100^2 * 0.01 = 100
        raw_back = converter.from_display(display_val)
        assert raw_back == pytest.approx(raw_val, rel=1e-3)
