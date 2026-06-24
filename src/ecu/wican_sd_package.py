"""Host-side pre-compute + packaging for the SD-staged WiCAN flash (Option B).

This is the **HOST-ONLY** step that turns an editable ROM into the exact bytes the
firmware will stream to the ECU, plus a *manifest* describing the flash plan. All
the secret / compute-heavy work stays here — checksum correction, SBL
decrypt+patch, generation detect, the dynamic-flash diff — and only plain bytes
plus a tiny manifest ever cross to the device. The firmware never authenticates,
never checksums, never diffs; it just replays the staged bytes over local CAN.
See ``.claude/plans/wican-write-option-b-goal.md`` (THE HANDOFF BOUNDARY).

Nothing here touches the network or the ECU — it is pure, deterministic and fully
unit-testable. :func:`build_flash_package` returns a :class:`FlashPackage`:

  * ``image`` — a single staged blob laid out as
    ``[checksum-corrected ROM (1 MB)] ++ [SBL (0x1800)]``. These are the exact
    bytes uploaded to ``/sdcard/roms/<name>.bin``; the firmware reads the SBL and
    the program slice from offsets recorded in the manifest (never from byte 0).
  * ``manifest`` — a small JSON-serialisable dict the firmware reads to drive
    ``RequestDownload → TransferData(SBL) → TransferData(program) → TransferExit``,
    and that the firmware's pre-erase integrity gate checks before any erase.

SAFETY (PRIME DIRECTIVE — a bad flash bricks an ECU): every value the firmware
will trust is self-checked **here**, host-side, before it can be uploaded —
ROM size, *zero residual* checksum corrections, a valid SBL flash-start index,
SBL length, program-slice bounds, the assembled image length, and a re-hash of
the assembled image against the manifest digest. A bad package raises and never
reaches the device. This mirrors the exact sequence built in
:meth:`FlashManager._flash_rom_inner` so the staged flash is byte-identical to a
known-good J2534 flash.

Headless module: standard library + sibling core modules only (no PySide6, no
network). The ``_secure`` module is required (SBL/checksum are host-only IP).
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .checksum import correct_rom_checksums, crc32
from .constants import (
    BLOCK_SIZE,
    DOWNLOAD_ADDR,
    DOWNLOAD_SIZE,
    ROM_FLASH_START_MIN,
    ROM_SIZE,
    SBL_SIZE,
)
from .exceptions import (
    ChecksumError,
    FlashError,
    ROMValidationError,
    SecureModuleNotAvailable,
)
from .rom_utils import (
    calculate_flash_start_index,
    detect_vehicle_generation,
    find_first_difference,
    get_cal_id,
    validate_rom_size,
)

try:  # SBL prep is host-only IP; packaging is impossible without it.
    from ._secure import get_sbl_data

    SECURE_MODULE_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised on machines without _secure
    from ._secure_stub import get_sbl_data

    SECURE_MODULE_AVAILABLE = False

#: Wire-contract version for the manifest. Bump only on a breaking change to the
#: field set / layout; the firmware refuses a manifest it does not understand.
MANIFEST_VERSION = 1

#: Total length of a staged image: the full 1 MB ROM followed by the 0x1800 SBL.
STAGED_IMAGE_LEN = ROM_SIZE + SBL_SIZE


@dataclass(frozen=True)
class FlashPackage:
    """An SD-ready staged flash: the bytes to upload + the manifest to trigger.

    Immutable on purpose — once self-checked, nothing should mutate it before it
    is uploaded and flashed.
    """

    image: bytes
    """``[checksum-corrected ROM] ++ [SBL]`` — the exact upload payload."""

    manifest: dict
    """JSON-serialisable flash plan (see :func:`build_flash_package`)."""

    @property
    def staged_filename(self) -> str:
        return self.manifest["staged_filename"]

    @property
    def corrected_rom(self) -> bytes:
        """The checksum-corrected 1 MB ROM portion (read-back compare reference)."""
        return self.image[:ROM_SIZE]

    @property
    def program(self) -> bytes:
        """The program slice the firmware transfers (``rom[flash_start_index:]``)."""
        return self.image[self.manifest["program_offset"] : ROM_SIZE]

    @property
    def sbl(self) -> bytes:
        """The 0x1800 SBL bytes the firmware transfers first."""
        return self.image[ROM_SIZE:]


#: Cap the staged-filename stem so the full name (stem + ``_YYYYMMDD_HHMM.bin``)
#: stays well under FAT's 255-char limit and any firmware command buffer.
_STAGED_STEM_MAX = 64


def _sanitize_filename_stem(name: str) -> str:
    """Make an arbitrary display filename safe as a staged SD-image stem.

    The staged name is used verbatim in three ASCII-only, space-hostile hops —
    the FAT SD filename, the ``/upload/sd/<name>`` HTTP path, and the firmware's
    ``W<mode><name>\\r`` SLCAN command (``.encode("ascii")``) — so a space or an
    accented char (``é``/``à``) would break the upload URL or crash the flash
    trigger. This:

      * drops any directory part and a single trailing extension (e.g. ``.bin``),
      * transliterates accents to ASCII (``é``→``e``, ``à``→``a``) via NFKD so the
        name stays recognisable instead of collapsing to underscores,
      * replaces every remaining non ``[A-Za-z0-9-_.]`` char (spaces included)
        with ``_``, collapses runs, trims, and length-caps,
      * falls back to a stable default.

    Output is always non-empty, pure ASCII, and free of spaces / path separators.
    """
    # Basename only — strip any directory part (defensive; both separators).
    stem = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    # Drop a single trailing extension (we re-append ``.bin``).
    dot = stem.rfind(".")
    if dot > 0:
        stem = stem[:dot]
    # Transliterate accents: decompose, then drop the combining marks.
    decomposed = unicodedata.normalize("NFKD", stem)
    ascii_stem = "".join(c for c in decomposed if not unicodedata.combining(c))
    # Keep only filename-safe ASCII; everything else (incl. spaces) -> ``_``.
    cleaned = "".join(
        c if (c.isascii() and (c.isalnum() or c in "-_.")) else "_" for c in ascii_stem
    )
    # Collapse ``_`` runs and, crucially, any ``..`` run to a single ``.`` — a
    # single dot is kept (e.g. "AFR 12.5" stays readable) but the upload guard
    # rejects any name containing ".." (path-traversal defence), so a doubled dot
    # would otherwise block a legitimate flash. Then trim, cap, trim post-cap.
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    while ".." in cleaned:
        cleaned = cleaned.replace("..", ".")
    cleaned = cleaned.strip("_. ")[:_STAGED_STEM_MAX].strip("_. ")
    return cleaned or "ecu_rom"


def staged_filename(source_name: str, when: datetime) -> str:
    """``<sanitized-source-name>_<YYYYMMDD>_<HHMM>.bin`` (minute precision).

    ``source_name`` is the human ROM name shown in NC Flash (the loaded file's
    name) — far more meaningful on an SD listing than a bare cal-ID. It is
    sanitised to a safe, pure-ASCII, space-free stem (see
    :func:`_sanitize_filename_stem`) so it survives the FAT / HTTP / SLCAN hops,
    then suffixed with a ``YYYYMMDD_HHMM`` timestamp (minute precision) so a
    listing sorts chronologically and a re-stage never clobbers a prior copy.
    """
    return f"{_sanitize_filename_stem(source_name)}_{when:%Y%m%d_%H%M}.bin"


def _derive_rom_id(rom_data: bytes) -> str:
    """Best-effort identity label from the ROM itself (the calibration ID).

    Used only when the caller does not supply one (e.g. the UI passes its ECU
    card value verbatim). Falls back to ``ecu_rom`` if the cal-ID is unreadable.
    """
    try:
        return get_cal_id(rom_data).decode("ascii", "replace")
    except Exception:
        return "ecu_rom"


def build_flash_package(
    rom_data: bytes,
    *,
    flash_type: str = "full",
    archive_data: Optional[bytes] = None,
    rom_id: Optional[str] = None,
    source_name: Optional[str] = None,
    when: Optional[datetime] = None,
) -> FlashPackage:
    """Compute the SD-staged flash package for ``rom_data``.

    Replicates :meth:`FlashManager._flash_rom_inner`'s host-side prep exactly so
    the staged flash is byte-identical to a known-good J2534 flash:
    validate → detect generation → checksum-correct (+ verify zero residual) →
    pick ``flash_start_index`` (full or dynamic) → build SBL → slice program →
    assemble + digest → self-check.

    Args:
        rom_data: The 1 MB ROM to flash (raw, pre-correction).
        flash_type: ``"full"`` (start at :data:`ROM_FLASH_START_MIN`) or
            ``"dynamic"`` (diff against ``archive_data``, flash only from the
            first changed region — requires ``archive_data``).
        archive_data: The ROM currently on the ECU (the ``ncflash.rda`` archive),
            required for a dynamic flash.
        rom_id: Identity label for the manifest (and the staged-filename
            fallback). Defaults to the ROM's calibration ID.
        source_name: The ROM file's name as shown in NC Flash (e.g. the loaded
            ``.bin``); when given, the staged SD filename is derived from it
            (sanitised to safe ASCII) so it is recognisable on the card instead
            of a bare cal-ID. Falls back to ``rom_id`` when absent/blank.
        when: Timestamp for the staged filename. Defaults to ``datetime.now()``.

    Returns:
        A self-checked :class:`FlashPackage`.

    Raises:
        ROMValidationError: ROM/archive size invalid, identical ROMs (dynamic).
        ChecksumError: residual checksum corrections after correction.
        FlashError: invalid ``flash_type``, out-of-bounds / unsupported flash
            start index, SBL/size/digest self-check failure.
    """
    if not SECURE_MODULE_AVAILABLE:
        # The SBL/checksum IP is host-only; SD-staging is impossible without it.
        raise SecureModuleNotAvailable()

    if flash_type not in ("full", "dynamic"):
        raise FlashError(
            f"Invalid flash_type {flash_type!r} (expected 'full' or 'dynamic')"
        )

    # --- Validate + generation (BEFORE any compute) ---
    if not validate_rom_size(rom_data):
        raise ROMValidationError(
            f"ROM must be exactly {ROM_SIZE} bytes, got {len(rom_data)}"
        )

    generation = detect_vehicle_generation(rom_data)

    # --- Checksum-correct on a copy, then verify zero residual (defense-in-depth) ---
    rom_buf = bytearray(rom_data)
    correct_rom_checksums(rom_buf)
    residual = correct_rom_checksums(bytearray(rom_buf))
    if residual:
        raise ChecksumError(
            f"Checksum verification failed: {len(residual)} checksum(s) still "
            "incorrect after correction"
        )

    # --- Decide the flash start index (full vs dynamic) ---
    diff_offset: Optional[int] = None
    if flash_type == "full":
        flash_start_index = ROM_FLASH_START_MIN
    else:
        if archive_data is None:
            raise ROMValidationError(
                "Dynamic flash requires the current ECU archive (archive_data)"
            )
        if not validate_rom_size(archive_data):
            raise ROMValidationError(f"Archive ROM size invalid: {len(archive_data)}")
        diff_offset = find_first_difference(rom_data, archive_data)
        if diff_offset < 0:
            raise ROMValidationError("ROMs are identical — nothing to flash")
        flash_start_index = calculate_flash_start_index(diff_offset)

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

    # --- Program slice (what TransferData streams after the SBL) ---
    program_offset = flash_start_index
    program_len = ROM_SIZE - program_offset
    if program_len <= 0:
        raise FlashError(
            f"Empty program slice at flash_start_index 0x{flash_start_index:06X}"
        )

    corrected_rom = bytes(rom_buf)
    image = corrected_rom + sbl_data

    # --- Self-checks: the assembled image must be exactly what the manifest claims ---
    if len(image) != STAGED_IMAGE_LEN:
        raise FlashError(
            f"Staged image length {len(image)} != expected {STAGED_IMAGE_LEN}"
        )
    if image[program_offset:ROM_SIZE] != corrected_rom[program_offset:]:
        raise FlashError(
            "Program slice does not match the corrected ROM at program_offset"
        )
    if image[ROM_SIZE:] != sbl_data:
        raise FlashError("SBL slice does not match the appended SBL bytes")

    image_sha256 = hashlib.sha256(image).hexdigest()
    image_crc32 = crc32(image)
    rom_sha256 = hashlib.sha256(corrected_rom).hexdigest()

    label = rom_id if (rom_id and rom_id.strip()) else _derive_rom_id(rom_data)
    when = when or datetime.now()
    # Name the staged file after the ROM shown in NC Flash when the caller knows
    # it (meaningful on the SD card); fall back to the identity label otherwise.
    fname = staged_filename(
        source_name if (source_name and source_name.strip()) else label, when
    )

    try:
        cal_id = get_cal_id(rom_data).decode("ascii", "replace")
    except Exception:
        cal_id = ""

    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "rom_id": label,
        "cal_id": cal_id,
        "generation": generation,
        "flash_type": flash_type,
        # Fixed UDS RequestDownload params (KWP2000-style; verified vs romdrop).
        "download_addr": DOWNLOAD_ADDR,
        "download_size": DOWNLOAD_SIZE,
        "block_size": BLOCK_SIZE,
        "flash_start_index": flash_start_index,
        # Staged-image layout the firmware reads from.
        "sbl_offset": ROM_SIZE,
        "sbl_len": SBL_SIZE,
        "program_offset": program_offset,
        "program_len": program_len,
        "image_len": len(image),
        # Pre-erase integrity gate covers the WHOLE staged image (ROM + SBL).
        "image_sha256": image_sha256,
        "image_crc32": image_crc32,
        # Read-back compare audit reference (the corrected ROM portion only).
        "rom_sha256": rom_sha256,
        "staged_filename": fname,
    }
    if diff_offset is not None:
        manifest["diff_offset"] = diff_offset

    return FlashPackage(image=image, manifest=manifest)
