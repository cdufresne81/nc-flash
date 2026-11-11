"""Boot-loader recovery detection ("bricked" ECU).

A bricked NC ECU sits in its boot loader's recovery server: it answers Tester
Present, refuses OBD Mode 0x01 and the VIN read with NRC 0x11, and hands out the
fixed seed 55 AA 55 (bench, 2026-11-09). The probe must:

* stop at the first request on a running ECU (no programming session, no seed);
* only ask for the seed after BOTH refusals are NRC 0x11 for that service;
* never send a key;
* treat anything inconclusive (timeout, other NRC) as UNKNOWN without going on.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.ecu.constants import BOOT_LOADER_SEED
from src.ecu.exceptions import (
    LinkQualityError,
    NegativeResponseError,
    UDSTimeoutError,
)
from src.ecu.recovery import (
    EcuMode,
    RecoveryProbeResult,
    is_boot_loader_seed,
    probe_ecu_mode,
)


class _FakeEcu:
    """Answers ``send_request`` from a {sid: reply-or-exception} table and
    records every request sent."""

    def __init__(self, replies):
        self.replies = replies
        self.sent = []

    def send_request(self, sid, data=b"", **kwargs):
        self.sent.append((sid, bytes(data)))
        reply = self.replies.get(sid)
        if reply is None:
            raise AssertionError(f"unexpected request SID 0x{sid:02X}")
        if isinstance(reply, Exception):
            raise reply
        return reply

    @property
    def sids(self):
        return [sid for sid, _ in self.sent]


def _nrc(nrc, sid):
    return NegativeResponseError(nrc, service_id=sid)


def _boot_loader(seed=BOOT_LOADER_SEED):
    return _FakeEcu(
        {
            0x01: _nrc(0x11, 0x01),
            0x21: _nrc(0x11, 0x21),
            0x22: _nrc(0x31, 0x22),
            0x10: b"\x85",
            0x27: b"\x01" + seed,
        }
    )


def test_fixed_seed_constant():
    assert BOOT_LOADER_SEED == bytes([0x55, 0xAA, 0x55])
    assert is_boot_loader_seed(bytes([0x55, 0xAA, 0x55]))
    assert not is_boot_loader_seed(bytes([0x17, 0x2E, 0x5C]))
    assert not is_boot_loader_seed(None)


def test_boot_loader_detected_and_no_key_sent():
    ecu = _boot_loader()
    result = probe_ecu_mode(ecu)
    assert result.mode == EcuMode.BOOT_LOADER
    assert result.needs_recovery
    assert result.seed == BOOT_LOADER_SEED
    # The exact opening of the normal flash, and nothing more: no 0x27 0x02.
    assert ecu.sent == [
        (0x01, b"\x0c"),
        (0x21, b"\x00"),
        (0x22, b"\xe6\x11"),
        (0x10, b"\x85"),
        (0x27, b"\x01"),
    ]


def test_running_ecu_stops_after_first_request():
    ecu = _FakeEcu({0x01: b"\x0c\x00\x00"})
    result = probe_ecu_mode(ecu)
    assert result.mode == EcuMode.NORMAL
    assert not result.needs_recovery
    assert ecu.sids == [0x01]


def test_vin_answered_is_normal_and_no_session_change():
    ecu = _FakeEcu({0x01: _nrc(0x11, 0x01), 0x21: b"\x00" * 20})
    assert probe_ecu_mode(ecu).mode == EcuMode.NORMAL
    assert 0x10 not in ecu.sids and 0x27 not in ecu.sids


@pytest.mark.parametrize(
    "obd_reply",
    [
        UDSTimeoutError("no reply"),
        _nrc(0x22, 0x01),  # programming session still active (after a read)
        _nrc(0x12, 0x01),  # sub-function not supported: not the pattern
        _nrc(0x11, 0x3E),  # a refusal meant for another request
    ],
)
def test_inconclusive_obd_stops_probe(obd_reply):
    ecu = _FakeEcu({0x01: obd_reply})
    result = probe_ecu_mode(ecu)
    assert result.mode == EcuMode.UNKNOWN
    assert not result.needs_recovery
    assert ecu.sids == [0x01]


def test_inconclusive_vin_stops_before_seed():
    ecu = _FakeEcu({0x01: _nrc(0x11, 0x01), 0x21: UDSTimeoutError("x")})
    assert probe_ecu_mode(ecu).mode == EcuMode.UNKNOWN
    assert ecu.sids == [0x01, 0x21]


def test_varying_seed_is_the_application_not_recovery():
    result = probe_ecu_mode(_boot_loader(seed=bytes([0x17, 0x2E, 0x5C])))
    assert result.mode == EcuMode.UNKNOWN
    assert not result.needs_recovery


def test_seed_with_extra_bytes_is_not_the_boot_loader_seed():
    result = probe_ecu_mode(_boot_loader(seed=BOOT_LOADER_SEED + b"\x00"))
    assert result.mode == EcuMode.UNKNOWN


def test_empty_seed_reply_is_unknown():
    ecu = _boot_loader()
    ecu.replies[0x27] = b"\x01"
    assert probe_ecu_mode(ecu).mode == EcuMode.UNKNOWN


@pytest.mark.parametrize(
    "rom_id_reply",
    [b"\xe6\x11LF9VEB", _nrc(0x11, 0x22), _nrc(0x22, 0x22), _nrc(0x31, 0x23)],
)
def test_rom_id_must_be_refused_out_of_range(rom_id_reply):
    ecu = _boot_loader()
    ecu.replies[0x22] = rom_id_reply
    probe_ecu_mode(ecu)
    assert 0x10 not in ecu.sids and 0x27 not in ecu.sids


def test_foreign_sid_on_vin_refusal_stops_probe():
    ecu = _boot_loader()
    ecu.replies[0x21] = _nrc(0x11, 0x01)
    assert probe_ecu_mode(ecu).mode == EcuMode.UNKNOWN
    assert ecu.sids == [0x01, 0x21]


@pytest.mark.parametrize("failing_sid", [0x10, 0x27])
def test_seed_not_obtained_is_suspect(failing_sid):
    ecu = _boot_loader()
    ecu.replies[failing_sid] = UDSTimeoutError("no reply")
    result = probe_ecu_mode(ecu)
    assert result.mode == EcuMode.SUSPECT
    assert result.seed is None


def test_no_session_is_unknown():
    assert probe_ecu_mode(None).mode == EcuMode.UNKNOWN


def test_probe_over_real_uds_connection_quiet_nrcs():
    """Through UDSConnection.send_request: the expected NRCs are logged quietly
    and the probe reads the seed from the positive 0x67 reply."""
    from src.ecu.protocol import UDSConnection

    replies = [
        bytes([0x7F, 0x01, 0x11]),
        bytes([0x7F, 0x21, 0x11]),
        bytes([0x7F, 0x22, 0x31]),
        bytes([0x50, 0x85]),
        bytes([0x67, 0x01, 0x55, 0xAA, 0x55]),
    ]
    transport = MagicMock()
    transport.receive_message.side_effect = replies
    uds = UDSConnection(transport)
    result = probe_ecu_mode(uds)
    assert result.mode == EcuMode.BOOT_LOADER
    sent = [c.args[0] for c in transport.send_message.call_args_list]
    assert sent == [b"\x01\x0c", b"\x21\x00", b"\x22\xe6\x11", b"\x10\x85", b"\x27\x01"]


# --- Link-check failures are their own error type -----------------------------


def test_link_gate_raises_link_quality_error():
    from src.ecu.exceptions import FlashError
    from src.ecu.wican_flash import WiCANFlasher

    flasher = WiCANFlasher.__new__(WiCANFlasher)
    flasher.preflight = MagicMock(
        return_value=SimpleNamespace(ok=False, reason="56% loss")
    )
    with pytest.raises(LinkQualityError) as info:
        flasher._gate()
    # Still a FlashError with the same wording, so existing callers are unchanged.
    assert isinstance(info.value, FlashError)
    assert "link-quality gate FAILED" in str(info.value)


# --- Window logic (duck-typed fake self, no QApplication) ---------------------


from src.ui.ecu_window import ECUProgrammingWindow  # noqa: E402


class _FakeWindow(SimpleNamespace):
    _set_recovery = ECUProgrammingWindow._set_recovery
    _in_recovery = ECUProgrammingWindow._in_recovery
    _boot_loader_confirmed = ECUProgrammingWindow._boot_loader_confirmed
    _dtc_refusal_means_recovery = ECUProgrammingWindow._dtc_refusal_means_recovery


def _fake_window(**kw):
    base = dict(
        _recovery=None,
        _recovery_banner=MagicMock(),
        _recovery_text=MagicMock(),
        _card_ecu=MagicMock(),
        _conn_label=MagicMock(),
        _update_action_states=MagicMock(),
    )
    base.update(kw)
    return _FakeWindow(**base)


def test_window_shows_recovery_banner_for_boot_loader():
    win = _fake_window()
    win._set_recovery(RecoveryProbeResult(EcuMode.BOOT_LOADER, BOOT_LOADER_SEED))
    assert win._in_recovery and win._boot_loader_confirmed
    win._recovery_banner.setVisible.assert_called_with(True)
    text = win._recovery_text.setText.call_args.args[0]
    assert "Recovery mode" in text and "known-good ROM" in text
    assert "full flash" in text
    win._card_ecu.set_value.assert_called()
    assert win._card_ecu.set_value.call_args.args[0] == "RECOVERY MODE"


def test_boot_loader_right_after_a_flash_is_recovery_mode():
    """A valid flash restarts the ECU by itself (bench 2026-11-11: running 9 s
    after the flash, no key cycle), so a boot loader seen after any flash means
    the ROM can't start: recovery mode, never "wait for an ignition cycle"."""
    win = _fake_window(_flash_was_recovery=False)
    win._set_recovery(RecoveryProbeResult(EcuMode.BOOT_LOADER, BOOT_LOADER_SEED))
    text = win._recovery_text.setText.call_args.args[0]
    assert "Recovery mode" in text and "ignition" not in text


def test_window_normal_result_hides_banner():
    win = _fake_window()
    win._set_recovery(RecoveryProbeResult(EcuMode.NORMAL))
    assert win._recovery is None and not win._in_recovery
    win._recovery_banner.setVisible.assert_called_with(False)


def test_window_unknown_result_shows_nothing():
    win = _fake_window()
    win._set_recovery(RecoveryProbeResult(EcuMode.UNKNOWN))
    assert win._recovery is None
    win._recovery_banner.setVisible.assert_called_with(False)


def test_dtc_refusal_runs_probe_only_for_unsupported_or_out_of_range():
    win = _fake_window(_check_recovery_mode=MagicMock(return_value=None))
    assert not win._dtc_refusal_means_recovery(UDSTimeoutError("x"))
    assert not win._dtc_refusal_means_recovery(_nrc(0x22, 0x22))
    win._check_recovery_mode.assert_not_called()
    assert not win._dtc_refusal_means_recovery(_nrc(0x31, 0x22))
    win._check_recovery_mode.assert_called_once()


def _conditions_window(uds):
    win = _fake_window(
        _session=SimpleNamespace(uds=uds, is_connected=True),
        _conditions_pending=False,
        _on_conditions_loaded=MagicMock(),
        _on_poll_no_reply=MagicMock(),
        _poll_timer=MagicMock(),
        _check_recovery_mode=MagicMock(),
    )
    win._set_recovery = MagicMock()
    return win


def test_connect_reads_trigger_probe_when_obd_and_vin_fail():
    uds = MagicMock()
    uds.read_vin_block.side_effect = _nrc(0x11, 0x21)
    uds.read_rom_id.side_effect = _nrc(0x31, 0x22)
    uds.read_dtc_status.side_effect = _nrc(0x31, 0x22)
    uds.read_battery_voltage.return_value = None
    uds.read_engine_rpm.return_value = None
    win = _conditions_window(uds)
    ECUProgrammingWindow._read_conditions_async(win)
    win._check_recovery_mode.assert_called_once()


def test_connect_reads_skip_probe_on_running_ecu():
    uds = MagicMock()
    uds.read_vin_block.return_value = b"JM1NC25F" + b"0" * 9
    uds.read_rom_id.return_value = "LF9VEB"
    uds.read_dtc_status.return_value = []
    uds.read_battery_voltage.return_value = 12.6
    uds.read_engine_rpm.return_value = 0.0
    win = _conditions_window(uds)
    ECUProgrammingWindow._read_conditions_async(win)
    win._check_recovery_mode.assert_not_called()
    win._set_recovery.assert_called_once_with(None)


def _gating_window(*, busy=False, recovery=None):
    # Reuse the interlock tests' stand-in window, so it follows the real one.
    from tests.test_ecu_window_wican_interlock import _fake_window as _interlock

    fake = _interlock(adapter="j2534", ecu_busy=busy, connected=True)
    fake._session.state = ECUSessionState.CONNECTED
    fake._recovery = recovery
    return fake


def _update(fake):
    ECUProgrammingWindow._update_action_states(fake)


from src.ecu.session import ECUSessionState  # noqa: E402


def test_flash_buttons_disabled_while_busy_in_recovery():
    win = _gating_window(busy=True, recovery=RecoveryProbeResult(EcuMode.BOOT_LOADER))
    _update(win)
    win._btn_full_flash.setEnabled.assert_called_with(False)
    win._btn_flash_current.setEnabled.assert_called_with(False)


def test_recovery_mode_enables_only_the_two_flash_buttons(monkeypatch, tmp_path):
    """Recovery uses the window's normal flow: open a known-good ROM, then
    Flash or Full Flash (both write the whole ROM). Nothing else works."""
    import src.ui.ecu_window as ew

    monkeypatch.setattr(ew, "SECURE_MODULE_AVAILABLE", True)
    win = _gating_window(recovery=RecoveryProbeResult(EcuMode.BOOT_LOADER))
    doc = SimpleNamespace(rom_path=str(tmp_path / "backup.bin"), file_name="backup.bin")
    win._main_window.get_current_document = lambda: doc
    win._get_current_rom_data = lambda: bytes(16)
    (tmp_path / ew.ARCHIVE_FILENAME).write_bytes(b"")  # would mean dynamic
    _update(win)
    win._btn_full_flash.setEnabled.assert_called_with(True)
    win._btn_flash_current.setEnabled.assert_called_with(True)
    win._btn_flash_current.setText.assert_called_with("Flash Current ROM (Full)")
    for name in ("_btn_read_dtcs", "_btn_clear_dtcs", "_btn_read_rom", "_btn_scan_ram"):
        getattr(win, name).setEnabled.assert_called_with(False)


def test_recovery_poll_sends_only_tester_present():
    """In recovery the boot loader refuses OBD reads; polling them only filled
    the log with refusals (bench 2026-11-10)."""
    uds = MagicMock()
    win = _fake_window(
        _recovery=RecoveryProbeResult(EcuMode.BOOT_LOADER, BOOT_LOADER_SEED),
        _session=SimpleNamespace(uds=uds, is_connected=True, state=None),
        _ecu_busy=False,
        _main_window=SimpleNamespace(wican_log_sync=None),
        _poll_timer=MagicMock(),
        _on_ecu_answered=MagicMock(),
        _on_poll_no_reply=MagicMock(),
    )
    ECUProgrammingWindow._poll_conditions(win)
    uds.tester_present.assert_called_once()
    uds.read_battery_voltage.assert_not_called()
    uds.read_engine_rpm.assert_not_called()
    win._on_ecu_answered.assert_called_once()


def test_activity_log_hides_raw_protocol_lines():
    import logging

    from src.ui.log_console import _ConsoleScopeFilter

    f = _ConsoleScopeFilter(hidden_message_prefixes=("UDS NRC:", "ECU mode check:"))

    def rec(msg):
        return logging.LogRecord(
            "src.ecu.protocol", logging.WARNING, "", 0, msg, (), None
        )

    assert not f.filter(rec("UDS NRC: SID=0x01 NRC=0x11 (Service not supported)"))
    assert not f.filter(rec("ECU mode check: %s (%s)"))
    assert f.filter(rec("The ECU is in recovery mode (boot loader)."))
