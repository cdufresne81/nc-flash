"""Download Logs button gating: WiCAN-only visibility + busy/sync disable.

The button is a WiCAN device utility (pure HTTP to the adapter's SD log
endpoints). With Tactrix/J2534 selected the SD card is a manual operation, so
the button must be HIDDEN entirely — not just disabled. Exercised against a
duck-typed fake ``self`` (mirrors ``test_ecu_read_naming``) — no QApplication.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.ui.ecu_window import ECUProgrammingWindow


def _fake_window(adapter="wican", sync_running=False, ecu_busy=False):
    settings = MagicMock()
    settings.is_wican_adapter.return_value = adapter == "wican"
    main_window = SimpleNamespace(
        settings=settings,
        wican_log_sync=SimpleNamespace(is_running=sync_running),
        get_current_document=lambda: None,
    )
    return SimpleNamespace(
        _session=None,
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


def test_disabled_but_visible_while_sync_running():
    fake = _fake_window(adapter="wican", sync_running=True)
    _update(fake)
    fake._btn_download_logs.setVisible.assert_called_with(True)
    fake._btn_download_logs.setEnabled.assert_called_with(False)


def test_disabled_but_visible_while_ecu_busy():
    fake = _fake_window(adapter="wican", ecu_busy=True)
    _update(fake)
    fake._btn_download_logs.setVisible.assert_called_with(True)
    fake._btn_download_logs.setEnabled.assert_called_with(False)
