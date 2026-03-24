"""Tests for ECU Info display fixes — VIN parsing, ROM ID, DTC deduplication."""

import pytest
from unittest.mock import MagicMock, patch

from src.ecu.dtc import (
    format_dtc,
    get_dtc_description,
    DTC_TABLE,
)
from src.ecu.protocol import DTC

# ---------------------------------------------------------------------------
# VIN Parsing (truncate to 17 chars, strip garbage)
# ---------------------------------------------------------------------------


class TestVINParsing:
    """VIN should contain only printable ASCII, non-printable bytes stripped."""

    def _parse_vin(self, vin_data: bytes | None) -> str:
        """Replicate the VIN parsing logic from flash_mixin._on_ecu_info."""
        if vin_data:
            raw = vin_data[:17] if len(vin_data) >= 17 else vin_data
            return "".join(chr(b) if 0x20 <= b <= 0x7E else "" for b in raw) or "N/A"
        return "N/A"

    def test_clean_17_char_vin(self):
        vin_data = b"JM1NC2MF9B0123456"
        assert self._parse_vin(vin_data) == "JM1NC2MF9B0123456"

    def test_vin_with_trailing_garbage(self):
        """VIN block from ECU has garbage bytes after the 17-char VIN."""
        vin_data = b"JM1NC2MF9B0123456\xff\xfe\x00\x80garbage"
        result = self._parse_vin(vin_data)
        assert result == "JM1NC2MF9B0123456"

    def test_vin_with_null_padding(self):
        vin_data = b"JM1NC2MF9B0123456\x00\x00\x00\x00"
        assert self._parse_vin(vin_data) == "JM1NC2MF9B0123456"

    def test_vin_with_embedded_non_ascii(self):
        """Non-printable bytes within the 17-char window are stripped."""
        vin_data = b"2FF0A0207980\x80\x90\x80\x90*"
        result = self._parse_vin(vin_data)
        assert "\ufffd" not in result  # No replacement characters
        assert all(0x20 <= ord(c) <= 0x7E for c in result)

    def test_short_vin_data(self):
        """If VIN data is shorter than 17 bytes, return printable chars."""
        vin_data = b"JM1NC2MF9\x00\x00"
        assert self._parse_vin(vin_data) == "JM1NC2MF9"

    def test_none_vin_data(self):
        assert self._parse_vin(None) == "N/A"

    def test_empty_vin_data(self):
        assert self._parse_vin(b"") == "N/A"

    def test_all_garbage_returns_na(self):
        """If all bytes are non-printable, return N/A."""
        vin_data = b"\x00\x80\xff\xfe\x01\x02"
        assert self._parse_vin(vin_data) == "N/A"

    def test_real_world_screenshot_vin(self):
        """Reproduce the garbled VIN from the screenshot — no diamonds."""
        vin_data = b"2FF0A0207980\x80\x90\x80\x90*"
        result = self._parse_vin(vin_data)
        assert result == "2FF0A0207980*"
        assert "\ufffd" not in result


# ---------------------------------------------------------------------------
# ROM ID Prefix Stripping
# ---------------------------------------------------------------------------


class TestROMIDParsing:
    """ROM ID response has 2-byte echo prefix (0xE6, 0x11) to strip."""

    def _parse_rom_id(self, response: bytes) -> str:
        """Replicate read_rom_id logic."""
        if response and len(response) > 2:
            return response[2:].rstrip(b"\x00").decode("ascii", errors="replace")
        return ""

    def test_normal_rom_id(self):
        response = b"\xe6\x11SW-LFDJEA000.HEX\x00"
        assert self._parse_rom_id(response) == "SW-LFDJEA000.HEX"

    def test_rom_id_no_null(self):
        response = b"\xe6\x11SW-LFDJEA000.HEX"
        assert self._parse_rom_id(response) == "SW-LFDJEA000.HEX"

    def test_rom_id_without_prefix_strip_shows_garbage(self):
        """Before the fix, the raw response decoded with garbled first char."""
        response = b"\xe6\x11SW-LFDJEA000.HEX"
        raw = response.rstrip(b"\x00").decode("ascii", errors="replace")
        # First char would be replacement character
        assert raw[0] != "S"

    def test_empty_response(self):
        assert self._parse_rom_id(b"") == ""

    def test_too_short_response(self):
        assert self._parse_rom_id(b"\xe6\x11") == ""


# ---------------------------------------------------------------------------
# DTC Deduplication
# ---------------------------------------------------------------------------


class TestDTCDeduplication:
    """DTCs from the ECU often contain duplicates that should be merged."""

    def _deduplicate(self, dtcs: list[DTC]) -> list[DTC]:
        """Replicate dedup logic from flash_mixin."""
        seen = set()
        unique = []
        for d in dtcs:
            if d.code not in seen:
                seen.add(d.code)
                unique.append(d)
        return unique

    def test_no_duplicates(self):
        dtcs = [DTC(0x0300, 0x01), DTC(0x0420, 0x01)]
        result = self._deduplicate(dtcs)
        assert len(result) == 2

    def test_removes_duplicates(self):
        dtcs = [
            DTC(0xFF01, 0x01),  # U3F01
            DTC(0xFF01, 0x01),  # U3F01 duplicate
            DTC(0xFF01, 0x01),  # U3F01 duplicate
            DTC(0xFF02, 0x01),  # U3F02
        ]
        result = self._deduplicate(dtcs)
        assert len(result) == 2
        assert result[0].code == 0xFF01
        assert result[1].code == 0xFF02

    def test_preserves_order(self):
        dtcs = [
            DTC(0x0F01, 0x01),
            DTC(0xFF02, 0x01),
            DTC(0xFFC1, 0x01),
            DTC(0xFFC1, 0x01),
            DTC(0xFF21, 0x01),
            DTC(0xFF21, 0x01),
            DTC(0xFF21, 0x01),
            DTC(0xFF01, 0x01),
            DTC(0xFF01, 0x01),
            DTC(0xFF03, 0x01),
            DTC(0xFF01, 0x01),
            DTC(0xFF04, 0x01),
            DTC(0xFF04, 0x01),
            DTC(0xFF04, 0x01),
            DTC(0xFF01, 0x01),
        ]
        result = self._deduplicate(dtcs)
        assert len(result) == 7
        assert [d.code for d in result] == [
            0x0F01,
            0xFF02,
            0xFFC1,
            0xFF21,
            0xFF01,
            0xFF03,
            0xFF04,
        ]

    def test_real_world_screenshot_dtcs(self):
        """Reproduce the exact 15 DTCs from the user's screenshot."""
        codes = [
            0x0F01,
            0xFF02,
            0xFFC1,
            0xFFC1,
            0xFF21,
            0xFF21,
            0xFF21,
            0xFF01,
            0xFF01,
            0xFF03,
            0xFF01,
            0xFF04,
            0xFF04,
            0xFF04,
            0xFF01,
        ]
        dtcs = [DTC(c, 0x01) for c in codes]
        result = self._deduplicate(dtcs)
        # 15 raw -> 7 unique
        assert len(result) == 7

    def test_empty_list(self):
        assert self._deduplicate([]) == []


# ---------------------------------------------------------------------------
# New DTC Table Entries
# ---------------------------------------------------------------------------


class TestNewDTCCodes:
    """Verify the U-codes and P0F01 added to the DTC table."""

    @pytest.mark.parametrize(
        "raw_code, expected_formatted, expected_keyword",
        [
            (0x0F01, "P0F01", "battery"),
            (0xFF01, "U3F01", "ECM"),
            (0xFF02, "U3F02", "TCM"),
            (0xFF03, "U3F03", "ABS"),
            (0xFF04, "U3F04", "instrument"),
            (0xFF21, "U3F21", "invalid data"),
            (0xFFC1, "U3FC1", "bus off"),
        ],
    )
    def test_new_code_in_table(self, raw_code, expected_formatted, expected_keyword):
        assert raw_code in DTC_TABLE
        assert expected_formatted in DTC_TABLE[raw_code]
        assert expected_keyword.lower() in DTC_TABLE[raw_code].lower()

    def test_new_codes_format_correctly(self):
        assert format_dtc(0xFF01) == "U3F01"
        assert format_dtc(0xFF02) == "U3F02"
        assert format_dtc(0xFFC1) == "U3FC1"
        assert format_dtc(0x0F01) == "P0F01"

    def test_new_codes_not_unknown(self):
        """These codes should no longer show 'Unknown DTC'."""
        for code in [0x0F01, 0xFF01, 0xFF02, 0xFF03, 0xFF04, 0xFF21, 0xFFC1]:
            desc = get_dtc_description(code)
            assert "Unknown" not in desc, f"0x{code:04X} still shows as unknown"
