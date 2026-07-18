"""ECU window ⇄ WiCAN trip-log download mutual exclusion, ECU-side half.

The download itself lives in the Trip Logs window now; this file covers what
the ECU window still owns: while a download runs, every ECU action locks
(with an explanatory tooltip) and Connect is held off while disconnected —
and the window broadcasts its busy state (``busy_changed``, emit-on-change)
so the Trip Logs window can hold its download in return. Exercised against a
duck-typed fake ``self`` (mirrors ``test_ecu_read_naming``) — no QApplication.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.ui.ecu_window import ECUProgrammingWindow

UTILITY_LOCK_TIP = "Stop the WiCAN trip-log download first"


class _FakeECUWindow(SimpleNamespace):
    # The REAL property, so gating and broadcasts exercise the one busy
    # predicate (_ecu_busy OR running flash thread) instead of a test copy.
    is_busy = ECUProgrammingWindow.is_busy


def _fake_window(
    adapter="wican",
    sync_running=False,
    ecu_busy=False,
    connected=False,
):
    settings = MagicMock()
    settings.is_wican_adapter.return_value = adapter == "wican"
    main_window = SimpleNamespace(
        settings=settings,
        wican_log_sync=SimpleNamespace(is_running=sync_running),
        get_current_document=lambda: None,
    )
    return _FakeECUWindow(
        _session=SimpleNamespace(is_connected=True) if connected else None,
        _flash_thread=None,
        _ecu_busy=ecu_busy,
        _rpm=None,
        _main_window=main_window,
        _last_busy_broadcast=None,
        busy_changed=MagicMock(),
        _get_current_rom_data=lambda: None,
        _btn_read_dtcs=MagicMock(),
        _btn_clear_dtcs=MagicMock(),
        _btn_full_flash=MagicMock(),
        _btn_read_rom=MagicMock(),
        _btn_scan_ram=MagicMock(),
        _btn_flash_current=MagicMock(),
        _btn_connect=MagicMock(),
        _flash_subtitle=MagicMock(),
    )


def _update(fake):
    ECUProgrammingWindow._update_action_states(fake)


def test_ecu_ops_locked_while_sync_running():
    # The utility and ECU operations never mix: a running trip-log download
    # locks every ECU action even with a live connection, and the lock
    # explains itself via the tooltip.
    fake = _fake_window(adapter="wican", sync_running=True, connected=True)
    _update(fake)
    fake._btn_read_dtcs.setEnabled.assert_called_with(False)
    fake._btn_clear_dtcs.setEnabled.assert_called_with(False)
    fake._btn_full_flash.setEnabled.assert_called_with(False)
    fake._btn_read_rom.setEnabled.assert_called_with(False)
    fake._btn_scan_ram.setEnabled.assert_called_with(False)
    fake._btn_flash_current.setEnabled.assert_called_with(False)
    fake._btn_flash_current.setToolTip.assert_called_with(UTILITY_LOCK_TIP)
    fake._btn_full_flash.setToolTip.assert_called_with(UTILITY_LOCK_TIP)


def test_ecu_ops_unlocked_when_no_utility_runs():
    # Control for the test above: the same connected window with no utility
    # running leaves connection-gated ops (DTCs) enabled — proving they
    # exercise the utility lock, not the no-connection gate.
    fake = _fake_window(adapter="wican", connected=True)
    _update(fake)
    fake._btn_read_dtcs.setEnabled.assert_called_with(True)
    fake._btn_clear_dtcs.setEnabled.assert_called_with(True)


def test_connect_locked_while_utility_runs_when_disconnected():
    # Connecting mid-download would contend for the WiCAN's SD/CPU/WiFi (and
    # re-park the datalogger); while DISCONNECTED the Connect button follows
    # the utility lock.
    fake = _fake_window(adapter="wican", sync_running=True)
    _update(fake)
    fake._btn_connect.setEnabled.assert_called_with(False)

    fake = _fake_window(adapter="wican")
    _update(fake)
    fake._btn_connect.setEnabled.assert_called_with(True)


def test_connect_untouched_while_connected():
    # Connected (or mid-connect) states own the Connect button elsewhere —
    # the utility lock must not fight the session-state handler for it.
    fake = _fake_window(adapter="wican", sync_running=True, connected=True)
    _update(fake)
    fake._btn_connect.setEnabled.assert_not_called()


def test_sync_lock_only_applies_to_the_wican_adapter():
    # With Tactrix/J2534 a running sync (leftover from an adapter switch)
    # must not lock ECU ops — there is no shared device to contend for.
    fake = _fake_window(adapter="j2534", sync_running=True, connected=True)
    _update(fake)
    fake._btn_read_dtcs.setEnabled.assert_called_with(True)


# --- busy_changed broadcast (consumed by the Trip Logs window) -----------------


def test_busy_broadcast_emits_on_change_only():
    fake = _fake_window(adapter="wican", ecu_busy=True)
    _update(fake)
    fake.busy_changed.emit.assert_called_once_with(True)

    _update(fake)  # same state again: no re-emit
    fake.busy_changed.emit.assert_called_once_with(True)

    fake._ecu_busy = False
    _update(fake)
    assert fake.busy_changed.emit.call_args_list[-1].args == (False,)
    assert fake.busy_changed.emit.call_count == 2


def test_busy_broadcast_covers_flash_thread_runs():
    # busy is _ecu_busy OR a running flash thread — the broadcast (and the
    # is_busy property a late-created Trip Logs window reads) must see both.
    fake = _fake_window(adapter="wican")
    fake._flash_thread = SimpleNamespace(isRunning=lambda: True)
    _update(fake)
    fake.busy_changed.emit.assert_called_once_with(True)
    assert fake.is_busy is True
