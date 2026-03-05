"""Tests for src/ecu/rom_utils.py — ROM validation and utilities."""

import pytest
from src.ecu.rom_utils import (
    validate_rom_size,
    detect_vehicle_generation,
    get_cal_id,
    find_first_difference,
    calculate_flash_start_index,
)
from src.ecu.constants import (
    ROM_SIZE,
    GEN_DETECT_OFFSET,
    GEN_NC1,
    GEN_NC2_A,
    GEN_NC2_B,
    CAL_ID_OFFSETS,
    ROM_FLASH_START_MIN,
    DYNAMIC_THRESHOLD,
)
from src.ecu.exceptions import ROMValidationError, VehicleGenerationError


class TestValidateRomSize:
    """Test validate_rom_size()."""

    def test_valid_1mb(self):
        """1MB ROM passes validation."""
        assert validate_rom_size(b"\x00" * ROM_SIZE) is True

    def test_too_small(self):
        """Smaller than 1MB fails."""
        assert validate_rom_size(b"\x00" * (ROM_SIZE - 1)) is False

    def test_too_large(self):
        """Larger than 1MB fails."""
        assert validate_rom_size(b"\x00" * (ROM_SIZE + 1)) is False

    def test_empty(self):
        """Empty data fails."""
        assert validate_rom_size(b"") is False


class TestDetectVehicleGeneration:
    """Test detect_vehicle_generation()."""

    def _rom_with_gen_byte(self, byte_val):
        """Create a 1MB ROM with a specific generation byte."""
        rom = bytearray(ROM_SIZE)
        rom[GEN_DETECT_OFFSET] = byte_val
        return bytes(rom)

    def test_nc1(self):
        """0x35 at offset 0x2030 is NC1."""
        assert detect_vehicle_generation(self._rom_with_gen_byte(GEN_NC1)) == "NC1"

    def test_nc2_a(self):
        """0x36 at offset 0x2030 is NC2."""
        assert detect_vehicle_generation(self._rom_with_gen_byte(GEN_NC2_A)) == "NC2"

    def test_nc2_b(self):
        """0x37 at offset 0x2030 is NC2."""
        assert detect_vehicle_generation(self._rom_with_gen_byte(GEN_NC2_B)) == "NC2"

    def test_unknown_raises(self):
        """Unknown generation byte raises VehicleGenerationError."""
        with pytest.raises(VehicleGenerationError, match="Unknown generation byte"):
            detect_vehicle_generation(self._rom_with_gen_byte(0x99))

    def test_rom_too_small_raises(self):
        """ROM smaller than detection offset raises."""
        with pytest.raises(VehicleGenerationError, match="ROM too small"):
            detect_vehicle_generation(b"\x00" * GEN_DETECT_OFFSET)


class TestGetCalId:
    """Test get_cal_id()."""

    def test_primary_offset(self):
        """Cal-ID found at primary offset (0xC0046)."""
        rom = bytearray(ROM_SIZE)
        cal_id = b"LFNC01"
        rom[CAL_ID_OFFSETS[0] : CAL_ID_OFFSETS[0] + 6] = cal_id
        assert get_cal_id(bytes(rom)) == cal_id

    def test_fallback_offset(self):
        """Cal-ID found at fallback offset (0xB8046) when primary is 0xFF."""
        rom = bytearray(b"\xff" * ROM_SIZE)
        cal_id = b"LFNC02"
        rom[CAL_ID_OFFSETS[1] : CAL_ID_OFFSETS[1] + 6] = cal_id
        assert get_cal_id(bytes(rom)) == cal_id

    def test_no_valid_cal_id_raises(self):
        """All-0xFF at both offsets raises ROMValidationError."""
        rom = b"\xff" * ROM_SIZE
        with pytest.raises(
            ROMValidationError, match="Could not find valid calibration ID"
        ):
            get_cal_id(rom)


class TestFindFirstDifference:
    """Test find_first_difference()."""

    def test_identical(self):
        """Identical ROMs return -1."""
        data = b"\xaa" * 100
        assert find_first_difference(data, data) == -1

    def test_first_byte_differs(self):
        """Difference at offset 0."""
        a = b"\x01" + b"\x00" * 99
        b = b"\x02" + b"\x00" * 99
        assert find_first_difference(a, b) == 0

    def test_middle_difference(self):
        """Difference in the middle."""
        a = bytearray(100)
        b = bytearray(100)
        b[50] = 0xFF
        assert find_first_difference(bytes(a), bytes(b)) == 50

    def test_length_mismatch(self):
        """Different lengths return min_len as diff offset."""
        a = b"\x00" * 100
        b = b"\x00" * 50
        assert find_first_difference(a, b) == 50


class TestCalculateFlashStartIndex:
    """Test calculate_flash_start_index()."""

    def test_small_offset_alignment(self):
        """Offsets < 0x8000 align to 0x1000."""
        # 0x3500 aligns down to 0x3000
        assert calculate_flash_start_index(0x3500) == 0x3000

    def test_large_offset_alignment(self):
        """Offsets >= 0x8000 align to 0x20000."""
        # 0x45000 aligns down to 0x40000
        assert calculate_flash_start_index(0x45000) == 0x40000

    def test_minimum_clamp(self):
        """Result is clamped to ROM_FLASH_START_MIN (0x2000)."""
        # 0x0500 aligns to 0x0000 but is clamped to 0x2000
        assert calculate_flash_start_index(0x0500) == ROM_FLASH_START_MIN

    def test_exact_threshold(self):
        """Offset exactly at threshold uses large alignment, clamped to minimum."""
        # 0x8000 // 0x20000 = 0, clamped to ROM_FLASH_START_MIN
        result = calculate_flash_start_index(DYNAMIC_THRESHOLD)
        assert result == ROM_FLASH_START_MIN

    def test_already_aligned_small(self):
        """Already-aligned small offset stays the same."""
        assert calculate_flash_start_index(0x3000) == 0x3000

    def test_already_aligned_large(self):
        """Already-aligned large offset stays the same."""
        assert calculate_flash_start_index(0x60000) == 0x60000
