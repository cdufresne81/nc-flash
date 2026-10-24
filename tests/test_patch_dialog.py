"""Tests for the Patch ROM dialog's success handoff.

A successful patch used to announce itself only in the log console, so the user
had no explicit confirmation that anything happened. The dialog now raises a
modal ("ROM patched successfully" + "open it now?") and reports the answer back
to MainWindow, which owns ROM opening.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from src.ui.flash_mixin import FlashMixin
from src.ui.patch_dialog import PatchRomDialog

_app = QApplication.instance() or QApplication([])

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"
STOCK_ROM = EXAMPLES_DIR / "lf9veb.bin"
PATCH_FILE = EXAMPLES_DIR / "lf9veb.patch"

pytestmark = pytest.mark.skipif(
    not (STOCK_ROM.is_file() and PATCH_FILE.is_file()),
    reason="example ROM/patch files not available",
)


def _armed_dialog(output_path):
    """A dialog with both inputs chosen and an explicit output path."""
    dlg = PatchRomDialog()
    dlg._stock_path = str(STOCK_ROM)
    dlg._patch_path = str(PATCH_FILE)
    dlg._output_edit.setText(str(output_path))
    return dlg


def test_success_prompts_and_opens_when_user_accepts(tmp_path):
    """Yes → patched path recorded, open requested, dialog closes accepted."""
    output = tmp_path / "patched.bin"
    dlg = _armed_dialog(output)

    with patch.object(QMessageBox, "exec", return_value=QMessageBox.Yes) as box:
        dlg._apply_patch()

    box.assert_called_once()
    assert output.is_file()
    assert dlg.patched_rom_path == str(output)
    assert dlg.open_requested is True
    assert dlg.result() == QDialog.Accepted
    dlg.deleteLater()


def test_success_prompt_declined_keeps_dialog_open(tmp_path):
    """No → the ROM is still written, but nothing is opened and the dialog stays."""
    output = tmp_path / "patched.bin"
    dlg = _armed_dialog(output)

    with patch.object(QMessageBox, "exec", return_value=QMessageBox.No):
        dlg._apply_patch()

    assert output.is_file()
    assert dlg.patched_rom_path == str(output)
    assert dlg.open_requested is False
    assert dlg.result() != QDialog.Accepted
    dlg.deleteLater()


def test_failed_patch_clears_previous_success(tmp_path):
    """A failed retry must not leave a stale patched path armed for opening."""
    output = tmp_path / "patched.bin"
    dlg = _armed_dialog(output)

    with patch.object(QMessageBox, "exec", return_value=QMessageBox.No):
        dlg._apply_patch()
    assert dlg.patched_rom_path == str(output)

    # Retry with a patch file that cannot apply.
    bad_patch = tmp_path / "bad.patch"
    bad_patch.write_bytes(b"\x00" * 16)
    dlg._patch_path = str(bad_patch)

    with patch.object(QMessageBox, "warning"), patch.object(QMessageBox, "critical"):
        dlg._apply_patch()

    assert dlg.patched_rom_path is None
    assert dlg.open_requested is False
    dlg.deleteLater()


def test_prompt_surfaces_crc_warnings():
    """A CRC-warning patch still reports success, but with the warning shown."""
    dlg = PatchRomDialog()
    result = SimpleNamespace(crc_warnings=["stock CRC not in database"])

    captured = {}

    def fake_exec(self):
        captured["icon"] = self.icon()
        captured["text"] = self.text()
        captured["informative"] = self.informativeText()
        return QMessageBox.No

    with patch.object(QMessageBox, "exec", fake_exec):
        assert dlg._confirm_open("C:/roms/out.bin", result) is False

    assert captured["icon"] == QMessageBox.Warning
    assert "successfully" in captured["text"]
    assert "stock CRC not in database" in captured["informative"]
    assert "C:/roms/out.bin" in captured["informative"]
    dlg.deleteLater()


def test_flash_mixin_opens_patched_rom():
    """MainWindow opens the ROM only when the dialog says the user asked for it."""
    host = SimpleNamespace(_open_rom_file=MagicMock())

    dlg = MagicMock()
    dlg.open_requested = True
    dlg.patched_rom_path = "C:/roms/out.bin"

    with patch("src.ui.patch_dialog.PatchRomDialog", return_value=dlg):
        FlashMixin._on_patch_rom(host)

    host._open_rom_file.assert_called_once_with("C:/roms/out.bin")


def test_flash_mixin_does_not_open_when_declined():
    """Declining the prompt leaves MainWindow untouched."""
    host = SimpleNamespace(_open_rom_file=MagicMock())

    dlg = MagicMock()
    dlg.open_requested = False
    dlg.patched_rom_path = "C:/roms/out.bin"

    with patch("src.ui.patch_dialog.PatchRomDialog", return_value=dlg):
        FlashMixin._on_patch_rom(host)

    host._open_rom_file.assert_not_called()


def test_unverifiable_patch_writes_nothing(tmp_path):
    """A ROM the database can't vouch for is refused before anything is saved."""
    from src.ecu.constants import CAL_ID_OFFSETS

    stock = bytearray(STOCK_ROM.read_bytes())
    for offset in CAL_ID_OFFSETS:
        if stock[offset : offset + 1] == b"L":
            stock[offset : offset + 6] = b"LFJ1ED"  # no RomDrop patch exists
    stock_path = tmp_path / "lfj1ed.bin"
    stock_path.write_bytes(bytes(stock))

    output = tmp_path / "patched.bin"
    dlg = _armed_dialog(output)
    dlg._stock_path = str(stock_path)

    with (
        patch.object(QMessageBox, "warning") as warning,
        patch.object(QMessageBox, "exec") as success,
    ):
        dlg._apply_patch()

    warning.assert_called_once()
    assert "LFJ1ED" in warning.call_args.args[2]
    success.assert_not_called()
    assert not output.exists()
    assert dlg.patched_rom_path is None
    dlg.deleteLater()
