"""A silent ECU (ignition off) must fail the "is it there?" probe in seconds,
not after the 60 s response-pending budget (#131), and the strict pre-flash
reads must only count a refusal of THEIR request as "declined" (#130). Runs the
real UDS response loop over fake transports."""

import time
from unittest.mock import MagicMock

import pytest

from src.ecu.constants import SID_TESTER_PRESENT, TESTER_PRESENT_SUB, TIMEOUT_PROBE
from src.ecu.exceptions import FlashError, UDSError, UDSTimeoutError
from src.ecu.protocol import UDSConnection


class _SilentTransport:
    """Every receive waits its full timeout and returns nothing."""

    def __init__(self):
        self.sent = []
        self.receive_timeouts = []

    def send_message(self, data, timeout_ms):
        self.sent.append(bytes(data))

    def receive_message(self, timeout_ms):
        self.receive_timeouts.append(timeout_ms)
        time.sleep(timeout_ms / 1000.0)
        return None


class _AnsweringTransport(_SilentTransport):
    def receive_message(self, timeout_ms):
        self.receive_timeouts.append(timeout_ms)
        return bytes([0x7E, TESTER_PRESENT_SUB])  # positive Tester Present reply


class _ScriptedTransport(_SilentTransport):
    """Returns the scripted replies in order; an Exception entry is raised."""

    def __init__(self, replies):
        super().__init__()
        self._replies = list(replies)

    def receive_message(self, timeout_ms):
        self.receive_timeouts.append(timeout_ms)
        r = self._replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


# --- #131: probe budget -----------------------------------------------------


def test_probe_gives_up_after_its_own_budget():
    t = _SilentTransport()
    start = time.monotonic()
    with pytest.raises(UDSTimeoutError, match="ignition is ON"):
        UDSConnection(t).tester_present(timeout_ms=200)
    assert time.monotonic() - start < 1.0  # was 60 s on the pending budget
    assert t.sent == [bytes([SID_TESTER_PRESENT, TESTER_PRESENT_SUB])]
    assert t.receive_timeouts == [200]


def test_probe_succeeds_when_ecu_answers():
    t = _AnsweringTransport()
    UDSConnection(t).tester_present(timeout_ms=200)
    assert t.receive_timeouts == [200]


def test_keepalive_default_keeps_normal_timeout():
    """Keep-alives during an operation keep the long budgets unchanged."""
    from src.ecu.constants import TIMEOUT_DEFAULT

    t = _AnsweringTransport()
    UDSConnection(t).tester_present()
    assert t.receive_timeouts == [TIMEOUT_DEFAULT]


def test_corrupt_frame_does_not_get_the_ignition_hint():
    """A garbled frame is a link problem, not a silent ECU."""
    from src.ecu.wican_transport import WiCANError

    t = _ScriptedTransport([WiCANError("Invalid SLCAN DLC 't'")])
    with pytest.raises(UDSTimeoutError) as exc:
        UDSConnection(t).tester_present(timeout_ms=200)
    assert "ignition" not in str(exc.value)


def test_borrowed_session_check_uses_the_probe_budget():
    """The check at the start of every flash/read on an open session."""
    from src.ecu.flash_manager import FlashManager

    uds = MagicMock()
    fm = FlashManager()
    fm.use_uds(uds)
    fm._connect(None)
    uds.tester_present.assert_called_once_with(timeout_ms=TIMEOUT_PROBE)


def test_silent_borrowed_session_fails_the_flash_fast():
    from src.ecu.flash_manager import FlashManager

    uds = UDSConnection(_SilentTransport())
    fm = FlashManager()
    fm.use_uds(uds)
    start = time.monotonic()
    with pytest.raises(FlashError, match="not responsive"):
        fm._connect(None)
    assert time.monotonic() - start < TIMEOUT_PROBE / 1000.0 + 2.0


# --- #130: only a refusal of the OBD request is a "decline" -------------------

RPM_3000 = bytes([0x41, 0x0C, 0x2E, 0xE0])  # 0x2EE0 / 4 = 3000 RPM


def test_foreign_sid_refusal_is_not_a_decline():
    """A stale 0x7F for another request (here Tester Present) must not make the
    RPM gate wave the flash through while the engine runs (review finding)."""
    from src.ecu.flash_manager import enforce_rpm_gate

    uds = UDSConnection(_ScriptedTransport([bytes([0x7F, 0x3E, 0x12]), RPM_3000]))
    with pytest.raises(FlashError, match="Could not read the engine RPM"):
        enforce_rpm_gate(uds)


def test_obd_refusal_is_a_decline():
    """The bootloader's NRC 0x11 to SID 0x01: proceed (recovery re-flash)."""
    from src.ecu.flash_manager import enforce_rpm_gate

    uds = UDSConnection(_ScriptedTransport([bytes([0x7F, 0x01, 0x11])]))
    assert enforce_rpm_gate(uds) is None


def test_engine_running_still_blocks_over_real_loop():
    from src.ecu.exceptions import EngineRunningError
    from src.ecu.flash_manager import enforce_rpm_gate

    uds = UDSConnection(_ScriptedTransport([RPM_3000]))
    with pytest.raises(EngineRunningError):
        enforce_rpm_gate(uds)


def test_malformed_refusal_raises_when_strict():
    uds = UDSConnection(_ScriptedTransport([bytes([0x7F, 0x01])]))
    with pytest.raises(UDSError):
        uds.read_battery_voltage(strict=True)


def test_transport_error_raises_when_strict():
    """A corrupt frame on the WiCAN link (#94) reaches the guard as an error."""
    from src.ecu.wican_transport import WiCANError

    uds = UDSConnection(_ScriptedTransport([WiCANError("Invalid SLCAN DLC 't'")]))
    with pytest.raises(UDSError):
        uds.read_engine_rpm(strict=True)


# --- #130 follow-up: ECU still in a programming session after a read ---------

NRC_22 = bytes([0x7F, 0x01, 0x22])
VOLT_13_9 = bytes([0x41, 0x42, 0x36, 0x4C])  # 0x364C / 1000 = 13.9 V


def test_programming_session_refusal_waits_then_really_checks(monkeypatch):
    from src.ecu import protocol
    from src.ecu.constants import PROGRAMMING_SESSION_EXIT_WAIT_MS

    sleeps = []
    monkeypatch.setattr(protocol.time, "sleep", lambda s: sleeps.append(s))
    uds = UDSConnection(_ScriptedTransport([NRC_22, VOLT_13_9]))
    voltage = uds.read_battery_voltage(strict=True, wait_session_exit=True)
    assert voltage == pytest.approx(13.9)
    assert sleeps == [PROGRAMMING_SESSION_EXIT_WAIT_MS / 1000.0]


def test_programming_session_refusal_then_running_engine_blocks(monkeypatch):
    """The retry is what makes the engine-off check real after a ROM read."""
    from src.ecu import protocol
    from src.ecu.exceptions import EngineRunningError
    from src.ecu.flash_manager import enforce_rpm_gate

    monkeypatch.setattr(protocol.time, "sleep", lambda s: None)
    uds = UDSConnection(_ScriptedTransport([NRC_22, RPM_3000]))
    with pytest.raises(EngineRunningError):
        enforce_rpm_gate(uds)


def test_still_refused_after_the_wait_blocks_the_flash(monkeypatch):
    """Still in a programming session after the wait: the check could not
    run, so the guard must refuse (the UI then offers the override), never
    skip it silently."""
    from src.ecu import protocol
    from src.ecu.exceptions import GuardReadError
    from src.ecu.flash_manager import enforce_rpm_gate

    sleeps = []
    monkeypatch.setattr(protocol.time, "sleep", lambda s: sleeps.append(s))
    uds = UDSConnection(_ScriptedTransport([NRC_22, NRC_22]))
    with pytest.raises(GuardReadError, match="programming session"):
        enforce_rpm_gate(uds)
    assert len(sleeps) == 1  # one wait, never a loop


def test_still_refused_voltage_raises_when_strict(monkeypatch):
    from src.ecu import protocol

    monkeypatch.setattr(protocol.time, "sleep", lambda s: None)
    uds = UDSConnection(_ScriptedTransport([NRC_22, NRC_22]))
    with pytest.raises(UDSError, match="programming session"):
        uds.read_battery_voltage(strict=True, wait_session_exit=True)


@pytest.mark.parametrize("strict", [False, True])
def test_dashboard_reads_never_wait(monkeypatch, strict):
    """The UI-thread poll reads strictly but must never sleep on 0x22: a
    programming session there is a normal reply, not a freeze."""
    from src.ecu import protocol

    sleeps = []
    monkeypatch.setattr(protocol.time, "sleep", lambda s: sleeps.append(s))
    uds = UDSConnection(_ScriptedTransport([NRC_22]))
    assert uds.read_battery_voltage(strict=strict) is None
    assert sleeps == []


def test_bootloader_refusal_does_not_wait(monkeypatch):
    """NRC 0x11 (bootloader after a failed flash) is a plain decline."""
    from src.ecu import protocol

    sleeps = []
    monkeypatch.setattr(protocol.time, "sleep", lambda s: sleeps.append(s))
    uds = UDSConnection(_ScriptedTransport([bytes([0x7F, 0x01, 0x11])]))
    assert uds.read_engine_rpm(strict=True, wait_session_exit=True) is None
    assert sleeps == []
