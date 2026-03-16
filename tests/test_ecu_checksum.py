"""Tests for src/ecu/checksum.py — Mazda ROM checksum calculation."""

import zlib
from src.ecu.checksum import mazda_checksum, correct_rom_checksums, crc32, bswap32
from src.ecu.constants import (
    CHECKSUM_MAGIC,
    CHECKSUM_TABLE_OFFSET,
    CHECKSUM_ENTRY_SIZE,
    ROM_SIZE,
)


class TestMazdaChecksum:
    """Test mazda_checksum() calculation."""

    def test_known_value(self):
        """Verify checksum against a hand-computed value."""
        # Build a 16-byte region with known 32-bit big-endian words:
        # 0x00000001, 0x00000002, 0x00000003, 0x00000004
        # Sum = 10, expected checksum = CHECKSUM_MAGIC - 10
        data = b"\x00\x00\x00\x01\x00\x00\x00\x02\x00\x00\x00\x03\x00\x00\x00\x04"
        result = mazda_checksum(data, 0, 16)
        assert result == (CHECKSUM_MAGIC - 10) & 0xFFFFFFFF

    def test_all_zeros(self):
        """All-zero region should return CHECKSUM_MAGIC."""
        data = b"\x00" * 16
        assert mazda_checksum(data, 0, 16) == CHECKSUM_MAGIC

    def test_single_word(self):
        """Single 32-bit word."""
        word = 0xDEADBEEF
        data = word.to_bytes(4, "big")
        assert mazda_checksum(data, 0, 4) == (CHECKSUM_MAGIC - word) & 0xFFFFFFFF

    def test_subrange(self):
        """Checksum of a subrange within a larger buffer."""
        data = b"\x00" * 8 + b"\x00\x00\x00\x05" + b"\x00" * 4
        # Range [8, 12) has one word = 5
        assert mazda_checksum(data, 8, 12) == (CHECKSUM_MAGIC - 5) & 0xFFFFFFFF


class TestCorrectRomChecksums:
    """Test correct_rom_checksums() on a synthetic ROM."""

    def _build_rom_with_checksum_table(self, entries):
        """Build a 1MB ROM with a checksum table containing given entries.

        Each entry is (start, end) — the checksum field is set to 0
        so it will always need correction.
        """
        rom = bytearray(ROM_SIZE)
        offset = CHECKSUM_TABLE_OFFSET

        for start, end in entries:
            # Write start address (4 bytes BE)
            rom[offset : offset + 4] = start.to_bytes(4, "big")
            # Write end address (4 bytes BE)
            rom[offset + 4 : offset + 8] = end.to_bytes(4, "big")
            # Checksum = 0 (will be corrected)
            rom[offset + 8 : offset + 12] = b"\x00\x00\x00\x00"
            offset += CHECKSUM_ENTRY_SIZE

        # Sentinel: zeros
        rom[offset : offset + 8] = b"\x00" * 8
        return rom

    def test_corrects_single_entry(self):
        """Single checksum entry gets corrected."""
        rom = self._build_rom_with_checksum_table([(0x0000, 0x0010)])
        corrections = correct_rom_checksums(rom)
        assert len(corrections) == 1

        start, end, cksum_offset, old_val, new_val = corrections[0]
        assert start == 0x0000
        assert end == 0x0010
        assert old_val == 0
        # Verify the stored checksum was written
        stored = int.from_bytes(rom[cksum_offset : cksum_offset + 4], "big")
        assert stored == new_val

    def test_already_correct(self):
        """No corrections when checksum is already correct."""
        rom = self._build_rom_with_checksum_table([(0x0000, 0x0004)])
        # First, correct it
        correct_rom_checksums(rom)
        # Second pass should find no corrections
        corrections = correct_rom_checksums(rom)
        assert len(corrections) == 0

    def test_multiple_entries(self):
        """Multiple checksum table entries are all corrected."""
        rom = self._build_rom_with_checksum_table(
            [
                (0x0000, 0x0010),
                (0x1000, 0x1010),
            ]
        )
        corrections = correct_rom_checksums(rom)
        assert len(corrections) == 2


class TestCrc32:
    """Test CRC-32 wrapper."""

    def test_matches_zlib(self):
        """Our crc32() must match zlib.crc32() for arbitrary data."""
        data = b"Hello, Mazda NC Miata!"
        assert crc32(data) == zlib.crc32(data) & 0xFFFFFFFF

    def test_empty_data(self):
        """CRC of empty data matches zlib."""
        assert crc32(b"") == zlib.crc32(b"") & 0xFFFFFFFF

    def test_unsigned(self):
        """Result is always an unsigned 32-bit value."""
        result = crc32(b"\xff" * 1000)
        assert 0 <= result <= 0xFFFFFFFF


class TestBswap32:
    """Test byte-swap helper."""

    def test_identity(self):
        """Double swap is identity."""
        val = 0xDEADBEEF
        assert bswap32(bswap32(val)) == val

    def test_known_swap(self):
        """Known byte-swap value."""
        assert bswap32(0x01020304) == 0x04030201
