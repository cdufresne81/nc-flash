"""The ECU window's fast flash: when the one-click flash uses it, what the
buttons say, the interrupted-flash state, and what the user is told after.

Fast flash is opt-in (Settings > ECU, off by default) and runs only on the
Tactrix / OBDX with an accepted kernel and an NC2 ROM; everything else keeps
today's flash. Full Flash is always the normal flash. Exercised against
duck-typed fake windows (no adapter), like the other window tests.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.ecu import fast_flash_kernel as fk
from src.ecu.exceptions import FastFlashUnavailable
from src.ecu.fast_flash import KernelInfo
from src.ecu.recovery import EcuMode, RecoveryProbeResult
from src.ui.ecu_window import (
    FLASH_OPERATIONS,
    KERNEL_RESET_RECONNECT_MS,
    ECUProgrammingWindow,
    _FlashWorker,
)
from tests.test_ecu_fast_flash import make_kernel

EXAMPLE_ROM = Path(__file__).parent.parent / "examples" / "lf9veb.bin"
KERNEL = fk.LoadedKernel(make_kernel(), "test", fk.KernelPin(0xC00, "", 3))
KERNEL_STATE = RecoveryProbeResult(
    EcuMode.FAST_FLASH_KERNEL, kernel=KernelInfo(3, 1, True)
)


@pytest.fixture(scope="module")
def nc2_rom():
    return EXAMPLE_ROM.read_bytes()


@pytest.fixture
def kernel_installed():
    with patch("src.ui.ecu_window.load_kernel", return_value=KERNEL) as load:
        yield load


def test_flash_operations():
    assert FLASH_OPERATIONS == {"flash", "dynamic_flash", "fast_flash"}


def test_setting_is_off_by_default():
    from unittest.mock import patch as _patch

    from src.utils.settings import AppSettings

    with _patch("src.utils.settings.QSettings") as qs:
        store = {}
        qs.return_value.value = lambda key, default=None, type=None: store.get(
            key, default
        )
        qs.return_value.setValue = store.__setitem__
        settings = AppSettings()
        assert settings.get_fast_flash_enabled() is False
        settings.set_fast_flash_enabled(True)
        assert settings.get_fast_flash_enabled() is True


def test_setting_is_in_the_ecu_settings():
    from src.ui.settings_dialog import SETTINGS_REGISTRY

    desc = next(d for d in SETTINGS_REGISTRY if d.key == "ecu.flash.fast_flash")
    assert desc.category == "ECU" and "experimental" in desc.label
    assert desc.label.startswith("Dynamic Flash")
    assert desc.getter == "get_fast_flash_enabled"


# --- Which flash the one-click button uses ---


class _ClickWindow(SimpleNamespace):
    _fast_flash_offer = ECUProgrammingWindow._fast_flash_offer
    _in_recovery = ECUProgrammingWindow._in_recovery
    _fast_kernel_running = ECUProgrammingWindow._fast_kernel_running
    _is_wican = ECUProgrammingWindow._is_wican
    _wican_still_checking = ECUProgrammingWindow._wican_still_checking


def _click_window(tmp_path, rom, *, enabled=True, adapter="j2534", recovery=None):
    rom_file = tmp_path / "tune.bin"
    rom_file.write_bytes(rom)
    settings = MagicMock()
    settings.get_fast_flash_enabled.return_value = enabled
    main_window = MagicMock(settings=settings)
    main_window.get_current_document.return_value = SimpleNamespace(
        rom_path=str(rom_file), is_modified=lambda: False
    )
    return _ClickWindow(
        _main_window=main_window,
        _session=SimpleNamespace(adapter_kind=adapter),
        _recovery=recovery,
        _fast_flash_refused=False,
        _ecu_busy=False,
        _update_action_states=MagicMock(),
        _ecu_answers=lambda: True,
        _check_voltage_warning=lambda: True,
        _check_rpm_gate=lambda: True,
        _confirm_adapter_flash=lambda: True,
        _check_rom_compatibility=MagicMock(return_value=True),
        _get_current_rom_data=lambda: rom,
        _offer_kernel_reset=MagicMock(),
        _start_flash=MagicMock(),
        _wican_fast_flash_caps=lambda: None,  # an old WiCAN: no block compare
        _wican_caps_pending=lambda: False,  # it answered (no fast_flash key)
    )


def _op(win):
    win._start_flash.assert_called_once()
    return win._start_flash.call_args.args[0], win._start_flash.call_args.kwargs


@pytest.mark.parametrize("adapter", ["j2534", "obdx"])
def test_one_click_uses_fast_flash_when_on_and_available(
    tmp_path, nc2_rom, kernel_installed, adapter
):
    win = _click_window(tmp_path, nc2_rom, adapter=adapter)
    ECUProgrammingWindow._on_flash_current(win)
    op, kw = _op(win)
    assert op == "fast_flash"
    assert kw["kernel"] is KERNEL and kw["resume"] is False
    assert kw["rom_data"] == nc2_rom


def test_one_click_keeps_the_cal_id_warning(tmp_path, nc2_rom, kernel_installed):
    win = _click_window(tmp_path, nc2_rom)
    (tmp_path / "ecu.rda").write_bytes(nc2_rom)
    win._check_rom_compatibility.return_value = False
    with patch("src.ui.ecu_window.ARCHIVE_FILENAME", "ecu.rda"):
        ECUProgrammingWindow._on_flash_current(win)
    win._start_flash.assert_not_called()


@pytest.mark.parametrize(
    "case",
    ["setting_off", "wican", "refused_before", "recovery"],
)
def test_one_click_falls_back_to_the_normal_flash(
    tmp_path, nc2_rom, kernel_installed, case
):
    kw = {}
    if case == "setting_off":
        kw["enabled"] = False
    elif case == "wican":
        kw["adapter"] = "wican"
    elif case == "recovery":
        kw["recovery"] = RecoveryProbeResult(EcuMode.BOOT_LOADER)
    win = _click_window(tmp_path, nc2_rom, **kw)
    if case == "refused_before":
        win._fast_flash_refused = True
    ECUProgrammingWindow._on_flash_current(win)
    op, _ = _op(win)
    assert op == "flash"  # no archive here: the normal full flash


def test_one_click_without_a_kernel_uses_the_normal_flash(tmp_path, nc2_rom):
    win = _click_window(tmp_path, nc2_rom)
    with patch("src.ui.ecu_window.load_kernel", return_value=None):
        ECUProgrammingWindow._on_flash_current(win)
    assert _op(win)[0] == "flash"


def test_interrupted_fast_flash_is_finished_with_resume(
    tmp_path, nc2_rom, kernel_installed
):
    win = _click_window(tmp_path, nc2_rom, recovery=KERNEL_STATE)
    ECUProgrammingWindow._on_flash_current(win)
    op, kw = _op(win)
    assert op == "fast_flash" and kw["resume"] is True


@pytest.mark.parametrize("handler", ["_on_flash_current", "_on_full_flash"])
def test_interrupted_fast_flash_without_fast_flash_offers_the_kernel_reset(
    tmp_path, nc2_rom, handler
):
    win = _click_window(tmp_path, nc2_rom, enabled=False, recovery=KERNEL_STATE)
    with patch("src.ui.ecu_window.QTimer") as timer:
        getattr(ECUProgrammingWindow, handler)(win)
    win._start_flash.assert_not_called()
    timer.singleShot.assert_called_once_with(0, win._offer_kernel_reset)


def test_full_flash_never_uses_fast_flash(tmp_path, nc2_rom, kernel_installed):
    win = _click_window(tmp_path, nc2_rom)
    ECUProgrammingWindow._on_full_flash(win)
    assert _op(win)[0] == "flash"


# --- Button labels ---


def _gating(rom, *, enabled=True, recovery=None):
    from src.ecu.session import ECUSessionState
    from tests.test_ecu_window_wican_interlock import _fake_window

    fake = _fake_window(adapter="j2534", connected=True)
    fake._session.state = ECUSessionState.CONNECTED
    fake._session.adapter_kind = "j2534"
    fake._recovery = recovery
    fake._main_window.settings.get_fast_flash_enabled.return_value = enabled
    fake._get_current_rom_data = lambda: rom
    return fake


def _doc(tmp_path):
    return SimpleNamespace(rom_path=str(tmp_path / "tune.bin"), file_name="tune.bin")


@pytest.fixture
def secure(monkeypatch):
    import src.ui.ecu_window as ew

    monkeypatch.setattr(ew, "SECURE_MODULE_AVAILABLE", True)


def test_block_compare_is_dynamic_flash_even_without_an_archive(
    tmp_path, nc2_rom, kernel_installed, secure
):
    """No new name: with block compare available the button says Dynamic, and
    it needs no archive (none exists in tmp_path)."""
    fake = _gating(nc2_rom)
    fake._main_window.get_current_document = lambda: _doc(tmp_path)
    ECUProgrammingWindow._update_action_states(fake)
    fake._btn_flash_current.setText.assert_called_with("Flash Current ROM (Dynamic)")
    fake._btn_flash_current.setEnabled.assert_called_with(True)


def test_label_unchanged_when_off(tmp_path, nc2_rom, kernel_installed, secure):
    fake = _gating(nc2_rom, enabled=False)
    fake._main_window.get_current_document = lambda: _doc(tmp_path)
    ECUProgrammingWindow._update_action_states(fake)
    fake._btn_flash_current.setText.assert_called_with("Flash Current ROM (Full)")


def test_label_for_an_interrupted_fast_flash(tmp_path, nc2_rom, secure):
    fake = _gating(nc2_rom, enabled=False, recovery=KERNEL_STATE)
    fake._main_window.get_current_document = lambda: _doc(tmp_path)
    ECUProgrammingWindow._update_action_states(fake)
    fake._btn_flash_current.setText.assert_called_with("Flash Current ROM")
    assert "reset" in fake._flash_subtitle.setText.call_args.args[0]
    # Only the flash buttons stay usable; reads and DTCs need the application.
    fake._btn_read_rom.setEnabled.assert_called_with(False)
    fake._btn_full_flash.setEnabled.assert_called_with(True)


# --- The interrupted-flash banner and the reset ---


def test_banner_for_a_running_kernel():
    from tests.test_ecu_recovery_detect import _fake_window

    win = _fake_window()
    win._set_recovery(KERNEL_STATE)
    assert win._in_recovery
    text = win._recovery_text.setText.call_args.args[0]
    assert "Dynamic Flash interrupted" in text and "reset the ECU" in text
    assert win._card_ecu.set_value.call_args.args[0] == "FLASH INTERRUPTED"


def _reset_window():
    from src.ecu.session import ECUSessionState

    session = SimpleNamespace(
        state=ECUSessionState.CONNECTED,
        is_connected=True,
        uds=MagicMock(),
        disconnect_ecu=MagicMock(),
    )
    return SimpleNamespace(
        is_busy=False,
        _session=session,
        _on_connect=MagicMock(),
        _progress_detail=MagicMock(),
    )


def test_kernel_reset_is_the_bare_reset_and_reconnects_later():
    win = _reset_window()
    with (
        patch("src.ui.ecu_window.QMessageBox"),
        patch("src.ui.ecu_window.FlashManager") as fm,
        patch("src.ui.ecu_window.QTimer") as timer,
    ):
        ECUProgrammingWindow._reset_programming_ecu(win, fast_flash_kernel=True)
    fm.return_value.reset_programming_ecu.assert_called_once_with(
        win._session.uds, fast_flash_kernel=True
    )
    win._session.disconnect_ecu.assert_called_once()
    timer.singleShot.assert_called_once_with(KERNEL_RESET_RECONNECT_MS, win._on_connect)


@pytest.mark.parametrize("answer, resets", [("Yes", True), ("No", False)])
def test_kernel_reset_offer_defaults_to_no(answer, resets):
    from PySide6.QtWidgets import QMessageBox

    settings = MagicMock()
    settings.get_fast_flash_enabled.return_value = False
    win = SimpleNamespace(
        _reset_programming_ecu=MagicMock(),
        _main_window=SimpleNamespace(settings=settings),
        _wican_reset_blocked=lambda: False,
        _wican_reset_status_unknown=lambda: False,
    )
    with patch(
        "src.ui.ecu_window.QMessageBox.question",
        return_value=getattr(QMessageBox, answer),
    ) as ask:
        ECUProgrammingWindow._offer_kernel_reset(win)
    assert ask.call_args.args[4] == QMessageBox.No
    assert win._reset_programming_ecu.called is resets
    if resets:
        win._reset_programming_ecu.assert_called_once_with(fast_flash_kernel=True)


# --- The worker ---


def test_worker_runs_the_fast_flash(qapp):
    manager = MagicMock()
    worker = _FlashWorker(
        manager,
        "fast_flash",
        rom_data=b"rom",
        archive_path="a.rda",
        kernel=KERNEL,
        resume=True,
    )
    done = []
    worker.finished.connect(lambda: done.append(True))
    worker.run()
    manager.fast_flash.assert_called_once()
    args, kw = manager.fast_flash.call_args
    assert args[:2] == (b"rom", "a.rda")
    assert kw["kernel"] is KERNEL and kw["resume"] is True
    assert done


def test_worker_flags_fast_flash_not_available(qapp):
    manager = MagicMock()
    manager.fast_flash.side_effect = FastFlashUnavailable(
        "Fast flash is not available on this ECU: x. Nothing was changed."
    )
    worker = _FlashWorker(manager, "fast_flash", rom_data=b"", kernel=KERNEL)
    errors = []
    worker.error.connect(errors.append)
    worker.run()
    assert worker.fast_flash_unavailable is True
    assert errors and "Nothing was changed" in errors[0]


# --- Independent review fixes (2026-11-15) ---


def _connect_reads(win, vin, rom_id):
    """Run the window's connect-time reads with this VIN / ROM ID."""
    uds = MagicMock()
    uds.read_vin_block.return_value = vin.encode()
    uds.read_rom_id.return_value = rom_id
    uds.read_dtc_status.return_value = []
    uds.read_battery_voltage.return_value = 12.6
    uds.read_engine_rpm.return_value = 0.0
    win._session = SimpleNamespace(uds=uds, is_connected=True, adapter_kind="j2534")
    ECUProgrammingWindow._read_conditions_async(win)


def _refusal_window(tmp_path, rom):
    win = _click_window(tmp_path, rom)
    win._conditions_pending = False
    win._on_conditions_loaded = MagicMock()
    win._set_recovery = MagicMock()
    win._check_recovery_mode = MagicMock()
    win._flash_read_generation = ""
    return win


def test_refusal_holds_for_the_same_ecu_after_reconnect(
    tmp_path, nc2_rom, kernel_installed
):
    """Re-review R1: a deterministic refusal (chip class, boot code) must not
    send the user back to the block compare after every Connect."""
    win = _refusal_window(tmp_path, nc2_rom)
    _connect_reads(win, "JM1NC25F000000001", "LF9VEB")
    win._fast_flash_refused = win._ecu_identity  # refused this ECU
    _connect_reads(win, "JM1NC25F000000001", "LF9VEB")  # Connect again
    ECUProgrammingWindow._on_flash_current(win)
    assert _op(win)[0] == "flash"  # the standard method (no archive: full)


def test_refusal_cleared_for_another_ecu(tmp_path, nc2_rom, kernel_installed):
    win = _refusal_window(tmp_path, nc2_rom)
    _connect_reads(win, "JM1NC25F000000001", "LF9VEB")
    win._fast_flash_refused = win._ecu_identity
    _connect_reads(win, "JM1NC25F000000002", "LFDJEA")  # a different ECU
    assert win._fast_flash_refused is False
    ECUProgrammingWindow._on_flash_current(win)
    assert _op(win)[0] == "fast_flash"


class _DtcWindow(SimpleNamespace):
    _fast_kernel_running = ECUProgrammingWindow._fast_kernel_running


def test_dtc_refusal_with_a_running_kernel_is_not_called_boot_loader():
    from src.ecu.exceptions import NegativeResponseError

    win = _DtcWindow(_recovery=KERNEL_STATE, _check_recovery_mode=lambda: KERNEL_STATE)
    with patch("src.ui.ecu_window.QMessageBox") as box:
        assert ECUProgrammingWindow._dtc_refusal_means_recovery(
            win, NegativeResponseError(0x11, service_id=0x18)
        )
    text = box.information.call_args.args[2]
    assert "interrupted Dynamic Flash" in text and "boot loader" not in text


def test_reset_offer_with_the_setting_on_does_not_say_turn_it_on():
    """Finding 10."""
    settings = MagicMock()
    settings.get_fast_flash_enabled.return_value = True
    win = SimpleNamespace(
        _reset_programming_ecu=MagicMock(),
        _main_window=SimpleNamespace(settings=settings),
        _wican_reset_blocked=lambda: False,
        _wican_reset_status_unknown=lambda: False,
    )
    with patch("src.ui.ecu_window.QMessageBox.question") as ask:
        ECUProgrammingWindow._offer_kernel_reset(win)
    text = ask.call_args.args[2]
    assert "turn on" not in text and "open the ROM" in text
