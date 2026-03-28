"""Tests for rounding utility functions in src/utils/formatting.py"""

import pytest

from src.utils.formatting import (
    get_effective_decimal_places,
    round_one_level_coarser,
    _get_format_precision,
)


class TestGetFormatPrecision:
    def test_two_decimal_float(self):
        assert _get_format_precision(".2f") == 2

    def test_zero_decimal_float(self):
        assert _get_format_precision(".0f") == 0

    def test_one_decimal_float(self):
        assert _get_format_precision(".1f") == 1

    def test_three_decimal_float(self):
        assert _get_format_precision(".3f") == 3

    def test_integer_specifier(self):
        assert _get_format_precision("d") == 0

    def test_hex_specifier(self):
        assert _get_format_precision("8x") == 0

    def test_width_and_precision(self):
        assert _get_format_precision("8.2f") == 2

    def test_empty_string(self):
        assert _get_format_precision("") == 0


class TestGetEffectiveDecimalPlaces:
    def test_two_decimals(self):
        assert get_effective_decimal_places(12.11, 2) == 2

    def test_trailing_zero(self):
        assert get_effective_decimal_places(12.10, 2) == 1

    def test_two_trailing_zeros(self):
        assert get_effective_decimal_places(12.00, 2) == 0

    def test_integer_value(self):
        assert get_effective_decimal_places(100.0, 2) == 0

    def test_max_zero(self):
        assert get_effective_decimal_places(12.34, 0) == 0

    def test_three_decimals(self):
        assert get_effective_decimal_places(1.234, 3) == 3

    def test_one_effective_in_three(self):
        assert get_effective_decimal_places(1.200, 3) == 1

    def test_negative_value(self):
        assert get_effective_decimal_places(-12.30, 2) == 1

    def test_zero(self):
        assert get_effective_decimal_places(0.0, 2) == 0


class TestRoundOneLevelCoarser:
    def test_two_decimals_to_one(self):
        # 12.11 has 2 effective decimals -> round to 1
        assert round_one_level_coarser(12.11, ".2f") == 12.1

    def test_one_decimal_to_zero(self):
        # 12.10 has 1 effective decimal -> round to 0
        assert round_one_level_coarser(12.10, ".2f") == 12.0

    def test_already_integer_no_change(self):
        # 12.00 has 0 effective decimals -> no change
        assert round_one_level_coarser(12.0, ".2f") == 12.0

    def test_round_up(self):
        # 12.55 has 2 effective decimals -> round to 1 -> 12.6
        assert round_one_level_coarser(12.55, ".2f") == 12.6

    def test_round_down(self):
        # 12.14 has 2 effective decimals -> round to 1 -> 12.1
        assert round_one_level_coarser(12.14, ".2f") == 12.1

    def test_negative_value(self):
        # -12.36 has 2 effective decimals -> round to 1 -> -12.4
        assert round_one_level_coarser(-12.36, ".2f") == -12.4

    def test_integer_format_no_change(self):
        # With .0f format, value is already at 0 precision
        assert round_one_level_coarser(100.0, ".0f") == 100.0

    def test_three_decimals_to_two(self):
        # 1.234 has 3 effective -> round to 2
        assert round_one_level_coarser(1.234, ".3f") == 1.23

    def test_one_decimal_format(self):
        # 12.3 has 1 effective decimal in .1f format -> round to 0
        assert round_one_level_coarser(12.3, ".1f") == 12.0

    def test_zero_value(self):
        assert round_one_level_coarser(0.0, ".2f") == 0.0

    def test_large_value(self):
        assert round_one_level_coarser(1234.56, ".2f") == 1234.6

    def test_repeated_rounding(self):
        """Simulate pressing R multiple times"""
        val = 12.34
        val = round_one_level_coarser(val, ".2f")
        assert val == 12.3
        val = round_one_level_coarser(val, ".2f")
        assert val == 12.0
        val = round_one_level_coarser(val, ".2f")
        assert val == 12.0  # No further change
