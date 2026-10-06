"""Tests for Tools > Patches (src/ui/patches_dialog.py) and its MainWindow hooks."""

import hashlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from src.core.definition_parser import load_definition
from src.ecu import bundled_patches
from src.ui import patches_dialog as D

from test_ncflash_patch import R1_SHA, REPO, _fixture, _r0

_app = QApplication.instance() or QApplication([])


@pytest.fixture
def r0_file(tmp_path):
    p = tmp_path / "car.bin"
    p.write_bytes(_r0())
    return p


def _dialog(rom_path, save=lambda: True, show_test=True):
    dlg = D.PatchesDialog(str(rom_path) if rom_path else None, save, [_fixture()])
    dlg._show_test.setChecked(show_test)
    return dlg


def _select_first(dlg):
    dlg._list.selectRow(0)


def test_no_rom_open():
    dlg = _dialog(None)
    assert "Open a ROM first" in dlg._header.text()
    assert dlg._list.rowCount() == 0


def test_test_patches_hidden_until_opted_in(r0_file):
    dlg = _dialog(r0_file, show_test=False)
    assert dlg._list.rowCount() == 0
    dlg._show_test.setChecked(True)
    assert dlg._list.rowCount() == 1
    assert "[TEST]" in dlg._list.item(0, 0).text()
    assert dlg._list.item(0, 2).text() == "Available"


def test_factory_rom_says_install_romdrop_first(tmp_path):
    p = tmp_path / "factory.bin"
    p.write_bytes((REPO / "examples" / "lf9veb.bin").read_bytes())
    dlg = _dialog(p)
    assert dlg._list.item(0, 2).text() == "Not available"
    assert "install Romdrop first" in dlg._list.item(0, 3).text()
    _select_first(dlg)
    assert not dlg._check_btn.isEnabled()


def test_consent_gates_the_apply(r0_file):
    dlg = _dialog(r0_file)
    _select_first(dlg)
    assert list(dlg._consent_boxes) == ["tyre_restore"]
    dlg._on_check()
    assert dlg._verdict.text().startswith("REFUSED")
    assert not dlg._apply_btn.isEnabled()
    dlg._consent_boxes["tyre_restore"].setChecked(True)
    assert not dlg._apply_btn.isEnabled()  # a new consent needs a new check
    dlg._on_check()
    assert dlg._verdict.text().startswith("OK with warnings")
    assert dlg._apply_btn.isEnabled()


def test_apply_writes_a_new_file_and_offers_to_open(r0_file, tmp_path):
    dlg = _dialog(r0_file)
    _select_first(dlg)
    dlg._consent_boxes["tyre_restore"].setChecked(True)
    out = tmp_path / "patched.bin"
    with (
        patch.object(D.QFileDialog, "getSaveFileName", return_value=(str(out), "")),
        patch.object(QMessageBox, "exec", return_value=QMessageBox.Yes),
    ):
        dlg._on_apply()
    assert hashlib.sha256(out.read_bytes()).hexdigest() == R1_SHA
    assert r0_file.read_bytes() == _r0()  # the original is untouched
    assert dlg.open_requested and dlg.patched_rom_path == str(out)


def test_apply_refuses_an_existing_file(r0_file, tmp_path):
    dlg = _dialog(r0_file)
    _select_first(dlg)
    dlg._consent_boxes["tyre_restore"].setChecked(True)
    out = tmp_path / "exists.bin"
    out.write_bytes(b"keep me")
    with (
        patch.object(D.QFileDialog, "getSaveFileName", return_value=(str(out), "")),
        patch.object(QMessageBox, "warning") as warn,
    ):
        dlg._on_apply()
    warn.assert_called_once()
    assert out.read_bytes() == b"keep me"
    assert dlg.patched_rom_path is None


def test_apply_refuses_the_input_itself(r0_file):
    dlg = _dialog(r0_file)
    _select_first(dlg)
    dlg._consent_boxes["tyre_restore"].setChecked(True)
    with (
        patch.object(D.QFileDialog, "getSaveFileName", return_value=(str(r0_file), "")),
        patch.object(QMessageBox, "warning") as warn,
    ):
        dlg._on_apply()
    warn.assert_called_once()
    assert r0_file.read_bytes() == _r0()


def test_cancelled_save_runs_nothing(r0_file):
    dlg = _dialog(r0_file, save=lambda: False)
    _select_first(dlg)
    dlg._on_check()
    assert dlg._report.rowCount() == 0


def test_installed_rom_shows_installed(tmp_path):
    from src.ecu import ncflash_patch as N

    p = tmp_path / "installed.bin"
    p.write_bytes(N.apply_patch(_r0(), _fixture(), {"tyre_restore"}).output)
    dlg = _dialog(p)
    assert dlg._list.item(0, 2).text() == "Installed"


def test_run_dialog_asks_to_save_first(r0_file):
    doc = SimpleNamespace(
        rom_path=str(r0_file),
        is_modified=lambda: True,
        save=lambda: None,
        set_modified=lambda v: None,
    )
    seen = {}

    class Fake:
        def __init__(self, rom_path, save_document, patches, problems, parent=None):
            seen["save_result"] = save_document()
            self.open_requested, self.patched_rom_path = False, None

        def exec(self):
            return 0

    with (
        patch.object(D, "PatchesDialog", Fake),
        patch.object(QMessageBox, "question", return_value=QMessageBox.Cancel),
    ):
        D.run_patches_dialog(None, doc, lambda d: None, lambda p: None)
    assert seen["save_result"] is False


def test_flash_size_note():
    note = D.flash_size_note([0x230CC, 0xFD800])
    assert "0x020000" in note and "896 KB" in note


# ------------------------------------------------------------ MainWindow hooks


def test_romdrop_patch_file_is_recognised():
    from main import MainWindow

    assert MainWindow._is_romdrop_patch_file(str(REPO / "examples" / "lf9veb.patch"))
    assert not MainWindow._is_romdrop_patch_file(str(REPO / "examples" / "lf9veb.bin"))


def test_installed_patch_adds_its_tables(monkeypatch):
    from main import MainWindow
    from src.ecu import ncflash_patch as N

    monkeypatch.setattr(bundled_patches, "patches_for", lambda cal: [_fixture()])
    base = load_definition(str(REPO / "examples" / "metadata" / "lf9veb.xml"))
    installed = N.apply_patch(_r0(), _fixture(), {"tyre_restore"}).output
    merged = MainWindow._overlay_patch_definitions(None, base, bytearray(installed))
    assert merged.get_table_by_name("AutoBlip - Enable") is not None
    # not installed: definition unchanged
    same = MainWindow._overlay_patch_definitions(None, base, bytearray(_r0()))
    assert same is base
