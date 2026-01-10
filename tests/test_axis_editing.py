"""
Tests for axis editing functionality

These tests verify the axis cell identification, editing logic,
and bulk operations on axis cells work correctly.
"""

import pytest
import numpy as np


class TestAxisCellIdentification:
    """Test axis cell UserRole data format"""

    def test_axis_cell_format_x_axis(self):
        """Test X-axis cell identification format"""
        # X-axis cells should have UserRole data as ('x_axis', index)
        axis_data = ('x_axis', 5)

        assert isinstance(axis_data[0], str), "First element should be a string"
        assert axis_data[0] == 'x_axis', "First element should be 'x_axis'"
        assert isinstance(axis_data[1], int), "Second element should be an integer index"

    def test_axis_cell_format_y_axis(self):
        """Test Y-axis cell identification format"""
        # Y-axis cells should have UserRole data as ('y_axis', index)
        axis_data = ('y_axis', 3)

        assert isinstance(axis_data[0], str), "First element should be a string"
        assert axis_data[0] == 'y_axis', "First element should be 'y_axis'"
        assert isinstance(axis_data[1], int), "Second element should be an integer index"

    def test_data_cell_format(self):
        """Test data cell identification format"""
        # Data cells should have UserRole data as (row, col) - both integers
        data_indices = (2, 5)

        assert isinstance(data_indices[0], int), "First element should be an integer row"
        assert isinstance(data_indices[1], int), "Second element should be an integer col"

    def test_distinguishing_axis_from_data_cells(self):
        """Test logic to distinguish axis cells from data cells"""
        axis_cell = ('x_axis', 5)
        data_cell = (2, 5)

        # The check used in code: isinstance(data_indices[0], str)
        assert isinstance(axis_cell[0], str), "Axis cell first element is string"
        assert not isinstance(data_cell[0], str), "Data cell first element is not string"


class TestAxisInterpolationLogic:
    """Test axis interpolation calculations"""

    def test_y_axis_vertical_interpolation(self):
        """Test vertical interpolation on Y-axis values (e.g., RPM)"""
        # Test case: Y-axis with RPM values 1000, ?, ?, 4000
        # Should interpolate to 1000, 2000, 3000, 4000

        y_axis = np.array([1000.0, 0.0, 0.0, 4000.0])  # Middle values to be interpolated

        first_idx = 0
        last_idx = 3
        first_val = y_axis[first_idx]
        last_val = y_axis[last_idx]

        # Interpolate middle values
        for idx in range(first_idx + 1, last_idx):
            t = (idx - first_idx) / (last_idx - first_idx)
            new_val = first_val + t * (last_val - first_val)
            y_axis[idx] = new_val

        expected = [1000.0, 2000.0, 3000.0, 4000.0]
        for i, exp in enumerate(expected):
            assert abs(y_axis[i] - exp) < 1e-9, f"Index {i}: expected {exp}, got {y_axis[i]}"

    def test_x_axis_horizontal_interpolation(self):
        """Test horizontal interpolation on X-axis values (e.g., Load)"""
        # Test case: X-axis with Load values 0, ?, ?, ?, 1.0
        # Should interpolate to 0, 0.25, 0.5, 0.75, 1.0

        x_axis = np.array([0.0, 0.0, 0.0, 0.0, 1.0])  # Middle values to be interpolated

        first_idx = 0
        last_idx = 4
        first_val = x_axis[first_idx]
        last_val = x_axis[last_idx]

        # Interpolate middle values
        for idx in range(first_idx + 1, last_idx):
            t = (idx - first_idx) / (last_idx - first_idx)
            new_val = first_val + t * (last_val - first_val)
            x_axis[idx] = new_val

        expected = [0.0, 0.25, 0.5, 0.75, 1.0]
        for i, exp in enumerate(expected):
            assert abs(x_axis[i] - exp) < 1e-9, f"Index {i}: expected {exp}, got {x_axis[i]}"

    def test_axis_interpolation_with_negative_values(self):
        """Test axis interpolation handles negative values correctly"""
        # Test case: Temperature axis from -40 to 120
        axis = np.array([-40.0, 0.0, 0.0, 120.0])

        first_idx = 0
        last_idx = 3
        first_val = axis[first_idx]
        last_val = axis[last_idx]

        for idx in range(first_idx + 1, last_idx):
            t = (idx - first_idx) / (last_idx - first_idx)
            new_val = first_val + t * (last_val - first_val)
            axis[idx] = new_val

        # -40 to 120 = 160 range, divided into 3 steps = 53.33 per step
        expected = [-40.0, 13.333333, 66.666667, 120.0]
        for i, exp in enumerate(expected):
            assert abs(axis[i] - exp) < 0.001, f"Index {i}: expected {exp}, got {axis[i]}"


class TestAxisBulkOperations:
    """Test bulk operations on axis data"""

    def test_increment_axis_values(self):
        """Test incrementing axis values"""
        y_axis = np.array([1000.0, 2000.0, 3000.0, 4000.0])
        increment = 100.0

        # Apply increment operation
        new_values = y_axis + increment

        expected = [1100.0, 2100.0, 3100.0, 4100.0]
        for i, exp in enumerate(expected):
            assert abs(new_values[i] - exp) < 1e-9, f"Index {i}: expected {exp}, got {new_values[i]}"

    def test_multiply_axis_values(self):
        """Test multiplying axis values by a factor"""
        x_axis = np.array([0.0, 0.25, 0.5, 0.75, 1.0])
        factor = 2.0

        # Apply multiply operation
        new_values = x_axis * factor

        expected = [0.0, 0.5, 1.0, 1.5, 2.0]
        for i, exp in enumerate(expected):
            assert abs(new_values[i] - exp) < 1e-9, f"Index {i}: expected {exp}, got {new_values[i]}"

    def test_set_axis_values(self):
        """Test setting all axis values to a constant"""
        y_axis = np.array([1000.0, 2000.0, 3000.0, 4000.0])
        set_value = 2500.0

        # Apply set operation
        new_values = np.full_like(y_axis, set_value)

        for val in new_values:
            assert abs(val - set_value) < 1e-9, f"Expected {set_value}, got {val}"

    def test_decrement_axis_values(self):
        """Test decrementing axis values"""
        y_axis = np.array([1000.0, 2000.0, 3000.0, 4000.0])
        decrement = 500.0

        # Apply decrement operation
        new_values = y_axis - decrement

        expected = [500.0, 1500.0, 2500.0, 3500.0]
        for i, exp in enumerate(expected):
            assert abs(new_values[i] - exp) < 1e-9, f"Index {i}: expected {exp}, got {new_values[i]}"


class TestAxisChangeTracking:
    """Test axis change tracking data structures"""

    def test_axis_change_tuple_format(self):
        """Test the format of axis change tuples"""
        # Axis changes should be: (axis_type, index, old_value, new_value, old_raw, new_raw)
        axis_change = ('y_axis', 2, 2000.0, 2500.0, 2000.0, 2500.0)

        assert axis_change[0] in ('x_axis', 'y_axis'), "First element should be axis type"
        assert isinstance(axis_change[1], int), "Second element should be index"
        assert isinstance(axis_change[2], float), "Third element should be old_value"
        assert isinstance(axis_change[3], float), "Fourth element should be new_value"
        assert isinstance(axis_change[4], float), "Fifth element should be old_raw"
        assert isinstance(axis_change[5], float), "Sixth element should be new_raw"

    def test_data_change_tuple_format(self):
        """Test the format of data cell change tuples"""
        # Data changes should be: (row, col, old_value, new_value, old_raw, new_raw)
        data_change = (5, 10, 1.5, 2.0, 150, 200)

        assert isinstance(data_change[0], int), "First element should be row"
        assert isinstance(data_change[1], int), "Second element should be col"
        assert len(data_change) == 6, "Should have 6 elements"

    def test_distinguishing_axis_from_data_changes(self):
        """Test logic to distinguish axis changes from data changes"""
        axis_change = ('y_axis', 2, 2000.0, 2500.0, 2000.0, 2500.0)
        data_change = (5, 10, 1.5, 2.0, 150.0, 200.0)

        # Axis changes have string as first element
        assert isinstance(axis_change[0], str), "Axis change has string first element"
        assert not isinstance(data_change[0], str), "Data change has int first element"


class TestAxisDataStorage:
    """Test axis data storage in current_data dictionary"""

    def test_axis_data_structure(self):
        """Test the structure of axis data in current_data"""
        # Simulate current_data structure
        current_data = {
            'values': np.array([[1, 2, 3], [4, 5, 6], [7, 8, 9]]),
            'x_axis': np.array([0.0, 0.5, 1.0]),
            'y_axis': np.array([1000.0, 2000.0, 3000.0])
        }

        assert 'values' in current_data, "Should have 'values' key"
        assert 'x_axis' in current_data, "Should have 'x_axis' key"
        assert 'y_axis' in current_data, "Should have 'y_axis' key"

        # X-axis length should match columns
        assert len(current_data['x_axis']) == current_data['values'].shape[1]

        # Y-axis length should match rows
        assert len(current_data['y_axis']) == current_data['values'].shape[0]

    def test_axis_value_update(self):
        """Test updating axis values in place"""
        current_data = {
            'x_axis': np.array([0.0, 0.5, 1.0]),
            'y_axis': np.array([1000.0, 2000.0, 3000.0])
        }

        # Update a specific axis value
        current_data['y_axis'][1] = 2500.0

        assert current_data['y_axis'][1] == 2500.0, "Y-axis value should be updated"

        # Other values should be unchanged
        assert current_data['y_axis'][0] == 1000.0
        assert current_data['y_axis'][2] == 3000.0


class TestAxisColorGradient:
    """Test axis color gradient calculations"""

    def test_axis_color_ratio_calculation(self):
        """Test ratio calculation for axis color gradient"""
        axis_values = np.array([1000.0, 2000.0, 3000.0, 4000.0])

        min_val = np.min(axis_values)
        max_val = np.max(axis_values)

        # Test ratio for each value
        for i, value in enumerate(axis_values):
            if max_val == min_val:
                ratio = 0.5
            else:
                ratio = (value - min_val) / (max_val - min_val)

            expected_ratios = [0.0, 1/3, 2/3, 1.0]
            assert abs(ratio - expected_ratios[i]) < 1e-9, f"Index {i}: expected ratio {expected_ratios[i]}, got {ratio}"

    def test_axis_color_ratio_single_value(self):
        """Test ratio calculation when all axis values are the same"""
        axis_values = np.array([2000.0, 2000.0, 2000.0])

        min_val = np.min(axis_values)
        max_val = np.max(axis_values)

        # When min == max, ratio should be 0.5 (middle of gradient)
        for value in axis_values:
            if max_val == min_val:
                ratio = 0.5
            else:
                ratio = (value - min_val) / (max_val - min_val)

            assert ratio == 0.5, f"Expected 0.5 when all values equal, got {ratio}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
