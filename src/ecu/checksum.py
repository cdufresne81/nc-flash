"""Mazda ROM checksum calculation and correction."""

import struct
import zlib
from .constants import (
    CHECKSUM_MAGIC,
    CHECKSUM_TABLE_OFFSET,
    CHECKSUM_TABLE_END,
    CHECKSUM_ENTRY_SIZE,
)


def bswap32(value: int) -> int:
    """Byte-swap a 32-bit value (big-endian <-> little-endian)."""
    return struct.unpack("<I", struct.pack(">I", value & 0xFFFFFFFF))[0]


def mazda_checksum(rom_data: bytes, start: int, end: int) -> int:
    """
    Calculate Mazda ECU checksum over a ROM range.

    Sums all big-endian 32-bit words in [start, end) and returns
    CHECKSUM_MAGIC - total. The checksum table stores the expected
    value so that sum + stored_checksum == CHECKSUM_MAGIC.
    """
    total = 0
    for offset in range(start, end, 4):
        word = int.from_bytes(rom_data[offset : offset + 4], "big")
        total = (total + word) & 0xFFFFFFFF
    return (CHECKSUM_MAGIC - total) & 0xFFFFFFFF


def correct_rom_checksums(rom_data: bytearray) -> list[tuple[int, int, int, int, int]]:
    """
    Fix all checksums in the ROM checksum table.

    Reads checksum table at ROM offset CHECKSUM_TABLE_OFFSET.
    Each 12-byte entry: start_addr(4, BE), end_addr(4, BE), checksum(4, BE).
    Iterates entries until CHECKSUM_TABLE_END.

    Returns list of (start, end, checksum_offset, old_value, new_value) tuples.
    """
    corrections = []
    offset = CHECKSUM_TABLE_OFFSET

    while offset < CHECKSUM_TABLE_END:
        start = int.from_bytes(rom_data[offset : offset + 4], "big")
        end = int.from_bytes(rom_data[offset + 4 : offset + 8], "big")

        # End of table sentinel: start==0 and end==0
        if start == 0 and end == 0:
            break

        checksum_offset = offset + 8
        old_value = int.from_bytes(
            rom_data[checksum_offset : checksum_offset + 4], "big"
        )
        new_value = mazda_checksum(rom_data, start, end)

        if old_value != new_value:
            rom_data[checksum_offset : checksum_offset + 4] = new_value.to_bytes(
                4, "big"
            )
            corrections.append((start, end, checksum_offset, old_value, new_value))

        offset += CHECKSUM_ENTRY_SIZE

    return corrections


def crc32(data: bytes) -> int:
    """
    Calculate standard CRC-32 checksum (unsigned 32-bit).

    Used for patch validation against romdrop.crc database.
    """
    return zlib.crc32(data) & 0xFFFFFFFF
