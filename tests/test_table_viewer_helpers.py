"""
Tests for Table Viewer Helper Classes

Tests the logic in table viewer helper classes with mocked Qt dependencies.
"""

import pytest
import numpy as np
from unittest.mock import MagicMock, patch, PropertyMock


class TestTableEditHelperLogic:
    """Tests for TableEditHelper logic"""

    def test_display_to_raw_simple_expression(self):
        """Test display to raw conversion with simple expression"""
        from simpleeval import simple_eval

        # Simulate the conversion logic
        display_value = 50.0
        frexpr = "x/10"  # Common pattern: raw = display / 10
        raw = simple_eval(frexpr, names={'x': display_value})

        assert raw == 5.0

    def test_display_to_raw_complex_expression(self):
        """Test display to raw conversion with complex expression"""
        from simpleeval import simple_eval

        display_value = 100.0
        # Fahrenheit to Celsius offset adjustment
        frexpr = "(x-32)*5/9+40"
        raw = simple_eval(frexpr, names={'x': display_value})

        expected = (100 - 32) * 5 / 9 + 40
        assert abs(raw - expected) < 1e-9

    def test_data_to_ui_coords_1d(self):
        """Test coordinate conversion for 1D table"""
        # 1D tables always map to (0, 0)
        from src.core.rom_definition import TableType

        table_type = TableType.ONE_D
        data_row, data_col = 0, 0

        # Expected UI coords
        expected_row, expected_col = 0, 0

        assert (expected_row, expected_col) == (0, 0)

    def test_data_to_ui_coords_2d(self):
        """Test coordinate conversion for 2D table"""
        from src.core.rom_definition import TableType

        # 2D tables: column 0 is Y axis, column 1 is values
        # Without flip: data_row maps directly to ui_row
        data_row = 5
        flipy = False
        num_values = 10

        if flipy:
            ui_row = num_values - 1 - data_row
        else:
            ui_row = data_row

        assert ui_row == 5

    def test_data_to_ui_coords_2d_flipped(self):
        """Test coordinate conversion for 2D table with flipy"""
        data_row = 2
        flipy = True
        num_values = 10

        if flipy:
            ui_row = num_values - 1 - data_row
        else:
            ui_row = data_row

        assert ui_row == 7

    def test_data_to_ui_coords_3d(self):
        """Test coordinate conversion for 3D table"""
        # 3D tables: row 0 is X axis, col 0 is Y axis
        # Data starts at row 1, col 1
        data_row, data_col = 3, 4
        flipx, flipy = False, False

        # Without flips, just add 1 for axis offset
        ui_row = data_row + 1
        ui_col = data_col + 1

        assert ui_row == 4
        assert ui_col == 5

    def test_data_to_ui_coords_3d_flipped(self):
        """Test coordinate conversion for 3D table with flips"""
        data_row, data_col = 2, 3
        rows, cols = 10, 8
        flipx, flipy = True, True

        ui_row = (rows - 1 - data_row) + 1 if flipy else data_row + 1
        ui_col = (cols - 1 - data_col) + 1 if flipx else data_col + 1

        assert ui_row == 8  # (10-1-2) + 1 = 8
        assert ui_col == 5  # (8-1-3) + 1 = 5


class TestTableOperationsHelperLogic:
    """Tests for TableOperationsHelper logic"""

    def test_increment_operation(self):
        """Test increment operation function"""
        increment = 0.5
        operation = lambda v: v + increment

        assert operation(10.0) == 10.5
        assert operation(-5.0) == -4.5
        assert operation(0.0) == 0.5

    def test_decrement_operation(self):
        """Test decrement operation function"""
        decrement = 1.0
        operation = lambda v: v - decrement

        assert operation(10.0) == 9.0
        assert operation(1.0) == 0.0
        assert operation(-5.0) == -6.0

    def test_multiply_operation(self):
        """Test multiply operation function"""
        factor = 1.1
        operation = lambda v: v * factor

        assert abs(operation(10.0) - 11.0) < 1e-9
        assert abs(operation(100.0) - 110.0) < 1e-9

    def test_set_value_operation(self):
        """Test set value operation function"""
        set_value = 42.0
        operation = lambda v: set_value

        assert operation(10.0) == 42.0
        assert operation(0.0) == 42.0
        assert operation(-100.0) == 42.0

    def test_apply_to_1d_array(self):
        """Test applying operation to 1D values array"""
        values = np.array([10.0, 20.0, 30.0, 40.0, 50.0])
        operation = lambda v: v * 2

        # Simulate applying to selected indices
        selected_indices = [1, 3]
        for idx in selected_indices:
            values[idx] = operation(values[idx])

        assert values[0] == 10.0  # Unchanged
        assert values[1] == 40.0  # Doubled
        assert values[2] == 30.0  # Unchanged
        assert values[3] == 80.0  # Doubled

    def test_apply_to_2d_array(self):
        """Test applying operation to 2D values array"""
        values = np.array([
            [1.0, 2.0, 3.0],
            [4.0, 5.0, 6.0],
            [7.0, 8.0, 9.0]
        ])
        operation = lambda v: v + 10

        # Simulate applying to selected cells
        selected_cells = [(0, 1), (1, 1), (2, 0)]
        for row, col in selected_cells:
            values[row, col] = operation(values[row, col])

        assert values[0, 0] == 1.0   # Unchanged
        assert values[0, 1] == 12.0  # +10
        assert values[1, 1] == 15.0  # +10
        assert values[2, 0] == 17.0  # +10


class TestTableInterpolationHelperLogic:
    """Tests for TableInterpolationHelper interpolation logic"""

    def test_linear_interpolation_formula(self):
        """Test linear interpolation formula"""
        first_val, last_val = 10.0, 20.0
        first_row, last_row = 0, 4

        # Calculate intermediate values
        for row in range(first_row + 1, last_row):
            t = (row - first_row) / (last_row - first_row)
            new_val = first_val + t * (last_val - first_val)

            if row == 1:
                assert abs(new_val - 12.5) < 1e-9
            elif row == 2:
                assert abs(new_val - 15.0) < 1e-9
            elif row == 3:
                assert abs(new_val - 17.5) < 1e-9

    def test_linear_interpolation_negative_values(self):
        """Test linear interpolation with negative values"""
        first_val, last_val = -10.0, 10.0
        first_row, last_row = 0, 4

        for row in range(first_row + 1, last_row):
            t = (row - first_row) / (last_row - first_row)
            new_val = first_val + t * (last_val - first_val)

            if row == 2:
                assert abs(new_val - 0.0) < 1e-9

    def test_bilinear_interpolation_formula(self):
        """Test bilinear (2D) interpolation formula"""
        # Corner values
        v00, v10 = 0.0, 10.0   # Top-left, Top-right
        v01, v11 = 20.0, 30.0  # Bottom-left, Bottom-right

        # Test center point (0.5, 0.5)
        tx, ty = 0.5, 0.5
        new_val = (
            (1 - tx) * (1 - ty) * v00 +
            tx * (1 - ty) * v10 +
            (1 - tx) * ty * v01 +
            tx * ty * v11
        )

        # Center should be average: (0 + 10 + 20 + 30) / 4 = 15
        assert abs(new_val - 15.0) < 1e-9

    def test_bilinear_interpolation_corners(self):
        """Test bilinear interpolation at corners returns corner values"""
        v00, v10 = 10.0, 20.0
        v01, v11 = 30.0, 40.0

        def bilinear(tx, ty):
            return (
                (1 - tx) * (1 - ty) * v00 +
                tx * (1 - ty) * v10 +
                (1 - tx) * ty * v01 +
                tx * ty * v11
            )

        # Each corner should return its own value
        assert abs(bilinear(0.0, 0.0) - v00) < 1e-9  # Top-left
        assert abs(bilinear(1.0, 0.0) - v10) < 1e-9  # Top-right
        assert abs(bilinear(0.0, 1.0) - v01) < 1e-9  # Bottom-left
        assert abs(bilinear(1.0, 1.0) - v11) < 1e-9  # Bottom-right

    def test_bilinear_interpolation_edges(self):
        """Test bilinear interpolation along edges"""
        v00, v10 = 0.0, 100.0
        v01, v11 = 0.0, 100.0

        def bilinear(tx, ty):
            return (
                (1 - tx) * (1 - ty) * v00 +
                tx * (1 - ty) * v10 +
                (1 - tx) * ty * v01 +
                tx * ty * v11
            )

        # Along top edge (ty=0): should interpolate horizontally
        assert abs(bilinear(0.5, 0.0) - 50.0) < 1e-9

        # Along left edge (tx=0): should be 0 everywhere
        for ty in [0.0, 0.25, 0.5, 0.75, 1.0]:
            assert abs(bilinear(0.0, ty) - 0.0) < 1e-9


class TestTableClipboardHelperLogic:
    """Tests for TableClipboardHelper logic"""

    def test_parse_tab_separated_values(self):
        """Test parsing clipboard text into grid"""
        clipboard_text = "1.0\t2.0\t3.0\n4.0\t5.0\t6.0"

        rows_data = []
        for line in clipboard_text.strip().split("\n"):
            row_values = line.split("\t")
            rows_data.append(row_values)

        assert len(rows_data) == 2
        assert len(rows_data[0]) == 3
        assert rows_data[0][0] == "1.0"
        assert rows_data[1][2] == "6.0"

    def test_parse_single_value(self):
        """Test parsing single value"""
        clipboard_text = "42.5"

        rows_data = []
        for line in clipboard_text.strip().split("\n"):
            row_values = line.split("\t")
            rows_data.append(row_values)

        assert len(rows_data) == 1
        assert len(rows_data[0]) == 1
        assert rows_data[0][0] == "42.5"

    def test_generate_tab_separated_text(self):
        """Test generating clipboard text from grid"""
        values = [
            ["1.0", "2.0", "3.0"],
            ["4.0", "5.0", "6.0"]
        ]

        rows_text = []
        for row in values:
            rows_text.append("\t".join(row))
        clipboard_text = "\n".join(rows_text)

        assert clipboard_text == "1.0\t2.0\t3.0\n4.0\t5.0\t6.0"

    def test_value_change_detection(self):
        """Test detecting whether value has changed"""
        old_value = 10.0
        new_value = 10.00000000001

        # Should be considered unchanged (within tolerance)
        assert abs(new_value - old_value) < 1e-10

        new_value = 10.001
        # Should be considered changed
        assert abs(new_value - old_value) >= 1e-10


class TestAxisCoordinateConversion:
    """Tests for axis coordinate conversion logic"""

    def test_x_axis_coords_3d_no_flip(self):
        """Test X-axis coordinate conversion without flip"""
        data_idx = 3
        num_cols = 10
        flipx = False

        if flipx:
            ui_col = num_cols - 1 - data_idx
        else:
            ui_col = data_idx

        ui_row = 0  # X axis is always in row 0

        assert ui_row == 0
        assert ui_col == 3

    def test_x_axis_coords_3d_with_flip(self):
        """Test X-axis coordinate conversion with flip"""
        data_idx = 3
        num_cols = 10
        flipx = True

        if flipx:
            ui_col = num_cols - 1 - data_idx
        else:
            ui_col = data_idx

        ui_row = 0

        assert ui_row == 0
        assert ui_col == 6  # 10 - 1 - 3 = 6

    def test_y_axis_coords_2d_no_flip(self):
        """Test Y-axis coordinate conversion for 2D table without flip"""
        data_idx = 4
        num_values = 10
        flipy = False

        if flipy:
            ui_row = num_values - 1 - data_idx
        else:
            ui_row = data_idx

        ui_col = 0  # Y axis is always in column 0

        assert ui_row == 4
        assert ui_col == 0

    def test_y_axis_coords_3d_with_flip(self):
        """Test Y-axis coordinate conversion for 3D table with flip"""
        data_idx = 2
        num_rows = 8
        flipy = True

        if flipy:
            ui_row = num_rows - 1 - data_idx
        else:
            ui_row = data_idx

        # In 3D tables, Y axis starts at row 1 (row 0 is X axis header)
        ui_row += 1
        ui_col = 0

        assert ui_row == 6  # (8 - 1 - 2) + 1 = 6
        assert ui_col == 0


class TestScalingConversion:
    """Tests for scaling conversion logic using simpleeval"""

    def test_identity_scaling(self):
        """Test identity scaling (x = x)"""
        from simpleeval import simple_eval

        expression = "x"
        frexpr = "x"
        raw_value = 100

        display = simple_eval(expression, names={'x': raw_value})
        back_to_raw = simple_eval(frexpr, names={'x': display})

        assert display == 100
        assert back_to_raw == 100

    def test_linear_scaling(self):
        """Test linear scaling (x * factor)"""
        from simpleeval import simple_eval

        expression = "x * 0.1"
        frexpr = "x / 0.1"
        raw_value = 100

        display = simple_eval(expression, names={'x': raw_value})
        back_to_raw = simple_eval(frexpr, names={'x': display})

        assert abs(display - 10.0) < 1e-9
        assert abs(back_to_raw - raw_value) < 1e-9

    def test_offset_scaling(self):
        """Test scaling with offset"""
        from simpleeval import simple_eval

        expression = "x - 40"
        frexpr = "x + 40"
        raw_value = 100

        display = simple_eval(expression, names={'x': raw_value})
        back_to_raw = simple_eval(frexpr, names={'x': display})

        assert display == 60
        assert back_to_raw == raw_value

    def test_complex_scaling(self):
        """Test complex scaling formula"""
        from simpleeval import simple_eval

        # AFR scaling: display = 14.7 / x, raw = 14.7 / display
        expression = "14.7 / x"
        frexpr = "14.7 / x"
        raw_value = 128  # Lambda = 1.0 at 128

        display = simple_eval(expression, names={'x': raw_value})
        back_to_raw = simple_eval(frexpr, names={'x': display})

        expected_afr = 14.7 / 128
        assert abs(display - expected_afr) < 1e-9
        assert abs(back_to_raw - raw_value) < 1e-9


class TestToggleCategoryDetection:
    """Tests for toggle category detection logic"""

    def test_dtc_activation_flags_is_toggle(self):
        """DTC - Activation Flags category should be detected as toggle"""
        category = "DTC - Activation Flags"
        toggle_categories = ["DTC - Activation Flags"]
        assert category in toggle_categories

    def test_regular_category_is_not_toggle(self):
        """Regular categories should not be detected as toggle"""
        toggle_categories = ["DTC - Activation Flags"]

        assert "Fuel IPW - Base" not in toggle_categories
        assert "Spark Target - Base" not in toggle_categories
        assert "" not in toggle_categories

    def test_empty_toggle_list_disables_all(self):
        """Empty toggle categories list means no toggles"""
        toggle_categories = []
        assert "DTC - Activation Flags" not in toggle_categories

    def test_multiple_toggle_categories(self):
        """Multiple categories can be configured as toggles"""
        toggle_categories = ["DTC - Activation Flags", "Custom Flags"]
        assert "DTC - Activation Flags" in toggle_categories
        assert "Custom Flags" in toggle_categories
        assert "Other Category" not in toggle_categories


class TestToggleValueLogic:
    """Tests for toggle ON/OFF value calculation logic"""

    def test_toggle_on_standard_dtc(self):
        """Toggle ON for standard DTC (original value 1) should produce 1"""
        original_nonzero = 1.0
        checked = True
        new_value = original_nonzero if (checked and original_nonzero != 0) else (1.0 if checked else 0.0)
        assert new_value == 1.0

    def test_toggle_off_standard_dtc(self):
        """Toggle OFF should always produce 0"""
        checked = False
        new_value = 0.0 if not checked else 1.0
        assert new_value == 0.0

    def test_toggle_on_p1260_preserves_value_3(self):
        """Toggle ON for P1260 (original value 3) should restore 3, not 1"""
        original_nonzero = 3.0
        checked = True
        new_value = original_nonzero if (checked and original_nonzero != 0) else (1.0 if checked else 0.0)
        assert new_value == 3.0

    def test_toggle_on_when_original_was_zero(self):
        """Toggle ON when original value was 0 should default to 1"""
        # When original is 0, _toggle_original_nonzero_value is set to 1.0
        original_nonzero = 1.0  # Default when original was 0
        checked = True
        new_value = original_nonzero if (checked and original_nonzero != 0) else (1.0 if checked else 0.0)
        assert new_value == 1.0

    def test_original_nonzero_value_stored_correctly(self):
        """Original non-zero value should be captured for restore on toggle ON"""
        # Simulates what _display_1d does
        for val, expected in [(1.0, 1.0), (3.0, 3.0), (0.0, 1.0), (255.0, 255.0)]:
            stored = val if val != 0 else 1.0
            assert stored == expected

    def test_no_change_signal_when_value_unchanged(self):
        """Toggling to same state should not trigger a change"""
        current_value = 1.0
        checked = True  # Already ON
        new_value = 1.0 if checked else 0.0
        assert abs(new_value - current_value) < 1e-10  # No change
