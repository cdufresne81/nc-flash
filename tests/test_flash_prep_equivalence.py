"""D1: `prepare_flash_image` is the ONE flash-prep pipeline — both paths agree.

The brick-critical host-side prep (validate → generation → checksum-correct →
bounds → SBL) used to be copied step-for-step in `FlashManager._flash_rom_inner`
(J2534 flash) and `wican_sd_package.build_flash_package` (SD-staged flash). Any
drift between the two copies — a different SBL, an uncorrected checksum — bricks
an ECU. They now both compose `flash_prep.prepare_flash_image`.

This gate proves three complementary properties:
(a) `prepare_flash_image` reproduces the known-good host prep byte-for-byte
    via an INDEPENDENT recompute;
(b) the SD path's package (which composes the same function, plus its own
    slicing/assembly) emits corrected-ROM + SBL + program bytes identical to a
    direct `prepare_flash_image` call — pinning the SD-side composition;
(c) a STATIC composition ratchet: both flash modules actually call
    `prepare_flash_image` and neither references the prep primitives
    (`correct_rom_checksums` / `get_sbl_data` / `_secure`) directly, so the
    J2534 side — whose `_flash_rom_inner` cannot run without a live ECU —
    provably routes through the one pipeline rather than a re-inlined copy.
"""

import ast
from pathlib import Path

import pytest

from src.ecu.checksum import correct_rom_checksums
from src.ecu.constants import ROM_FLASH_START_MIN, SBL_SIZE
from src.ecu.exceptions import FlashError, ROMValidationError
from src.ecu.flash_prep import prepare_flash_image, SECURE_MODULE_AVAILABLE
from src.ecu.rom_utils import (
    calculate_flash_start_index,
    detect_vehicle_generation,
    find_first_difference,
)
from src.ecu.wican_sd_package import build_flash_package

_ROM_PATH = Path(__file__).resolve().parent.parent / "examples" / "lf9veb.bin"

requires_secure = pytest.mark.skipif(
    not SECURE_MODULE_AVAILABLE, reason="_secure module (SBL IP) not installed"
)


@pytest.fixture(scope="module")
def rom() -> bytes:
    return _ROM_PATH.read_bytes()


# --- bounds / validation (no _secure needed) --------------------------------


def test_wrong_size_rejected():
    with pytest.raises(ROMValidationError):
        prepare_flash_image(b"\x00" * 1234, ROM_FLASH_START_MIN)


@pytest.mark.parametrize("bad_index", [0, -1, 0x100000, 0x100001])
def test_out_of_bounds_start_rejected(bad_index):
    rom = bytearray(0x100000)
    rom[0x2030] = 0x35  # valid NC1 generation byte
    with pytest.raises(FlashError, match="out of bounds"):
        prepare_flash_image(bytes(rom), bad_index)


# --- static composition ratchet (no _secure needed) --------------------------

_ECU_DIR = Path(__file__).resolve().parent.parent / "src" / "ecu"

# The ONE pipeline's private primitives: any direct reference in a flash path
# means the prep is being re-inlined — the exact drift D1 exists to prevent.
# (Bare `_secure` is NOT in the set: flash_manager legitimately imports
# compute_security_key from it; the prep re-inline signature is these two.)
_PREP_PRIMITIVES = {"correct_rom_checksums", "get_sbl_data"}


def _names_referenced(tree):
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            names.update(a.name for a in node.names)
            if node.module:
                names.update(node.module.split("."))
        elif isinstance(node, ast.Import):
            for a in node.names:
                names.update(a.name.split("."))
    return names


@pytest.mark.parametrize("module", ["flash_manager.py", "wican_sd_package.py"])
def test_flash_path_composes_the_one_pipeline(module):
    tree = ast.parse((_ECU_DIR / module).read_text(encoding="utf-8"))
    names = _names_referenced(tree)
    assert "prepare_flash_image" in names, f"{module} no longer composes flash_prep"
    inlined = names & _PREP_PRIMITIVES
    assert not inlined, (
        f"{module} references prep primitive(s) {sorted(inlined)} directly — "
        "flash prep must live ONLY in flash_prep.prepare_flash_image (D1)"
    )


# --- byte-equality vs an independent recompute ------------------------------


@requires_secure
def test_prepare_matches_independent_recompute(rom):
    from src.ecu import _secure

    corrected, sbl, gen, corrections = prepare_flash_image(rom, ROM_FLASH_START_MIN)

    buf = bytearray(rom)
    expected_corrections = correct_rom_checksums(buf)
    assert corrected == bytes(buf)  # checksum-corrected ROM
    assert gen == detect_vehicle_generation(rom)  # generation
    assert sbl == _secure.get_sbl_data(ROM_FLASH_START_MIN, gen)  # SBL bytes
    assert len(sbl) == SBL_SIZE
    assert corrections == expected_corrections  # correction log identical


# --- the two flash paths emit identical bytes -------------------------------


@requires_secure
def test_j2534_and_sd_paths_agree_full(rom):
    corrected, sbl, _gen, _ = prepare_flash_image(rom, ROM_FLASH_START_MIN)
    pkg = build_flash_package(rom, flash_type="full")

    assert pkg.corrected_rom == corrected
    assert pkg.sbl == sbl
    # The bytes each path streams after the SBL are byte-identical.
    assert pkg.program == corrected[ROM_FLASH_START_MIN:]


@requires_secure
def test_j2534_and_sd_paths_agree_dynamic(rom):
    # An archive differing at offset 0 → diff_offset 0 → flash_start clamps to
    # ROM_FLASH_START_MIN (a guaranteed-supported SBL index). The point is that
    # the dynamic path composes the SAME prep; both paths must agree.
    archive = bytearray(rom)
    archive[0] ^= 0xFF
    diff_offset = find_first_difference(rom, bytes(archive))
    flash_start = calculate_flash_start_index(diff_offset)

    corrected, sbl, _gen, _ = prepare_flash_image(rom, flash_start)
    pkg = build_flash_package(rom, flash_type="dynamic", archive_data=bytes(archive))

    assert pkg.manifest["flash_start_index"] == flash_start
    assert pkg.corrected_rom == corrected
    assert pkg.sbl == sbl
    assert pkg.program == corrected[flash_start:]
