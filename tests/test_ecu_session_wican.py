"""WiCAN-adapter ECUSession lifecycle tests.

Covers the WiCAN connect/disconnect path: open the adapter's fixed coexistence
SLCAN port, reserve the CAN bus from the datalogger for the LIFE of the session
before the first UDS frame, release it on every teardown, and the
acquire()/transport surface the UI uses to drive a flash/read.

The bus reservation is the brick-safety-adjacent invariant here. On the
coexistence port the datalogger is the sole TWAI consumer and swallows the ECU's
UDS replies, so a connect that skips ``acquire_bus`` reports a healthy ECU as
dead; a teardown that skips ``release_bus`` leaves the user's datalogger parked.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.ecu.session import ECUSession, ECUSessionState
from src.ecu.wican_transport import WiCANError

WICAN_CFG = {"kind": "wican", "host": "192.168.1.169"}


@pytest.fixture
def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _make_session(_qapp):
    return ECUSession(adapter_config=dict(WICAN_CFG))


def _fake_transport():
    transport = MagicMock()
    transport.port = 35001
    return transport


class TestWiCANConnect:
    def test_connect_opens_the_coexist_port(self, _qapp):
        transport = _fake_transport()
        with (
            patch("src.ecu.wican_config.WiCANDatalogClient"),
            patch("src.ecu.protocol.UDSConnection") as MockUDS,
            patch.object(
                ECUSession, "_open_coexist_transport", return_value=transport
            ) as mock_open,
        ):
            session = _make_session(_qapp)
            session.connect_ecu()

            assert session.state == ECUSessionState.CONNECTED
            assert session.adapter_kind == "wican"
            assert session.transport is transport
            mock_open.assert_called_once()
            MockUDS.return_value.tester_present.assert_called_once()

    def test_connect_without_a_host_is_refused(self, _qapp):
        session = ECUSession(adapter_config={"kind": "wican"})
        spy = MagicMock()
        session.connection_lost.connect(spy)

        session.connect_ecu()

        assert session.state == ECUSessionState.DISCONNECTED
        spy.assert_called_once()

    def test_connect_failure_reports_and_releases_nothing(self, _qapp):
        """A probe failure must surface, and must not release a bus we never took.

        Calling release_bus() without a matching acquire would resume a
        datalogger this host never parked — and, with a second NC Flash instance
        mid-flash, hand the CAN bus back underneath it.
        """
        with (
            patch("src.ecu.wican_config.WiCANDatalogClient") as MockDatalog,
            patch("src.ecu.protocol.UDSConnection"),
            patch.object(
                ECUSession,
                "_open_coexist_transport",
                side_effect=WiCANError("Nothing is listening on SLCAN port 35001"),
            ),
        ):
            session = _make_session(_qapp)
            spy = MagicMock()
            session.connection_lost.connect(spy)

            session.connect_ecu()

            assert session.state == ECUSessionState.DISCONNECTED
            spy.assert_called_once()
            MockDatalog.return_value.release_bus.assert_not_called()

    def test_connect_reconciles_a_datalogger_left_parked(self, _qapp):
        """A prior run hard-killed mid-flash may have left the logger paused."""
        with (
            patch("src.ecu.wican_config.WiCANDatalogClient") as MockDatalog,
            patch("src.ecu.protocol.UDSConnection"),
            patch.object(
                ECUSession, "_open_coexist_transport", return_value=_fake_transport()
            ),
        ):
            session = _make_session(_qapp)
            session.connect_ecu()

            MockDatalog.return_value.reconcile.assert_called_once()

    def test_reconcile_failure_never_breaks_the_connect(self, _qapp):
        with (
            patch("src.ecu.wican_config.WiCANDatalogClient") as MockDatalog,
            patch("src.ecu.protocol.UDSConnection"),
            patch.object(
                ECUSession, "_open_coexist_transport", return_value=_fake_transport()
            ),
        ):
            MockDatalog.return_value.reconcile.side_effect = OSError("no /datalog")

            session = _make_session(_qapp)
            session.connect_ecu()

            assert session.state == ECUSessionState.CONNECTED


class TestWiCANBusReservation:
    def test_reserves_bus_for_whole_session(self, _qapp):
        """acquire_bus() BEFORE the first UDS frame (else poll_log eats the reply
        and Tester-Present times out), and release_bus() on teardown. Regression
        for the bench connect hang (datalogger stealing the ECU's UDS replies)."""
        transport = _fake_transport()
        calls = []
        with (
            patch("src.ecu.wican_config.WiCANDatalogClient") as MockDatalog,
            patch("src.ecu.protocol.UDSConnection") as MockUDS,
            patch.object(ECUSession, "_open_coexist_transport", return_value=transport),
        ):
            datalog = MockDatalog.return_value
            datalog.acquire_bus.side_effect = lambda: calls.append("acquire_bus")
            datalog.release_bus.side_effect = lambda: calls.append("release_bus")
            MockUDS.return_value.tester_present.side_effect = lambda: calls.append(
                "tester_present"
            )

            session = _make_session(_qapp)
            session.connect_ecu()
            # Reservation raised BEFORE the first UDS frame, and exposed for the
            # flasher (whose own fence nests on this SAME client).
            assert calls == ["acquire_bus", "tester_present"]
            assert session.wican_datalog is datalog

            session.disconnect_ecu()
            assert calls == ["acquire_bus", "tester_present", "release_bus"]
            assert session.wican_datalog is None
            datalog.acquire_bus.assert_called_once()
            datalog.release_bus.assert_called_once()

    def test_drains_stale_datalog_frames_before_first_uds(self, _qapp):
        """After claiming the bus, connect must FLUSH the transport before the
        first UDS frame: acquire_bus() pauses poll_log, but Mode-01 PID responses
        already in flight keep arriving and would be mis-parsed against
        TesterPresent (the benign "unexpected response byte 0x41" warnings).
        Order must be acquire_bus -> flush -> tester_present."""
        transport = _fake_transport()
        calls = []
        with (
            patch("src.ecu.wican_config.WiCANDatalogClient") as MockDatalog,
            patch("src.ecu.protocol.UDSConnection") as MockUDS,
            patch.object(ECUSession, "_open_coexist_transport", return_value=transport),
        ):
            MockDatalog.return_value.acquire_bus.side_effect = lambda: calls.append(
                "acquire_bus"
            )
            transport.flush.side_effect = lambda: calls.append("flush")
            MockUDS.return_value.tester_present.side_effect = lambda: calls.append(
                "tester_present"
            )

            session = _make_session(_qapp)
            session.connect_ecu()

            assert calls == ["acquire_bus", "flush", "tester_present"]
            transport.flush.assert_called_once()

    def test_release_bus_runs_before_the_transport_closes(self, _qapp):
        """Ordering matters: the /datalog resume is an HTTP call, but the park
        state it clears is about the CAN bus the transport still holds. Closing
        first has left the logger parked on the bench."""
        transport = _fake_transport()
        calls = []
        with (
            patch("src.ecu.wican_config.WiCANDatalogClient") as MockDatalog,
            patch("src.ecu.protocol.UDSConnection"),
            patch.object(ECUSession, "_open_coexist_transport", return_value=transport),
        ):
            MockDatalog.return_value.release_bus.side_effect = lambda: calls.append(
                "release_bus"
            )
            transport.close.side_effect = lambda: calls.append("close")

            session = _make_session(_qapp)
            session.connect_ecu()
            session.disconnect_ecu()

            assert calls == ["release_bus", "close"]

    def test_failed_release_still_closes_the_transport(self, _qapp):
        """A dead /datalog endpoint must not strand an open socket. The firmware
        dead-man reaper resumes the logger if this host vanishes."""
        transport = _fake_transport()
        with (
            patch("src.ecu.wican_config.WiCANDatalogClient") as MockDatalog,
            patch("src.ecu.protocol.UDSConnection"),
            patch.object(ECUSession, "_open_coexist_transport", return_value=transport),
        ):
            MockDatalog.return_value.release_bus.side_effect = OSError("unreachable")

            session = _make_session(_qapp)
            session.connect_ecu()
            session.disconnect_ecu()

            assert session.state == ECUSessionState.DISCONNECTED
            transport.close.assert_called_once()


class TestWiCANDisconnect:
    def test_disconnect_closes_the_transport(self, _qapp):
        transport = _fake_transport()
        with (
            patch("src.ecu.wican_config.WiCANDatalogClient"),
            patch("src.ecu.protocol.UDSConnection"),
            patch.object(ECUSession, "_open_coexist_transport", return_value=transport),
        ):
            session = _make_session(_qapp)
            session.connect_ecu()
            session.disconnect_ecu()

            assert session.state == ECUSessionState.DISCONNECTED
            transport.close.assert_called_once()

    def test_disconnect_refused_while_busy(self, _qapp):
        """BRICK GUARD: a flash/read worker owns the transport in BUSY. Closing
        it (and handing the CAN bus back to the datalogger) mid-write is a brick
        risk, so disconnect must refuse rather than tear down under it."""
        transport = _fake_transport()
        with (
            patch("src.ecu.wican_config.WiCANDatalogClient") as MockDatalog,
            patch("src.ecu.protocol.UDSConnection"),
            patch.object(ECUSession, "_open_coexist_transport", return_value=transport),
        ):
            session = _make_session(_qapp)
            session.connect_ecu()
            session.acquire()  # -> BUSY
            session.disconnect_ecu()  # must refuse mid-operation

            assert session.state == ECUSessionState.BUSY
            transport.close.assert_not_called()
            MockDatalog.return_value.release_bus.assert_not_called()

    def test_release_dead_releases_the_bus(self, _qapp):
        """The ECU reset killed the link. Teardown still runs, so the datalogger
        resumes instead of staying parked until the next reconnect."""
        transport = _fake_transport()
        with (
            patch("src.ecu.wican_config.WiCANDatalogClient") as MockDatalog,
            patch("src.ecu.protocol.UDSConnection"),
            patch.object(ECUSession, "_open_coexist_transport", return_value=transport),
        ):
            session = _make_session(_qapp)
            session.connect_ecu()
            session.acquire()
            session.release(connection_dead=True)

            assert session.state == ECUSessionState.DISCONNECTED
            transport.close.assert_called_once()
            MockDatalog.return_value.release_bus.assert_called_once()

    def test_reconnect_after_release_re_reserves_the_bus(self, _qapp):
        transport = _fake_transport()
        with (
            patch("src.ecu.wican_config.WiCANDatalogClient") as MockDatalog,
            patch("src.ecu.protocol.UDSConnection"),
            patch.object(ECUSession, "_open_coexist_transport", return_value=transport),
        ):
            session = _make_session(_qapp)
            session.connect_ecu()
            session.acquire()
            session.release(connection_dead=True)
            session.connect_ecu()  # auto-reconnect reuses the same session

            assert session.state == ECUSessionState.CONNECTED
            assert MockDatalog.return_value.acquire_bus.call_count == 2

    def test_cleanup_releases_the_bus(self, _qapp):
        transport = _fake_transport()
        with (
            patch("src.ecu.wican_config.WiCANDatalogClient") as MockDatalog,
            patch("src.ecu.protocol.UDSConnection"),
            patch.object(ECUSession, "_open_coexist_transport", return_value=transport),
        ):
            session = _make_session(_qapp)
            session.connect_ecu()
            session.cleanup()

            assert session.state == ECUSessionState.DISCONNECTED
            MockDatalog.return_value.release_bus.assert_called_once()

    def test_cleanup_after_release_dead_is_a_no_op(self, _qapp):
        """release(connection_dead=True) already released the bus; cleanup() must
        not fire a second release and resume a logger someone else has parked."""
        transport = _fake_transport()
        with (
            patch("src.ecu.wican_config.WiCANDatalogClient") as MockDatalog,
            patch("src.ecu.protocol.UDSConnection"),
            patch.object(ECUSession, "_open_coexist_transport", return_value=transport),
        ):
            session = _make_session(_qapp)
            session.connect_ecu()
            session.acquire()
            session.release(connection_dead=True)
            MockDatalog.return_value.release_bus.assert_called_once()

            session.cleanup()
            MockDatalog.return_value.release_bus.assert_called_once()


class TestWiCANAcquire:
    def test_acquire_returns_uds_without_device(self, _qapp):
        with (
            patch("src.ecu.wican_config.WiCANDatalogClient"),
            patch("src.ecu.protocol.UDSConnection") as MockUDS,
            patch.object(
                ECUSession, "_open_coexist_transport", return_value=_fake_transport()
            ),
        ):
            session = _make_session(_qapp)
            session.connect_ecu()
            device, channel_id, filter_id, uds = session.acquire()

            assert session.state == ECUSessionState.BUSY
            assert device is None
            assert channel_id is None
            assert filter_id is None
            assert uds is MockUDS.return_value


class TestNoLegacyProtocolSwitchSurvives:
    """The host must never again be able to rewrite a device's stored mode.

    That write is what stranded adapters in #92, and the firmware no longer has
    a mode to switch. These are negative assertions on purpose: they are cheap
    insurance that a merge or a revert cannot quietly bring the path back.
    """

    def test_session_has_no_protocol_restore_machinery(self, _qapp):
        session = _make_session(_qapp)
        for attr in (
            "_restore_wican_protocol",
            "_enter_slcan_durable",
            "_guard_inconclusive_probe",
            "_try_open_coexist_port",
        ):
            assert not hasattr(session, attr), f"{attr} came back"

    def test_configurator_cannot_write_config(self):
        from src.ecu.wican_config import WiCANConfigurator

        cfg = WiCANConfigurator("192.168.1.169")
        for attr in (
            "set_protocol",
            "switch_to_slcan",
            "slcan_session",
            "restore",
            "current_protocol",
            "read_recovery",
            "write_recovery",
            "clear_recovery",
            # The symbol that most directly names the write itself.
            "_post_config",
        ):
            assert not hasattr(cfg, attr), f"{attr} came back"

    def test_configurator_keeps_its_read_only_surface(self):
        """The kept surface is pinned too, not just the removed one.

        ``read_config_raw`` was deleted during this trim and restored only
        because issue #99's audit comment names it as must-stay. Nothing tested
        for its presence, which is exactly how it got deleted -- so pin it.
        """
        from src.ecu.wican_config import WiCANConfigurator

        cfg = WiCANConfigurator("192.168.1.169")
        for attr in ("host_caps", "read_config_raw"):
            assert hasattr(cfg, attr), f"{attr} was removed"
