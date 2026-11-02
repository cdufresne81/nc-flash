"""An ECU left mid-programming by an interrupted flash (bench-proven 2026-10-17).

Its SBL keeps running and refuses the flash-counter routine and RequestDownload
with NRC 0x22. The flash must stop there with ECUStillProgrammingError (nothing
written yet), and the reset that clears the state must be the authenticated one,
refused while the engine runs.

Also covers the seed format check that keeps a malformed reply from passing as
an all-zero "already unlocked" seed.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from src.ecu.constants import ROM_SIZE, SID_SECURITY_ACCESS
from src.ecu.exceptions import (
    ECUStillProgrammingError,
    EngineRunningError,
    FlashError,
    NegativeResponseError,
    SecurityAccessDenied,
)
from src.ecu.flash_manager import FlashManager
from src.ecu.protocol import UDSConnection
from src.ecu.session import ECUSessionState
from src.ecu.transport import FakeTransport
from src.ui.ecu_window import ECUProgrammingWindow, _FlashWorker


def _uds(seed=b"\x55\xaa\x55", rpm=None):
    uds = MagicMock()
    uds.security_access_request_seed.return_value = seed
    uds.read_engine_rpm.return_value = rpm
    return uds


def _flash(uds):
    fm = FlashManager()
    fm.use_uds(uds)
    with (
        patch("src.ecu.flash_manager.SECURE_MODULE_AVAILABLE", True),
        patch(
            "src.ecu.flash_manager.compute_security_key", return_value=b"\x01\x02\x03"
        ),
        patch(
            "src.ecu.flash_manager.prepare_flash_image",
            return_value=(bytearray(ROM_SIZE), b"\x00" * 0x1800, "NC1", []),
        ),
        patch("src.ecu.flash_manager.verify_ecu_generation"),
    ):
        fm.flash_rom(bytes(ROM_SIZE))


# --- Flash refused by a stuck ECU ---


def test_flash_counter_refusal_stops_before_download():
    uds = _uds()
    uds.check_flash_counter.side_effect = NegativeResponseError(0x22, "", 0)
    with pytest.raises(ECUStillProgrammingError):
        _flash(uds)
    uds.request_download.assert_not_called()
    uds.transfer_data.assert_not_called()


def test_download_refusal_stops_before_transfer():
    uds = _uds()
    uds.request_download.side_effect = NegativeResponseError(0x22, "", 0)
    with pytest.raises(ECUStillProgrammingError):
        _flash(uds)
    uds.transfer_data.assert_not_called()


def test_other_nrc_is_not_relabelled():
    uds = _uds()
    uds.check_flash_counter.side_effect = NegativeResponseError(0x33, "", 0)
    with pytest.raises(NegativeResponseError):
        _flash(uds)


def test_still_programming_is_a_flash_error():
    assert issubclass(ECUStillProgrammingError, FlashError)
    assert "Nothing was written" in str(ECUStillProgrammingError())


def test_worker_flags_a_stuck_ecu(qapp):
    manager = MagicMock()
    manager.flash_rom.side_effect = ECUStillProgrammingError()
    worker = _FlashWorker(manager, "flash", rom_data=b"")
    errors = []
    worker.error.connect(errors.append)
    worker.run()
    assert worker.needs_ecu_reset
    assert errors and "programming mode" in errors[0]


# --- The reset ---


@patch("src.ecu.flash_manager.compute_security_key", return_value=b"\x0a\x0b\x0c")
def test_reset_checks_rpm_authenticates_then_resets(_key):
    uds = _uds()
    FlashManager().reset_programming_ecu(uds)
    names = [c[0] for c in uds.method_calls]
    assert names == [
        "tester_present",
        "read_engine_rpm",
        "diagnostic_session",
        "security_access_request_seed",
        "security_access_send_key",
        "ecu_reset",
    ]
    assert uds.security_access_send_key.call_args == call(b"\x0a\x0b\x0c")


def test_reset_refuses_a_running_engine():
    uds = _uds(rpm=780.0)
    with pytest.raises(EngineRunningError):
        FlashManager().reset_programming_ecu(uds)
    uds.diagnostic_session.assert_not_called()
    uds.ecu_reset.assert_not_called()


def test_reset_skips_the_key_when_already_unlocked():
    uds = _uds(seed=b"\x00\x00\x00")
    FlashManager().reset_programming_ecu(uds)
    uds.security_access_send_key.assert_not_called()
    uds.ecu_reset.assert_called_once()


# --- Seed format ---


@pytest.mark.parametrize(
    "reply",
    [
        b"\x01\x00",  # 1-byte seed
        b"\x01\x00\x00",  # 2-byte seed
        b"\x01\x00\x00\x00\x00\x00",  # 5-byte seed
        b"\x02\x00\x00\x00",  # wrong sub-function echo
    ],
)
def test_malformed_seed_is_refused(reply):
    uds = UDSConnection(
        FakeTransport(responses=[bytes([SID_SECURITY_ACCESS + 0x40]) + reply])
    )
    with pytest.raises(SecurityAccessDenied):
        uds.security_access_request_seed()


def test_well_formed_zero_seed_is_returned():
    uds = UDSConnection(FakeTransport(responses=[b"\x67\x01\x00\x00\x00"]))
    assert uds.security_access_request_seed() == b"\x00\x00\x00"


# --- Window: the reset offer ---


def _window(**kw):
    session = SimpleNamespace(
        state=kw.get("state", ECUSessionState.CONNECTED),
        is_connected=kw.get("connected", True),
        uds=MagicMock(),
        disconnect_ecu=MagicMock(),
    )
    return SimpleNamespace(
        is_busy=kw.get("busy", False),
        _session=session,
        _on_connect=MagicMock(),
        _progress_detail=MagicMock(),
    )


@pytest.mark.parametrize(
    "kw", [{"busy": True}, {"state": ECUSessionState.BUSY}], ids=["thread", "session"]
)
def test_reset_refused_while_an_operation_runs(kw):
    win = _window(**kw)
    with (
        patch("src.ui.ecu_window.QMessageBox") as box,
        patch("src.ui.ecu_window.FlashManager") as fm,
    ):
        ECUProgrammingWindow._reset_programming_ecu(win)
    fm.assert_not_called()
    box.warning.assert_called_once()


def test_reset_reports_a_failed_connect():
    win = _window(connected=False)
    with (
        patch("src.ui.ecu_window.QMessageBox") as box,
        patch("src.ui.ecu_window.FlashManager") as fm,
    ):
        ECUProgrammingWindow._reset_programming_ecu(win)
    win._on_connect.assert_called_once()
    fm.assert_not_called()
    assert "Could not connect" in box.warning.call_args.args[2]


def test_reset_failure_is_shown_and_keeps_the_session():
    win = _window()
    with (
        patch("src.ui.ecu_window.QMessageBox") as box,
        patch("src.ui.ecu_window.FlashManager") as fm,
    ):
        fm.return_value.reset_programming_ecu.side_effect = EngineRunningError(800)
        ECUProgrammingWindow._reset_programming_ecu(win)
    assert "Engine is running" in box.critical.call_args.args[2]
    win._session.disconnect_ecu.assert_not_called()


def test_reset_success_disconnects_and_reconnects_later():
    win = _window()
    with (
        patch("src.ui.ecu_window.QMessageBox"),
        patch("src.ui.ecu_window.FlashManager") as fm,
        patch("src.ui.ecu_window.QTimer") as timer,
    ):
        ECUProgrammingWindow._reset_programming_ecu(win)
    fm.return_value.reset_programming_ecu.assert_called_once_with(win._session.uds)
    win._session.disconnect_ecu.assert_called_once()
    timer.singleShot.assert_called_once_with(3000, win._on_connect)


def test_connect_is_ignored_while_one_is_in_flight():
    session = SimpleNamespace(
        is_connected=False, state=ECUSessionState.CONNECTING, cleanup=MagicMock()
    )
    win = SimpleNamespace(_session=session)
    ECUProgrammingWindow._on_connect(win)
    session.cleanup.assert_not_called()
