"""A ROM that looks like a corrupt read is not flashed without an explicit OK (issue 169).

A read that fails its corruption check is saved as ``<CAL>_SUSPECT_<ts>.bin``.
Flashing it later "works" (the flash corrects the checksums) and puts the
corrupt data on the ECU. These tests check the flash asks first with Cancel as
the default, that a normal edited ROM never triggers it, that a renamed shifted
read is still caught, and that opening one shows a notice.

No dialog may reach the desktop: every QMessageBox entry point is patched.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtWidgets import QMessageBox

from src.ecu.constants import BLOCK_SIZE, ROM_SIZE
from src.ui.ecu_window import ECUProgrammingWindow
from src.ui.rom_read_check import suspect_image_reason

SUSPECT_NAME = "LF9VEB_SUSPECT_20261020_120000.bin"


@pytest.fixture(autouse=True)
def no_real_dialogs():
    """Patch every QMessageBox entry point: a test must never pop a real dialog."""
    with (
        patch.object(QMessageBox, "warning", return_value=QMessageBox.Cancel) as w,
        patch.object(QMessageBox, "information", return_value=QMessageBox.Ok),
        patch.object(QMessageBox, "critical", return_value=QMessageBox.Ok),
        patch.object(QMessageBox, "question", return_value=QMessageBox.No),
        patch.object(QMessageBox, "exec", return_value=QMessageBox.Cancel),
    ):
        yield w


@pytest.fixture(scope="module")
def genuine_rom():
    path = Path(__file__).parent.parent / "examples" / "lf9veb.bin"
    return path.read_bytes()


def _edited(rom: bytes) -> bytes:
    """A normal edit: table bytes changed, checksums NOT corrected (the editor
    leaves that to the flash), so the checksum check would fail."""
    out = bytearray(rom)
    for off in range(0x80010, 0x80050):
        out[off] = (out[off] + 0x11) & 0xFF
    return bytes(out)


def _shift_from(rom: bytes, block_offset: int) -> bytes:
    """The #169 read: every block after the retried one gets the previous data."""
    out = bytearray(rom)
    out[block_offset + BLOCK_SIZE :] = rom[block_offset : ROM_SIZE - BLOCK_SIZE]
    return bytes(out)


def _start_flash(rom: bytes, name: str, operation: str = "flash"):
    """Run ECUProgrammingWindow._start_flash on a fake window. The driver
    builder returns None, so a flash that passes the guard bails right after it
    (no worker, no thread, no ECU)."""
    fake = MagicMock()
    fake._ecu_busy = True
    fake._is_wican.return_value = False
    fake._build_flash_driver.return_value = None
    fake._confirm_flash_image = lambda n, d: (
        ECUProgrammingWindow._confirm_flash_image(fake, n, d)
    )
    ECUProgrammingWindow._start_flash(
        fake, operation, source_name=name, rom_data=bytearray(rom), archive_path="a"
    )
    return fake


# --- the pure check -----------------------------------------------------------


def test_reason_none_for_genuine_and_edited_rom(genuine_rom):
    assert suspect_image_reason("lf9veb.bin", genuine_rom) is None
    assert suspect_image_reason("my_tune_v3.bin", _edited(genuine_rom)) is None


def test_reason_for_suspect_name_any_path_and_case(genuine_rom):
    for name in (
        SUSPECT_NAME,
        rf"C:\Users\me\.nc-flash\reads\{SUSPECT_NAME}",
        "ecu_read_suspect_20261020_120000.bin",
        "LF9VEB_SUSPECT.bin",  # timestamp trimmed by hand
        "lf9veb suspect.bin",
        "LF9VEB_SUSPECT(1).bin",  # Windows copy
        "LF9VEB_suspect2.bin",
    ):
        assert "SUSPECT" in suspect_image_reason(name, genuine_rom)


def test_suspect_inside_another_word_is_not_a_marker(genuine_rom):
    for name in ("unsuspected_tune.bin", "suspected_knock_fix.bin"):
        assert suspect_image_reason(name, genuine_rom) is None


def test_reason_for_renamed_shifted_read(genuine_rom):
    reason = suspect_image_reason("good_backup.bin", _shift_from(genuine_rom, 0x80000))
    assert reason and "0x080400" in reason


# --- the flash path -----------------------------------------------------------


@pytest.mark.parametrize("operation", ["flash", "dynamic_flash"])
def test_suspect_named_file_asks_and_cancel_is_default(
    genuine_rom, no_real_dialogs, operation
):
    fake = _start_flash(genuine_rom, SUSPECT_NAME, operation)

    no_real_dialogs.assert_called_once()
    args = no_real_dialogs.call_args.args
    title, text, buttons, default = args[1], args[2], args[3], args[4]
    assert "Do Not Flash" in title
    assert "Do not flash this file" in text and "Read the ECU again" in text
    assert default == QMessageBox.Cancel
    assert buttons & QMessageBox.Cancel and buttons & QMessageBox.Yes
    # Cancelled: nothing built or started, and the window is usable again.
    fake._build_flash_driver.assert_not_called()
    fake._poll_timer.stop.assert_not_called()
    assert fake._ecu_busy is False
    fake._update_action_states.assert_called()


def test_cancel_clears_a_pending_rpm_gate_override(genuine_rom):
    fake = MagicMock()
    fake._guard_override = True
    assert not ECUProgrammingWindow._confirm_flash_image(
        fake, SUSPECT_NAME, genuine_rom
    )
    assert fake._guard_override is False


def test_explicit_yes_flashes_anyway(genuine_rom, no_real_dialogs):
    no_real_dialogs.return_value = QMessageBox.Yes
    fake = _start_flash(genuine_rom, SUSPECT_NAME)
    no_real_dialogs.assert_called_once()
    fake._build_flash_driver.assert_called_once()


def test_normal_edited_rom_flashes_without_warning(genuine_rom, no_real_dialogs):
    fake = _start_flash(_edited(genuine_rom), "LF9VEB_my_tune.bin")
    no_real_dialogs.assert_not_called()
    fake._build_flash_driver.assert_called_once()


def test_renamed_shifted_read_is_caught_on_flash(genuine_rom, no_real_dialogs):
    fake = _start_flash(_shift_from(genuine_rom, 0x80000), "backup_ok.bin")
    no_real_dialogs.assert_called_once()
    assert "copy of the 1 KB before it" in no_real_dialogs.call_args.args[2]
    fake._build_flash_driver.assert_not_called()


def test_read_is_not_guarded(genuine_rom, no_real_dialogs):
    """The guard is for writes only: reading the ECU must never ask."""
    fake = _start_flash(genuine_rom, SUSPECT_NAME, operation="read")
    no_real_dialogs.assert_not_called()
    fake._build_flash_driver.assert_called_once()


@pytest.mark.parametrize("handler", ["_on_flash_current", "_on_full_flash"])
def test_flash_buttons_reach_the_guard(tmp_path, genuine_rom, handler):
    """Both flash buttons hand the file name and bytes to _start_flash, where
    the guard lives."""
    rom_file = tmp_path / SUSPECT_NAME
    rom_file.write_bytes(genuine_rom)
    fake = MagicMock()
    fake._main_window.get_current_document.return_value = SimpleNamespace(
        rom_path=str(rom_file), is_modified=lambda: False
    )
    getattr(ECUProgrammingWindow, handler)(fake)
    fake._start_flash.assert_called_once()
    kwargs = fake._start_flash.call_args.kwargs
    assert kwargs["source_name"] == SUSPECT_NAME
    assert kwargs["rom_data"] == genuine_rom


# --- opening for editing ------------------------------------------------------


def _open_notice(name: str, rom: bytes):
    from main import MainWindow

    fake = MagicMock()
    doc = SimpleNamespace(file_name=name, rom_reader=SimpleNamespace(rom_data=rom))
    with patch("main.QTimer") as timer:
        timer.singleShot.side_effect = lambda _ms, fn: fn()
        MainWindow._warn_if_suspect_rom(fake, doc)
    return fake


def test_opening_suspect_rom_shows_notice(genuine_rom, no_real_dialogs):
    fake = _open_notice(SUSPECT_NAME, bytearray(genuine_rom))
    no_real_dialogs.assert_called_once()
    assert "do not flash it" in no_real_dialogs.call_args.args[2]
    assert "corrupt" in fake.statusBar().showMessage.call_args.args[0]


def test_opening_normal_rom_shows_nothing(genuine_rom, no_real_dialogs):
    fake = _open_notice("my_tune.bin", bytearray(_edited(genuine_rom)))
    no_real_dialogs.assert_not_called()
    fake.statusBar().showMessage.assert_not_called()


def test_project_open_runs_the_notice():
    """Project tabs get the same open notice (they lose the SUSPECT name, so
    only the copied-block trace can fire there)."""
    import inspect

    from src.ui.project_mixin import ProjectMixin

    assert "self._warn_if_suspect_rom(rom_document)" in inspect.getsource(
        ProjectMixin.open_project_path
    )


# --- creating a project (the copy drops the SUSPECT name) ---------------------


def _new_project(tmp_path, name: str, rom: bytes, reply=None):
    from src.ui.project_mixin import ProjectMixin

    rom_file = tmp_path / name
    rom_file.write_bytes(rom)
    fake = MagicMock()
    fake._confirm_project_source = lambda p: ProjectMixin._confirm_project_source(
        fake, p
    )
    wizard = MagicMock(rom_path=str(rom_file))
    wizard.exec.return_value = 1  # QDialog.Accepted
    with (
        patch("src.ui.project_mixin.ProjectWizard", return_value=wizard),
        patch("src.ui.project_mixin.QDialog") as dlg,
    ):
        dlg.Accepted = 1
        ProjectMixin.new_project(fake)
    return fake


def test_project_from_suspect_read_asks_and_cancel_creates_nothing(
    tmp_path, genuine_rom, no_real_dialogs
):
    fake = _new_project(tmp_path, SUSPECT_NAME, genuine_rom)
    no_real_dialogs.assert_called_once()
    assert no_real_dialogs.call_args.args[4] == QMessageBox.Cancel
    fake.project_manager.create_project.assert_not_called()


def test_project_from_suspect_read_created_after_explicit_yes(
    tmp_path, genuine_rom, no_real_dialogs
):
    no_real_dialogs.return_value = QMessageBox.Yes
    fake = _new_project(tmp_path, SUSPECT_NAME, genuine_rom)
    fake.project_manager.create_project.assert_called_once()


def test_project_from_normal_rom_does_not_ask(tmp_path, genuine_rom, no_real_dialogs):
    fake = _new_project(tmp_path, "my_tune.bin", _edited(genuine_rom))
    no_real_dialogs.assert_not_called()
    fake.project_manager.create_project.assert_called_once()
