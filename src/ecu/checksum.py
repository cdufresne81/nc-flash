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
    rom_len = len(rom_data)
    if end <= start or start >= rom_len:
        return CHECKSUM_MAGIC
    end = min(end, rom_len)
    n_words = (end - start) // 4
    if n_words == 0:
        return CHECKSUM_MAGIC
    words = struct.unpack(f">{n_words}I", rom_data[start : start + n_words * 4])
    total = sum(words) & 0xFFFFFFFF
    return (CHECKSUM_MAGIC - total) & 0xFFFFFFFF


def correct_rom_checksums(rom_data: bytearray) -> list[tuple[int, int, int, int, int]]:
    """
    Fix all checksums in the ROM checksum table.

    Reads checksum table at ROM offset CHECKSUM_TABLE_OFFSET.
    Each 12-byte entry: start_addr(4, BE), end_addr_inclusive(4, BE), checksum(4, BE).
    End address is the last byte of the summed range (inclusive).
    Iterates entries until CHECKSUM_TABLE_END or an invalid sentinel.

    Verified against romdrop.exe disassembly at 0x004014B7.

    Returns list of (start, end_inclusive, checksum_offset, old_value, new_value) tuples.
    """
    corrections = []
    offset = CHECKSUM_TABLE_OFFSET

    while offset < CHECKSUM_TABLE_END:
        start = int.from_bytes(rom_data[offset : offset + 4], "big")
        end_incl = int.from_bytes(rom_data[offset + 4 : offset + 8], "big")

        # Sentinel: invalid start address (0xFFFFFFFF or beyond ROM) ends table
        if start >= len(rom_data):
            break

        checksum_offset = offset + 8
        old_value = int.from_bytes(
            rom_data[checksum_offset : checksum_offset + 4], "big"
        )
        # End address in table is inclusive; mazda_checksum takes exclusive end
        new_value = mazda_checksum(rom_data, start, end_incl + 1)

        if old_value != new_value:
            rom_data[checksum_offset : checksum_offset + 4] = new_value.to_bytes(
                4, "big"
            )
            corrections.append((start, end_incl, checksum_offset, old_value, new_value))

        offset += CHECKSUM_ENTRY_SIZE

    return corrections


def crc32(data: bytes) -> int:
    """
    Calculate standard CRC-32 checksum (unsigned 32-bit).

    Used for patch validation against romdrop.crc database.
    """
    return zlib.crc32(data) & 0xFFFFFFFF
