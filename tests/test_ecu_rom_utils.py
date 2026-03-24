"""Tests for src/ecu/rom_utils.py — ROM validation and utilities."""

from pathlib import Path

import pytest
from src.ecu.rom_utils import (
    validate_rom_size,
    detect_vehicle_generation,
    get_cal_id,
    get_rom_id,
    get_calibration_crc,
    find_first_difference,
    calculate_flash_start_index,
    patch_rom,
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

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"

# LF9VEB test files
STOCK_ROM = EXAMPLES_DIR / "lf9veb.bin"
PATCH_FILE = EXAMPLES_DIR / "lf9veb.patch"
ROMDROP_OUTPUT = EXAMPLES_DIR / "LF9VEB_Rev_21053000.bin"

# LF5AEG test files
STOCK_ROM_5AEG = EXAMPLES_DIR / "lf5aeg.bin"
PATCH_FILE_5AEG = EXAMPLES_DIR / "lf5aeg.patch"
ROMDROP_OUTPUT_5AEG = EXAMPLES_DIR / "LF5AEG_Rev_21053000.bin"


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
        """Cal-ID found at primary offset (0xC0046) when it starts with L."""
        rom = bytearray(ROM_SIZE)
        cal_id = b"LFNC01"
        rom[CAL_ID_OFFSETS[0] : CAL_ID_OFFSETS[0] + 6] = cal_id
        assert get_cal_id(bytes(rom)) == cal_id

    def test_fallback_offset(self):
        """Cal-ID found at fallback offset (0xB8046) when primary doesn't start with L."""
        rom = bytearray(ROM_SIZE)
        # Primary offset has non-L garbage
        rom[CAL_ID_OFFSETS[0] : CAL_ID_OFFSETS[0] + 6] = b"\x00\x00Ax\x00\x00"
        # Fallback has valid cal-ID
        cal_id = b"LFNC02"
        rom[CAL_ID_OFFSETS[1] : CAL_ID_OFFSETS[1] + 6] = cal_id
        assert get_cal_id(bytes(rom)) == cal_id

    def test_fallback_when_primary_all_ff(self):
        """Cal-ID found at fallback offset when primary is all 0xFF."""
        rom = bytearray(b"\xff" * ROM_SIZE)
        cal_id = b"LFNC02"
        rom[CAL_ID_OFFSETS[1] : CAL_ID_OFFSETS[1] + 6] = cal_id
        assert get_cal_id(bytes(rom)) == cal_id

    def test_no_valid_cal_id_raises(self):
        """No L-prefixed cal-ID at either offset raises ROMValidationError."""
        rom = b"\xff" * ROM_SIZE
        with pytest.raises(
            ROMValidationError, match="Could not find valid calibration ID"
        ):
            get_cal_id(rom)

    def test_non_L_at_both_offsets_raises(self):
        """Non-L bytes at both offsets raises ROMValidationError."""
        rom = bytearray(ROM_SIZE)
        rom[CAL_ID_OFFSETS[0] : CAL_ID_OFFSETS[0] + 6] = b"XFAKE1"
        rom[CAL_ID_OFFSETS[1] : CAL_ID_OFFSETS[1] + 6] = b"ZFAKE2"
        with pytest.raises(
            ROMValidationError, match="Could not find valid calibration ID"
        ):
            get_cal_id(bytes(rom))

    @pytest.mark.skipif(not STOCK_ROM.exists(), reason="Stock ROM not available")
    def test_real_rom_lf9veb(self):
        """Real lf9veb.bin has cal-ID LF9VEB at fallback offset."""
        rom = STOCK_ROM.read_bytes()
        assert get_cal_id(rom) == b"LF9VEB"


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


# ---------------------------------------------------------------------------
# Patching tests
# ---------------------------------------------------------------------------

_has_9veb_files = STOCK_ROM.exists() and PATCH_FILE.exists() and ROMDROP_OUTPUT.exists()
_skip_no_9veb = pytest.mark.skipif(
    not _has_9veb_files, reason="Requires lf9veb.bin, lf9veb.patch, and RomDrop output"
)

_has_5aeg_files = (
    STOCK_ROM_5AEG.exists()
    and PATCH_FILE_5AEG.exists()
    and ROMDROP_OUTPUT_5AEG.exists()
)
_skip_no_5aeg = pytest.mark.skipif(
    not _has_5aeg_files, reason="Requires lf5aeg.bin, lf5aeg.patch, and RomDrop output"
)

_has_both = _has_9veb_files and _has_5aeg_files
_skip_no_both = pytest.mark.skipif(
    not _has_both, reason="Requires both LF9VEB and LF5AEG test files"
)


class TestPatchRomGoldenFile:
    """Golden-file test: patch_rom() must produce byte-identical output to RomDrop."""

    @_skip_no_9veb
    def test_byte_for_byte_match(self):
        """Patched ROM is identical to RomDrop's LF9VEB_Rev_21053000.bin."""
        stock = STOCK_ROM.read_bytes()
        patch = PATCH_FILE.read_bytes()
        expected = ROMDROP_OUTPUT.read_bytes()

        result = patch_rom(stock, patch)

        assert bytes(result.patched_rom) == expected

    @_skip_no_9veb
    def test_cal_id(self):
        """Patched ROM has cal-ID LF9VEB."""
        result = patch_rom(STOCK_ROM.read_bytes(), PATCH_FILE.read_bytes())
        assert bytes(result.cal_id) == b"LF9VEB"

    @_skip_no_9veb
    def test_rom_id(self):
        """Patched ROM has ROM ID from patch revision."""
        result = patch_rom(STOCK_ROM.read_bytes(), PATCH_FILE.read_bytes())
        assert result.rom_id == "21053000"

    @_skip_no_9veb
    def test_crc_values_match_romdrop(self):
        """CRC values match RomDrop's console output."""
        result = patch_rom(STOCK_ROM.read_bytes(), PATCH_FILE.read_bytes())
        assert result.stock_crc == 0x808CC3
        assert result.patch_crc == 0xA8C30A1
        assert result.patched_crc == 0x49338194

    @_skip_no_9veb
    def test_crc_verified(self):
        """CRC database verification passes."""
        result = patch_rom(STOCK_ROM.read_bytes(), PATCH_FILE.read_bytes())
        assert result.crc_verified is True
        assert result.crc_warnings == []


class TestPatchRomValidation:
    """Test patch_rom() input validation."""

    def _make_stock_rom(self):
        """Create a minimal valid stock ROM with an L-prefixed cal-ID and ROM ID."""
        rom = bytearray(ROM_SIZE)
        rom[CAL_ID_OFFSETS[1] : CAL_ID_OFFSETS[1] + 6] = b"LTEST1"
        rom[0xFFC4C : 0xFFC4C + 4] = b"\x21\x05\x30\x00"  # ROM ID
        return bytes(rom)

    def _make_patch(self):
        """Create a minimal valid patch file (starts with L)."""
        patch = bytearray(ROM_SIZE)
        patch[0] = ord("L")
        return bytes(patch)

    def test_stock_rom_wrong_size(self):
        """Stock ROM not 1MB raises ROMValidationError."""
        with pytest.raises(ROMValidationError, match="Stock ROM must be exactly"):
            patch_rom(b"\x00" * 1000, self._make_patch())

    def test_patch_wrong_size(self):
        """Patch file not 1MB raises ROMValidationError."""
        with pytest.raises(ROMValidationError, match="Patch file must be exactly"):
            patch_rom(self._make_stock_rom(), b"\x00" * 1000)

    def test_stock_rom_no_cal_id(self):
        """Stock ROM with no valid cal-ID raises ROMValidationError."""
        rom = b"\x00" * ROM_SIZE  # No L at either cal-ID offset
        with pytest.raises(ROMValidationError, match="calibration ID"):
            patch_rom(rom, self._make_patch())

    def test_patch_invalid_header(self):
        """Patch not starting with L raises ROMValidationError."""
        patch = b"\x00" * ROM_SIZE
        with pytest.raises(ROMValidationError, match="first byte is not 'L'"):
            patch_rom(self._make_stock_rom(), patch)

    def test_xor_identity(self):
        """Patching with all-zeros (except header) returns the stock ROM unchanged in the XOR region."""
        stock = self._make_stock_rom()
        patch = self._make_patch()  # L + zeros

        result = patch_rom(stock, patch)

        # XOR with zeros is identity — patched should match stock from 0x2000 onward
        # (except the 0xFFB00-0xFFB08 area which gets cleared to 0xFF)
        patched = result.patched_rom
        stock_arr = bytearray(stock)
        stock_arr[0xFFB00:0xFFB08] = b"\xff" * 8
        assert bytes(patched[0x2000:]) == bytes(stock_arr[0x2000:])


class TestPatchRomGoldenFileLF5AEG:
    """Golden-file test for LF5AEG calibration."""

    @_skip_no_5aeg
    def test_byte_for_byte_match(self):
        """Patched ROM is identical to RomDrop's LF5AEG_Rev_21053000.bin."""
        stock = STOCK_ROM_5AEG.read_bytes()
        patch = PATCH_FILE_5AEG.read_bytes()
        expected = ROMDROP_OUTPUT_5AEG.read_bytes()

        result = patch_rom(stock, patch)

        assert bytes(result.patched_rom) == expected

    @_skip_no_5aeg
    def test_cal_id(self):
        """Patched ROM has cal-ID LF5AEG."""
        result = patch_rom(STOCK_ROM_5AEG.read_bytes(), PATCH_FILE_5AEG.read_bytes())
        assert bytes(result.cal_id) == b"LF5AEG"


class TestPatchRomCrossCalReject:
    """Cross-calibration patching must be rejected."""

    @_skip_no_both
    def test_lf9veb_stock_with_lf5aeg_patch(self):
        """Applying LF5AEG patch to LF9VEB stock ROM raises ROMValidationError."""
        with pytest.raises(ROMValidationError, match="Patch file does not match"):
            patch_rom(STOCK_ROM.read_bytes(), PATCH_FILE_5AEG.read_bytes())

    @_skip_no_both
    def test_lf5aeg_stock_with_lf9veb_patch(self):
        """Applying LF9VEB patch to LF5AEG stock ROM raises ROMValidationError."""
        with pytest.raises(ROMValidationError, match="Patch file does not match"):
            patch_rom(STOCK_ROM_5AEG.read_bytes(), PATCH_FILE.read_bytes())
