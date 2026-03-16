"""ROM file validation and utility functions."""

import logging

from .constants import (
    ROM_SIZE,
    ROM_FLASH_START_MIN,
    ROM_ID_OFFSET,
    CAL_ID_OFFSETS,
    GEN_DETECT_OFFSET,
    GEN_NC1,
    GEN_NC2_A,
    GEN_NC2_B,
    DYNAMIC_ALIGN_SMALL,
    DYNAMIC_ALIGN_LARGE,
    DYNAMIC_THRESHOLD,
)
from .exceptions import ROMValidationError, VehicleGenerationError

logger = logging.getLogger(__name__)

# Offset and size for calibration CRC region (matches romdrop)
_CAL_CRC_OFFSET = 0x2000
_CAL_CRC_SIZE = 0xFE000  # rom[0x2000:0x100000]


def validate_rom_size(rom_data: bytes) -> bool:
    """Validate that ROM data is exactly 1 MB."""
    return len(rom_data) == ROM_SIZE


def detect_vehicle_generation(rom_data: bytes) -> str:
    """
    Detect NC1/NC2 generation from ROM byte at offset 0x2030.

    0x35 = NC1, 0x36/0x37 = NC2.
    Raises VehicleGenerationError if unknown.
    """
    if len(rom_data) <= GEN_DETECT_OFFSET:
        raise VehicleGenerationError("ROM too small to detect vehicle generation")

    gen_byte = rom_data[GEN_DETECT_OFFSET]
    if gen_byte == GEN_NC1:
        return "NC1"
    elif gen_byte in (GEN_NC2_A, GEN_NC2_B):
        return "NC2"
    else:
        raise VehicleGenerationError(
            f"Unknown generation byte 0x{gen_byte:02X} at offset 0x{GEN_DETECT_OFFSET:X}"
        )


def get_rom_id(rom_data: bytes) -> str:
    """
    Extract ROM ID string from offset 0xFFC4C.

    Returns 3-4 byte hex string identifier.
    """
    if len(rom_data) < ROM_ID_OFFSET + 4:
        raise ROMValidationError("ROM too small to read ROM ID")

    # ROM ID is typically 3-4 ASCII bytes
    raw = rom_data[ROM_ID_OFFSET : ROM_ID_OFFSET + 4]
    # Strip trailing null/0xFF bytes
    id_bytes = raw.rstrip(b"\x00").rstrip(b"\xff")
    if not id_bytes:
        raise ROMValidationError("Empty ROM ID at offset 0x{:X}".format(ROM_ID_OFFSET))
    return id_bytes.hex().upper()


def get_cal_id(rom_data: bytes) -> bytes:
    """
    Extract 6-byte calibration ID from ROM.

    Tries offset 0xC0046 first, then 0xB8046 as fallback.
    """
    for offset in CAL_ID_OFFSETS:
        if len(rom_data) >= offset + 6:
            cal_id = rom_data[offset : offset + 6]
            # Check if it looks valid (not all 0xFF)
            if cal_id != b"\xff" * 6:
                return cal_id

    raise ROMValidationError("Could not find valid calibration ID in ROM")


def find_first_difference(rom_a: bytes, rom_b: bytes) -> int:
    """
    Find the offset of the first differing byte between two ROMs.

    Returns -1 if ROMs are identical.
    """
    min_len = min(len(rom_a), len(rom_b))
    for i in range(min_len):
        if rom_a[i] != rom_b[i]:
            return i
    if len(rom_a) != len(rom_b):
        return min_len
    return -1


def calculate_flash_start_index(diff_offset: int) -> int:
    """
    Calculate the aligned flash start index for dynamic flashing.

    For offsets < DYNAMIC_THRESHOLD (0x8000): align down to DYNAMIC_ALIGN_SMALL (0x1000)
    For offsets >= DYNAMIC_THRESHOLD: align down to DYNAMIC_ALIGN_LARGE (0x20000)

    The result is clamped to ROM_FLASH_START_MIN (0x2000) minimum.
    """
    if diff_offset < DYNAMIC_THRESHOLD:
        aligned = (diff_offset // DYNAMIC_ALIGN_SMALL) * DYNAMIC_ALIGN_SMALL
    else:
        aligned = (diff_offset // DYNAMIC_ALIGN_LARGE) * DYNAMIC_ALIGN_LARGE

    return max(aligned, ROM_FLASH_START_MIN)


def get_calibration_crc(rom_data: bytes) -> int:
    """
    Calculate CRC-32 over the calibration region (0x2000 to end).

    This matches romdrop's validate_factory_calibration / validate_patched_calibration
    which compute crc32(rom[0x2000], 0xFE000).
    """
    from .checksum import crc32

    return crc32(rom_data[_CAL_CRC_OFFSET : _CAL_CRC_OFFSET + _CAL_CRC_SIZE])


class PatchResult:
    """Result of a ROM patch operation."""

    def __init__(
        self,
        patched_rom: bytearray,
        cal_id: bytes,
        rom_id: str,
        stock_crc: int,
        patch_crc: int,
        patched_crc: int,
        crc_verified: bool = False,
        crc_warnings: list[str] | None = None,
    ):
        self.patched_rom = patched_rom
        self.cal_id = cal_id
        self.rom_id = rom_id
        self.stock_crc = stock_crc
        self.patch_crc = patch_crc
        self.patched_crc = patched_crc
        self.crc_verified = crc_verified
        self.crc_warnings = crc_warnings or []

    def suggested_filename(self) -> str:
        """Generate filename in romdrop style: <cal-id>_Rev_<rom-id>.bin"""
        cal_str = self.cal_id.decode("ascii", errors="replace").rstrip("\x00")
        return f"{cal_str}_Rev_{self.rom_id}.bin"


def _load_crc_database():
    """Load the bundled romdrop.crc database. Returns None if not found."""
    from pathlib import Path

    crc_path = Path(__file__).parent / "romdrop.crc"
    if not crc_path.is_file():
        return None
    try:
        from .crc_database import CRCDatabase

        return CRCDatabase.from_file(crc_path)
    except Exception as e:
        logger.warning("Failed to load CRC database %s: %s", crc_path, e)
        return None


def patch_rom(stock_rom: bytes, patch_data: bytes) -> PatchResult:
    """
    Apply an XOR patch to a stock ROM, producing a patched ROM.

    Reproduces romdrop's patch_stock_rom (0x004060EA):
    1. Validate both are 1MB
    2. Validate stock ROM has a valid cal-ID (starts with 'L')
    3. Validate patch data has a valid header (starts with 'L')
    4. XOR patch onto stock ROM from offset 0x2000 to end
    5. Extract cal-ID and ROM-ID from the result
    6. Verify CRCs against romdrop.crc database

    Args:
        stock_rom: Factory/stock ROM data (1MB)
        patch_data: Patch file data (1MB, XOR mask)

    Returns:
        PatchResult with patched ROM, identifiers, CRCs, and verification status

    Raises:
        ROMValidationError: If inputs fail validation
    """
    if not validate_rom_size(stock_rom):
        raise ROMValidationError(
            f"Stock ROM must be exactly {ROM_SIZE} bytes, got {len(stock_rom)}"
        )
    if not validate_rom_size(patch_data):
        raise ROMValidationError(
            f"Patch file must be exactly {ROM_SIZE} bytes, got {len(patch_data)}"
        )

    # Validate stock ROM has valid cal-ID (romdrop checks for 'L' at cal-ID offset)
    try:
        stock_cal_id = get_cal_id(stock_rom)
    except ROMValidationError:
        raise ROMValidationError("Invalid stock ROM: cannot locate calibration ID")

    if stock_cal_id[0:1] != b"L":
        raise ROMValidationError(
            "Invalid stock ROM: calibration ID does not start with 'L'"
        )

    # Validate patch starts with 'L' (romdrop checks patch_data[0] == 'L')
    if patch_data[0:1] != b"L":
        raise ROMValidationError(
            "Invalid patch file: first byte is not 'L' — cannot locate cal-id"
        )

    # Compute CRCs
    stock_crc = get_calibration_crc(stock_rom)
    patch_crc = get_calibration_crc(patch_data)

    # Clear the factory checksum area (romdrop sets rom[0xFFB00:0xFFB08] = 0xFFFFFFFF)
    patched = bytearray(stock_rom)
    patched[0xFFB00:0xFFB04] = b"\xff\xff\xff\xff"
    patched[0xFFB04:0xFFB08] = b"\xff\xff\xff\xff"

    # XOR patch onto ROM from offset 0x2000 to end
    for i in range(_CAL_CRC_OFFSET, ROM_SIZE):
        patched[i] ^= patch_data[i]

    # Extract identifiers from patched result
    cal_id = get_cal_id(patched)
    rom_id_str = get_rom_id(patched)
    patched_crc = get_calibration_crc(patched)

    # Verify against CRC database
    crc_verified = False
    crc_warnings = []

    db = _load_crc_database()
    if db is None:
        crc_warnings.append(
            "CRC database (romdrop.crc) not found — skipping verification"
        )
    else:
        entry = db.find_entry(stock_cal_id)
        if entry is None:
            crc_warnings.append(
                f"Cal-ID {stock_cal_id!r} not found in CRC database — "
                "cannot verify this calibration"
            )
        else:
            all_ok = True
            if stock_crc != entry.factory_crc:
                crc_warnings.append(
                    f"Stock ROM CRC mismatch: got 0x{stock_crc:08X}, "
                    f"expected 0x{entry.factory_crc:08X}"
                )
                all_ok = False
            if patch_crc != entry.patch_crc:
                crc_warnings.append(
                    f"Patch file CRC mismatch: got 0x{patch_crc:08X}, "
                    f"expected 0x{entry.patch_crc:08X}"
                )
                all_ok = False
            if patched_crc != entry.patched_cal_crc:
                crc_warnings.append(
                    f"Patched ROM CRC mismatch: got 0x{patched_crc:08X}, "
                    f"expected 0x{entry.patched_cal_crc:08X}"
                )
                all_ok = False
            if all_ok:
                crc_verified = True

    return PatchResult(
        patched_rom=patched,
        cal_id=cal_id,
        rom_id=rom_id_str,
        stock_crc=stock_crc,
        patch_crc=patch_crc,
        patched_crc=patched_crc,
        crc_verified=crc_verified,
        crc_warnings=crc_warnings,
    )
