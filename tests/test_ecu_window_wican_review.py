"""ECU window fixes from the reviews of the WiCAN Dynamic Flash wiring.

T1  the refusal is keyed to the ECU the flash was for, although the session
    release clears the connect-time identity first;
(b) the window opens even when the WiCAN fast-flash module can't be imported;
M1  /host_caps answered None is read again; no kernel-reset offer on the WiCAN
    while that read is pending, and never after a wrong start page (st 62);
M2  a busy adapter (st 9/14) is not a refusal and asks for no ignition cycle;
M4  "no application" also means a resume is due;
L3  GET /fast_flash is read off the UI thread; a click before it answers waits.
"""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.ecu import wican_fast_flash as wff  # noqa: E402
from src.ecu.exceptions import FastFlashUnavailable  # noqa: E402
from src.ecu.fast_flash import KernelInfo  # noqa: E402
from src.ecu.recovery import EcuMode, RecoveryProbeResult  # noqa: E402
from src.ecu.session import ECUSessionState  # noqa: E402
from src.ui.ecu_window import ECUProgrammingWindow  # noqa: E402
from tests.test_ecu_window_post_flash import (  # noqa: E402
    _session,
    _window,
    connects,
    no_dialogs,
)

_ = (connects, no_dialogs)  # fixtures, used by name below
REPO = Path(__file__).resolve().parent.parent


def _ff_error(st, state, ponr=False, app="1"):
    return wff.WiCANFastFlashError(
        "Dynamic Flash: x",
        event=wff.FFEvent("err", "", {"st": str(st), "app": app}),
        state=state,
        ponr=ponr,
    )


def _finish_with(window, error, operation="fast_flash"):
    window._session_acquired = True
    window._current_operation = operation
    window._stack.setCurrentIndex(1)
    worker = MagicMock(
        result=None,
        needs_ecu_reset=False,
        error_kind="",
        fast_flash_unavailable=False,
        wican_ff_error=error,
    )
    window._on_flash_finished(False, None, worker, str(error))


# --- T1 -------------------------------------------------------------------------


class _Refusing:
    def fast_flash(self, rom_data, archive_path=None, progress_cb=None, **kw):
        time.sleep(0.05)
        raise FastFlashUnavailable(
            "Dynamic Flash can't compare blocks on this ECU: x. Nothing was changed."
        )


def test_refusal_keyed_to_the_flashed_ecu_through_the_real_release(
    qtbot, connects, monkeypatch
):
    """The real order: the worker ends, session.release() fires DISCONNECTED
    (which clears _ecu_identity), then the outcome is handled."""
    session = _session("j2534", state=ECUSessionState.CONNECTED)
    window = _window(qtbot, connects, "j2534", session)
    window._ecu_identity = ("JM1NC25F000000001", "LF9VEB")

    def build(self, op, source_name=None):
        self._session.acquire()
        self._session_acquired = True
        return _Refusing()

    monkeypatch.setattr(ECUProgrammingWindow, "_build_flash_driver", build)
    window._start_flash(
        "fast_flash", rom_data=b"\x00" * 16, archive_path="a.rda", kernel=None
    )
    thread = window._flash_thread
    qtbot.waitUntil(lambda: window._flash_thread is None, timeout=5000)
    qtbot.waitUntil(lambda: not thread.isRunning(), timeout=5000)
    assert window._ecu_identity is None  # cleared by the release
    assert window._fast_flash_refused == ("JM1NC25F000000001", "LF9VEB")
    qtbot.wait(400)


# --- (b) -------------------------------------------------------------------------


def test_window_imports_without_the_wican_fast_flash_module():
    code = (
        "import sys; sys.modules['src.ecu.wican_fast_flash'] = None\n"
        "import src.ui.ecu_window as w\n"
        "assert w.fast_flash_available is None and w.EcuState is None\n"
        "assert issubclass(w.WiCANFastFlashError, Exception)\n"
        "print('ok')\n"
    )
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().endswith("ok")


# --- M1 ---------------------------------------------------------------------------


def test_caps_answered_none_are_read_again(monkeypatch):
    import src.ui.ecu_window as ew

    monkeypatch.setattr(ew, "WICAN_CAPS_RETRY_S", (0.0, 0.0))
    calls = []

    def caps(self):
        calls.append(1)
        return None if len(calls) == 1 else {"fast_flash": 1}

    fake = SimpleNamespace(
        _session=SimpleNamespace(transport=SimpleNamespace(host="192.0.2.1"))
    )
    with patch("src.ecu.wican_config.WiCANConfigurator.host_caps", caps):
        assert ECUProgrammingWindow._wican_fast_flash_caps(fake) is None
        for _ in range(100):
            if "caps" in fake._wican_caps_cache[1]:
                break
            time.sleep(0.02)
        assert ECUProgrammingWindow._wican_fast_flash_caps(fake) is None  # retry
        for _ in range(100):
            if fake._wican_caps_cache[1].get("caps"):
                break
            time.sleep(0.02)
        assert ECUProgrammingWindow._wican_fast_flash_caps(fake) == {"fast_flash": 1}
    assert len(calls) == 2


class _CheckingWindow(SimpleNamespace):
    _wican_still_checking = ECUProgrammingWindow._wican_still_checking
    _wican_caps_pending = ECUProgrammingWindow._wican_caps_pending
    _wican_caps_gave_up = ECUProgrammingWindow._wican_caps_gave_up


def test_no_kernel_reset_offer_on_wican_while_caps_are_pending():
    session = SimpleNamespace(transport=SimpleNamespace(host=None))
    win = _CheckingWindow(
        _session=session,
        _is_wican=lambda: True,
        _wican_fast_flash_caps=lambda: None,
        _wican_caps_cache=(session, {}),  # read in flight
    )
    with patch("src.ui.ecu_window.QMessageBox") as box:
        assert ECUProgrammingWindow._wican_still_checking(win) is True
    assert "still checking" in box.information.call_args.args[2]


def test_kernel_reset_blocked_after_a_wrong_start_page(qtbot, connects, no_dialogs):
    window = _window(qtbot, connects, "wican", _session("wican"))
    _finish_with(window, _ff_error(62, wff.EcuState.WRONG_ENTRY, ponr=True))
    assert window._wican_wrong_entry
    qtbot.wait(400)
    no_dialogs.clear()
    with patch.object(ECUProgrammingWindow, "_reset_programming_ecu") as reset:
        window._offer_kernel_reset()
    reset.assert_not_called()
    kind, args = no_dialogs[-1]
    assert kind == "warning" and "Do NOT reset" in args[2]
    assert "ignition off" in args[2]


# --- M2 / M4 ------------------------------------------------------------------------


BUSY_TEXT = (
    "Dynamic Flash did not start: another flash or read is using the adapter. "
    "Leave the ignition on and wait."
)


@pytest.mark.parametrize("stage", [9, 14])
def test_busy_adapter_is_not_a_refusal_and_asks_no_ignition_cycle(
    qtbot, connects, no_dialogs, stage
):
    window = _window(qtbot, connects, "wican", _session("wican"))
    err = wff.WiCANFastFlashError(
        BUSY_TEXT,
        event=wff.FFEvent("err", "", {"st": str(stage)}),
        state=wff.EcuState.BUSY,
    )
    _finish_with(window, err)
    assert not window._fast_flash_refused
    assert window._wican_resume_pending is False
    assert window._wican_wrong_entry  # no reset offer after BUSY
    qtbot.waitUntil(lambda: bool(no_dialogs), timeout=2000)
    text = no_dialogs[0][1][2]
    assert BUSY_TEXT in text  # the adapter's message, as is
    assert "Switch the ignition off and on" not in text


@pytest.mark.parametrize("stage", [9, 14])
def test_worker_does_not_mark_a_busy_adapter_as_refused(qapp, stage):
    from src.ui.ecu_window import _FlashWorker

    manager = MagicMock()
    manager.fast_flash.side_effect = _ff_error(stage, wff.EcuState.BUSY)
    worker = _FlashWorker(manager, "fast_flash", rom_data=b"", kernel=None)
    worker.run()
    assert worker.fast_flash_unavailable is False


def test_untouched_failure_still_allows_the_reset_offer(qtbot, connects, no_dialogs):
    window = _window(qtbot, connects, "wican", _session("wican"))
    _finish_with(window, _ff_error(31, wff.EcuState.UNTOUCHED))
    assert not getattr(window, "_wican_wrong_entry", False)
    qtbot.wait(400)


@pytest.mark.parametrize("state", [wff.EcuState.NOT_STARTED, wff.EcuState.UNKNOWN])
def test_unknown_endings_keep_the_reset_offer(qtbot, connects, no_dialogs, state):
    """Re-review R1: an ECU with no application must still be resettable to
    its boot loader for a full flash."""
    window = _window(qtbot, connects, "wican", _session("wican"))
    _finish_with(window, wff.WiCANFastFlashError("Dynamic Flash: lost", state=state))
    assert not getattr(window, "_wican_wrong_entry", None)
    assert not window._wican_reset_blocked()
    qtbot.wait(400)


def test_no_application_means_resume(qtbot, connects, no_dialogs):
    window = _window(qtbot, connects, "wican", _session("wican"))
    _finish_with(window, _ff_error(31, wff.EcuState.NO_APPLICATION, app="0"))
    assert window._wican_resume_pending is True
    qtbot.wait(400)


# --- L3 ------------------------------------------------------------------------------


def test_fast_flash_status_is_read_off_the_ui_thread():
    gate = threading.Event()
    threads = []

    def status(client, timeout_s=5.0):
        threads.append(threading.current_thread())
        gate.wait(2)
        return {"state": "interrupted"}

    win = SimpleNamespace(
        _session=SimpleNamespace(transport=SimpleNamespace(host="192.0.2.1")),
        _wican_resume_pending=False,
    )
    win._wican_status_holder = lambda: ECUProgrammingWindow._wican_status_holder(win)
    with patch("src.ecu.wican_fast_flash.WiCANFastFlashClient.status", status):
        assert ECUProgrammingWindow._wican_resume_due(win) is None  # pending
        gate.set()
        for _ in range(100):
            if "interrupted" in win._wican_status_cache[1]:
                break
            time.sleep(0.02)
        assert ECUProgrammingWindow._wican_resume_due(win) is True
    assert threads and threads[0] is not threading.current_thread()


def test_click_before_the_status_answers_waits(tmp_path):
    from src.ecu import fast_flash_kernel as fk
    from tests.test_ecu_fast_flash import make_kernel

    kernel = fk.LoadedKernel(make_kernel(), "t", fk.KernelPin(0xC00, "", 3))
    rom_file = tmp_path / "tune.bin"
    rom_file.write_bytes(b"\x00" * 16)
    win = MagicMock()
    win._main_window.get_current_document.return_value = SimpleNamespace(
        rom_path=str(rom_file), is_modified=lambda: False
    )
    win._fast_kernel_running = False
    win._fast_flash_offer.return_value = (kernel, "")
    win._is_wican.return_value = True
    win._wican_resume_due.return_value = None
    with patch("src.ui.ecu_window.QMessageBox") as box:
        ECUProgrammingWindow._on_flash_current(win)
    win._start_flash.assert_not_called()
    assert "still asking the WiCAN" in box.information.call_args.args[2]


# --- L4 ------------------------------------------------------------------------------


def test_wican_restart_not_confirmed_is_reported(qtbot, connects, no_dialogs):
    window = _window(qtbot, connects, "wican", _session("wican"))
    window._session_acquired = True
    window._current_operation = "fast_flash"
    window._flash_was_resume = False
    window._stack.setCurrentIndex(1)
    outcome = SimpleNamespace(restart_confirmed=False)
    worker = MagicMock(result=outcome, needs_ecu_reset=False, error_kind="")
    window._on_flash_finished(True, None, worker, "")
    qtbot.waitUntil(lambda: bool(no_dialogs), timeout=2000)
    assert "did not confirm that it restarted" in no_dialogs[0][1][2]


def test_kernel_state_for_resume_is_unchanged():
    """Sanity: the kernel-running state used by these paths."""
    r = RecoveryProbeResult(EcuMode.FAST_FLASH_KERNEL, kernel=KernelInfo(3, 1, True))
    assert r.needs_recovery


# --- re-review R1 / R4 / R5 -----------------------------------------------------------


def test_block_is_for_this_wican_only_not_a_cable_or_another_ecu(
    qtbot, connects, no_dialogs
):
    window = _window(qtbot, connects, "wican", _session("wican"))
    window._flash_ecu_identity = ("VIN1", "LF9VEB")
    _finish_with(window, _ff_error(61, wff.EcuState.MAY_BE_COMPLETE, ponr=True))
    assert window._wican_reset_blocked()  # kernel running: identity unreadable
    window._ecu_identity = ("VIN2", "LFDJEA")  # another ECU, application running
    assert not window._wican_reset_blocked()
    window._ecu_identity = None
    cable = _session("j2534")
    window._session = cable  # a Tactrix connection later in the run
    assert not window._wican_reset_blocked()
    qtbot.wait(400)


def test_connect_reading_an_application_clears_the_block():
    win = SimpleNamespace(
        _session=SimpleNamespace(uds=MagicMock(), is_connected=True),
        _conditions_pending=False,
        _on_conditions_loaded=MagicMock(),
        _set_recovery=MagicMock(),
        _check_recovery_mode=MagicMock(),
        _flash_read_generation="",
        _wican_wrong_entry=("192.0.2.1", None),
        _fast_flash_refused=False,
    )
    uds = win._session.uds
    uds.read_vin_block.return_value = b"JM1NC25F000000001"
    uds.read_rom_id.return_value = "LF9VEB"
    uds.read_dtc_status.return_value = []
    uds.read_battery_voltage.return_value = 12.6
    uds.read_engine_rpm.return_value = 0.0
    ECUProgrammingWindow._read_conditions_async(win)
    assert win._wican_wrong_entry is None


@pytest.mark.parametrize(
    "status, blocked",
    [
        ({"state": "failed", "err": {"st": 62}}, True),
        ({"state": "interrupted", "err": {"st": 61}}, True),
        ({"state": "running", "name": "ff_x.ffp"}, True),
        ({"state": "failed", "err": {"st": 42}}, False),
        ({"state": "done"}, False),
        ({}, False),
    ],
)
def test_adapter_record_rearms_the_block_after_an_app_restart(status, blocked):
    """Re-review R5: the adapter's GET /fast_flash (read at connect) carries
    the st 62 verdict across an NC Flash restart."""
    session = SimpleNamespace(transport=SimpleNamespace(host="192.0.2.1"))
    win = SimpleNamespace(
        _session=session,
        _is_wican=lambda: True,
        _wican_status_cache=(session, {"status": status}),
    )
    assert ECUProgrammingWindow._wican_reset_blocked(win) is blocked


def test_caps_retry_backs_off_stops_and_never_runs_during_a_flash(monkeypatch):
    import src.ui.ecu_window as ew

    monkeypatch.setattr(ew, "WICAN_CAPS_RETRY_S", (0.0, 0.0))
    calls = []

    def caps(self):
        calls.append(1)
        return None

    fake = SimpleNamespace(
        _session=SimpleNamespace(transport=SimpleNamespace(host="192.0.2.1")),
        is_busy=False,
    )

    def settle():
        for _ in range(100):
            if "caps" in fake._wican_caps_cache[1]:
                return
            time.sleep(0.02)

    with patch("src.ecu.wican_config.WiCANConfigurator.host_caps", caps):
        ECUProgrammingWindow._wican_fast_flash_caps(fake)
        settle()
        fake.is_busy = True  # a flash runs: no retry
        ECUProgrammingWindow._wican_fast_flash_caps(fake)
        assert len(calls) == 1
        fake.is_busy = False
        for _ in range(5):
            ECUProgrammingWindow._wican_fast_flash_caps(fake)
            settle()
    assert len(calls) == ew.WICAN_CAPS_TRIES
    assert ECUProgrammingWindow._wican_caps_gave_up(fake)


def test_gave_up_caps_with_a_blocked_reset_gets_a_clear_message():
    session = SimpleNamespace(transport=SimpleNamespace(host="192.0.2.1"))
    win = SimpleNamespace(
        _session=session,
        _is_wican=lambda: True,
        _wican_caps_cache=(session, {"caps": None, "tries": 3}),
        _wican_status_cache=(
            session,
            {"status": {"state": "failed", "err": {"st": 62}}},
        ),
        is_busy=False,
    )
    for name in (
        "_wican_fast_flash_caps",
        "_wican_caps_pending",
        "_wican_caps_gave_up",
        "_wican_reset_blocked",
    ):
        setattr(win, name, getattr(ECUProgrammingWindow, name).__get__(win))
    with patch("src.ui.ecu_window.QMessageBox") as box:
        assert ECUProgrammingWindow._wican_still_checking(win) is True
    text = box.warning.call_args.args[2]
    assert "Tactrix or OBDX Pro" in text and "still checking" not in text


def test_reset_offer_after_no_application_prefers_finishing_first():
    """Re-review R1: the reset stays possible (the way to a full flash) but the
    dialog says to finish the flash first."""
    settings = MagicMock()
    settings.get_fast_flash_enabled.return_value = True
    win = SimpleNamespace(
        _reset_programming_ecu=MagicMock(),
        _main_window=SimpleNamespace(settings=settings),
        _wican_reset_blocked=lambda: False,
        _wican_reset_status_unknown=lambda: False,
        _wican_resume_pending=True,
    )
    with patch("src.ui.ecu_window.QMessageBox.question") as ask:
        ECUProgrammingWindow._offer_kernel_reset(win)
    text = ask.call_args.args[2]
    assert "finishing the flash is the better way" in text
    assert "Reset the ECU now?" in text


# --- final review F1 ------------------------------------------------------------


def _kernel_on_wican(holder):
    """A window stand-in: WiCAN, kernel running, the adapter record in ``holder``."""
    win = SimpleNamespace(
        _is_wican=lambda: True,
        _fast_kernel_running=True,
        _wican_status_holder=lambda: holder,
        _reset_programming_ecu=MagicMock(),
        _wican_reset_blocked=lambda: False,
    )
    win._wican_reset_status_unknown = (
        ECUProgrammingWindow._wican_reset_status_unknown.__get__(win)
    )
    return win


@pytest.mark.parametrize(
    "holder, dialog",
    [
        ({}, "information"),  # GET /fast_flash still pending
        ({"status": {}, "status_ok": False}, "warning"),  # could not be read
    ],
)
def test_no_kernel_reset_while_the_adapter_record_is_unknown(holder, dialog):
    """F1: after an st 62 and an app restart the adapter record is the only
    memory of it; while it is unread or unreadable no reset is offered."""
    win = _kernel_on_wican(holder)
    with patch("src.ui.ecu_window.QMessageBox") as box:
        ECUProgrammingWindow._offer_kernel_reset(win)
    assert getattr(box, dialog).called
    box.question.assert_not_called()
    win._reset_programming_ecu.assert_not_called()


def test_a_read_record_with_no_hazard_still_allows_the_reset():
    win = _kernel_on_wican({"status": {"state": "idle"}, "status_ok": True})
    assert win._wican_reset_status_unknown() is False


def test_record_read_failure_is_flagged(monkeypatch):
    from src.ui import ecu_window as ew

    class _Boom:
        def __init__(self, transport):
            pass

        def status(self):
            raise OSError("unreachable")

    monkeypatch.setattr(wff, "WiCANFastFlashClient", _Boom)
    holder: dict = {}
    ew._read_wican_ff_status(object(), holder)
    assert holder["status_ok"] is False and holder["status"] == {}


def test_unknown_record_never_matters_on_a_cable():
    win = _kernel_on_wican({})
    win._is_wican = lambda: False
    assert win._wican_reset_status_unknown() is False
