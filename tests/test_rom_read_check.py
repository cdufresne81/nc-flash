"""A fresh ROM read is checked against its own checksums before it is trusted (#169).

A late ECU reply on a retried block shifts every later block by 1 KB and the read
still reports success. These tests rebuild that shift on the sample ROM and check
it is flagged wherever it starts, that a genuine image passes, and that a ROM
with no known checksum layout is not flagged.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.ecu.checksum import correct_rom_checksums
from src.ecu.constants import BLOCK_SIZE, CHECKSUM_TABLE_OFFSET, ROM_SIZE
from src.ui.ecu_window import ECUProgrammingWindow
from src.ui.rom_read_check import OK, SUSPECT, UNVERIFIED, check_rom_read


@pytest.fixture(scope="module")
def genuine_rom():
    path = Path(__file__).parent.parent / "examples" / "lf9veb.bin"
    return path.read_bytes()


def _shift_from(rom: bytes, block_offset: int) -> bytes:
    """The #169 read: the retried block is right, then every later block gets
    the previous block's data (the last block of the ROM is lost)."""
    out = bytearray(rom)
    out[block_offset + BLOCK_SIZE :] = rom[block_offset : ROM_SIZE - BLOCK_SIZE]
    return bytes(out)


# --- the check itself -------------------------------------------------------


def test_genuine_rom_passes(genuine_rom):
    check = check_rom_read(genuine_rom)
    assert check.status == OK
    assert not check.suspect
    assert check.regions == 35


@pytest.mark.parametrize(
    "block_offset",
    [
        0x0,  # moves the generation byte and the table: copied-block trace
        0x400,
        0x1C00,
        0x2000,  # first checksummed block
        0x80000,
        0xFA800,
        0xFF000,  # moves the checksum table itself
        0xFF400,
        0xFF800,  # only rewrites the last block, which no checksum covers
    ],
)
def test_shifted_read_is_suspect(genuine_rom, block_offset):
    shifted = _shift_from(genuine_rom, block_offset)
    assert shifted != genuine_rom
    check = check_rom_read(shifted)
    assert check.status == SUSPECT, check.detail


def test_flipped_byte_in_a_checksummed_region_is_suspect(genuine_rom):
    rom = bytearray(genuine_rom)
    rom[0x0E0E66] ^= 0x01
    check = check_rom_read(bytes(rom))
    assert check.suspect
    assert check.bad_regions == ((0x0B7000, 0x0F3FFF),)


def test_edited_rom_with_corrected_checksums_passes(genuine_rom):
    # What the ECU holds after a flash: edited data plus corrected checksums
    # (adaptive-looking bytes like the 0x0E0Exx flags included).
    rom = bytearray(genuine_rom)
    rom[0x0E0E66:0x0E0E6E] = bytes(8)
    correct_rom_checksums(rom)
    assert check_rom_read(bytes(rom)).status == OK


def test_check_never_modifies_the_read(genuine_rom):
    rom = bytearray(_shift_from(genuine_rom, 0x80000))
    before = bytes(rom)
    check_rom_read(rom)
    assert bytes(rom) == before


# --- no known layout: no false alarm ----------------------------------------


def test_short_buffer_is_unverified():
    assert check_rom_read(bytes(0xC000)).status == UNVERIFIED


def test_blank_rom_is_unverified():
    # No table, no NC generation byte, and blank fill is not a copied block.
    assert check_rom_read(b"\xff" * ROM_SIZE).status == UNVERIFIED


def test_unknown_layout_rom_is_unverified():
    # Distinct content in every block, no checksum table, unknown generation.
    rom = bytearray(ROM_SIZE)
    for i in range(0, ROM_SIZE, 4):
        rom[i : i + 4] = i.to_bytes(4, "big")
    rom[CHECKSUM_TABLE_OFFSET : CHECKSUM_TABLE_OFFSET + 4] = b"\xff" * 4
    rom[0x2030] = 0x00
    assert check_rom_read(bytes(rom)).status == UNVERIFIED


def test_nc_rom_with_its_checksum_table_wiped_is_suspect(genuine_rom):
    rom = bytearray(genuine_rom)
    rom[CHECKSUM_TABLE_OFFSET : CHECKSUM_TABLE_OFFSET + 4] = b"\xff" * 4
    assert check_rom_read(bytes(rom)).status == SUSPECT


# --- wiring in the ECU window ------------------------------------------------


def _finish_read(rom: bytes):
    """Run the read-success branch of _on_flash_finished against a fake window."""
    fake = MagicMock()
    fake._current_operation = "read"
    fake._session_acquired = False
    fake._check_rom_read = ECUProgrammingWindow._check_rom_read
    fake._auto_save_rom.return_value = Path("LF9VEB_20261020_120000.bin")
    worker = SimpleNamespace(result=bytearray(rom))
    with patch("src.ui.ecu_window.QTimer"), patch("subprocess.Popen"):
        ECUProgrammingWindow._on_flash_finished(fake, True, None, worker)
    return fake


def test_suspect_read_is_saved_marked_and_warned(genuine_rom):
    fake = _finish_read(_shift_from(genuine_rom, 0x80000))
    assert fake._auto_save_rom.call_args.kwargs == {"suspect": True}
    fake._show_suspect_read.assert_called_once()
    assert fake._show_suspect_read.call_args.args[1].suspect


def test_genuine_read_is_saved_normally_without_warning(genuine_rom):
    fake = _finish_read(genuine_rom)
    assert fake._auto_save_rom.call_args.kwargs == {"suspect": False}
    fake._show_suspect_read.assert_not_called()


def test_suspect_read_filename_carries_suspect_label():
    captured = {}

    def fake_save(data, label="", name_override=None):
        captured["label"] = label
        return None

    fake = SimpleNamespace(
        _auto_save_to_reads_dir=fake_save, _cal_id_from_rom=lambda _d: "LF9VEB"
    )
    ECUProgrammingWindow._auto_save_rom(fake, bytearray(ROM_SIZE), suspect=True)
    assert captured["label"] == "SUSPECT"


def test_suspect_warning_tells_the_user_not_to_flash(genuine_rom):
    check = check_rom_read(_shift_from(genuine_rom, 0x80000))
    fake = SimpleNamespace(_progress_state=MagicMock(), _progress_detail=MagicMock())
    with (
        patch("src.ui.ecu_window.QTimer") as timer,
        patch("src.ui.ecu_window.QMessageBox") as box,
    ):
        timer.singleShot.side_effect = lambda _ms, fn: fn()
        ECUProgrammingWindow._show_suspect_read(
            fake, Path("LF9VEB_SUSPECT_20261020_120000.bin"), check
        )
    box.warning.assert_called_once()
    title, text = box.warning.call_args.args[1:3]
    assert "Corrupt" in title
    assert "Do NOT flash" in text and "Read the ROM again" in text
    assert "LF9VEB_SUSPECT_20261020_120000.bin" in text
