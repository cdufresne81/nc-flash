"""Trip Logs window: inventory table, gating, and the single download path.

Real widgets over a fake sync owner (a QObject with the real signal
signatures — the window wires signals in its constructor, so a
SimpleNamespace won't do). The sync/HTTP behavior itself is covered by
``test_wican_log_sync`` / ``test_ecu_wican_logs``; here only the window's
rendering and gating are under test.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from PySide6.QtCore import QObject, Signal

from src.ecu.wican_logs import (
    STATUS_ACTIVE,
    STATUS_DOWNLOADED,
    STATUS_NEW,
    LogInventoryEntry,
    TripLog,
)
from src.ui.trip_logs_window import ECU_BUSY_TIP, TripLogsWindow, _format_mtime


class _FakeSync(QObject):
    running_changed = Signal(bool)
    progress_changed = Signal(int, int, str)
    inventory_ready = Signal(object)
    inventory_failed = Signal(str)
    checking_changed = Signal(bool)

    def __init__(self):
        super().__init__()
        self.is_running = False
        self.is_checking = False
        self.refresh_inventory = MagicMock(return_value=True)
        self.start = MagicMock(return_value=True)
        self.cancel = MagicMock()


def _window(qtbot, tmp_path, adapter="wican"):
    sync = _FakeSync()
    settings = MagicMock()
    settings.is_wican_adapter.return_value = adapter == "wican"
    settings.get_logs_directory.return_value = str(tmp_path / "logs")
    main_window = SimpleNamespace(settings=settings, wican_log_sync=sync)
    window = TripLogsWindow(main_window=main_window)
    qtbot.addWidget(window)
    return window, sync


def _entry(name, size, status, mtime=1750000000):
    return LogInventoryEntry(
        log=TripLog(name=name, size=size, mtime=mtime), status=status
    )


def test_inventory_populates_table_and_status_line(qtbot, tmp_path):
    window, sync = _window(qtbot, tmp_path)
    entries = [
        _entry("open.csv", 512, STATUS_ACTIVE, mtime=0),  # clockless boot
        _entry("new.csv", 4 * 1024 * 1024, STATUS_NEW),
        _entry("old.csv", 2048, STATUS_DOWNLOADED),
    ]
    sync.inventory_ready.emit(entries)

    table = window._table
    assert table.rowCount() == 3
    assert [table.item(r, 0).text() for r in range(3)] == [
        "open.csv",
        "new.csv",
        "old.csv",
    ]
    assert [table.item(r, 3).text() for r in range(3)] == [
        "Recording",
        "New",
        "Downloaded",
    ]
    assert table.item(1, 1).text() == "4.0 MB"
    assert table.item(0, 2).text() == "—"  # clockless mtime never shown as 1970
    status = window._status_label.text()
    assert "3 logs on device" in status
    assert "1 new" in status
    assert "4.0 MB" in status


def test_all_downloaded_and_empty_device_status_lines(qtbot, tmp_path):
    window, sync = _window(qtbot, tmp_path)
    sync.inventory_ready.emit([_entry("a.csv", 10, STATUS_DOWNLOADED)])
    assert "everything downloaded" in window._status_label.text()
    sync.inventory_ready.emit([])
    assert "No trip logs" in window._status_label.text()


def test_unreachable_device_is_a_quiet_status_line(qtbot, tmp_path):
    window, sync = _window(qtbot, tmp_path)
    sync.inventory_failed.emit("timed out")
    assert "not reachable" in window._status_label.text()


def test_start_download_is_the_single_sync_path(qtbot, tmp_path):
    window, sync = _window(qtbot, tmp_path)
    assert window.start_download() is True
    sync.start.assert_called_once()


def test_ecu_busy_holds_the_download(qtbot, tmp_path):
    # The other half of the ECU⇄utility mutual exclusion: a busy ECU
    # (flash/read/DTC) blocks the download from BOTH entry points — the
    # button state and the public start_download() the startup prompt uses.
    window, sync = _window(qtbot, tmp_path)
    window.set_ecu_busy(True)
    assert not window._btn_download.isEnabled()
    assert window._btn_download.toolTip() == ECU_BUSY_TIP
    assert window.start_download() is False
    sync.start.assert_not_called()

    window.set_ecu_busy(False)
    assert window._btn_download.isEnabled()


def test_running_sync_shows_progress_row_and_locks_buttons(qtbot, tmp_path):
    window, sync = _window(qtbot, tmp_path)
    assert not window._progress_row.isVisibleTo(window)

    sync.is_running = True
    sync.running_changed.emit(True)
    assert window._progress_row.isVisibleTo(window)
    assert not window._btn_download.isEnabled()
    assert not window._btn_refresh.isEnabled()

    # KiB units with an MB label, same math the ECU window's dialog had.
    two_mb, four_mb = 2 * 1024 * 1024, 4 * 1024 * 1024
    sync.progress_changed.emit(two_mb, four_mb, "trip.csv")
    assert window._progress_bar.maximum() == 4096
    assert window._progress_bar.value() == 2048
    assert "trip.csv" in window._progress_label.text()
    assert "2.0 of 4.0 MB" in window._progress_label.text()

    sync.is_running = False
    sync.running_changed.emit(False)
    assert not window._progress_row.isVisibleTo(window)
    assert window._btn_download.isEnabled()


def test_refresh_reenables_via_checking_changed_not_inventory_ready(qtbot, tmp_path):
    # REGRESSION (2026-07-18): at inventory_ready time is_checking is still
    # True (the check thread's queued cleanup runs after it), so gating
    # recomputed on inventory_ready leaves Refresh disabled. Only the later
    # checking_changed(False) may re-enable it — without that connection the
    # button stayed dead after every refresh.
    window, sync = _window(qtbot, tmp_path)
    sync.is_checking = True
    sync.inventory_ready.emit([_entry("a.csv", 10, STATUS_NEW)])
    assert not window._btn_refresh.isEnabled()  # cleanup not yet run

    sync.is_checking = False
    sync.checking_changed.emit(False)
    assert window._btn_refresh.isEnabled()


def test_download_end_refreshes_a_visible_window(qtbot, tmp_path):
    window, sync = _window(qtbot, tmp_path)
    window.show()
    qtbot.waitUntil(lambda: sync.refresh_inventory.called, timeout=2000)
    sync.refresh_inventory.reset_mock()
    sync.running_changed.emit(False)  # statuses must flip to Downloaded
    sync.refresh_inventory.assert_called_once()


def test_cancel_is_nonblocking_and_disables_itself(qtbot, tmp_path):
    window, sync = _window(qtbot, tmp_path)
    sync.is_running = True
    sync.running_changed.emit(True)
    window._on_cancel()
    sync.cancel.assert_called_once()
    assert not window._btn_cancel.isEnabled()
    assert "Cancelling" in window._progress_label.text()


def test_non_wican_adapter_disables_the_surface(qtbot, tmp_path):
    window, _ = _window(qtbot, tmp_path, adapter="j2534")
    assert not window._btn_download.isEnabled()
    assert not window._btn_refresh.isEnabled()
    assert window._status_label.text() == "WiCAN adapter not selected"


def test_format_mtime_clockless_sanity():
    assert _format_mtime(0) == "—"
    assert _format_mtime(1000) == "—"  # 1970: clockless boot, not a real date
    assert _format_mtime(1750000000).startswith("2025-")
