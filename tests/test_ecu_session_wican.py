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


@pytest.fixture(autouse=True)
def _default_no_coexist():
    """Default every test here to NON-coexistence firmware (the legacy
    reboot-switch path), matching all current hardware. The coexist-port probe
    would otherwise fire against the mocked ``create_ecu_transport`` and skew the
    open/switch assertions. The coexist-path tests below re-patch this to return
    a transport, which overrides this default for their duration."""
    with patch.object(ECUSession, "_try_open_coexist_port", return_value=None):
        yield


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


class TestWiCANCoexistConnect:
    """No-reboot dedicated-port path: when the probe finds coexistence firmware,
    connect over that transport WITHOUT the WiCANConfigurator reboot dance."""

    def test_coexist_skips_reboot_and_configurator(self, _qapp):
        coexist_transport = MagicMock()
        coexist_transport.port = 35001
        with (
            patch("src.ecu.wican_config.WiCANConfigurator") as MockCfg,
            patch("src.ecu.wican_config.WiCANDatalogClient"),
            patch("src.ecu.transport.create_ecu_transport") as mock_create,
            patch("src.ecu.protocol.UDSConnection") as MockUDS,
            patch.object(
                ECUSession, "_try_open_coexist_port", return_value=coexist_transport
            ),
        ):
            session = _make_session(_qapp)
            session.connect_ecu()

            assert session.state == ECUSessionState.CONNECTED
            # No protocol switch, no reboot: the configurator is never built.
            MockCfg.assert_not_called()
            # The dedicated-port transport is used as-is (no second open()).
            mock_create.assert_not_called()
            assert session.transport is coexist_transport
            MockUDS.return_value.tester_present.assert_called_once()

    def test_coexist_disconnect_does_not_reboot(self, _qapp):
        coexist_transport = MagicMock()
        coexist_transport.port = 35001
        with (
            patch("src.ecu.wican_config.WiCANConfigurator") as MockCfg,
            patch("src.ecu.wican_config.WiCANDatalogClient"),
            patch("src.ecu.transport.create_ecu_transport"),
            patch("src.ecu.protocol.UDSConnection"),
            patch.object(
                ECUSession, "_try_open_coexist_port", return_value=coexist_transport
            ),
        ):
            session = _make_session(_qapp)
            session.connect_ecu()
            session.disconnect_ecu()

            assert session.state == ECUSessionState.DISCONNECTED
            coexist_transport.close.assert_called_once()
            # Nothing to restore — we never switched a protocol.
            MockCfg.return_value.restore.assert_not_called()

    def test_coexist_reserves_bus_for_whole_session(self, _qapp):
        """The coexist path must hold a bus reservation for the LIFE of the session:
        acquire_bus() BEFORE the first UDS frame (else poll_log eats the reply and
        Tester-Present times out), and release_bus() on teardown. Regression for the
        bench connect hang (datalogger stealing the ECU's UDS replies on port 35001)."""
        coexist_transport = MagicMock()
        coexist_transport.port = 35001
        calls = []
        with (
            patch("src.ecu.wican_config.WiCANConfigurator"),
            patch("src.ecu.wican_config.WiCANDatalogClient") as MockDatalog,
            patch("src.ecu.transport.create_ecu_transport"),
            patch("src.ecu.protocol.UDSConnection") as MockUDS,
            patch.object(
                ECUSession, "_try_open_coexist_port", return_value=coexist_transport
            ),
        ):
            datalog = MockDatalog.return_value
            datalog.acquire_bus.side_effect = lambda: calls.append("acquire_bus")
            datalog.release_bus.side_effect = lambda: calls.append("release_bus")
            MockUDS.return_value.tester_present.side_effect = lambda: calls.append(
                "tester_present"
            )

            session = _make_session(_qapp)
            session.connect_ecu()
            # Reservation raised BEFORE the first UDS frame, and exposed for the flasher.
            assert calls == ["acquire_bus", "tester_present"]
            assert session.wican_datalog is datalog

            session.disconnect_ecu()
            # Released on teardown; the handle is dropped.
            assert calls == ["acquire_bus", "tester_present", "release_bus"]
            assert session.wican_datalog is None
            datalog.acquire_bus.assert_called_once()
            datalog.release_bus.assert_called_once()

    def test_coexist_drains_stale_datalog_frames_before_first_uds(self, _qapp):
        """After claiming the bus, the coexist connect must FLUSH the transport
        before the first UDS frame: acquire_bus() pauses poll_log, but Mode-01 PID
        responses already in flight keep arriving and would be mis-parsed against
        TesterPresent (the benign "unexpected response byte 0x41" warnings). Order
        must be acquire_bus -> flush -> tester_present. Regression for that noise."""
        coexist_transport = MagicMock()
        coexist_transport.port = 35001
        calls = []
        with (
            patch("src.ecu.wican_config.WiCANConfigurator"),
            patch("src.ecu.wican_config.WiCANDatalogClient") as MockDatalog,
            patch("src.ecu.transport.create_ecu_transport"),
            patch("src.ecu.protocol.UDSConnection") as MockUDS,
            patch.object(
                ECUSession, "_try_open_coexist_port", return_value=coexist_transport
            ),
        ):
            MockDatalog.return_value.acquire_bus.side_effect = lambda: calls.append(
                "acquire_bus"
            )
            coexist_transport.flush.side_effect = lambda: calls.append("flush")
            MockUDS.return_value.tester_present.side_effect = lambda: calls.append(
                "tester_present"
            )

            session = _make_session(_qapp)
            session.connect_ecu()

            assert calls == ["acquire_bus", "flush", "tester_present"]
            coexist_transport.flush.assert_called_once()


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
