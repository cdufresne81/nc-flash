"""The Standard ROM dialog: shows where a ROM belongs, converts, hands back."""

import json
import struct
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QMessageBox

from src.ui.standard_rom_dialog import StandardRomDialog
from test_standard_rom import SHIFT, _stock, pack_dir  # noqa: F401 (fixture)

_app = QApplication.instance() or QApplication([])


def _dialog(pack_dir, rom_path, isolated_settings):  # noqa: F811
    isolated_settings.set_standard_pack_directory(str(pack_dir))
    return StandardRomDialog(rom_path=str(rom_path))


def test_shows_the_standard_and_enables_convert(
    pack_dir, tmp_path, isolated_settings  # noqa: F811
):
    rom = tmp_path / "mine.bin"
    rom.write_bytes(_stock(pack_dir, "OLDBEA"))
    dlg = _dialog(pack_dir, rom, isolated_settings)
    text = dlg._report.toPlainText()
    assert "Standard ROM: NEWAEB" in text and "Image to use: NEWBEB" in text
    assert dlg._convert_btn.isEnabled()


def test_carried_id_cannot_be_converted(
    pack_dir, tmp_path, isolated_settings  # noqa: F811
):
    rom = tmp_path / "mine.bin"
    rom.write_bytes(_stock(pack_dir, "CARREA"))
    dlg = _dialog(pack_dir, rom, isolated_settings)
    assert "cannot be converted" in dlg._report.toPlainText()
    assert not dlg._convert_btn.isEnabled()


def test_no_pack_chosen_yet(tmp_path, isolated_settings):
    rom = tmp_path / "mine.bin"
    rom.write_bytes(b"\x00" * 16)
    dlg = StandardRomDialog(rom_path=str(rom))
    assert dlg._report.toPlainText() == ""
    assert not dlg._convert_btn.isEnabled()


def test_convert_writes_rom_and_report_and_asks_to_open(
    pack_dir, tmp_path, isolated_settings  # noqa: F811
):
    src = _stock(pack_dir, "OLDAEA")
    struct.pack_into(">f", src, 0x2904, 2.5)
    rom = tmp_path / "mine.bin"
    rom.write_bytes(src)
    out = tmp_path / "converted.bin"
    dlg = _dialog(pack_dir, rom, isolated_settings)
    with (
        patch(
            "src.ui.standard_rom_dialog.QFileDialog.getSaveFileName",
            return_value=(str(out), ""),
        ),
        patch(
            "src.ui.standard_rom_dialog.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ),
    ):
        dlg._convert()
    assert struct.unpack_from(">f", out.read_bytes(), 0x2904 + SHIFT)[0] == 2.5
    report = json.loads((tmp_path / "converted.bin.report.json").read_text())
    assert report["original_rom_id"] == "OLDAEA" and report["ok"]
    assert dlg.open_requested and dlg.converted_rom_path == str(out)
    assert rom.read_bytes() == bytes(src)  # the source is untouched


def test_refused_conversion_writes_nothing(
    pack_dir, tmp_path, isolated_settings  # noqa: F811
):
    src = _stock(pack_dir, "OLDAEA")
    struct.pack_into(">2f", src, 0x2930, 11.0, 22.0)  # one half of a pair
    rom = tmp_path / "mine.bin"
    rom.write_bytes(src)
    dlg = _dialog(pack_dir, rom, isolated_settings)
    with (
        patch("src.ui.standard_rom_dialog.QMessageBox.warning") as warned,
        patch("src.ui.standard_rom_dialog.QFileDialog.getSaveFileName") as save,
    ):
        dlg._convert()
    assert warned.called and not save.called
    assert dlg.converted_rom_path is None
    assert "REFUSED" in dlg._report.toPlainText()


def test_never_overwrites_the_source(
    pack_dir, tmp_path, isolated_settings  # noqa: F811
):
    src = bytes(_stock(pack_dir, "OLDAEA"))
    rom = tmp_path / "mine.bin"
    rom.write_bytes(src)
    dlg = _dialog(pack_dir, rom, isolated_settings)
    with (
        patch(
            "src.ui.standard_rom_dialog.QFileDialog.getSaveFileName",
            return_value=(str(rom), ""),
        ),
        patch("src.ui.standard_rom_dialog.QMessageBox.warning") as warned,
    ):
        dlg._convert()
    assert warned.called and rom.read_bytes() == src
    assert dlg.converted_rom_path is None
