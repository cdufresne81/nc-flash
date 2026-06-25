"""WiCAN-adapter ECUSession lifecycle tests.

Covers the WiCAN-specific connect/disconnect path: enter SLCAN once per session
(writing the crash-recovery breadcrumb BEFORE switching), restore the original
protocol only on a real disconnect / app exit (never on the internal
auto-reconnect after a read), recover a session that was abandoned without a
restore, and the acquire()/transport surface the UI uses to drive a flash/read.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.ecu.session import ECUSession, ECUSessionState
from src.ecu.wican_transport import WiCANError

WICAN_CFG = {
    "kind": "wican",
    "host": "192.168.1.169",
    "port": 35000,
    "auto_config": True,
}


@pytest.fixture
def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _make_session(_qapp, auto_config=True):
    cfg = dict(WICAN_CFG)
    cfg["auto_config"] = auto_config
    return ECUSession(adapter_config=cfg)


def _cfg(MockCfg, prev="realdash", recorded=None):
    """Configure the mocked WiCANConfigurator instance for a clean connect."""
    inst = MockCfg.return_value
    inst.read_recovery.return_value = recorded
    inst.current_protocol.return_value = prev
    return inst


class TestWiCANConnect:
    def test_connect_switches_and_opens(self, _qapp):
        with (
            patch("src.ecu.wican_config.WiCANConfigurator") as MockCfg,
            patch("src.ecu.transport.create_ecu_transport") as mock_create,
            patch("src.ecu.protocol.UDSConnection") as MockUDS,
        ):
            inst = _cfg(MockCfg, prev="realdash")
            transport = mock_create.return_value

            session = _make_session(_qapp)
            session.connect_ecu()

            assert session.state == ECUSessionState.CONNECTED
            assert session.adapter_kind == "wican"
            # Breadcrumb written BEFORE the switch, then switched to slcan.
            inst.write_recovery.assert_called_once_with("realdash")
            inst.set_protocol.assert_called_once_with("slcan")
            transport.open.assert_called_once()
            MockUDS.return_value.tester_present.assert_called_once()
            assert session.transport is transport

    def test_connect_prefers_recovery_breadcrumb_when_stranded(self, _qapp):
        # Device already in slcan from a prior crashed run, but a breadcrumb
        # records the true original — restore must target that, not "slcan".
        with (
            patch("src.ecu.wican_config.WiCANConfigurator") as MockCfg,
            patch("src.ecu.transport.create_ecu_transport"),
            patch("src.ecu.protocol.UDSConnection"),
        ):
            inst = _cfg(MockCfg, prev="slcan", recorded="poll_log")

            session = _make_session(_qapp)
            session.connect_ecu()
            session.disconnect_ecu()

            # current == slcan -> no re-switch, but the recorded original restores.
            inst.set_protocol.assert_not_called()
            inst.restore.assert_called_once_with("poll_log")

    def test_auto_config_off_skips_switch(self, _qapp):
        with (
            patch("src.ecu.wican_config.WiCANConfigurator") as MockCfg,
            patch("src.ecu.transport.create_ecu_transport") as mock_create,
            patch("src.ecu.protocol.UDSConnection"),
        ):
            session = _make_session(_qapp, auto_config=False)
            session.connect_ecu()

            assert session.state == ECUSessionState.CONNECTED
            MockCfg.assert_not_called()
            mock_create.return_value.open.assert_called_once()

    def test_connect_failure_restores_and_reports(self, _qapp):
        with (
            patch("src.ecu.wican_config.WiCANConfigurator") as MockCfg,
            patch("src.ecu.transport.create_ecu_transport") as mock_create,
            patch("src.ecu.protocol.UDSConnection"),
        ):
            inst = _cfg(MockCfg, prev="realdash")
            mock_create.return_value.open.side_effect = WiCANError("link down")

            session = _make_session(_qapp)
            spy = MagicMock()
            session.connection_lost.connect(spy)

            session.connect_ecu()

            assert session.state == ECUSessionState.DISCONNECTED
            # We switched before the failure, so the protocol must be restored.
            inst.restore.assert_called_once_with("realdash")
            spy.assert_called_once()


class TestWiCANDisconnect:
    def test_disconnect_restores_protocol_and_clears_breadcrumb(self, _qapp):
        with (
            patch("src.ecu.wican_config.WiCANConfigurator") as MockCfg,
            patch("src.ecu.transport.create_ecu_transport") as mock_create,
            patch("src.ecu.protocol.UDSConnection"),
        ):
            inst = _cfg(MockCfg, prev="realdash")
            transport = mock_create.return_value

            session = _make_session(_qapp)
            session.connect_ecu()
            session.disconnect_ecu()

            assert session.state == ECUSessionState.DISCONNECTED
            transport.close.assert_called_once()
            inst.restore.assert_called_once_with("realdash")
            inst.clear_recovery.assert_called_once()

    def test_disconnect_refused_while_busy(self, _qapp):
        with (
            patch("src.ecu.wican_config.WiCANConfigurator") as MockCfg,
            patch("src.ecu.transport.create_ecu_transport") as mock_create,
            patch("src.ecu.protocol.UDSConnection"),
        ):
            inst = _cfg(MockCfg, prev="realdash")
            transport = mock_create.return_value

            session = _make_session(_qapp)
            session.connect_ecu()
            session.acquire()  # -> BUSY
            session.disconnect_ecu()  # must refuse mid-operation

            assert session.state == ECUSessionState.BUSY
            transport.close.assert_not_called()
            inst.restore.assert_not_called()

    def test_release_dead_keeps_slcan(self, _qapp):
        with (
            patch("src.ecu.wican_config.WiCANConfigurator") as MockCfg,
            patch("src.ecu.transport.create_ecu_transport") as mock_create,
            patch("src.ecu.protocol.UDSConnection"),
        ):
            inst = _cfg(MockCfg, prev="realdash")
            transport = mock_create.return_value

            session = _make_session(_qapp)
            session.connect_ecu()
            session.acquire()
            session.release(connection_dead=True)

            assert session.state == ECUSessionState.DISCONNECTED
            transport.close.assert_called_once()
            # The auto-reconnect path must NOT reboot the adapter back.
            inst.restore.assert_not_called()

    def test_reconnect_after_release_skips_second_switch(self, _qapp):
        with (
            patch("src.ecu.wican_config.WiCANConfigurator") as MockCfg,
            patch("src.ecu.transport.create_ecu_transport") as mock_create,
            patch("src.ecu.protocol.UDSConnection"),
        ):
            inst = _cfg(MockCfg, prev="realdash")
            transport = mock_create.return_value

            session = _make_session(_qapp)
            session.connect_ecu()
            session.acquire()
            session.release(connection_dead=True)
            session.connect_ecu()  # auto-reconnect reuses the same session

            assert session.state == ECUSessionState.CONNECTED
            # Switched once total; transport reopened twice.
            inst.set_protocol.assert_called_once_with("slcan")
            assert transport.open.call_count == 2

    def test_cleanup_restores_protocol(self, _qapp):
        with (
            patch("src.ecu.wican_config.WiCANConfigurator") as MockCfg,
            patch("src.ecu.transport.create_ecu_transport"),
            patch("src.ecu.protocol.UDSConnection"),
        ):
            inst = _cfg(MockCfg, prev="realdash")

            session = _make_session(_qapp)
            session.connect_ecu()
            session.cleanup()

            inst.restore.assert_called_once_with("realdash")

    def test_cleanup_restores_after_release_dead_orphan(self, _qapp):
        # release(connection_dead=True) leaves the session DISCONNECTED but still
        # holding the SLCAN switch; cleanup() (e.g. before discarding it in
        # _on_connect) must still restore the original protocol.
        with (
            patch("src.ecu.wican_config.WiCANConfigurator") as MockCfg,
            patch("src.ecu.transport.create_ecu_transport"),
            patch("src.ecu.protocol.UDSConnection"),
        ):
            inst = _cfg(MockCfg, prev="realdash")

            session = _make_session(_qapp)
            session.connect_ecu()
            session.acquire()
            session.release(connection_dead=True)
            inst.restore.assert_not_called()  # not yet

            session.cleanup()
            inst.restore.assert_called_once_with("realdash")


class TestWiCANAcquire:
    def test_acquire_returns_uds_without_device(self, _qapp):
        with (
            patch("src.ecu.wican_config.WiCANConfigurator") as MockCfg,
            patch("src.ecu.transport.create_ecu_transport"),
            patch("src.ecu.protocol.UDSConnection") as MockUDS,
        ):
            _cfg(MockCfg, prev="realdash")

            session = _make_session(_qapp)
            session.connect_ecu()
            device, channel_id, filter_id, uds = session.acquire()

            assert session.state == ECUSessionState.BUSY
            assert device is None
            assert channel_id is None
            assert filter_id is None
            assert uds is MockUDS.return_value
