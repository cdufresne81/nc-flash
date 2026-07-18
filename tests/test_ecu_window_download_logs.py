"""WiCAN device-utility button gating: Download Logs.

Download Logs is a WiCAN device utility (pure HTTP to the adapter, no ECU
session). With Tactrix/J2534 selected it must be HIDDEN entirely — not just
disabled. The utility and ECU operations never mix: while a download runs,
every ECU action locks (with an explanatory tooltip) and Connect is held off
while disconnected. It is also held off while an ECU operation is busy.
Exercised against a duck-typed fake ``self`` (mirrors ``test_ecu_read_naming``)
— no QApplication.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.ui.ecu_window import ECUProgrammingWindow

UTILITY_LOCK_TIP = "Stop the WiCAN trip-log download first"


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
    return SimpleNamespace(
        _session=SimpleNamespace(is_connected=True) if connected else None,
        _flash_thread=None,
        _ecu_busy=ecu_busy,
        _rpm=None,
        _main_window=main_window,
        _get_current_rom_data=lambda: None,
        _btn_read_dtcs=MagicMock(),
        _btn_clear_dtcs=MagicMock(),
        _btn_full_flash=MagicMock(),
        _btn_read_rom=MagicMock(),
        _btn_scan_ram=MagicMock(),
        _btn_flash_current=MagicMock(),
        _btn_download_logs=MagicMock(),
        _btn_connect=MagicMock(),
        _flash_subtitle=MagicMock(),
    )


def _update(fake):
    ECUProgrammingWindow._update_action_states(fake)


def test_hidden_and_disabled_with_j2534_adapter():
    fake = _fake_window(adapter="j2534")
    _update(fake)
    fake._btn_download_logs.setVisible.assert_called_with(False)
    fake._btn_download_logs.setEnabled.assert_called_with(False)


def test_visible_and_enabled_with_wican_adapter():
    fake = _fake_window(adapter="wican")
    _update(fake)
    fake._btn_download_logs.setVisible.assert_called_with(True)
    fake._btn_download_logs.setEnabled.assert_called_with(True)


def test_download_disabled_but_visible_while_sync_running():
    fake = _fake_window(adapter="wican", sync_running=True)
    _update(fake)
    fake._btn_download_logs.setVisible.assert_called_with(True)
    fake._btn_download_logs.setEnabled.assert_called_with(False)


def test_disabled_but_visible_while_ecu_busy():
    fake = _fake_window(adapter="wican", ecu_busy=True)
    _update(fake)
    fake._btn_download_logs.setVisible.assert_called_with(True)
    fake._btn_download_logs.setEnabled.assert_called_with(False)


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


# --- download progress dialog (manual path only) -------------------------------
#
# Same duck-typed-fake approach: the dialog factory is mocked so no QWidget is
# ever constructed — these exercise the wiring, not Qt.


def _dialog_fake(start_ok=True):
    sync = MagicMock()
    sync.start.return_value = start_ok
    fake = SimpleNamespace(
        _main_window=SimpleNamespace(wican_log_sync=sync),
        _download_progress=None,
        _create_download_progress_dialog=MagicMock(return_value=MagicMock()),
    )
    return fake, sync


def test_download_click_shows_dialog_when_sync_starts():
    fake, sync = _dialog_fake(start_ok=True)
    ECUProgrammingWindow._on_download_logs(fake)
    dialog = fake._create_download_progress_dialog.return_value
    assert fake._download_progress is dialog
    dialog.show.assert_called_once()
    sync.start.assert_called_once()


def test_download_click_no_dialog_when_start_refused():
    # start() returning False (already running / not WiCAN / unconfigured)
    # must not leave a zombie dialog.
    fake, _ = _dialog_fake(start_ok=False)
    ECUProgrammingWindow._on_download_logs(fake)
    fake._create_download_progress_dialog.assert_not_called()
    assert fake._download_progress is None


def test_progress_drives_dialog_in_kib_with_mb_label():
    fake, _ = _dialog_fake()
    dialog = MagicMock()
    fake._download_progress = dialog
    two_mb, four_mb = 2 * 1024 * 1024, 4 * 1024 * 1024
    ECUProgrammingWindow._on_download_progress(fake, two_mb, four_mb, "trip.csv")
    dialog.setMaximum.assert_called_with(4096)
    dialog.setValue.assert_called_with(2048)
    label = dialog.setLabelText.call_args[0][0]
    assert "trip.csv" in label
    assert "2.0 of 4.0 MB" in label


def test_progress_before_plan_or_without_dialog_is_a_noop():
    fake, _ = _dialog_fake()
    # Auto-sync path: signals fire with no dialog around.
    ECUProgrammingWindow._on_download_progress(fake, 10, 100, "a.csv")
    # total == 0 (nothing to download): bar stays as-is until the run ends.
    dialog = MagicMock()
    fake._download_progress = dialog
    ECUProgrammingWindow._on_download_progress(fake, 0, 0, "")
    dialog.setMaximum.assert_not_called()
    dialog.setValue.assert_not_called()


def test_cancel_relabels_and_requests_sync_cancel():
    fake, sync = _dialog_fake()
    dialog = MagicMock()
    fake._download_progress = dialog
    ECUProgrammingWindow._on_download_logs_cancel(fake)
    dialog.setLabelText.assert_called_with("Cancelling...")
    sync.cancel.assert_called_once()


def test_run_end_closes_dialog_and_running_true_leaves_it():
    fake, _ = _dialog_fake()
    dialog = MagicMock()
    fake._download_progress = dialog

    ECUProgrammingWindow._on_download_sync_running(fake, True)
    dialog.close.assert_not_called()
    assert fake._download_progress is dialog

    ECUProgrammingWindow._on_download_sync_running(fake, False)
    dialog.close.assert_called_once()
    dialog.deleteLater.assert_called_once()
    assert fake._download_progress is None

    # Auto-sync (no dialog) end: a no-op.
    ECUProgrammingWindow._on_download_sync_running(fake, False)
