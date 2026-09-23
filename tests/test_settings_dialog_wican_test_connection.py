"""Tests for the rewritten Settings > WiCAN > Test Connection flow.

The old button switched the adapter's protocol, rebooted it, connected to port
35000, and failed after ~45 s. The firmware now has one mode on one fixed port,
so the button probes that port and grades the ``NCFRv<rev>`` firmware marker
instead.

Two properties are worth protecting beyond "it prints the right string":

* **It must never take the CAN bus.** ``version_ping`` is answered by the
  firmware without touching CAN, so the probe holds no bus reservation. If a
  refactor ever added a UDS exchange (or a link-quality sweep) here, pressing a
  Settings button mid-drive would park the user's datalog trip — and would
  report a false "ECU dead" for a car whose ignition is simply off.
* **It must not block the GUI thread**, and the worker thread must be joined,
  never garbage-collected while running.

The flow is decomposed the same way the mDNS Scan is, so the parts that matter
are unit-testable without real threading.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.ecu.constants import COEXIST_MIN_FW_REV, WICAN_DEDICATED_SLCAN_PORT
from src.ui.settings_dialog import (
    SETTINGS_REGISTRY,
    SettingsDialog,
    _grade_wican_test,
    _WiCANTestWorker,
)

HOST = "192.168.1.169"


@pytest.fixture(autouse=True)
def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _refused_error():
    err = OSError("connection refused")
    err.__cause__ = ConnectionRefusedError(111, "Connection refused")
    return err


# ---------------------------------------------------------------------------
# The pure grader — every outcome, no Qt and no sockets.
# ---------------------------------------------------------------------------


class TestGradeWiCANTest:
    def test_supported_firmware_is_ok(self):
        """Graded at exactly COEXIST_MIN_FW_REV — the boundary is the contract
        both sides commit to, so an off-by-one would reject every device on the
        minimum build."""
        kind, title, text = _grade_wican_test(HOST, COEXIST_MIN_FW_REV, 42.0, None)
        assert kind == "ok"
        assert title == "Connection OK"
        assert HOST in text
        assert str(WICAN_DEDICATED_SLCAN_PORT) in text
        assert f"NCFRv{COEXIST_MIN_FW_REV}" in text
        assert "42 ms" in text

    def test_success_says_the_ecu_was_not_contacted(self):
        """The message must not let the user read this as an ECU health check —
        that is exactly the false confidence the old link-quality wording gave."""
        _kind, _title, text = _grade_wican_test(HOST, COEXIST_MIN_FW_REV, 10.0, None)
        assert "ECU" in text and "ECU Programming window" in text

    def test_rev_one_below_the_threshold_fails(self):
        kind, title, text = _grade_wican_test(HOST, COEXIST_MIN_FW_REV - 1, 1.0, None)
        assert kind == "warn"
        assert title == "Firmware Too Old"
        # Names what it found AND what is needed, or the user cannot tell
        # whether their OTA took.
        assert f"NCFRv{COEXIST_MIN_FW_REV - 1}" in text
        assert f"NCFRv{COEXIST_MIN_FW_REV}" in text

    def test_no_marker_is_a_retryable_warning(self):
        kind, title, text = _grade_wican_test(HOST, None, 0.0, None)
        assert kind == "warn"
        assert title == "Unexpected Response"
        assert "try again" in text

    def test_refused_blames_the_firmware(self):
        kind, title, text = _grade_wican_test(HOST, None, 0.0, _refused_error())
        assert kind == "warn"
        assert title == "Connection Refused"
        assert "stock firmware" in text
        assert f"NCFRv{COEXIST_MIN_FW_REV}" in text

    def test_unreachable_blames_the_network(self):
        kind, title, text = _grade_wican_test(HOST, None, 0.0, OSError("timed out"))
        assert kind == "warn"
        assert title == "Connection Failed"
        assert "timed out" in text
        assert "network" in text
        # Must NOT send someone with a powered-off adapter off to reflash it.
        assert "stock firmware" not in text

    def test_an_error_wins_over_a_marker(self):
        """Defensive: a rev alongside an error must not be graded as success."""
        kind, _t, _x = _grade_wican_test(HOST, COEXIST_MIN_FW_REV, 5.0, OSError("boom"))
        assert kind == "warn"


# ---------------------------------------------------------------------------
# The off-thread worker body.
# ---------------------------------------------------------------------------


class TestWiCANTestWorker:
    def test_probes_the_fixed_port_and_returns_the_rev(self):
        transport = MagicMock()
        transport.version_ping.return_value = b"NCFRv%d" % COEXIST_MIN_FW_REV
        with patch(
            "src.ecu.transport.create_ecu_transport", return_value=transport
        ) as mock_create:
            worker = _WiCANTestWorker(HOST)
            got = []
            worker.finished.connect(got.append)
            worker.run()

        rev, elapsed_ms, error = got[0]
        assert rev == COEXIST_MIN_FW_REV
        assert error is None
        assert elapsed_ms >= 0
        cfg = mock_create.call_args.args[0]
        assert cfg["host"] == HOST
        assert cfg["port"] == WICAN_DEDICATED_SLCAN_PORT
        transport.close.assert_called_once()

    def test_opens_the_socket_only_never_the_can_channel(self):
        """THE hazard check.

        ``open()`` sends the SLCAN ``C``/``S6``/``O`` bring-up and a prime
        TesterPresent frame; on this firmware ``C`` disables the SHARED CAN
        peripheral, so a probe that used it would punch a ~10 s hole in a
        running datalog trip. Only ``open_socket_only()`` is safe here.

        This asserts the CALL, which is all a mock can honestly prove. The bytes
        that actually reach the wire are fenced in
        ``tests/test_ecu_wican_probe_wire.py`` against a real socket — an
        earlier version of this file asserted on mocks alone and passed with the
        bug fully present.
        """
        transport = MagicMock()
        transport.version_ping.return_value = b"NCFRv%d" % COEXIST_MIN_FW_REV
        with patch("src.ecu.transport.create_ecu_transport", return_value=transport):
            _WiCANTestWorker(HOST).run()

        transport.open_socket_only.assert_called_once()
        transport.open.assert_not_called()

    def test_never_reserves_the_can_bus(self):
        """No reservation is needed BECAUSE the probe never touches the bus. If
        one ever appears here it means the probe started needing CAN — which is
        the thing that must not happen."""
        transport = MagicMock()
        transport.version_ping.return_value = b"NCFRv%d" % COEXIST_MIN_FW_REV
        with (
            patch("src.ecu.transport.create_ecu_transport", return_value=transport),
            patch("src.ecu.wican_config.WiCANDatalogClient") as MockDatalog,
        ):
            _WiCANTestWorker(HOST).run()

        MockDatalog.assert_not_called()

    def test_never_opens_a_uds_session(self):
        """No UDS here: without a bus reservation the datalogger eats the reply,
        so any UDS exchange would report a healthy ECU as dead."""
        transport = MagicMock()
        transport.version_ping.return_value = b"NCFRv%d" % COEXIST_MIN_FW_REV
        with (
            patch("src.ecu.transport.create_ecu_transport", return_value=transport),
            patch("src.ecu.protocol.UDSConnection") as MockUDS,
        ):
            _WiCANTestWorker(HOST).run()

        MockUDS.assert_not_called()
        transport.send_message.assert_not_called()

    def test_silence_earns_one_retry(self):
        transport = MagicMock()
        transport.version_ping.return_value = None
        with patch("src.ecu.transport.create_ecu_transport", return_value=transport):
            worker = _WiCANTestWorker(HOST)
            got = []
            worker.finished.connect(got.append)
            worker.run()

        assert transport.version_ping.call_count == 2
        assert got[0][0] is None

    def test_open_failure_is_reported_not_raised(self):
        transport = MagicMock()
        transport.open_socket_only.side_effect = OSError("no route to host")
        with patch("src.ecu.transport.create_ecu_transport", return_value=transport):
            worker = _WiCANTestWorker(HOST)
            got = []
            worker.finished.connect(got.append)
            worker.run()  # must not raise on a worker thread

        rev, _elapsed, error = got[0]
        assert rev is None
        assert isinstance(error, OSError)
        transport.close.assert_called_once()  # no leaked socket

    def test_a_failing_close_is_swallowed(self):
        transport = MagicMock()
        transport.version_ping.return_value = b"NCFRv%d" % COEXIST_MIN_FW_REV
        transport.close.side_effect = OSError("already gone")
        with patch("src.ecu.transport.create_ecu_transport", return_value=transport):
            worker = _WiCANTestWorker(HOST)
            got = []
            worker.finished.connect(got.append)
            worker.run()

        assert got[0][0] == COEXIST_MIN_FW_REV


# ---------------------------------------------------------------------------
# GUI-thread slots.
# ---------------------------------------------------------------------------


class TestTestConnectionSlots:
    """Slots are exercised against a SimpleNamespace stand-in for the dialog.

    Same approach as the mDNS-scan slot tests: constructing a real
    ``SettingsDialog`` builds the whole settings tree, which none of these
    behaviours depend on.
    """

    def _self(self, **over):
        base = dict(
            _test_cancelled=False,
            _test_thread=MagicMock(),  # an active probe
            _test_worker=MagicMock(),
            _test_timer=MagicMock(),
            _test_progress=MagicMock(),
            _test_host=HOST,
            _test_timeout_s=10.0,
            _teardown_test=MagicMock(),
            _cleanup_thread=SettingsDialog._cleanup_thread,
        )
        base.update(over)
        return SimpleNamespace(**base)

    def test_finished_reports_success(self):
        fake = self._self()
        with (
            patch("PySide6.QtWidgets.QMessageBox.information") as info,
            patch("PySide6.QtWidgets.QMessageBox.warning") as warn,
        ):
            SettingsDialog._on_test_finished(fake, (COEXIST_MIN_FW_REV, 12.0, None))

        info.assert_called_once()
        warn.assert_not_called()

    def test_finished_reports_failure_as_a_warning(self):
        fake = self._self()
        with (
            patch("PySide6.QtWidgets.QMessageBox.information") as info,
            patch("PySide6.QtWidgets.QMessageBox.warning") as warn,
        ):
            SettingsDialog._on_test_finished(fake, (None, 0.0, _refused_error()))

        warn.assert_called_once()
        info.assert_not_called()

    def test_finished_is_silent_when_cancelled(self):
        fake = self._self(_test_cancelled=True)
        with (
            patch("PySide6.QtWidgets.QMessageBox.information") as info,
            patch("PySide6.QtWidgets.QMessageBox.warning") as warn,
        ):
            SettingsDialog._on_test_finished(fake, (COEXIST_MIN_FW_REV, 12.0, None))

        info.assert_not_called()
        warn.assert_not_called()

    def test_finished_drops_a_stale_signal(self):
        """A late delivery from an already-torn-down worker must not pop a dialog
        over whatever the user is doing now."""
        fake = self._self(_test_thread=None)
        with (
            patch("PySide6.QtWidgets.QMessageBox.information") as info,
            patch("PySide6.QtWidgets.QMessageBox.warning") as warn,
        ):
            SettingsDialog._on_test_finished(fake, (COEXIST_MIN_FW_REV, 12.0, None))

        info.assert_not_called()
        warn.assert_not_called()

    def test_cancel_stops_the_ticker(self):
        fake = self._self()

        SettingsDialog._on_test_cancel(fake)

        assert fake._test_cancelled is True
        fake._test_timer.stop.assert_called_once()

    def test_tick_is_skipped_after_cancel(self):
        fake = self._self(_test_cancelled=True)

        SettingsDialog._on_test_tick(fake)

        fake._test_progress.setValue.assert_not_called()

    def test_tick_caps_below_the_maximum(self):
        """The bar must never reach its maximum on its own: an auto-closed dialog
        would look like a finished test that never reported anything."""
        progress = MagicMock()
        progress.value.return_value = 99
        progress.maximum.return_value = 100
        fake = self._self(_test_progress=progress)

        SettingsDialog._on_test_tick(fake)

        progress.setValue.assert_called_once_with(99)

    def test_tick_is_a_noop_without_a_progress_dialog(self):
        fake = self._self(_test_progress=None)
        SettingsDialog._on_test_tick(fake)  # must not raise

    def test_teardown_resets_state_and_joins_the_thread(self):
        thread = MagicMock()
        thread.isRunning.return_value = True
        fake = self._self(_test_thread=thread, _teardown_test=None)

        SettingsDialog._teardown_test(fake, blocking=True)

        # Joined, not garbage-collected mid-run (PySide6 would destroy the C++
        # thread underneath a running probe).
        thread.quit.assert_called_once()
        thread.wait.assert_called_once()
        assert fake._test_thread is None
        assert fake._test_worker is None
        assert fake._test_progress is None
        assert fake._test_timer is None

    def test_reentrancy_guard_blocks_a_second_probe(self):
        fake = self._self()
        fake._widgets = {}
        with patch("src.ui.settings_dialog._WiCANTestWorker") as MockWorker:
            SettingsDialog._test_wican_connection(fake)
        MockWorker.assert_not_called()


# ---------------------------------------------------------------------------
# The settings surface itself.
# ---------------------------------------------------------------------------


class TestWiCANSettingsSurface:
    def test_port_and_auto_config_rows_are_gone(self):
        keys = {d.key for d in SETTINGS_REGISTRY}
        assert "ecu.wican.port" not in keys
        assert "ecu.wican.auto_config" not in keys

    def test_test_connection_row_survives(self):
        keys = {d.key for d in SETTINGS_REGISTRY}
        assert "ecu.wican.test_connection" in keys

    def test_test_connection_no_longer_promises_a_link_check(self):
        (desc,) = [d for d in SETTINGS_REGISTRY if d.key == "ecu.wican.test_connection"]
        assert "firmware" in desc.description.lower()
        assert "datalog" in desc.description.lower()
