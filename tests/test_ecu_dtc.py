"""Tests for src/ecu/dtc.py — DTC formatting and lookup."""

import pytest
from src.ecu.dtc import format_dtc, get_dtc_prefix, get_dtc_description, DTC_TABLE


class TestFormatDtc:
    """Test format_dtc() OBD-II string formatting."""

    def test_p_code(self):
        """Powertrain code (category 0b00)."""
        assert format_dtc(0x0011) == "P0011"

    def test_p_code_high(self):
        """Higher P-code."""
        assert format_dtc(0x2101) == "P2101"

    def test_c_code(self):
        """Chassis code (category 0b01 = 0x4000 prefix)."""
        assert format_dtc(0x4073) == "C0073"

    def test_b_code(self):
        """Body code (category 0b10 = 0x8000 prefix)."""
        assert format_dtc(0x8100) == "B0100"

    def test_u_code(self):
        """Network code (category 0b11 = 0xC000 prefix)."""
        assert format_dtc(0xC100) == "U0100"


class TestGetDtcPrefix:
    """Test get_dtc_prefix() category mapping."""

    def test_powertrain(self):
        """Category 0b00 -> P."""
        assert get_dtc_prefix(0x0000) == "P"

    def test_chassis(self):
        """Category 0b01 -> C."""
        assert get_dtc_prefix(0x4000) == "C"

    def test_body(self):
        """Category 0b10 -> B."""
        assert get_dtc_prefix(0x8000) == "B"

    def test_network(self):
        """Category 0b11 -> U."""
        assert get_dtc_prefix(0xC000) == "U"


class TestGetDtcDescription:
    """Test get_dtc_description() lookup."""

    def test_known_p_code(self):
        """Known powertrain code returns full description."""
        desc = get_dtc_description(0x0300)
        assert "Random/multiple cylinder misfire" in desc

    def test_known_c_code(self):
        """Known chassis code returns description."""
        desc = get_dtc_description(0x4073)
        assert "bus off" in desc.lower()

    def test_unknown_code(self):
        """Unknown code returns formatted fallback."""
        desc = get_dtc_description(0x0001)
        assert "Unknown DTC" in desc
        assert "P0001" in desc

    def test_all_table_entries_have_descriptions(self):
        """Every entry in DTC_TABLE has a non-empty description string."""
        for code, desc in DTC_TABLE.items():
            assert (
                isinstance(desc, str) and len(desc) > 0
            ), f"Bad entry for 0x{code:04X}"
