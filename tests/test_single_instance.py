"""
Tests for single-instance IPC and command-line file argument handling.
"""

import os
import uuid
import pytest
from unittest.mock import patch, MagicMock
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget
from PySide6.QtNetwork import QLocalServer, QLocalSocket


@pytest.fixture
def ipc_name():
    """Unique server name per test to avoid collisions."""
    name = f"NCFlash_test_{uuid.uuid4().hex[:8]}"
    yield name
    QLocalServer.removeServer(name)


class TestTrySendToRunningInstance:
    """Tests for _try_send_to_running_instance."""

    def test_returns_false_when_no_server(self, ipc_name):
        from main import _try_send_to_running_instance

        assert _try_send_to_running_instance("C:\\fake\\path.bin", ipc_name) is False

    def test_returns_false_when_server_accepts_but_never_acks(self, ipc_name):
        """A hung instance accepts the OS-level connection but its event loop
        never ACKs — the sender must NOT exit on connect alone (B11)."""
        from main import _try_send_to_running_instance

        server = QLocalServer()
        assert server.listen(ipc_name)
        # The server's newConnection slot never runs (no event loop spin here),
        # exactly like a deadlocked GUI thread.
        result = _try_send_to_running_instance("C:\\some\\file.bin", ipc_name)
        assert result is False

        server.close()

    def test_returns_true_when_instance_acks(self):
        from main import _IPC_ACK, _try_send_to_running_instance

        socket = MagicMock()
        socket.waitForConnected.return_value = True
        socket.waitForReadyRead.return_value = True
        socket.readAll.return_value.data.return_value = bytes(_IPC_ACK)
        with patch("main.QLocalSocket", return_value=socket):
            assert _try_send_to_running_instance("C:\\some\\file.bin") is True
        socket.write.assert_called_once()

    def test_returns_false_on_wrong_ack_payload(self):
        from main import _try_send_to_running_instance

        socket = MagicMock()
        socket.waitForConnected.return_value = True
        socket.waitForReadyRead.return_value = True
        socket.readAll.return_value.data.return_value = b"garbage"
        with patch("main.QLocalSocket", return_value=socket):
            assert _try_send_to_running_instance("C:\\some\\file.bin") is False


class _IpcTestWidget(QWidget):
    """Lightweight stand-in for MainWindow — only the IPC server logic."""

    def __init__(self):
        super().__init__()
        self._ipc_server = None
        self._ipc_server_name = None
        self._open_rom_file = MagicMock()

    def start_ipc_server(self, server_name):
        self._ipc_server_name = server_name
        self._ipc_server = QLocalServer(self)
        self._ipc_server.newConnection.connect(self._on_ipc_connection)
        QLocalServer.removeServer(self._ipc_server_name)
        if not self._ipc_server.listen(self._ipc_server_name):
            raise RuntimeError(self._ipc_server.errorString())

    def _on_ipc_connection(self):
        from main import _IPC_ACK

        conn = self._ipc_server.nextPendingConnection()
        if not conn:
            return
        conn.waitForReadyRead(1000)
        data = conn.readAll().data().decode("utf-8").strip()
        # ACK so the sender knows this event loop is alive (B11).
        conn.write(_IPC_ACK)
        conn.flush()
        conn.waitForBytesWritten(500)
        conn.disconnectFromServer()
        if data and os.path.isfile(data):
            self._open_rom_file(data)
        # A second launch always surfaces this window (B11).
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized | Qt.WindowActive)
        self.raise_()
        self.activateWindow()


class TestIpcServer:
    """Tests for IPC server logic (start_ipc_server / _on_ipc_connection).

    Uses a lightweight widget instead of MainWindow to avoid heavy UI init.
    The IPC handler logic is duplicated here from MainWindow.start_ipc_server /
    _on_ipc_connection — if those methods change, these tests should be updated.
    """

    def test_server_starts_and_listens(self, qtbot, ipc_name):
        widget = _IpcTestWidget()
        qtbot.addWidget(widget)
        widget.start_ipc_server(server_name=ipc_name)

        assert widget._ipc_server is not None
        assert widget._ipc_server.isListening()

        widget.close()

    def test_server_receives_file(self, qtbot, sample_rom_path, ipc_name):
        widget = _IpcTestWidget()
        qtbot.addWidget(widget)
        widget.start_ipc_server(server_name=ipc_name)

        socket = QLocalSocket()
        socket.connectToServer(ipc_name)
        assert socket.waitForConnected(1000)

        file_path = str(sample_rom_path)
        socket.write(file_path.encode("utf-8"))
        socket.flush()
        socket.waitForBytesWritten(1000)
        socket.disconnectFromServer()

        qtbot.waitUntil(lambda: widget._open_rom_file.call_count == 1, timeout=2000)
        widget._open_rom_file.assert_called_once_with(file_path)

        widget.close()

    def test_server_ignores_nonexistent_file(self, qtbot, ipc_name):
        widget = _IpcTestWidget()
        qtbot.addWidget(widget)
        widget.start_ipc_server(server_name=ipc_name)

        socket = QLocalSocket()
        socket.connectToServer(ipc_name)
        assert socket.waitForConnected(1000)

        socket.write(b"C:\\nonexistent\\fake.bin")
        socket.flush()
        socket.waitForBytesWritten(1000)
        socket.disconnectFromServer()

        # Give it time to process — should NOT call _open_rom_file
        qtbot.wait(500)
        widget._open_rom_file.assert_not_called()

        widget.close()

    def test_server_ignores_empty_message(self, qtbot, ipc_name):
        widget = _IpcTestWidget()
        qtbot.addWidget(widget)
        widget.start_ipc_server(server_name=ipc_name)

        socket = QLocalSocket()
        socket.connectToServer(ipc_name)
        assert socket.waitForConnected(1000)

        socket.write(b"")
        socket.flush()
        socket.waitForBytesWritten(1000)
        socket.disconnectFromServer()

        qtbot.wait(500)
        widget._open_rom_file.assert_not_called()

        widget.close()

    # NOTE: Full round-trip test (_try_send_to_running_instance → MainWindow)
    # is not feasible in-process because the sender's QLocalSocket gets
    # garbage-collected before the server reads from it. The real scenario
    # (separate processes) was verified manually and works correctly.
    # Individual pieces are covered by TestTrySendToRunningInstance and
    # TestIpcServer above.


class TestOnIpcConnectionRealMethod:
    """Exercise the REAL MainWindow._on_ipc_connection logic via a fake conn (B11)."""

    @staticmethod
    def _fake_self(data):
        from types import SimpleNamespace

        conn = MagicMock()
        conn.readAll.return_value.data.return_value.decode.return_value = data
        server = MagicMock()
        server.nextPendingConnection.return_value = conn
        return SimpleNamespace(
            _ipc_server=server,
            _open_rom_file=MagicMock(),
            setWindowState=MagicMock(),
            windowState=MagicMock(return_value=Qt.WindowNoState),
            raise_=MagicMock(),
            activateWindow=MagicMock(),
        )

    def test_focus_token_surfaces_window_without_opening_a_file(self):
        from main import MainWindow, _IPC_FOCUS_TOKEN

        fake = self._fake_self(_IPC_FOCUS_TOKEN)
        MainWindow._on_ipc_connection(fake)

        fake._open_rom_file.assert_not_called()
        fake.raise_.assert_called_once()
        fake.activateWindow.assert_called_once()

    def test_connection_is_acknowledged(self):
        """The handler must ACK so the sender knows the event loop is alive (B11)."""
        from main import MainWindow, _IPC_ACK, _IPC_FOCUS_TOKEN

        fake = self._fake_self(_IPC_FOCUS_TOKEN)
        MainWindow._on_ipc_connection(fake)

        conn = fake._ipc_server.nextPendingConnection.return_value
        conn.write.assert_called_once_with(_IPC_ACK)

    def test_file_path_opens_and_surfaces_window(self, sample_rom_path):
        from main import MainWindow

        fake = self._fake_self(str(sample_rom_path))
        MainWindow._on_ipc_connection(fake)

        fake._open_rom_file.assert_called_once_with(str(sample_rom_path))
        fake.activateWindow.assert_called_once()
