"""
Tests for interpolation logic and ScalingConverter integration.

These tests exercise actual production code: ScalingConverter round-trips
through interpolated values, and _convert_expr_to_python expression conversion.
"""

import numpy as np
import pytest

from src.core.rom_reader import ScalingConverter, _convert_expr_to_python
from src.core.rom_definition import Scaling


class TestConvertExprToPython:
    """Test the _convert_expr_to_python expression converter"""

    def test_caret_to_power(self):
        """x^2 should become x**2"""
        assert _convert_expr_to_python("x^2") == "x**2"

    def test_multiple_carets(self):
        """Multiple ^ should all be converted"""
        assert _convert_expr_to_python("x^2+y^3") == "x**2+y**3"

    def test_no_caret_unchanged(self):
        """Expressions without ^ should pass through unchanged"""
        assert _convert_expr_to_python("x*0.01-40") == "x*0.01-40"

    def test_already_python_power(self):
        """Expressions already using ** should not be double-converted"""
        result = _convert_expr_to_python("x**2")
        # re.sub replaces ^ with **, so ** stays as **
        assert result == "x**2"

    def test_complex_expression(self):
        """Complex expression with parentheses and caret"""
        assert _convert_expr_to_python("(x+1)^2*0.5") == "(x+1)**2*0.5"

    def test_empty_expression(self):
        """Empty string should return empty string"""
        assert _convert_expr_to_python("") == ""


class TestInterpolationWithScaling:
    """Test that interpolated values survive ScalingConverter round-trips.

    This catches issues where interpolation in display space produces values
    that don't convert cleanly back to raw space.
    """

    def _make_converter(self, toexpr="x*0.01", frexpr="x/0.01"):
        """Helper to create a ScalingConverter with given expressions"""
        scaling = Scaling(
            name="test",
            units="",
            toexpr=toexpr,
            frexpr=frexpr,
            format="%.2f",
            min=0,
            max=500,
            inc=0.01,
            storagetype="uint16",
            endian="big",
        )
        return ScalingConverter(scaling)

    def test_linear_interpolation_round_trip(self):
        """Linear interpolated values should survive display->raw->display"""
        converter = self._make_converter("x*0.1", "x/0.1")

        # Simulate: raw endpoints [100, 500], interpolate in display space
        raw_endpoints = np.array([100.0, 500.0])
        display_endpoints = converter.to_display(raw_endpoints)
        # display = [10.0, 50.0]

        # Interpolate 3 intermediate values in display space
        interpolated_display = np.linspace(
            display_endpoints[0], display_endpoints[1], 5
        )
        # [10, 20, 30, 40, 50]

        # Convert back to raw
        interpolated_raw = converter.from_display(interpolated_display)
        # [100, 200, 300, 400, 500]

        # Convert raw back to display to verify
        final_display = converter.to_display(interpolated_raw)
        np.testing.assert_array_almost_equal(
            interpolated_display, final_display, decimal=5
        )

    def test_offset_scaling_interpolation(self):
        """Interpolation with offset scaling (e.g., temperature)"""
        converter = self._make_converter("x*0.01-40", "(x+40)/0.01")

        # Raw endpoints in temp range
        raw_vals = np.array([4000.0, 16000.0])
        display_vals = converter.to_display(raw_vals)
        # display = [0.0, 120.0]

        # Interpolate
        interp = np.linspace(display_vals[0], display_vals[1], 5)
        raw_back = converter.from_display(interp)
        display_back = converter.to_display(raw_back)

        np.testing.assert_array_almost_equal(interp, display_back, decimal=3)

    def test_bilinear_interpolation_values_round_trip(self):
        """Bilinear-interpolated values should survive scaling round-trip"""
        converter = self._make_converter("x*0.5", "x/0.5")

        # 2x2 corner values in raw
        corners_raw = np.array([10.0, 20.0, 30.0, 40.0])
        corners_display = converter.to_display(corners_raw)

        # Bilinear interpolation at center
        f00, f10, f01, f11 = corners_display
        tx, ty = 0.5, 0.5
        center_display = (
            (1 - tx) * (1 - ty) * f00
            + tx * (1 - ty) * f10
            + (1 - tx) * ty * f01
            + tx * ty * f11
        )

        # Round-trip
        center_raw = converter.from_display(center_display)
        center_display_back = converter.to_display(center_raw)

        assert center_display_back == pytest.approx(center_display, rel=1e-5)

    def test_interpolation_near_zero(self):
        """Interpolation near zero shouldn't produce NaN or Inf"""
        converter = self._make_converter("x*0.001", "x/0.001")

        small_vals = np.array([0.0, 0.001, 0.002, 0.003])
        raw = converter.from_display(small_vals)
        display_back = converter.to_display(raw)

        assert not np.any(np.isnan(display_back))
        assert not np.any(np.isinf(display_back))
        np.testing.assert_array_almost_equal(small_vals, display_back, decimal=5)

    def test_negative_interpolation_values(self):
        """Negative values in display space should convert correctly"""
        converter = self._make_converter("x-128", "x+128")

        display_vals = np.array([-40.0, -20.0, 0.0, 20.0, 40.0])
        raw = converter.from_display(display_vals)
        display_back = converter.to_display(raw)

        np.testing.assert_array_almost_equal(display_vals, display_back, decimal=5)


class TestScalingConverterEdgeCases:
    """Test edge cases in ScalingConverter"""

    def test_identity_conversion(self):
        """Identity expression x should pass through unchanged"""
        scaling = Scaling(
            name="identity",
            units="",
            toexpr="x",
            frexpr="x",
            format="%.0f",
            min=0,
            max=255,
            inc=1,
            storagetype="uint8",
            endian="big",
        )
        converter = ScalingConverter(scaling)

        vals = np.array([0, 1, 127, 255], dtype=float)
        assert np.array_equal(converter.to_display(vals), vals)
        assert np.array_equal(converter.from_display(vals), vals)

    def test_large_array_conversion(self):
        """Conversion should handle arrays of typical ROM table size"""
        scaling = Scaling(
            name="test",
            units="",
            toexpr="x*0.01",
            frexpr="x/0.01",
            format="%.2f",
            min=0,
            max=500,
            inc=0.01,
            storagetype="uint16",
            endian="big",
        )
        converter = ScalingConverter(scaling)

        # Typical 20x20 3D table = 400 elements
        raw = np.arange(400, dtype=float) * 100
        display = converter.to_display(raw)
        raw_back = converter.from_display(display)

        np.testing.assert_array_almost_equal(raw, raw_back, decimal=3)

    def test_single_value_conversion(self):
        """Single scalar value (not array) should work"""
        scaling = Scaling(
            name="test",
            units="V",
            toexpr="x*0.001",
            frexpr="x/0.001",
            format="%.3f",
            min=0,
            max=5,
            inc=0.001,
            storagetype="uint16",
            endian="big",
        )
        converter = ScalingConverter(scaling)

        raw = 3500.0
        display = converter.to_display(raw)
        assert isinstance(display, float)
        assert display == pytest.approx(3.5)

        raw_back = converter.from_display(display)
        assert isinstance(raw_back, float)
        assert raw_back == pytest.approx(3500.0)
