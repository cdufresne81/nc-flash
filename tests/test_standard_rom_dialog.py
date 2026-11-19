"""The Standard ROM dialog: shows where a ROM belongs, converts, hands back."""

import json
import struct
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QMessageBox

import src.ui.standard_rom_dialog as dialog_module
from src.core.standard_rom import StandardPack
from src.ui.standard_rom_dialog import BUNDLED_PACK, StandardRomDialog
from src.utils.paths import get_app_root
from test_standard_rom import (  # noqa: F401 (fixture)
    SHIFT,
    _pack_file,
    _wrapped,
    _stock,
    pack_dir,
)

_app = QApplication.instance() or QApplication([])


def _dialog(pack_dir, rom_path, isolated_settings):  # noqa: F811
    isolated_settings.set_standard_pack_path(str(pack_dir))
    return StandardRomDialog(rom_path=str(rom_path))


def test_shows_the_standard_and_enables_convert(
    pack_dir, tmp_path, isolated_settings  # noqa: F811
):
    rom = tmp_path / "mine.bin"
    rom.write_bytes(_stock(pack_dir, "OLDBEA"))
    dlg = _dialog(pack_dir, rom, isolated_settings)
    text = dlg._report.toPlainText()
    assert (
        "Standard ROM: NEWAEB" in text
        and "Image to use: NEWAEB with the 5MT preset (the same as Mazda's NEWBEB)"
        in text
    )
    assert dlg._convert_btn.isEnabled()


def test_reads_a_one_file_pack(
    pack_dir, tmp_path_factory, tmp_path, isolated_settings  # noqa: F811
):
    packed = _pack_file(pack_dir, tmp_path_factory.mktemp("p") / "std.ncpack")
    rom = tmp_path / "mine.bin"
    rom.write_bytes(_stock(pack_dir, "OLDBEA"))
    dlg = _dialog(packed, rom, isolated_settings)
    assert (
        "Image to use: NEWAEB with the 5MT preset (the same as Mazda's NEWBEB)"
        in dlg._report.toPlainText()
    )
    assert dlg._convert_btn.isEnabled()


def test_a_standard_rom_has_nothing_to_convert(
    pack_dir, tmp_path, isolated_settings  # noqa: F811
):
    for rom_id in ("NEWAEB", "NEWBEB"):  # the standard, and a variant on it
        rom = tmp_path / f"{rom_id}.bin"
        rom.write_bytes(_stock(pack_dir, rom_id))
        dlg = _dialog(pack_dir, rom, isolated_settings)
        assert "nothing to move" in dlg._report.toPlainText()
        assert not dlg._convert_btn.isEnabled()


def test_reads_a_rom_saved_behind_a_tool_header(
    pack_dir, tmp_path, isolated_settings  # noqa: F811
):
    rom = tmp_path / "wrapped.bin"
    rom.write_bytes(_wrapped(bytes(_stock(pack_dir, "OLDBEA"))))
    dlg = _dialog(pack_dir, rom, isolated_settings)
    text = dlg._report.toPlainText()
    assert "header from the tool that saved it" in text
    assert "ROM ID: OLDBEA" in text
    assert dlg._convert_btn.isEnabled()


def test_a_file_of_the_wrong_size_says_so(
    pack_dir, tmp_path, isolated_settings  # noqa: F811
):
    rom = tmp_path / "short.bin"
    rom.write_bytes(b"\x00" * 1000)
    dlg = _dialog(pack_dir, rom, isolated_settings)
    assert "it is 1,000 bytes" in dlg._report.toPlainText()
    assert not dlg._convert_btn.isEnabled()


def test_carried_id_cannot_be_converted(
    pack_dir, tmp_path, isolated_settings  # noqa: F811
):
    rom = tmp_path / "mine.bin"
    rom.write_bytes(_stock(pack_dir, "CARREA"))
    dlg = _dialog(pack_dir, rom, isolated_settings)
    assert "cannot be converted" in dlg._report.toPlainText()
    assert "newer software revision" not in dlg._report.toPlainText()
    assert not dlg._convert_btn.isEnabled()


def test_no_pack_at_all(tmp_path, isolated_settings, monkeypatch):
    monkeypatch.setattr(dialog_module, "get_app_root", lambda: tmp_path)
    rom = tmp_path / "mine.bin"
    rom.write_bytes(b"\x00" * 16)
    dlg = StandardRomDialog(rom_path=str(rom))
    assert dlg._pack_edit.text() == ""
    assert dlg._report.toPlainText() == ""
    assert not dlg._convert_btn.isEnabled()


def test_starts_with_the_shipped_pack(tmp_path, isolated_settings):
    dlg = StandardRomDialog()
    assert dlg._pack_edit.text().endswith("nc_standard_roms.ncpack")
    # the shipped pack is never written to settings
    assert isolated_settings.get_standard_pack_path() == ""


def test_the_users_own_pack_wins_over_the_shipped_one(
    pack_dir, tmp_path, isolated_settings  # noqa: F811
):
    isolated_settings.set_standard_pack_path(str(pack_dir))
    assert StandardRomDialog()._pack_edit.text() == str(pack_dir)
    # a stored path that is gone falls back to the shipped pack
    isolated_settings.set_standard_pack_path(str(tmp_path / "gone.ncpack"))
    assert StandardRomDialog()._pack_edit.text().endswith("nc_standard_roms.ncpack")


def test_the_shipped_pack_is_whole():
    """Every ID has a standard, and every image matches the pack's hashes."""
    pack = StandardPack(get_app_root() / BUNDLED_PACK)
    assert len(pack.rom_ids) == 78
    for rom_id in pack.rom_ids:
        identity = pack.identify(pack.image(rom_id))
        assert identity is not None and identity.rom_id == rom_id
        assert identity.convertible
        if identity.needs_move:
            pack.build_base(identity)  # standard + preset == Mazda's image


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
