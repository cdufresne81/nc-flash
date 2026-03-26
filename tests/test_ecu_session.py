"""
ECU Session Lifecycle Tests

Tests ECUSession connect/disconnect/acquire/release with mocked
J2534Device and UDSConnection.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.ecu.session import ECUSession, ECUSessionState
from src.ecu.exceptions import J2534DeviceNotFound


@pytest.fixture
def _qapp():
    """Ensure QApplication exists for signal testing."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    return app


@pytest.fixture
def session(_qapp):
    """ECUSession with a dummy DLL path."""
    return ECUSession("dummy.dll")


# ---------------------------------------------------------------------------
# Connect
# ---------------------------------------------------------------------------


class TestSessionConnect:
    @patch("src.ecu.protocol.UDSConnection")
    @patch("src.ecu.j2534.setup_isotp_flow_control", return_value=100)
    @patch("src.ecu.j2534.J2534Device")
    def test_connect_happy_path(self, MockDevice, mock_setup_fc, MockUDS, session):
        mock_dev = MockDevice.return_value
        mock_dev.connect.return_value = 1

        session.connect_ecu()

        assert session.state == ECUSessionState.CONNECTED
        assert session.device is mock_dev
        assert session.channel_id == 1
        assert session.filter_id == 100

    @patch("src.ecu.j2534.J2534Device")
    def test_connect_device_failure(self, MockDevice, session):
        MockDevice.return_value.open.side_effect = J2534DeviceNotFound("no device")

        spy = MagicMock()
        session.connection_lost.connect(spy)

        session.connect_ecu()

        assert session.state == ECUSessionState.DISCONNECTED
        spy.assert_called_once()
        assert "no device" in spy.call_args[0][0]

    @patch("src.ecu.protocol.UDSConnection")
    @patch("src.ecu.j2534.setup_isotp_flow_control", return_value=100)
    @patch("src.ecu.j2534.J2534Device")
    def test_connect_when_connected_noop(
        self, MockDevice, mock_setup_fc, MockUDS, session
    ):
        mock_dev = MockDevice.return_value
        mock_dev.connect.return_value = 1

        session.connect_ecu()
        session.connect_ecu()  # Second call

        # open() only called once
        mock_dev.open.assert_called_once()


# ---------------------------------------------------------------------------
# Disconnect
# ---------------------------------------------------------------------------


class TestSessionDisconnect:
    @patch("src.ecu.protocol.UDSConnection")
    @patch("src.ecu.j2534.setup_isotp_flow_control", return_value=100)
    @patch("src.ecu.j2534.J2534Device")
    def test_disconnect_cleans_resources(
        self, MockDevice, mock_setup_fc, MockUDS, session
    ):
        mock_dev = MockDevice.return_value
        mock_dev.connect.return_value = 1
        session.connect_ecu()

        session.disconnect_ecu()

        assert session.state == ECUSessionState.DISCONNECTED
        mock_dev.stop_msg_filter.assert_called()
        mock_dev.disconnect.assert_called()
        mock_dev.close.assert_called()

    def test_disconnect_when_disconnected_noop(self, session):
        session.disconnect_ecu()  # Should not raise
        assert session.state == ECUSessionState.DISCONNECTED

    @patch("src.ecu.protocol.UDSConnection")
    @patch("src.ecu.j2534.setup_isotp_flow_control", return_value=100)
    @patch("src.ecu.j2534.J2534Device")
    def test_disconnect_tolerates_errors(
        self, MockDevice, mock_setup_fc, MockUDS, session
    ):
        mock_dev = MockDevice.return_value
        mock_dev.connect.return_value = 1
        mock_dev.close.side_effect = Exception("device error")
        session.connect_ecu()

        session.disconnect_ecu()  # Should not raise despite error
        assert session.state == ECUSessionState.DISCONNECTED


# ---------------------------------------------------------------------------
# Acquire / Release
# ---------------------------------------------------------------------------


class TestSessionAcquireRelease:
    @patch("src.ecu.protocol.UDSConnection")
    @patch("src.ecu.j2534.setup_isotp_flow_control", return_value=100)
    @patch("src.ecu.j2534.J2534Device")
    def test_acquire_returns_tuple(self, MockDevice, mock_setup_fc, MockUDS, session):
        mock_dev = MockDevice.return_value
        mock_dev.connect.return_value = 1
        session.connect_ecu()

        result = session.acquire()

        assert len(result) == 4
        assert result[0] is mock_dev
        assert result[1] == 1  # channel_id
        assert result[2] == 100  # filter_id

    @patch("src.ecu.protocol.UDSConnection")
    @patch("src.ecu.j2534.setup_isotp_flow_control", return_value=100)
    @patch("src.ecu.j2534.J2534Device")
    def test_acquire_sets_busy(self, MockDevice, mock_setup_fc, MockUDS, session):
        mock_dev = MockDevice.return_value
        mock_dev.connect.return_value = 1
        session.connect_ecu()

        session.acquire()
        assert session.state == ECUSessionState.BUSY

    def test_acquire_when_not_connected_raises(self, session):
        with pytest.raises(RuntimeError, match="Cannot acquire"):
            session.acquire()

    @patch("src.ecu.protocol.UDSConnection")
    @patch("src.ecu.j2534.setup_isotp_flow_control", return_value=100)
    @patch("src.ecu.j2534.J2534Device")
    def test_release_returns_to_connected(
        self, MockDevice, mock_setup_fc, MockUDS, session
    ):
        mock_dev = MockDevice.return_value
        mock_dev.connect.return_value = 1
        session.connect_ecu()
        session.acquire()

        session.release()
        assert session.state == ECUSessionState.CONNECTED

    @patch("src.ecu.protocol.UDSConnection")
    @patch("src.ecu.j2534.setup_isotp_flow_control", return_value=100)
    @patch("src.ecu.j2534.J2534Device")
    def test_release_dead_tears_down(self, MockDevice, mock_setup_fc, MockUDS, session):
        mock_dev = MockDevice.return_value
        mock_dev.connect.return_value = 1
        session.connect_ecu()
        session.acquire()

        session.release(connection_dead=True)

        assert session.state == ECUSessionState.DISCONNECTED
        mock_dev.close.assert_called()
