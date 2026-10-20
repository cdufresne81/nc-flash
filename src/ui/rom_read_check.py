"""Check a freshly read ECU ROM against its own checksums before it is trusted.

Why (issue 169): when a ROM-read block times out, the read asks for it again. If the
ECU's first answer arrives late, the retry takes it, and the block's own answer
is then taken as the NEXT block: every later block is shifted by 1 KB and the
read still reports success (the reply carries no address). Flashing that
backup later "works", because the flash corrects the checksums first, and puts
a garbage calibration on the ECU.

The ECU ROM carries a checksum table (see ``src.ecu.checksum``) covering
0x2000-0xFAFFF plus 0xFF800-0xFF9BF. A genuine read always matches it (the
flash path writes corrected checksums, so whatever is on the ECU is
self-consistent); a shifted read breaks it, or moves the table itself. Two
cases the table can't see are checked by the shift's own trace, a 1 KB block
that is an exact copy of the one before it: a shift starting at the
second-to-last block (it only rewrites the last block, which the table doesn't
cover), and one starting before 0x2000 (it moves the table and the generation
byte, so the ROM no longer looks like an NC ROM at all).

Read-only and pure: nothing here writes to the ROM or touches the ECU.
"""

from dataclasses import dataclass

from src.ecu.checksum import correct_rom_checksums
from src.ecu.constants import (
    BLOCK_SIZE,
    CHECKSUM_ENTRY_SIZE,
    CHECKSUM_TABLE_END,
    CHECKSUM_TABLE_OFFSET,
    ROM_SIZE,
)
from src.ecu.exceptions import VehicleGenerationError
from src.ecu.rom_utils import detect_vehicle_generation

OK = "ok"
SUSPECT = "suspect"
UNVERIFIED = "unverified"


@dataclass(frozen=True)
class RomReadCheck:
    """Outcome of :func:`check_rom_read`.

    ``status`` is :data:`OK`, :data:`SUSPECT` (do not trust or flash this read)
    or :data:`UNVERIFIED` (no known checksum layout: nothing could be checked).
    ``detail`` is a short plain-English reason for the log and the dialog.
    """

    status: str
    detail: str
    regions: int = 0
    bad_regions: tuple = ()  # ((start, end_inclusive), ...)

    @property
    def suspect(self) -> bool:
        return self.status == SUSPECT


def _checksum_table(rom: bytes) -> list[tuple[int, int]] | None:
    """Return the checksum table's ``(start, end_inclusive)`` ranges, or None
    when the bytes at the table offset don't look like a checksum table.

    Walks the table exactly like ``correct_rom_checksums`` (stop at the first
    start address past the ROM, or at the end of the table area), and requires
    at least one entry and every range inside the ROM.
    """
    ranges = []
    offset = CHECKSUM_TABLE_OFFSET
    while offset < CHECKSUM_TABLE_END:
        start = int.from_bytes(rom[offset : offset + 4], "big")
        end_incl = int.from_bytes(rom[offset + 4 : offset + 8], "big")
        if start >= len(rom):
            break  # end marker
        if end_incl < start or end_incl >= len(rom):
            return None
        ranges.append((start, end_incl))
        offset += CHECKSUM_ENTRY_SIZE
    return ranges or None


def _is_filler(block: bytes) -> bool:
    """True for padding: one short pattern repeated (0xFF erase fill, zeros,
    NOP runs). Two such blocks in a row are normal ROM content."""
    period = 16
    return block == block[:period] * (len(block) // period)


def _copied_block(rom: bytes, first: int = 0) -> int | None:
    """Offset of the first read block (from block ``first``) that is an exact
    copy of the block before it, ignoring padding; None if there is none.

    That copy is the trace the late-reply shift leaves: the retried block is
    right, and the next block receives the same data again.
    """
    for offset in range(max(first, 1) * BLOCK_SIZE, ROM_SIZE, BLOCK_SIZE):
        block = rom[offset : offset + BLOCK_SIZE]
        if block == rom[offset - BLOCK_SIZE : offset] and not _is_filler(block):
            return offset
    return None


def check_rom_read(rom: bytes) -> RomReadCheck:
    """Verify a full ECU ROM read against its own checksum table."""
    if len(rom) != ROM_SIZE:
        return RomReadCheck(UNVERIFIED, f"not a full 1 MB ECU ROM ({len(rom)} bytes)")

    ranges = _checksum_table(rom)
    if ranges is None:
        copied = _copied_block(rom)
        if copied is not None:
            # A shift that starts before 0x2000 also moves the table and the
            # generation byte, so this is the only trace left of it.
            return RomReadCheck(
                SUSPECT,
                f"the ROM's checksum table is missing and the 1 KB at "
                f"0x{copied:06X} is a copy of the 1 KB before it",
            )
        try:
            detect_vehicle_generation(rom)
        except VehicleGenerationError:
            # Not a ROM we know: no layout to check against, so no alarm.
            return RomReadCheck(UNVERIFIED, "no known checksum layout in this ROM")
        # An NC ECU ROM always has the table (every flash rewrites it), so a
        # missing one means the data around it moved.
        return RomReadCheck(SUSPECT, "the ROM's checksum table is missing or damaged")

    mismatches = correct_rom_checksums(bytearray(rom))  # on a copy: read-only
    if mismatches:
        bad = tuple((start, end) for start, end, _off, _old, _new in mismatches)
        return RomReadCheck(
            SUSPECT,
            f"{len(bad)} of {len(ranges)} checksummed regions do not match "
            "their checksum",
            regions=len(ranges),
            bad_regions=bad,
        )

    # The table doesn't cover the last block: a shift starting at the block
    # before it only rewrites that block, with a copy of the block before.
    if _copied_block(rom, first=ROM_SIZE // BLOCK_SIZE - 1) is not None:
        return RomReadCheck(
            SUSPECT,
            "the last 1 KB of the ROM is a copy of the 1 KB before it",
            regions=len(ranges),
        )

    return RomReadCheck(
        OK, f"all {len(ranges)} checksummed regions match", regions=len(ranges)
    )
