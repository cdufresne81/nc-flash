"""Host-side flash-image preparation — the ONE copy of the brick-critical prep.

Both flash paths — the J2534 wired flash (:meth:`FlashManager._flash_rom_inner`)
and the SD-staged wireless flash (:func:`wican_sd_package.build_flash_package`) —
must perform the identical host-side preparation before a single byte reaches an
ECU: validate the ROM, detect the vehicle generation, checksum-correct it,
bounds-check the flash start, and build the matching secondary bootloader (SBL).

Any DRIFT between the two copies of this pipeline can brick an ECU (a mismatched
SBL or an uncorrected checksum), which is exactly why the architecture rule says
"table rendering, flash-image prep, and read loops each live in exactly one
module." This module IS that one module for flash prep; both callers compose it.

Pure and headless: no ECU contact, no I/O, no progress reporting — just bytes in,
validated bytes out.
"""

from typing import List, Tuple

from .constants import ROM_SIZE, SBL_SIZE
from .exceptions import ChecksumError, FlashError, ROMValidationError
from .checksum import correct_rom_checksums
from .rom_utils import detect_vehicle_generation, validate_rom_size

try:  # SBL prep is host-only IP; flash prep is impossible without it.
    from ._secure import get_sbl_data

    SECURE_MODULE_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised on machines without _secure
    from ._secure_stub import get_sbl_data

    SECURE_MODULE_AVAILABLE = False


def prepare_flash_image(
    rom_data: bytes, flash_start_index: int
) -> Tuple[bytes, bytes, str, List[Tuple[int, int, int, int, int]]]:
    """Validate + checksum-correct ``rom_data`` and build its SBL for flashing.

    This is the host-side prep both flash paths must perform IDENTICALLY. It does
    NOT touch the ECU and does NOT assemble the SD image or the transferred
    stream — the caller slices ``corrected_rom[flash_start_index:]`` for the
    program data and appends ``sbl_data`` as its path requires.

    Args:
        rom_data: The raw (pre-correction) 1 MB ROM to flash.
        flash_start_index: Byte offset where the program transfer begins. The
            caller decides this (``ROM_FLASH_START_MIN`` for a full flash, or a
            dynamic value from the first-difference diff).

    Returns:
        ``(corrected_rom, sbl_data, generation, corrections)``:
          - ``corrected_rom``: the checksum-corrected 1 MB ROM (bytes).
          - ``sbl_data``: the SBL for ``(flash_start_index, generation)``.
          - ``generation``: detected NC1/NC2 generation string.
          - ``corrections``: the checksum corrections applied, as
            ``(start, end, offset, old, new)`` tuples (for the caller to log).

    Raises:
        ROMValidationError: ROM is not exactly ``ROM_SIZE`` bytes.
        ChecksumError: checksums are still incorrect after correction.
        FlashError: ``flash_start_index`` out of bounds, unsupported by the SBL
            builder, or the SBL is the wrong size.
    """
    # --- Validate (BEFORE any compute) ---
    if not validate_rom_size(rom_data):
        raise ROMValidationError(
            f"ROM must be exactly {ROM_SIZE} bytes, got {len(rom_data)}"
        )

    generation = detect_vehicle_generation(rom_data)

    # --- Checksum-correct on a copy, then verify zero residual (defense-in-depth) ---
    rom_buf = bytearray(rom_data)
    corrections = correct_rom_checksums(rom_buf)
    residual = correct_rom_checksums(bytearray(rom_buf))
    if residual:
        # The corrections list never reaches the caller on this path, so fold
        # the diagnostic (what was corrected, what still fails) into the error.
        still_bad = ", ".join(
            f"0x{start:06X}-0x{end:06X}@0x{offset:06X}"
            for start, end, offset, _old, _new in residual
        )
        raise ChecksumError(
            f"Checksum verification failed: {len(residual)} checksum(s) still "
            f"incorrect after applying {len(corrections)} correction(s) — "
            f"residual: {still_bad}"
        )

    # --- Bounds (defense-in-depth, before any ECU contact) ---
    if not (0 < flash_start_index < len(rom_buf)):
        raise FlashError(
            f"flash_start_index out of bounds: 0x{flash_start_index:06X} "
            f"(ROM size: 0x{len(rom_buf):06X})"
        )

    # --- SBL (host-only IP). get_sbl_data rejects an unsupported start index. ---
    try:
        sbl_data = get_sbl_data(flash_start_index, generation)
    except ValueError as e:
        raise FlashError(
            f"Cannot build SBL for flash_start_index 0x{flash_start_index:06X}: {e}"
        )
    if len(sbl_data) != SBL_SIZE:
        raise FlashError(f"SBL size mismatch: expected {SBL_SIZE}, got {len(sbl_data)}")

    return bytes(rom_buf), sbl_data, generation, corrections
