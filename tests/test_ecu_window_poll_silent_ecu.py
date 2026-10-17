"""Dashboard poll vs a silent ECU (#131).

The poll runs on the UI thread every 5 s. With the ignition off, each read used
to wait out a 60 s budget, so the window froze for minutes on end. Now a failed
read is reported as "No reply from ECU" and two misses in a row disconnect, so
polling stops. An ECU that answers and declines (PID unsupported) is still a
normal reply. Exercised against a duck-typed fake ``self`` (no QApplication).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.ecu.exceptions import NegativeResponseError, UDSTimeoutError
from src.ecu.session import ECUSessionState
from src.ui.ecu_window import POLL_MISSES_BEFORE_DISCONNECT, ECUProgrammingWindow


class _FakeWindow(SimpleNamespace):
    _on_poll_no_reply = ECUProgrammingWindow._on_poll_no_reply
    _on_ecu_answered = ECUProgrammingWindow._on_ecu_answered
    _show_connected_label = ECUProgrammingWindow._show_connected_label
    _ecu_answers = ECUProgrammingWindow._ecu_answers


def _fake(uds):
    session = SimpleNamespace(
        is_connected=True,
        state=ECUSessionState.CONNECTED,
        uds=uds,
        disconnect_ecu=MagicMock(),
    )
    return _FakeWindow(
        _session=session,
        _ecu_busy=False,
        _poll_misses=0,
        _poll_timer=MagicMock(),
        _voltage=12.4,
        _rpm=0.0,
        _disconnect_reason=None,
        _card_battery=MagicMock(),
        _card_engine=MagicMock(),
        _conn_label=MagicMock(),
        _update_action_states=MagicMock(),
        _conditions_pending=False,
        _main_window=SimpleNamespace(wican_log_sync=SimpleNamespace(is_running=False)),
    )


def _poll(fake):
    ECUProgrammingWindow._poll_conditions(fake)


def test_silent_ecu_disconnects_after_two_misses():
    uds = MagicMock()
    uds.read_battery_voltage.side_effect = UDSTimeoutError("no reply")
    fake = _fake(uds)

    _poll(fake)
    assert fake._poll_misses == 1
    fake._session.disconnect_ecu.assert_not_called()
    fake._card_battery.set_subtitle.assert_called_with("No reply from ECU")
    assert fake._voltage is None and fake._rpm is None
    # One failed read skips the other: one timeout per poll, not two.
    uds.read_engine_rpm.assert_not_called()

    _poll(fake)
    assert fake._poll_misses == POLL_MISSES_BEFORE_DISCONNECT
    fake._session.disconnect_ecu.assert_called_once()
    fake._poll_timer.stop.assert_called()
    assert "ignition is ON" in fake._disconnect_reason


def test_reply_resets_the_miss_count():
    uds = MagicMock()
    uds.read_battery_voltage.side_effect = [UDSTimeoutError("no reply"), 12.4]
    uds.read_engine_rpm.return_value = 0.0
    fake = _fake(uds)

    _poll(fake)
    _poll(fake)
    assert fake._poll_misses == 0
    fake._session.disconnect_ecu.assert_not_called()


def test_declined_pid_is_a_normal_reply():
    """The real reads return None for a declined PID; the poll must not count
    that as a miss."""
    from src.ecu.protocol import UDSConnection

    uds = UDSConnection.__new__(UDSConnection)
    uds._transport = MagicMock()
    uds.send_request = MagicMock(side_effect=NegativeResponseError(0x11))
    fake = _fake(uds)

    _poll(fake)
    _poll(fake)
    assert fake._poll_misses == 0
    fake._session.disconnect_ecu.assert_not_called()
    assert fake._voltage is None and fake._rpm is None


def test_poll_uses_strict_reads():
    uds = MagicMock()
    uds.read_battery_voltage.return_value = 12.4
    uds.read_engine_rpm.return_value = 0.0
    _poll(_fake(uds))
    # strict, but never wait_session_exit: the poll runs on the UI thread.
    uds.read_battery_voltage.assert_called_once_with(strict=True)
    uds.read_engine_rpm.assert_called_once_with(strict=True)


def test_header_warns_after_a_miss_and_clears_on_reply():
    uds = MagicMock()
    uds.read_battery_voltage.side_effect = [UDSTimeoutError("no reply"), 12.4]
    uds.read_engine_rpm.return_value = 0.0
    fake = _fake(uds)

    _poll(fake)
    fake._conn_label.setText.assert_called_with("Connected — ECU not answering")
    _poll(fake)
    fake._conn_label.setText.assert_called_with("Connected")


def test_ecu_action_precheck_fails_fast_and_counts_a_miss(monkeypatch):
    """Every ECU action (DTC, read, scan, flash) checks the ECU answers first,
    with the short probe budget, instead of waiting out a 60 s request."""
    from src.ecu.constants import TIMEOUT_PROBE
    from src.ui import ecu_window

    warnings = []
    monkeypatch.setattr(
        ecu_window.QMessageBox, "warning", lambda *a, **k: warnings.append(a)
    )
    uds = MagicMock()
    uds.tester_present.side_effect = UDSTimeoutError("No reply from the ECU")
    fake = _fake(uds)

    assert fake._ecu_answers() is False
    uds.tester_present.assert_called_once_with(timeout_ms=TIMEOUT_PROBE)
    assert fake._poll_misses == 1
    assert warnings and "No reply" in warnings[0][2]


def test_ecu_action_precheck_passes_when_ecu_answers():
    uds = MagicMock()
    fake = _fake(uds)
    fake._poll_misses = 1
    assert fake._ecu_answers() is True
    assert fake._poll_misses == 0


class _ActionFake(SimpleNamespace):
    """Fake window whose ECU check fails: every action must stop right there."""


def _action_fake(answers):
    calls = []
    fake = _ActionFake(
        _session=SimpleNamespace(uds=MagicMock(), is_connected=True),
        _ecu_busy=False,
        _ecu_answers=lambda: calls.append("probe") or answers,
        _check_voltage_warning=lambda *a, **k: calls.append("voltage") or True,
        _check_rpm_gate=lambda: calls.append("rpm") or False,
        _confirm_wican_flash=lambda: True,
        _start_flash=lambda *a, **k: calls.append("start"),
        _update_action_states=MagicMock(),
        _main_window=SimpleNamespace(get_current_document=lambda: None),
        _get_dll_path=lambda: None,
    )
    return fake, calls


def test_failed_precheck_stops_every_ecu_action(monkeypatch):
    """No ECU request after a failed "is it there?" check, and the busy flag
    is released: DTC read/clear, ROM read, RAM scan, both flash buttons."""
    from src.ui import ecu_window

    fm = MagicMock()
    monkeypatch.setattr(ecu_window, "FlashManager", fm)
    monkeypatch.setattr(
        ecu_window.QMessageBox, "question", lambda *a, **k: ecu_window.QMessageBox.Yes
    )
    for name in (
        "_on_read_dtcs",
        "_on_clear_dtcs",
        "_on_read_rom",
        "_on_scan_ram",
        "_on_flash_current",
        "_on_full_flash",
    ):
        fake, calls = _action_fake(answers=False)
        getattr(ECUProgrammingWindow, name)(fake)
        assert calls == ["probe"], name
        assert fake._ecu_busy is False, name
    fm.assert_not_called()


def test_passed_precheck_continues_to_the_flash_checks():
    fake, calls = _action_fake(answers=True)
    ECUProgrammingWindow._on_flash_current(fake)
    assert calls[:3] == ["probe", "voltage", "rpm"]


def test_rpm_gate_read_failure_refuses_the_flash(monkeypatch):
    """enforce_rpm_gate raising FlashError (couldn't read) -> flash not started,
    no override offered."""
    from src.ecu import flash_manager
    from src.ecu.exceptions import FlashError
    from src.ui import ecu_window

    shown = []
    monkeypatch.setattr(
        ecu_window.QMessageBox, "warning", lambda *a, **k: shown.append(a) or None
    )

    def _fail(uds, **_):
        raise FlashError("Could not read the engine RPM")

    monkeypatch.setattr(flash_manager, "enforce_rpm_gate", _fail)
    _no_cursor(monkeypatch)
    fake = SimpleNamespace(_session=SimpleNamespace(uds=MagicMock()))
    assert ECUProgrammingWindow._check_rpm_gate(fake) is False
    assert shown and shown[0][1] == "Flash Not Started"


def _no_cursor(monkeypatch):
    from src.ui import ecu_window

    monkeypatch.setattr(ecu_window.QApplication, "setOverrideCursor", lambda *a: None)
    monkeypatch.setattr(ecu_window.QApplication, "restoreOverrideCursor", lambda: None)


def _gate_fake(monkeypatch, tp_answers, reply):
    """RPM read fails (GuardReadError); Tester Present answers or not; the
    operator's answer to the override prompt is ``reply``."""
    from src.ecu import flash_manager
    from src.ecu.exceptions import GuardReadError
    from src.ui import ecu_window

    def _fail(uds, **_):
        raise GuardReadError("Could not read the engine RPM")

    monkeypatch.setattr(flash_manager, "enforce_rpm_gate", _fail)
    _no_cursor(monkeypatch)
    titles = []
    monkeypatch.setattr(
        ecu_window.QMessageBox,
        "warning",
        lambda *a, **k: titles.append(a[1]) or reply,
    )
    uds = MagicMock()
    if not tp_answers:
        uds.tester_present.side_effect = UDSTimeoutError("no reply")
    fake = _FakeWindow(_session=SimpleNamespace(uds=uds))
    fake._offer_guard_override = lambda exc: ECUProgrammingWindow._offer_guard_override(
        fake, exc
    )
    return fake, titles


def test_override_offered_when_ecu_answers_and_operator_says_yes(monkeypatch):
    from src.ui import ecu_window

    fake, titles = _gate_fake(monkeypatch, True, ecu_window.QMessageBox.Yes)
    assert ECUProgrammingWindow._check_rpm_gate(fake) is True
    assert fake._guard_override is True
    assert titles == ["Safety Checks Could Not Run"]


def test_override_defaults_to_refusal(monkeypatch):
    from src.ui import ecu_window

    fake, titles = _gate_fake(monkeypatch, True, ecu_window.QMessageBox.No)
    assert ECUProgrammingWindow._check_rpm_gate(fake) is False
    assert fake._guard_override is False


def test_no_override_when_the_ecu_is_silent(monkeypatch):
    from src.ui import ecu_window

    fake, titles = _gate_fake(monkeypatch, False, ecu_window.QMessageBox.Yes)
    assert ECUProgrammingWindow._check_rpm_gate(fake) is False
    assert fake._guard_override is False
    assert titles == ["Flash Not Started"]


def test_poll_paused_during_trip_log_download():
    """A loaded WiCAN replying slowly is not a silent ECU: don't count misses."""
    uds = MagicMock()
    uds.read_battery_voltage.side_effect = UDSTimeoutError("slow")
    fake = _fake(uds)
    fake._main_window = SimpleNamespace(wican_log_sync=SimpleNamespace(is_running=True))
    _poll(fake)
    _poll(fake)
    uds.read_battery_voltage.assert_not_called()
    assert fake._poll_misses == 0


def test_skipped_connect_load_reloads_once_ecu_answers():
    uds = MagicMock()
    uds.read_battery_voltage.return_value = 12.4
    uds.read_engine_rpm.return_value = 0.0
    fake = _fake(uds)
    fake._conditions_pending = True
    fake._read_conditions_async = MagicMock()
    _poll(fake)
    fake._read_conditions_async.assert_called_once()


def test_declined_poll_after_a_miss_clears_no_reply_cards():
    uds = MagicMock()
    uds.read_battery_voltage.side_effect = [UDSTimeoutError("no reply"), None]
    uds.read_engine_rpm.return_value = None
    fake = _fake(uds)
    _poll(fake)
    _poll(fake)
    fake._card_battery.set_subtitle.assert_called_with("PID not supported")
    fake._card_engine.set_subtitle.assert_called_with("PID not supported")
