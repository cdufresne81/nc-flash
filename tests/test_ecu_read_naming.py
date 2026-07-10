"""Auto-saved ECU reads are named by the ROM's own calibration ID.

A full ROM read used to land as ``ecu_read_<timestamp>.bin`` whenever the ECU
status card had not been populated (it reads ``N/A`` / ``—`` before a full
read). The bytes we just read are the authoritative name, so the read is now
saved as ``<CAL-ID>_<timestamp>.bin`` (e.g. ``LF9VEB_...``), extracted straight
from the ROM. RAM dumps carry no cal ID but reuse the ECU status card's ROM_ID,
landing as ``<ROM_ID>_RAM_<timestamp>.bin``. The ROM_ID is spelled in all caps
in both filenames.

Exercised against a duck-typed fake ``self`` — no QApplication needed.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.ecu.constants import ROM_SIZE, CAL_ID_OFFSETS
from src.ui.ecu_window import ECUProgrammingWindow


def _rom_with_cal_id(cal: bytes = b"LF9VEB") -> bytearray:
    """A 1 MB buffer carrying a valid cal ID at the primary offset."""
    rom = bytearray(ROM_SIZE)
    off = CAL_ID_OFFSETS[0]
    rom[off : off + len(cal)] = cal
    return rom


def _fake_window(card_value: str = "N/A"):
    card = MagicMock()
    card.get_value.return_value = card_value
    return SimpleNamespace(_card_ecu=card)


# --- cal-ID extraction (the naming source) ---------------------------------


def test_cal_id_from_rom_is_uppercase():
    assert ECUProgrammingWindow._cal_id_from_rom(_rom_with_cal_id()) == "LF9VEB"


def test_cal_id_from_rom_returns_none_for_non_rom_bytes():
    # A RAM dump / short buffer carries no valid cal ID -> caller falls back.
    assert ECUProgrammingWindow._cal_id_from_rom(bytes(4096)) is None


# --- filename precedence in the save path ----------------------------------


def test_override_wins_over_card_and_names_the_file_in_caps(tmp_path):
    fake = _fake_window(card_value="N/A")  # card unpopulated, as on a fresh read
    with patch("src.utils.settings.get_settings") as gs:
        gs.return_value.get_reads_directory.return_value = str(tmp_path)
        # A lowercase override is normalized to all caps in the filename.
        path = ECUProgrammingWindow._auto_save_to_reads_dir(
            fake, b"\x01\x02", name_override="lf9veb"
        )
    assert path is not None
    assert path.name.startswith("LF9VEB_")
    assert path.parent == tmp_path


def test_ram_dump_named_rom_id_underscore_ram_in_caps(tmp_path):
    # A RAM dump carries no cal ID; it reuses the ECU status card's ROM_ID and
    # lands as ``<ROM_ID>_RAM_<ts>.bin`` with the ROM_ID in all caps.
    fake = _fake_window(card_value="lf9veb")
    with patch("src.utils.settings.get_settings") as gs:
        gs.return_value.get_reads_directory.return_value = str(tmp_path)
        path = ECUProgrammingWindow._auto_save_to_reads_dir(
            fake, b"\x01\x02", label="RAM"
        )
    assert path is not None
    assert path.name.startswith("LF9VEB_RAM_")


def test_falls_back_to_generic_when_nothing_available(tmp_path):
    fake = _fake_window(card_value="—")
    with patch("src.utils.settings.get_settings") as gs:
        gs.return_value.get_reads_directory.return_value = str(tmp_path)
        path = ECUProgrammingWindow._auto_save_to_reads_dir(fake, b"\x01")
    assert path is not None
    assert path.name.startswith("ecu_read_")


# --- wiring: ROM reads name by cal ID, RAM dumps do not --------------------


def test_rom_read_names_by_cal_id_ram_dump_does_not():
    captured = {}

    def fake_save(data, label="", name_override=None):
        captured["label"] = label
        captured["name_override"] = name_override
        return None

    fake = SimpleNamespace(
        _auto_save_to_reads_dir=fake_save,
        _cal_id_from_rom=ECUProgrammingWindow._cal_id_from_rom,
    )

    ECUProgrammingWindow._auto_save_rom(fake, _rom_with_cal_id())
    assert captured["name_override"] == "LF9VEB"
    assert captured["label"] == ""

    captured.clear()
    ECUProgrammingWindow._auto_save_ram_dump(fake, bytearray(4096))
    assert captured["name_override"] is None  # RAM dump: no cal ID
    assert captured["label"] == "RAM"
