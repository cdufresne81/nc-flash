"""What the ECU window does once an operation ends.

After a SUCCESSFUL flash (full or dynamic) the user goes to drive the car, so
the window ends the session and does not reconnect, not even on Done: the
adapter is released and, on the WiCAN, the bus reservation is released so the
datalogger logs the drive. A failed flash and a ROM read / RAM scan keep
reconnecting (the read leaves the ECU in a programming session; after a failed
flash the user needs the link to retry or recover).

Runs the real window (offscreen) with a real ``ECUSession`` whose transport
handles are mocks, so the session's own teardown is what gets checked.
"""

import os
import time
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal  # noqa: E402

from src.ecu.session import ECUSession, ECUSessionState  # noqa: E402
from src.ui import ecu_window  # noqa: E402
from src.ui.ecu_window import ECUProgrammingWindow  # noqa: E402


class _Utility(QObject):
    """Stand-in for the main window's trip-log sync / live datalog."""

    running_changed = Signal()

    is_running = False

    def shutdown(self):
        pass


class _MainWindow:
    def __init__(self, adapter: str):
        self.settings = MagicMock()
        self.settings.get_ecu_adapter.return_value = adapter
        self.settings.is_wican_adapter.return_value = adapter == "wican"
        self.settings.get_j2534_dll_path.return_value = ""
        self.wican_log_sync = _Utility()
        self.wican_live_datalog = _Utility()

    def get_current_document(self):
        return None


@pytest.fixture(autouse=True)
def no_dialogs(monkeypatch):
    """Never show a real modal; record what would have been shown."""
    shown = []

    def record(kind):
        def _show(*args, **kwargs):
            shown.append((kind, args))
            return ecu_window.QMessageBox.Ok

        return _show

    for kind in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(ecu_window.QMessageBox, kind, record(kind))
    return shown


@pytest.fixture
def connects(monkeypatch):
    """Count reconnect attempts instead of opening a real adapter."""
    calls = []
    monkeypatch.setattr(
        ECUProgrammingWindow, "_on_connect", lambda self: calls.append(self)
    )
    return calls


def _session(adapter: str, state=ECUSessionState.BUSY):
    """A real session in ``state`` with mocked transport handles."""
    session = ECUSession(adapter_config={"kind": adapter, "host": "192.0.2.1"})
    session._state = state
    session._uds = MagicMock(name="uds")
    if adapter == "wican":
        session._transport = MagicMock(name="transport")
        session._wican_datalog = MagicMock(name="datalog")
    else:
        session._device = MagicMock(name="device")
        session._channel_id = 1
        session._filter_id = 2
    return session


def _window(qtbot, connects, adapter: str, session):
    window = ECUProgrammingWindow(_MainWindow(adapter))
    qtbot.addWidget(window)
    # Let the window's connect-on-open fire, then count from zero.
    qtbot.waitUntil(lambda: len(connects) == 1, timeout=2000)
    connects.clear()
    window._session = session
    session.state_changed.connect(window._on_session_state)
    return window


def _finish(window, operation: str, success: bool, error: str = "", result=None):
    window._session_acquired = True
    window._current_operation = operation
    window._stack.setCurrentIndex(1)
    worker = MagicMock(result=result)
    window._on_flash_finished(success, None, worker, error)


def _assert_session_released(session, adapter: str, mocks):
    assert session.state == ECUSessionState.DISCONNECTED
    if adapter == "wican":
        # Bus reservation released -> the datalogger resumes for the drive.
        mocks["datalog"].release_bus.assert_called_once()
        mocks["transport"].close.assert_called_once()
        assert session.wican_datalog is None
    else:
        mocks["device"].close.assert_called_once()
        assert session.device is None


def _mocks(session):
    return {
        "datalog": session._wican_datalog,
        "transport": session._transport,
        "device": session._device,
    }


@pytest.mark.parametrize("adapter", ["wican", "j2534"])
@pytest.mark.parametrize("operation", ["flash", "dynamic_flash"])
def test_successful_flash_ends_session_and_done_does_not_reconnect(
    qtbot, connects, no_dialogs, adapter, operation
):
    session = _session(adapter)
    mocks = _mocks(session)
    window = _window(qtbot, connects, adapter, session)

    _finish(window, operation, success=True)

    _assert_session_released(session, adapter, mocks)
    assert window._progress_state.text() == "Flash complete"
    assert "unplug or drive" in window._progress_detail.text()
    assert window._conn_label.text() == "Disconnected — flash complete"
    assert not window._btn_done.isHidden()
    assert not window._poll_timer.isActive()

    # Nothing reconnects on its own...
    qtbot.wait(700)
    assert connects == []
    # ...and Done just returns to the actions page.
    window._btn_done.click()
    assert window._stack.currentIndex() == 0
    assert connects == []
    assert session.state == ECUSessionState.DISCONNECTED
    # The Connect button is one click away.
    assert window._btn_connect.isEnabled()

    infos = [a for kind, a in no_dialogs if kind == "information"]
    assert len(infos) == 1
    text = infos[0][2]
    assert "disconnected from the ECU" in text
    assert "Click Done to reconnect" not in text
    assert ("datalogger" in text) == (adapter == "wican")


def test_successful_flash_closes_a_session_the_flash_did_not_release(qtbot, connects):
    """Belt and braces: even if the flash never acquired the session (so its
    release did not run), a successful flash still ends it and frees the
    WiCAN bus."""
    session = _session("wican", state=ECUSessionState.CONNECTED)
    mocks = _mocks(session)
    window = _window(qtbot, connects, "wican", session)

    window._session_acquired = False
    window._current_operation = "flash"
    window._on_flash_finished(True, None, MagicMock(result=None))

    _assert_session_released(session, "wican", mocks)
    window._btn_done.click()
    assert connects == []


def test_successful_flash_never_closes_a_busy_session(qtbot, connects):
    """A session another operation still holds (BUSY) is never torn down."""
    session = _session("wican", state=ECUSessionState.BUSY)
    window = _window(qtbot, connects, "wican", session)

    window._session_acquired = False  # not ours to release
    window._current_operation = "flash"
    window._on_flash_finished(True, None, MagicMock(result=None))

    assert session.state == ECUSessionState.BUSY
    session._wican_datalog.release_bus.assert_not_called()
    session._transport.close.assert_not_called()


@pytest.mark.parametrize("adapter", ["wican", "j2534"])
@pytest.mark.parametrize("operation", ["flash", "dynamic_flash"])
def test_failed_flash_still_reconnects_on_done(qtbot, connects, adapter, operation):
    session = _session(adapter)
    mocks = _mocks(session)
    window = _window(qtbot, connects, adapter, session)

    _finish(window, operation, success=False, error="Block 3 not acknowledged")

    # Unchanged: the session is torn down, the error stays on screen...
    _assert_session_released(session, adapter, mocks)
    assert window._progress_state.text() == "Failed"
    assert "Block 3" in window._progress_detail.text()
    # ...and Done reconnects so the user can retry or recover.
    window._btn_done.click()
    assert len(connects) == 1


def test_identical_rom_still_reconnects_on_done(qtbot, connects):
    session = _session("j2534")
    window = _window(qtbot, connects, "j2534", session)

    _finish(window, "dynamic_flash", success=False, error="ROMs are identical")

    assert window._progress_state.text() == "Nothing to flash"
    window._btn_done.click()
    assert len(connects) == 1


@pytest.mark.parametrize("operation", ["read", "scan_ram"])
def test_read_still_auto_reconnects(qtbot, connects, monkeypatch, operation):
    monkeypatch.setattr(
        ECUProgrammingWindow, "_auto_save_rom", lambda self, *a, **k: None
    )
    monkeypatch.setattr(
        ECUProgrammingWindow, "_auto_save_ram_dump", lambda self, *a, **k: None
    )
    session = _session("j2534")
    window = _window(qtbot, connects, "j2534", session)

    _finish(window, operation, success=True, result=bytearray(16))

    # The ECU is left in a programming session: reconnect on its own.
    qtbot.waitUntil(lambda: len(connects) == 1, timeout=3000)
    assert "Reconnecting" in window._progress_detail.text()
    assert window._reconnect_on_done is True


class _FakeFlashManager:
    """Stands in for the flash driver; the flash itself succeeds."""

    def flash_rom(self, rom_data, progress_cb=None, archive_path=None):
        time.sleep(0.05)

    def dynamic_flash(self, rom_data, archive_path, progress_cb=None):
        time.sleep(0.05)


@pytest.mark.parametrize("operation", ["flash", "dynamic_flash"])
def test_flash_on_a_real_thread_ends_session_without_reconnect(
    qtbot, connects, monkeypatch, operation
):
    """End to end through the real worker thread and queued signals."""
    session = _session("wican", state=ECUSessionState.CONNECTED)
    mocks = _mocks(session)
    window = _window(qtbot, connects, "wican", session)

    def build(self, op, source_name=None):
        self._session.acquire()
        self._session_acquired = True
        return _FakeFlashManager()

    monkeypatch.setattr(ECUProgrammingWindow, "_build_flash_driver", build)
    kwargs = {"rom_data": b"\x00" * 16, "archive_path": "unused.rda"}
    window._start_flash(operation, **kwargs)
    thread = window._flash_thread
    assert thread is not None

    qtbot.waitUntil(lambda: window._flash_thread is None, timeout=5000)
    qtbot.waitUntil(lambda: not thread.isRunning(), timeout=5000)

    _assert_session_released(session, "wican", mocks)
    assert window._progress_state.text() == "Flash complete"
    window._btn_done.click()
    qtbot.wait(700)
    assert connects == []
