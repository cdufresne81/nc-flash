"""Tests for the patch-state warning shown when a ROM is opened."""

from pathlib import Path

import pytest

from src.core.definition_parser import load_definition
from src.core.patch_check import (
    PATCH_REVISION_OFFSET,
    PatchState,
    check_patch_state,
)
from src.ecu.constants import ROM_ID_OFFSET
from src.ecu.rom_utils import patch_rom

EXAMPLES = Path(__file__).parent.parent / "examples"
STOCK_ROM = EXAMPLES / "lf9veb.bin"
PATCH = EXAMPLES / "lf9veb.patch"
REV = "21053000"


def _rom_with_revision(raw: bytes) -> bytes:
    rom = bytearray(b"\xff" * 0x100000)
    rom[PATCH_REVISION_OFFSET : PATCH_REVISION_OFFSET + 4] = raw
    return bytes(rom)


def test_offset_matches_the_ecu_constant():
    assert PATCH_REVISION_OFFSET == ROM_ID_OFFSET


def test_factory_rom_is_unpatched():
    status = check_patch_state(STOCK_ROM.read_bytes(), REV)
    assert status.state is PatchState.UNPATCHED
    text = status.warning("lf9veb.bin", "LF9VEB")
    assert "unpatched" in text and REV in text


def test_patched_rom_matching_definition_gives_no_warning():
    patched = patch_rom(STOCK_ROM.read_bytes(), PATCH.read_bytes()).patched_rom
    status = check_patch_state(bytes(patched), REV)
    assert status.state is PatchState.OK
    assert status.warning("x.bin", "LF9VEB") is None


def test_other_patch_revision_is_a_mismatch():
    status = check_patch_state(_rom_with_revision(b"\x20\x01\x10\x00"), REV)
    assert status.state is PatchState.MISMATCH
    assert status.found == "20011000"
    text = status.warning("x.bin", "LF9VEB")
    assert "20011000" in text and REV in text


def test_zeroed_revision_counts_as_unpatched():
    status = check_patch_state(_rom_with_revision(b"\x00" * 4), REV)
    assert status.state is PatchState.UNPATCHED


@pytest.mark.parametrize("module", ["", None, "None", "  ", "2105300"])
def test_definition_without_a_revision_is_not_checked(module):
    status = check_patch_state(STOCK_ROM.read_bytes(), module)
    assert status.state is PatchState.NOT_APPLICABLE
    assert status.warning("x.bin", "X") is None


def test_short_rom_is_not_checked():
    status = check_patch_state(b"\xff" * 0x80000, REV)
    assert status.state is PatchState.NOT_APPLICABLE


def test_bundled_definitions_expect_the_shipped_patch_revision():
    """Every bundled definition with a revision must name the one our patches
    stamp; otherwise every correctly patched ROM for it would warn."""
    patched = bytes(patch_rom(STOCK_ROM.read_bytes(), PATCH.read_bytes()).patched_rom)
    for xml in sorted((EXAMPLES / "metadata").glob("*.xml")):
        module = load_definition(str(xml)).romid.checksummodule
        state = check_patch_state(patched, module).state
        assert state in (PatchState.OK, PatchState.NOT_APPLICABLE), xml.name
