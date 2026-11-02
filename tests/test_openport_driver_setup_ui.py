"""
Tests for the OpenPort WinUSB switch in the UI: the app-wide
OpenPortDriverSetup owner and the ECU window's offer after a failed Tactrix
connect. No process is started and no dialog is shown.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtWidgets import QMessageBox, QWidget

from src.ecu.openport_setup import OpenPortDriver, OpenPortState
from src.ui import openport_driver_setup as ods
from src.ui.ecu_window import ECUProgrammingWindow
from src.ui.openport_driver_setup import OpenPortDriverSetup

BLOCKED = OpenPortState(OpenPortDriver.BLOCKED, "openport", 39)
WINUSB = OpenPortState(OpenPortDriver.WINUSB, "WinUSB", 0)
TACTRIX = OpenPortState(OpenPortDriver.TACTRIX, "openport", 0)
NO_DRIVER = OpenPortState(OpenPortDriver.NO_DRIVER, None, 28)


@pytest.fixture
def window(qtbot):
    w = QWidget()
    qtbot.addWidget(w)
    return w


@pytest.fixture
def setup(window):
    with patch.object(ods, "builtin_driver_available", return_value=True):
        yield OpenPortDriverSetup(window, blocker=lambda: None)


def _results(setup):
    out = []
    setup.finished.connect(lambda ok, msg, who: out.append((ok, msg, who)))
    return out


class TestOfferFix:
    def test_blocked_cable_is_offered_and_started(self, setup, window):
        with patch.object(ods.QMessageBox, "question", return_value=QMessageBox.Yes):
            with patch.object(setup, "start", return_value=True) as start:
                assert setup.offer_fix(BLOCKED, window, "ecu_window")
        start.assert_called_once_with(ods.INSTALL, "ecu_window")

    def test_offer_says_to_close_other_ecu_software(self, setup, window):
        with patch.object(
            ods.QMessageBox, "question", return_value=QMessageBox.No
        ) as question:
            setup.offer_fix(BLOCKED, window, "ecu_window")
        assert "Close any other program using the cable" in question.call_args.args[2]

    def test_declined_offer_changes_nothing(self, setup, window):
        with patch.object(ods.QMessageBox, "question", return_value=QMessageBox.No):
            with patch.object(setup, "start") as start:
                assert not setup.offer_fix(BLOCKED, window, "ecu_window")
        start.assert_not_called()

    def test_working_cable_is_never_offered(self, setup, window):
        with patch.object(ods.QMessageBox, "question") as question:
            assert not setup.offer_fix(WINUSB, window, "ecu_window")
        question.assert_not_called()

    def test_not_offered_without_the_builtin_driver(self, setup, window):
        with patch.object(ods, "builtin_driver_available", return_value=False):
            with patch.object(ods.QMessageBox, "question") as question:
                assert not setup.offer_fix(BLOCKED, window, "ecu_window")
        question.assert_not_called()


class TestChooseAndStart:
    def _choose(self, setup, window, state, answer=QMessageBox.Yes):
        with patch.object(ods.sys, "platform", "win32"):
            with patch.object(ods, "detect_openport", return_value=state):
                with patch.object(
                    ods.QMessageBox, "question", return_value=answer
                ) as question:
                    with patch.object(setup, "start", return_value=True) as start:
                        setup.choose_and_start(window, "settings:1")
        return question, start

    def test_winusb_cable_offers_restore_defaulting_to_no(self, setup, window):
        question, start = self._choose(setup, window, WINUSB)
        start.assert_called_once_with(ods.RESTORE, "settings:1")
        assert question.call_args.args[-1] == QMessageBox.No

    def test_working_tactrix_driver_defaults_to_no(self, setup, window):
        question, start = self._choose(setup, window, TACTRIX)
        start.assert_called_once_with(ods.INSTALL, "settings:1")
        assert question.call_args.args[-1] == QMessageBox.No

    def test_blocked_cable_defaults_to_yes(self, setup, window):
        question, _ = self._choose(setup, window, BLOCKED)
        assert question.call_args.args[-1] == QMessageBox.Yes

    def test_unplugged_cable_only_informs(self, setup, window):
        state = OpenPortState(OpenPortDriver.NOT_FOUND)
        with patch.object(ods.sys, "platform", "win32"):
            with patch.object(ods, "detect_openport", return_value=state):
                with patch.object(ods.QMessageBox, "information") as info:
                    with patch.object(setup, "start") as start:
                        assert not setup.choose_and_start(window, "settings:1")
        info.assert_called_once()
        start.assert_not_called()

    def test_not_windows(self, setup, window):
        with patch.object(ods.sys, "platform", "linux"):
            with patch.object(ods.QMessageBox, "information") as info:
                assert not setup.choose_and_start(window, "settings:1")
        info.assert_called_once()

    def test_second_click_while_running_says_so(self, setup, window):
        setup._process = MagicMock()
        with patch.object(ods.sys, "platform", "win32"):
            with patch.object(ods.QMessageBox, "information") as info:
                assert not setup.choose_and_start(window, "settings:1")
        assert "already running" in info.call_args.args[2]


class TestStartGuards:
    def test_refused_while_the_ecu_window_holds_the_cable(self, window):
        setup = OpenPortDriverSetup(window, blocker=lambda: "Disconnect first")
        results = _results(setup)
        with patch.object(ods, "QProcess") as qprocess:
            assert not setup.start(ods.INSTALL, "settings:1")
        qprocess.assert_not_called()
        assert results == [(False, "Disconnect first", "settings:1")]
        assert not setup.is_running

    def test_missing_script_reports_without_starting(self, setup):
        results = _results(setup)
        with patch.object(ods, "find_setup_script", return_value=None):
            assert not setup.start(ods.INSTALL, "ecu_window")
        assert results[0][0] is False and not setup.is_running


class TestProcessResult:
    def _finish(self, setup, action, exit_code, result, log_text, after):
        results = _results(setup)
        setup._process = MagicMock()
        setup._action, setup._requester = action, "ecu_window"
        setup._log_path = r"C:\ProgramData\NC Flash\OpenPort\setup_x.log"
        with patch.object(ods, "read_setup_result", return_value=(result, log_text)):
            with patch.object(ods, "detect_openport", return_value=after):
                setup._on_process_finished(exit_code, None)
        assert not setup.is_running
        ok, message, requester = results[0]
        assert requester == "ecu_window"
        return ok, message

    def test_declined_admin_prompt(self, setup):
        ok, message = self._finish(setup, ods.INSTALL, 2, None, "", BLOCKED)
        assert not ok and "declined" in message

    def test_launch_failure_is_not_called_declined(self, setup):
        ok, message = self._finish(setup, ods.INSTALL, 4, None, "", BLOCKED)
        assert not ok and "couldn't start" in message and "declined" not in message

    def test_install_with_cable_unplugged_applies_on_plug_in(self, setup):
        unplugged = OpenPortState(OpenPortDriver.NOT_FOUND)
        ok, message = self._finish(setup, ods.INSTALL, 0, "ok", "RESULT: OK", unplugged)
        assert ok and "when you plug" in message

    def test_restore_leaving_winusb_is_a_failure(self, setup):
        ok, message = self._finish(setup, ods.RESTORE, 0, "ok", "RESULT: OK", WINUSB)
        assert not ok and "still on WinUSB" in message

    def test_another_switch_running(self, setup):
        ok, message = self._finish(setup, ods.INSTALL, 5, None, "", BLOCKED)
        assert not ok and "already running" in message

    def test_install_ok_and_cable_on_winusb(self, setup):
        ok, message = self._finish(setup, ods.INSTALL, 0, "ok", "RESULT: OK", WINUSB)
        assert ok and "NC Flash's driver" in message

    def test_install_reported_ok_but_cable_not_switched(self, setup):
        ok, _ = self._finish(setup, ods.INSTALL, 0, "ok", "RESULT: OK", BLOCKED)
        assert not ok

    def test_restart_needed(self, setup):
        ok, message = self._finish(setup, ods.INSTALL, 3, "restart", "x", TACTRIX)
        assert ok and "Restart Windows" in message

    def test_failure_says_it_may_be_partly_applied(self, setup):
        log = "RESULT: FAILED: pnputil 5"
        ok, message = self._finish(setup, ods.INSTALL, 1, "failed", log, BLOCKED)
        assert not ok and "pnputil 5" in message and "partly applied" in message

    @pytest.mark.parametrize(
        "after, words",
        [
            (TACTRIX, "back on Tactrix's driver."),
            (BLOCKED, "which Windows blocks"),
            (NO_DRIVER, "has no driver now"),
        ],
    )
    def test_restore_reports_where_the_cable_ended_up(self, setup, after, words):
        ok, message = self._finish(setup, ods.RESTORE, 0, "ok", "RESULT: OK", after)
        assert ok and words in message


class TestEcuWindowOffer:
    def _window(self):
        return SimpleNamespace(_openport_fix_offered=False, _openport_setup=MagicMock())

    def test_blocked_cable_offered_once(self):
        win = self._window()
        with patch("src.ui.ecu_window.detect_openport", return_value=BLOCKED):
            ECUProgrammingWindow._offer_openport_fix(win, "op20pt32.dll")
            ECUProgrammingWindow._offer_openport_fix(win, "op20pt32.dll")
        win._openport_setup.offer_fix.assert_called_once_with(
            BLOCKED, win, "ecu_window"
        )

    def test_custom_driver_is_left_alone(self):
        win = self._window()
        with patch("src.ui.ecu_window.detect_openport", return_value=BLOCKED) as detect:
            ECUProgrammingWindow._offer_openport_fix(win, r"C:\Other\pt.dll")
        detect.assert_not_called()
        win._openport_setup.offer_fix.assert_not_called()

    def test_working_cable_not_offered(self):
        win = self._window()
        with patch("src.ui.ecu_window.detect_openport", return_value=WINUSB):
            ECUProgrammingWindow._offer_openport_fix(win, "op20pt32.dll")
        win._openport_setup.offer_fix.assert_not_called()
        assert not win._openport_fix_offered


class TestEcuWindowAfterSwitch:
    def _window(self, busy=False, kind="j2534"):
        return SimpleNamespace(
            is_busy=busy,
            _build_adapter_config=lambda: {"kind": kind},
            _on_connect=MagicMock(),
        )

    def test_reconnects_after_a_successful_switch(self):
        win = self._window()
        with patch.object(QMessageBox, "information"):
            ECUProgrammingWindow._on_openport_setup_done(win, True, "ok", "ecu_window")
        win._on_connect.assert_called_once()

    def test_no_reconnect_while_busy_or_on_wican(self):
        for win in (self._window(busy=True), self._window(kind="wican")):
            with patch.object(QMessageBox, "information"):
                ECUProgrammingWindow._on_openport_setup_done(
                    win, True, "ok", "ecu_window"
                )
            win._on_connect.assert_not_called()

    def test_ignores_results_meant_for_settings(self):
        win = self._window()
        with patch.object(QMessageBox, "information") as info:
            ECUProgrammingWindow._on_openport_setup_done(win, True, "ok", "settings:1")
        info.assert_not_called()
        win._on_connect.assert_not_called()

    def test_holds_ecu_link_while_connected_or_busy(self):
        connected = SimpleNamespace(is_connected=True, state=None)
        idle = SimpleNamespace(is_connected=False, state=None)
        assert ECUProgrammingWindow.holds_ecu_link(
            SimpleNamespace(is_busy=False, _session=connected)
        )
        assert ECUProgrammingWindow.holds_ecu_link(
            SimpleNamespace(is_busy=True, _session=None)
        )
        assert not ECUProgrammingWindow.holds_ecu_link(
            SimpleNamespace(is_busy=False, _session=idle)
        )
