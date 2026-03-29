"""Tests for src/ecu/checksum.py — Mazda ROM checksum calculation."""

import os
import struct
import zlib

import pytest

from src.ecu.checksum import mazda_checksum, correct_rom_checksums, crc32, bswap32
from src.ecu.constants import (
    CHECKSUM_MAGIC,
    CHECKSUM_TABLE_OFFSET,
    CHECKSUM_ENTRY_SIZE,
    ROM_SIZE,
)

EXAMPLE_ROM = os.path.join(os.path.dirname(__file__), "..", "examples", "lf9veb.bin")


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

    def test_end_before_start_returns_magic(self):
        """Invalid range (end < start) returns CHECKSUM_MAGIC."""
        data = b"\xff" * 16
        assert mazda_checksum(data, 12, 4) == CHECKSUM_MAGIC

    def test_start_beyond_rom_returns_magic(self):
        """Start beyond ROM size returns CHECKSUM_MAGIC."""
        data = b"\xff" * 16
        assert mazda_checksum(data, 100, 200) == CHECKSUM_MAGIC

    def test_end_clamped_to_rom_size(self):
        """End beyond ROM is clamped — no crash."""
        data = b"\x00\x00\x00\x01" * 4  # 16 bytes
        # Request range [0, 1000) but ROM is only 16 bytes → clamp to [0, 16)
        result = mazda_checksum(data, 0, 1000)
        expected = mazda_checksum(data, 0, 16)
        assert result == expected

    def test_zero_size_range_returns_magic(self):
        """Zero-size range returns CHECKSUM_MAGIC."""
        data = b"\xff" * 16
        assert mazda_checksum(data, 8, 8) == CHECKSUM_MAGIC


class TestCorrectRomChecksums:
    """Test correct_rom_checksums() on a synthetic ROM."""

    def _build_rom_with_checksum_table(self, entries):
        """Build a 1MB ROM with a checksum table containing given entries.

        Each entry is (start, end_inclusive) — end is the last byte of the
        range (inclusive), matching the real Mazda ROM table format.
        The checksum field is set to 0 so it will always need correction.
        """
        rom = bytearray(ROM_SIZE)
        offset = CHECKSUM_TABLE_OFFSET

        for start, end_incl in entries:
            # Write start address (4 bytes BE)
            rom[offset : offset + 4] = start.to_bytes(4, "big")
            # Write end address inclusive (4 bytes BE)
            rom[offset + 4 : offset + 8] = end_incl.to_bytes(4, "big")
            # Checksum = 0 (will be corrected)
            rom[offset + 8 : offset + 12] = b"\x00\x00\x00\x00"
            offset += CHECKSUM_ENTRY_SIZE

        # Sentinel: 0xFF bytes (matches real ROM — triggers start >= rom_len check)
        rom[offset : offset + 8] = b"\xff" * 8
        return rom

    def test_corrects_single_entry(self):
        """Single checksum entry gets corrected."""
        rom = self._build_rom_with_checksum_table([(0x0000, 0x000F)])
        corrections = correct_rom_checksums(rom)
        assert len(corrections) == 1

        start, end_incl, cksum_offset, old_val, new_val = corrections[0]
        assert start == 0x0000
        assert end_incl == 0x000F
        assert old_val == 0
        # Verify the stored checksum was written
        stored = int.from_bytes(rom[cksum_offset : cksum_offset + 4], "big")
        assert stored == new_val

    def test_already_correct(self):
        """No corrections when checksum is already correct."""
        rom = self._build_rom_with_checksum_table([(0x0000, 0x0003)])
        # First, correct it
        correct_rom_checksums(rom)
        # Second pass should find no corrections
        corrections = correct_rom_checksums(rom)
        assert len(corrections) == 0

    def test_multiple_entries(self):
        """Multiple checksum table entries are all corrected."""
        rom = self._build_rom_with_checksum_table(
            [
                (0x0000, 0x000F),
                (0x1000, 0x100F),
            ]
        )
        corrections = correct_rom_checksums(rom)
        assert len(corrections) == 2

    @pytest.mark.skipif(
        not os.path.exists(EXAMPLE_ROM), reason="example ROM not available"
    )
    def test_real_rom_no_corrections(self):
        """A stock ROM with valid checksums must not be modified."""
        with open(EXAMPLE_ROM, "rb") as f:
            rom = bytearray(f.read())
        corrections = correct_rom_checksums(rom)
        assert len(corrections) == 0, (
            f"correct_rom_checksums modified {len(corrections)} entries "
            f"in a valid stock ROM — table parsing is wrong"
        )

    @pytest.mark.skipif(
        not os.path.exists(EXAMPLE_ROM), reason="example ROM not available"
    )
    def test_real_rom_idempotent(self):
        """Correcting a modified ROM twice yields no changes on the second pass."""
        with open(EXAMPLE_ROM, "rb") as f:
            rom = bytearray(f.read())
        # Corrupt one checksum entry to force correction
        cksum_field = CHECKSUM_TABLE_OFFSET + 8
        rom[cksum_field : cksum_field + 4] = b"\x00\x00\x00\x00"

        corrections = correct_rom_checksums(rom)
        assert len(corrections) == 1

        # Second pass: already correct, no changes
        corrections2 = correct_rom_checksums(rom)
        assert len(corrections2) == 0


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
