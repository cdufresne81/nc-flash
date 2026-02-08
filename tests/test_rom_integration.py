"""
Integration tests for ROM read-edit-save-readback lifecycle.

Tests the full cycle: read table data -> modify values -> save to disk -> re-read
and verify modifications persist across file operations.
"""

import pytest
import shutil
import numpy as np
from pathlib import Path

from src.core.rom_reader import RomReader
from src.core.definition_parser import load_definition
from src.core.rom_definition import TableType


class TestRomLifecycle:
    """Integration tests for full ROM read-edit-save-readback cycle."""

    def _find_table_by_type(self, definition, table_type, exclude_axis=True):
        """Helper to find first table of given type in definition."""
        for table in definition.tables:
            if table.type == table_type:
                if exclude_axis and table.is_axis:
                    continue
                return table
        return None

    def test_read_edit_save_readback_1d(self, sample_rom_path, sample_xml_path, tmp_path):
        """Test full lifecycle with a 1D table: read -> edit -> save -> re-read."""
        # Work on a copy so the original ROM is never modified
        rom_copy = tmp_path / "lf9veb.bin"
        shutil.copy2(sample_rom_path, rom_copy)

        # Load definition and create reader on the copy
        definition = load_definition(str(sample_xml_path))
        reader = RomReader(str(rom_copy), definition)

        # Find a 1D table
        table = self._find_table_by_type(definition, TableType.ONE_D)
        assert table is not None, "No 1D table found in definition"

        # Step 1: Read original data
        original_data = reader.read_table_data(table)
        assert original_data is not None
        original_values = original_data['values'].copy()

        # Step 2: Modify a single value (increment first element by 1.0)
        modified_values = original_values.copy()
        modified_values[0] = original_values[0] + 1.0

        # Step 3: Write modified values back to ROM (in memory)
        reader.write_table_data(table, modified_values)

        # Step 4: Save to disk
        reader.save_rom(str(rom_copy))

        # Step 5: Create a fresh reader from the saved file
        definition2 = load_definition(str(sample_xml_path))
        reader2 = RomReader(str(rom_copy), definition2)

        # Step 6: Re-read the same table
        table2 = self._find_table_by_type(definition2, TableType.ONE_D)
        readback_data = reader2.read_table_data(table2)
        readback_values = readback_data['values']

        # Step 7: Assert the modification persisted
        np.testing.assert_almost_equal(
            readback_values[0], modified_values[0], decimal=2,
            err_msg="Modified value did not persist after save and re-read"
        )

        # Also verify remaining values are unchanged
        if len(original_values) > 1:
            np.testing.assert_array_almost_equal(
                readback_values[1:], original_values[1:], decimal=2,
                err_msg="Unmodified values changed after save and re-read"
            )

    def test_read_edit_save_readback_2d(self, sample_rom_path, sample_xml_path, tmp_path):
        """Test full lifecycle with a 2D table: read -> edit -> save -> re-read."""
        rom_copy = tmp_path / "lf9veb.bin"
        shutil.copy2(sample_rom_path, rom_copy)

        definition = load_definition(str(sample_xml_path))
        reader = RomReader(str(rom_copy), definition)

        # Find a 2D table
        table = self._find_table_by_type(definition, TableType.TWO_D)
        assert table is not None, "No 2D table found in definition"

        # Read original
        original_data = reader.read_table_data(table)
        assert original_data is not None
        original_values = original_data['values'].copy()

        # Modify first element
        modified_values = original_values.copy()
        modified_values[0] = original_values[0] + 2.0

        # Write and save
        reader.write_table_data(table, modified_values)
        reader.save_rom(str(rom_copy))

        # Re-read from disk with fresh reader
        definition2 = load_definition(str(sample_xml_path))
        reader2 = RomReader(str(rom_copy), definition2)
        table2 = self._find_table_by_type(definition2, TableType.TWO_D)
        readback_data = reader2.read_table_data(table2)

        np.testing.assert_almost_equal(
            readback_data['values'][0], modified_values[0], decimal=2,
            err_msg="Modified 2D table value did not persist after save and re-read"
        )

    def test_save_preserves_rom_size(self, sample_rom_path, sample_xml_path, tmp_path):
        """Verify that editing and saving does not change ROM file size."""
        rom_copy = tmp_path / "lf9veb.bin"
        shutil.copy2(sample_rom_path, rom_copy)
        original_size = rom_copy.stat().st_size

        definition = load_definition(str(sample_xml_path))
        reader = RomReader(str(rom_copy), definition)

        # Modify a table
        table = self._find_table_by_type(definition, TableType.ONE_D)
        assert table is not None
        data = reader.read_table_data(table)
        reader.write_table_data(table, data['values'] + 1.0)
        reader.save_rom(str(rom_copy))

        # File size must remain identical
        assert rom_copy.stat().st_size == original_size, \
            "ROM file size changed after edit-save cycle"

    def test_multiple_tables_independent(self, sample_rom_path, sample_xml_path, tmp_path):
        """Verify editing one table does not corrupt another table's data."""
        rom_copy = tmp_path / "lf9veb.bin"
        shutil.copy2(sample_rom_path, rom_copy)

        definition = load_definition(str(sample_xml_path))
        reader = RomReader(str(rom_copy), definition)

        # Find two different 1D tables
        tables_1d = [
            t for t in definition.tables
            if t.type == TableType.ONE_D and not t.is_axis
        ]
        if len(tables_1d) < 2:
            pytest.skip("Need at least 2 non-axis 1D tables for this test")

        table_a = tables_1d[0]
        table_b = tables_1d[1]

        # Read both tables
        data_a = reader.read_table_data(table_a)
        data_b_original = reader.read_table_data(table_b)

        # Modify only table A
        reader.write_table_data(table_a, data_a['values'] + 5.0)
        reader.save_rom(str(rom_copy))

        # Re-read from disk
        definition2 = load_definition(str(sample_xml_path))
        reader2 = RomReader(str(rom_copy), definition2)

        # Find the same tables in the fresh definition
        tables_1d_2 = [
            t for t in definition2.tables
            if t.type == TableType.ONE_D and not t.is_axis
        ]
        table_b2 = tables_1d_2[1]

        # Table B should be unchanged
        data_b_readback = reader2.read_table_data(table_b2)
        np.testing.assert_array_almost_equal(
            data_b_readback['values'], data_b_original['values'], decimal=2,
            err_msg="Unrelated table data was corrupted by editing a different table"
        )
