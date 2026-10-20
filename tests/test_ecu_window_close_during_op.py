"""Closing the ECU window while an operation runs.

A flash must never be aborted by closing the window: FlashManager checks its
abort flag between TransferData blocks, so an abort mid-program leaves a partly
programmed ECU that cannot boot until reflashed. A read may still be aborted,
and the window then joins the worker thread before it goes away.

Per the repo's QThread rule these run the operation on a REAL QThread (the
window's own _start_flash wiring) with a fake driver; a qInstallMessageHandler
guard fails the test if Qt reports a thread destroyed while running.
"""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import qInstallMessageHandler
from PySide6.QtWidgets import QMessageBox

from main import MainWindow
from src.ecu.exceptions import FlashAbortedError
from src.ui import ecu_window as ecu_window_mod
from src.ui.ecu_window import ECUProgrammingWindow


@pytest.fixture(autouse=True)
def no_real_dialogs():
    """No test here may ever show a real (blocking) message box.

    Tests that check a dialog patch it again on top; this backstop answers
    "No" to any other question so a stray prompt can never hang the run.
    """
    with (
        patch.object(QMessageBox, "question", return_value=QMessageBox.No) as q,
        patch.object(QMessageBox, "information"),
        patch.object(QMessageBox, "warning"),
        patch.object(QMessageBox, "critical"),
    ):
        yield
    # Runs after qtbot has closed the test's window: that close must find no
    # operation still running (nothing left to ask about).
    assert not q.called, f"unexpected prompt: {q.call_args}"


@pytest.fixture
def qt_thread_guard():
    """Fail the test if Qt reports a QThread destroyed while running."""
    messages = []

    def handler(mode, ctx, msg):
        messages.append(msg)

    old = qInstallMessageHandler(handler)
    yield
    qInstallMessageHandler(old)
    aborts = [m for m in messages if "Destroyed while thread" in m]
    assert not aborts, f"QThread lifecycle violation: {aborts}"


class _FakeDriver:
    """Stands in for FlashManager: blocks in the operation until released.

    ``honour_abort`` mirrors FlashManager: the abort flag is polled while the
    operation runs and raises FlashAbortedError once seen. A released operation
    ends with an error, so the window takes its quiet failure path (no result
    file written, no completion dialog popping up in a later test).
    """

    def __init__(self, honour_abort=True):
        self.started = threading.Event()
        self.release = threading.Event()
        self.abort_calls = 0
        self._aborted = threading.Event()
        self._honour_abort = honour_abort

    def abort(self):
        self.abort_calls += 1
        self._aborted.set()

    def _run(self):
        self.started.set()
        while not self.release.wait(0.01):
            if self._honour_abort and self._aborted.is_set():
                raise FlashAbortedError("aborted by user")
        raise RuntimeError("fake operation ended")

    def flash_rom(self, rom_data, progress_cb=None, archive_path=None):
        self._run()

    def dynamic_flash(self, rom_data, archive_path, progress_cb=None):
        self._run()

    def read_rom(self, progress_cb=None):
        self._run()

    def scan_ram(self, progress_cb=None):
        self._run()


def _main_window():
    mw = MagicMock()
    mw.wican_log_sync.is_running = False
    mw.settings.is_wican_adapter.return_value = False
    mw.get_current_document.return_value = None
    return mw


@pytest.fixture
def window(qtbot):
    # Not qtbot.addWidget: pytest-qt closes registered widgets BEFORE fixture
    # teardown, i.e. while a test's operation may still run (that close would
    # prompt). This fixture stops the worker first, then closes the window.
    win = ECUProgrammingWindow(_main_window())
    win._session = MagicMock()  # cleanup() must not run under a write
    win._session.adapter_kind = "j2534"
    # dev's closeEvent offers to rejoin the home Wi-Fi; keep it off the host.
    win._ap_switch = MagicMock()
    yield win
    # Never leave a worker behind for the next test. Wait while pumping
    # events so the worker's queued signals are delivered.
    driver = win._current_manager
    if driver is not None:
        driver.release.set()
    thread = win._flash_thread
    if thread is not None:
        qtbot.waitUntil(lambda: not thread.isRunning(), timeout=5000)
    win.close()
    win.deleteLater()


def _start(qtbot, win, operation, driver, **kwargs):
    win._build_flash_driver = lambda op, source_name=None: driver
    win._ecu_busy = True
    win._start_flash(operation, **kwargs)
    assert driver.started.wait(5), "worker never started"
    assert win._flash_thread.isRunning()


@pytest.mark.parametrize("operation", ["flash", "dynamic_flash"])
def test_close_during_flash_is_refused_and_never_aborts(
    qtbot, window, qt_thread_guard, operation
):
    driver = _FakeDriver()
    _start(qtbot, window, operation, driver, rom_data=b"\x00", archive_path="a.bin")
    assert window.write_in_progress
    session = window._session

    with (
        patch.object(QMessageBox, "warning") as warning,
        patch.object(QMessageBox, "question") as question,
    ):
        closed = window.close()

    assert closed is False
    assert driver.abort_calls == 0
    question.assert_not_called()  # no "Close anyway?" for a write
    warning.assert_called_once()
    assert "unbootable" in warning.call_args.args[2]
    # The flash keeps its session and thread.
    session.cleanup.assert_not_called()
    assert window._session is session
    assert window._flash_thread.isRunning()

    # The flash runs to its end on its own, never aborted.
    thread = window._flash_thread
    driver.release.set()
    qtbot.waitUntil(lambda: not thread.isRunning(), timeout=5000)
    qtbot.waitUntil(lambda: not window.write_in_progress, timeout=5000)
    assert driver.abort_calls == 0


@pytest.mark.parametrize("operation", ["read", "scan_ram"])
def test_close_during_read_aborts_and_joins_the_thread(
    qtbot, window, qt_thread_guard, operation
):
    driver = _FakeDriver()
    _start(qtbot, window, operation, driver)
    assert not window.write_in_progress
    session = window._session
    thread = window._flash_thread

    with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
        closed = window.close()

    assert closed is True
    assert driver.abort_calls == 1
    # Joined before the window let go of its session: never destroyed running.
    assert not thread.isRunning()
    assert thread.isFinished()
    session.cleanup.assert_called_once()


def test_close_during_read_answered_no_keeps_running(qtbot, window, qt_thread_guard):
    driver = _FakeDriver()
    _start(qtbot, window, "read", driver)

    with patch.object(QMessageBox, "question", return_value=QMessageBox.No):
        closed = window.close()

    assert closed is False
    assert driver.abort_calls == 0
    assert window._flash_thread.isRunning()


def test_read_that_will_not_stop_refuses_the_close(qtbot, window, qt_thread_guard):
    # A block stuck in its retries does not see the abort in time: the window
    # must stay (with its session) rather than be destroyed with the thread live.
    driver = _FakeDriver(honour_abort=False)
    _start(qtbot, window, "read", driver)
    session = window._session

    with (
        patch.object(ecu_window_mod, "CLOSE_JOIN_MS", 50),
        patch.object(QMessageBox, "question", return_value=QMessageBox.Yes),
        patch.object(QMessageBox, "information") as info,
    ):
        closed = window.close()

    assert closed is False
    assert driver.abort_calls == 1
    info.assert_called_once()
    session.cleanup.assert_not_called()
    assert window._flash_thread.isRunning()


def test_unknown_operation_counts_as_a_write(qtbot, window):
    # Only explicitly read-only operations may be aborted; anything else
    # (a future operation) is protected by default.
    window._flash_thread = SimpleNamespace(isRunning=lambda: True)
    window._current_operation = "write_ram"
    assert window.write_in_progress
    window._current_operation = "read"
    assert not window.write_in_progress
    window._flash_thread = None


# --- app quit path ---------------------------------------------------------


def _fake_main(ecu):
    return SimpleNamespace(
        ecu_window=ecu,
        _handle_close=MagicMock(),
        wican_log_sync=MagicMock(),
        wican_live_datalog=MagicMock(),
        update_controller=MagicMock(),
    )


def _fake_ecu(*, is_busy=True, close_returns=True, running_after_close=False):
    ecu = SimpleNamespace(is_busy=is_busy, operation_running=True)

    def close():
        ecu.operation_running = running_after_close
        return close_returns

    ecu.close = MagicMock(side_effect=close)
    return ecu


def test_app_close_refused_while_ecu_window_refuses():
    ecu = _fake_ecu(close_returns=False, running_after_close=True)
    fake = _fake_main(ecu)
    event = MagicMock()

    MainWindow.closeEvent(fake, event)

    ecu.close.assert_called_once()
    event.ignore.assert_called_once()
    fake._handle_close.assert_not_called()
    fake.wican_log_sync.shutdown.assert_not_called()


def test_app_close_refused_when_close_reports_true_but_worker_runs():
    # Qt returns True from close() when it arrives while the window's own
    # close is still in progress (its refusal dialog open), even though that
    # close is then refused. The running worker must still veto the quit.
    ecu = _fake_ecu(close_returns=True, running_after_close=True)
    fake = _fake_main(ecu)
    event = MagicMock()

    MainWindow.closeEvent(fake, event)

    event.ignore.assert_called_once()
    fake._handle_close.assert_not_called()


def test_app_close_proceeds_once_ecu_window_agrees():
    ecu = _fake_ecu(close_returns=True, running_after_close=False)
    fake = _fake_main(ecu)
    event = MagicMock()

    MainWindow.closeEvent(fake, event)

    ecu.close.assert_called_once()
    fake._handle_close.assert_called_once_with(event)


def test_app_close_leaves_an_idle_ecu_window_alone():
    ecu = _fake_ecu(is_busy=False)
    fake = _fake_main(ecu)
    event = MagicMock()

    MainWindow.closeEvent(fake, event)

    ecu.close.assert_not_called()
    fake._handle_close.assert_called_once_with(event)
