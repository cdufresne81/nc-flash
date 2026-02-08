"""
Tests for Color Map

Tests color map loading and color generation.
"""

import pytest
from pathlib import Path
import tempfile
from unittest.mock import patch, MagicMock

from src.utils.colormap import ColorMap, get_colormap, set_colormap, reload_colormap
import src.utils.colormap as colormap_module


@pytest.fixture(autouse=True)
def _restore_colormap_globals():
    """Save and restore ColorMap class state and module globals between tests.

    This prevents mutations like ``ColorMap._builtin_gradient = None`` or
    ``_current_colormap = <some instance>`` from leaking across tests and
    causing order-dependent failures.
    """
    original_builtin_gradient = ColorMap._builtin_gradient
    original_current_colormap = colormap_module._current_colormap
    yield
    ColorMap._builtin_gradient = original_builtin_gradient
    colormap_module._current_colormap = original_current_colormap


@pytest.fixture
def valid_map_file():
    """Create a valid .map file with 256 color entries"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.map', delete=False) as f:
        # Create a simple gradient from blue to red
        for i in range(256):
            r = i
            g = 0
            b = 255 - i
            f.write(f"{r} {g} {b}\n")
        temp_path = Path(f.name)
    yield temp_path
    temp_path.unlink()


@pytest.fixture
def partial_map_file():
    """Create a .map file with fewer than 256 entries"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.map', delete=False) as f:
        # Only 100 entries
        for i in range(100):
            f.write(f"{i} {i} {i}\n")
        temp_path = Path(f.name)
    yield temp_path
    temp_path.unlink()


@pytest.fixture
def empty_map_file():
    """Create an empty .map file"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.map', delete=False) as f:
        pass  # Empty file
        temp_path = Path(f.name)
    yield temp_path
    temp_path.unlink()


@pytest.fixture
def invalid_map_file():
    """Create a .map file with invalid data"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.map', delete=False) as f:
        f.write("invalid data\n")
        f.write("not numbers\n")
        temp_path = Path(f.name)
    yield temp_path
    temp_path.unlink()


class TestColorMapInitialization:
    """Tests for ColorMap initialization"""

    def test_init_builtin_gradient(self):
        """Test initialization with built-in gradient"""
        colormap = ColorMap()

        assert colormap.name == "Built-in"
        assert colormap.map_path is None
        assert len(colormap.colors) == 256

    def test_init_with_valid_file(self, valid_map_file):
        """Test initialization with valid .map file"""
        colormap = ColorMap(str(valid_map_file))

        assert colormap.name == valid_map_file.stem
        assert len(colormap.colors) == 256
        # Check first and last colors
        assert colormap.colors[0] == (0, 0, 255)  # Blue
        assert colormap.colors[255] == (255, 0, 0)  # Red

    def test_init_with_nonexistent_file(self):
        """Test initialization falls back to builtin for nonexistent file"""
        colormap = ColorMap("/nonexistent/path/file.map")

        assert colormap.name == "Built-in"
        assert colormap.map_path is None
        assert len(colormap.colors) == 256

    def test_init_with_partial_file(self, partial_map_file):
        """Test initialization pads partial file to 256 entries"""
        colormap = ColorMap(str(partial_map_file))

        assert len(colormap.colors) == 256
        # Last color should be repeated to fill gaps
        last_color = colormap.colors[99]
        assert colormap.colors[100] == last_color
        assert colormap.colors[255] == last_color

    def test_init_with_empty_file(self, empty_map_file):
        """Test initialization falls back to builtin for empty file"""
        colormap = ColorMap(str(empty_map_file))

        assert colormap.name == "Built-in"
        assert len(colormap.colors) == 256


class TestBuiltinGradient:
    """Tests for built-in gradient generation"""

    def test_builtin_gradient_is_cached(self):
        """Test that built-in gradient is generated once and cached"""
        # Reset the cache
        ColorMap._builtin_gradient = None

        colormap1 = ColorMap()
        colormap2 = ColorMap()

        # Both should share the same colors list
        assert colormap1.colors is colormap2.colors

    def test_builtin_gradient_starts_blue(self):
        """Test that built-in gradient starts with blue"""
        colormap = ColorMap()
        r, g, b = colormap.colors[0]

        # At ratio 0, should be bluish
        assert b == 255
        assert r == 0

    def test_builtin_gradient_ends_red(self):
        """Test that built-in gradient ends with red"""
        colormap = ColorMap()
        r, g, b = colormap.colors[255]

        # At ratio 1, should be reddish
        assert r == 255
        assert b == 0

    def test_builtin_gradient_has_green_middle(self):
        """Test that built-in gradient has green in the middle"""
        colormap = ColorMap()
        # Around ratio 0.5 should be greenish
        r, g, b = colormap.colors[128]

        assert g == 255


class TestRatioToColor:
    """Tests for ratio_to_color method"""

    def test_ratio_zero(self, valid_map_file):
        """Test ratio 0 returns first color"""
        colormap = ColorMap(str(valid_map_file))
        color = colormap.ratio_to_color(0.0)

        assert color.red() == 0
        assert color.green() == 0
        assert color.blue() == 255

    def test_ratio_one(self, valid_map_file):
        """Test ratio 1 returns last color"""
        colormap = ColorMap(str(valid_map_file))
        color = colormap.ratio_to_color(1.0)

        assert color.red() == 255
        assert color.green() == 0
        assert color.blue() == 0

    def test_ratio_middle(self, valid_map_file):
        """Test ratio 0.5 returns middle color"""
        colormap = ColorMap(str(valid_map_file))
        color = colormap.ratio_to_color(0.5)

        # At index 127 (0.5 * 255 = 127.5 -> 127)
        assert color.red() == 127
        assert color.blue() == 128

    def test_ratio_clamped_below_zero(self, valid_map_file):
        """Test that ratio below 0 is clamped"""
        colormap = ColorMap(str(valid_map_file))
        color = colormap.ratio_to_color(-0.5)

        # Should return first color
        assert color.red() == 0
        assert color.blue() == 255

    def test_ratio_clamped_above_one(self, valid_map_file):
        """Test that ratio above 1 is clamped"""
        colormap = ColorMap(str(valid_map_file))
        color = colormap.ratio_to_color(1.5)

        # Should return last color
        assert color.red() == 255
        assert color.blue() == 0


class TestRatioToRgb:
    """Tests for ratio_to_rgb method"""

    def test_returns_tuple(self, valid_map_file):
        """Test that ratio_to_rgb returns a tuple"""
        colormap = ColorMap(str(valid_map_file))
        result = colormap.ratio_to_rgb(0.5)

        assert isinstance(result, tuple)
        assert len(result) == 3

    def test_ratio_zero(self, valid_map_file):
        """Test ratio 0"""
        colormap = ColorMap(str(valid_map_file))
        r, g, b = colormap.ratio_to_rgb(0.0)

        assert r == 0
        assert g == 0
        assert b == 255

    def test_ratio_one(self, valid_map_file):
        """Test ratio 1"""
        colormap = ColorMap(str(valid_map_file))
        r, g, b = colormap.ratio_to_rgb(1.0)

        assert r == 255
        assert g == 0
        assert b == 0


class TestRatioToRgbaFloat:
    """Tests for ratio_to_rgba_float method (for matplotlib)"""

    def test_returns_four_floats(self, valid_map_file):
        """Test that ratio_to_rgba_float returns 4 float values"""
        colormap = ColorMap(str(valid_map_file))
        result = colormap.ratio_to_rgba_float(0.5)

        assert isinstance(result, tuple)
        assert len(result) == 4
        for val in result:
            assert isinstance(val, float)

    def test_values_in_range(self, valid_map_file):
        """Test that all values are in 0-1 range"""
        colormap = ColorMap(str(valid_map_file))

        for ratio in [0.0, 0.25, 0.5, 0.75, 1.0]:
            result = colormap.ratio_to_rgba_float(ratio)
            for val in result:
                assert 0.0 <= val <= 1.0

    def test_alpha_is_one(self, valid_map_file):
        """Test that alpha is always 1.0"""
        colormap = ColorMap(str(valid_map_file))

        for ratio in [0.0, 0.5, 1.0]:
            r, g, b, a = colormap.ratio_to_rgba_float(ratio)
            assert a == 1.0

    def test_ratio_zero_normalized(self, valid_map_file):
        """Test ratio 0 with normalized values"""
        colormap = ColorMap(str(valid_map_file))
        r, g, b, a = colormap.ratio_to_rgba_float(0.0)

        assert r == 0.0
        assert g == 0.0
        assert b == 1.0  # 255/255


class TestGlobalColormapFunctions:
    """Tests for global colormap functions"""

    def test_set_colormap_with_path(self, valid_map_file):
        """Test set_colormap with file path"""
        set_colormap(str(valid_map_file))

        result = get_colormap()
        assert result.name == valid_map_file.stem

    def test_set_colormap_builtin(self):
        """Test set_colormap with None for built-in"""
        set_colormap(None)

        result = get_colormap()
        assert result.name == "Built-in"

    def test_reload_colormap(self):
        """Test reload_colormap resets global instance"""
        # Reset the global colormap
        colormap_module._current_colormap = None

        with patch('src.utils.settings.get_settings') as mock_get_settings:
            mock_settings = MagicMock()
            mock_settings.get_colormap_path.return_value = None
            mock_get_settings.return_value = mock_settings

            reload_colormap()

            result = get_colormap()
            assert result.name == "Built-in"


class TestFileFormatEdgeCases:
    """Tests for edge cases in file format parsing"""

    def test_extra_columns_ignored(self):
        """Test that extra columns in map file are ignored"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.map', delete=False) as f:
            for i in range(256):
                f.write(f"{i} {i} {i} extra_data 123\n")
            temp_path = Path(f.name)

        try:
            colormap = ColorMap(str(temp_path))
            assert len(colormap.colors) == 256
            # Values should still be parsed correctly
            assert colormap.colors[100] == (100, 100, 100)
        finally:
            temp_path.unlink()

    def test_blank_lines_skipped(self):
        """Test that blank lines in map file are skipped"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.map', delete=False) as f:
            count = 0
            for i in range(300):  # More lines than needed
                if i % 3 == 0:
                    f.write("\n")  # Blank line
                else:
                    f.write(f"{count} {count} {count}\n")
                    count += 1
            temp_path = Path(f.name)

        try:
            colormap = ColorMap(str(temp_path))
            assert len(colormap.colors) >= 256
        finally:
            temp_path.unlink()
