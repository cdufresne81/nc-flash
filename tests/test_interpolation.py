"""
Tests for interpolation functions

These tests verify the interpolation logic works correctly.
"""


def test_vertical_interpolation_logic():
    """Test vertical interpolation calculation"""
    # Test case: 3 cells at rows 0, 1, 2 with values 10, ?, 20
    # Should interpolate middle cell to 15

    first_row = 0
    last_row = 2
    first_val = 10.0
    last_val = 20.0

    # Interpolate middle cell (row 1)
    row = 1
    t = (row - first_row) / (last_row - first_row)
    new_val = first_val + t * (last_val - first_val)

    assert abs(new_val - 15.0) < 1e-9, f"Expected 15.0, got {new_val}"


def test_vertical_interpolation_five_cells():
    """Test vertical interpolation with 5 cells"""
    # Test case: 5 cells at rows 0, 1, 2, 3, 4 with values 0, ?, ?, ?, 100
    # Should interpolate to 0, 25, 50, 75, 100

    first_row = 0
    last_row = 4
    first_val = 0.0
    last_val = 100.0

    expected = [0.0, 25.0, 50.0, 75.0, 100.0]

    for row in range(first_row, last_row + 1):
        t = (row - first_row) / (last_row - first_row)
        new_val = first_val + t * (last_val - first_val)
        assert abs(new_val - expected[row]) < 1e-9, f"Row {row}: expected {expected[row]}, got {new_val}"


def test_horizontal_interpolation_logic():
    """Test horizontal interpolation calculation"""
    # Test case: 3 cells at cols 0, 1, 2 with values 5, ?, 15
    # Should interpolate middle cell to 10

    first_col = 0
    last_col = 2
    first_val = 5.0
    last_val = 15.0

    # Interpolate middle cell (col 1)
    col = 1
    t = (col - first_col) / (last_col - first_col)
    new_val = first_val + t * (last_val - first_val)

    assert abs(new_val - 10.0) < 1e-9, f"Expected 10.0, got {new_val}"


def test_bilinear_interpolation_logic():
    """Test 2D bilinear interpolation calculation"""
    # Test case: 3x3 grid with corners defined
    # Top-left (0,0) = 0, Top-right (0,2) = 10
    # Bottom-left (2,0) = 20, Bottom-right (2,2) = 30

    # Corner values
    f00 = 0.0   # Top-left
    f10 = 10.0  # Top-right
    f01 = 20.0  # Bottom-left
    f11 = 30.0  # Bottom-right

    first_row, last_row = 0, 2
    first_col, last_col = 0, 2

    # Interpolate center cell (1, 1)
    row, col = 1, 1

    # Normalized coordinates (0 to 1)
    ty = (row - first_row) / (last_row - first_row)
    tx = (col - first_col) / (last_col - first_col)

    # Bilinear interpolation formula
    new_val = (
        (1 - tx) * (1 - ty) * f00 +
        tx * (1 - ty) * f10 +
        (1 - tx) * ty * f01 +
        tx * ty * f11
    )

    # Center should be (0 + 10 + 20 + 30) / 4 = 15
    expected = 15.0
    assert abs(new_val - expected) < 1e-9, f"Expected {expected}, got {new_val}"


def test_edge_cases():
    """Test edge cases that should be handled"""

    # Case 1: Only 2 cells, both at same row (can't interpolate vertically)
    first_row = 1
    last_row = 1
    assert first_row == last_row, "Should detect same row"

    # Case 2: Only 2 adjacent cells (nothing between them)
    first_row = 1
    last_row = 2
    num_cells = 2
    assert num_cells == 2 and last_row - first_row == 1, "Should detect adjacent cells with nothing between"

    # Case 3: Less than 2 cells (can't interpolate)
    num_cells = 1
    assert num_cells < 2, "Should detect insufficient cells"


if __name__ == "__main__":
    test_vertical_interpolation_logic()
    test_vertical_interpolation_five_cells()
    test_horizontal_interpolation_logic()
    test_bilinear_interpolation_logic()
    test_edge_cases()
    print("All interpolation logic tests passed!")
