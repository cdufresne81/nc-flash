"""WiCAN Trip Logs window — the device-utility surface, session-free.

The ECU Programming window is session-gated top to bottom (Connect → operate);
this window is the home of everything that talks to the WiCAN *device* over
plain HTTP/WiFi and needs no ECU session: today the trip-log inventory table
and the download; designed to grow the on-device SD file manager (per-row
actions on the same table) and the live-datalog controls (start/stop/status +
MegaLogViewerHD launch) later.

Ownership: all background state stays with the main-window-owned
:class:`~src.ui.wican_log_sync.WiCANLogSync` (single owner; THE one download
path — the startup auto-check prompt funnels through :meth:`start_download`
too). This window is a thin subscriber: closing it mid-download does NOT
cancel the download; the main window's closeEvent owns shutdown.

Gating model (one per surface): everything here needs the WiCAN adapter
selected, and the download locks against a busy ECU (flash/read/DTC) exactly
as ECU operations lock against a running download on the other side — the
PR #89 mutual exclusion, rendered from both ends.
"""

import logging
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.ecu.wican_logs import (
    STATUS_ACTIVE,
    STATUS_DOWNLOADED,
    STATUS_NEW,
    STATUS_UNSAFE_NAME,
)
from src.ui import theme
from src.ui.wican_log_sync import estimate_download_text, format_size

logger = logging.getLogger(__name__)

#: Device mtimes before this (2000-01-01) come from a clockless boot — shown
#: as "—" rather than a misleading 1970 date.
_MTIME_SANITY_EPOCH = 946684800

_STATUS_LABELS = {
    STATUS_NEW: "New",
    STATUS_DOWNLOADED: "Downloaded",
    STATUS_ACTIVE: "Recording",
    STATUS_UNSAFE_NAME: "Skipped (unsafe name)",
}

ECU_BUSY_TIP = "Wait for the running ECU operation to finish"


def _format_mtime(mtime: int) -> str:
    if mtime < _MTIME_SANITY_EPOCH:
        return "—"
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")


class TripLogsWindow(QMainWindow):
    """Trip-log inventory table + download driver for the WiCAN device."""

    def __init__(self, main_window, parent=None):
        super().__init__(parent)
        self._main_window = main_window
        self._ecu_busy = False
        self._entries = []

        self.setWindowTitle("WiCAN Trip Logs")
        self.setMinimumSize(560, 380)
        self.resize(680, 460)
        self._build_ui()

        # Owner-alive connects (the sync outlives this window; Qt
        # auto-disconnects these when the window is destroyed).
        sync = main_window.wican_log_sync
        sync.running_changed.connect(self._on_sync_running)
        sync.progress_changed.connect(self._on_progress)
        sync.inventory_ready.connect(self._on_inventory)
        sync.inventory_failed.connect(self._on_inventory_failed)
        # Refresh re-enables ONLY on this signal: at inventory_ready time
        # is_checking is still True (the check thread's cleanup is queued
        # behind it), so gating recomputed there sees a busy sync.
        sync.checking_changed.connect(self._update_action_states)

        self._update_action_states()
        QTimer.singleShot(0, self.refresh)

    # --- UI ------------------------------------------------------------------

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        header = QHBoxLayout()
        self._status_label = QLabel("")
        header.addWidget(self._status_label)
        header.addStretch()
        self._btn_refresh = QPushButton("Refresh")
        self._btn_refresh.clicked.connect(self.refresh)
        header.addWidget(self._btn_refresh)
        root.addLayout(header)

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Name", "Size", "Modified", "Status"])
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.verticalHeader().setVisible(False)
        self._table.setColumnWidth(0, 260)
        self._table.setColumnWidth(1, 80)
        self._table.setColumnWidth(2, 130)
        self._table.horizontalHeader().setStretchLastSection(True)
        root.addWidget(self._table, stretch=1)

        actions = QHBoxLayout()
        self._btn_download = QPushButton("Download New Logs")
        self._btn_download.setMinimumHeight(32)
        self._btn_download.clicked.connect(self.start_download)
        actions.addWidget(self._btn_download)
        self._btn_open_folder = QPushButton("Open Logs Folder")
        self._btn_open_folder.clicked.connect(self._on_open_folder)
        actions.addWidget(self._btn_open_folder)
        actions.addStretch()
        root.addLayout(actions)

        # Inline download progress (shown only while a sync runs).
        self._progress_row = QWidget()
        progress_layout = QHBoxLayout(self._progress_row)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        self._progress_bar = QProgressBar()
        progress_layout.addWidget(self._progress_bar, stretch=1)
        self._progress_label = QLabel("")
        progress_layout.addWidget(self._progress_label)
        self._btn_cancel = QPushButton("Cancel")
        self._btn_cancel.clicked.connect(self._on_cancel)
        progress_layout.addWidget(self._btn_cancel)
        self._progress_row.setVisible(False)
        root.addWidget(self._progress_row)

    # --- public surface (also driven by the main window's auto-check) --------

    def refresh(self) -> bool:
        """Kick a background device inventory; the table rides its signals."""
        if not self._main_window.wican_log_sync.refresh_inventory():
            self._update_action_states()
            return False
        self._status_label.setText("Checking device...")
        self._update_action_states()
        return True

    def start_download(self) -> bool:
        """Start the trip-log download (THE single download path).

        Refuses while an ECU operation runs — every caller (button, startup
        prompt) funnels through here, so the interlock cannot be bypassed.
        """
        if self._ecu_busy:
            return False
        started = self._main_window.wican_log_sync.start()
        if not started:
            self._update_action_states()
        return started

    def set_ecu_busy(self, busy: bool):
        """ECU flash/read/DTC in progress — hold the download (bus/SD/WiFi
        contention, mirrored from the ECU window's utility lock)."""
        self._ecu_busy = bool(busy)
        self._update_action_states()

    # --- sync signal handlers -------------------------------------------------

    def _on_inventory(self, entries):
        self._entries = list(entries)
        self._populate_table()
        new = [e for e in self._entries if e.status == STATUS_NEW]
        if not self._entries:
            self._status_label.setText("No trip logs on the device")
        elif new:
            total = sum(e.log.size for e in new)
            self._status_label.setText(
                f"{len(self._entries)} logs on device — {len(new)} new "
                f"({format_size(total)}, {estimate_download_text(total)})"
            )
        else:
            self._status_label.setText(
                f"{len(self._entries)} logs on device — everything downloaded"
            )
        self._update_action_states()

    def _on_inventory_failed(self, message: str):
        # Car off / device asleep is the normal case: a quiet status line.
        self._status_label.setText("WiCAN not reachable (device off or asleep)")
        logger.debug("Trip-log inventory failed: %s", message)
        self._update_action_states()

    def _on_sync_running(self, running: bool):
        self._progress_row.setVisible(running)
        if running:
            self._progress_bar.setRange(0, 0)  # indeterminate until the plan
            self._progress_label.setText("Checking WiCAN for new trip logs...")
            self._btn_cancel.setEnabled(True)
        elif self.isVisible():
            self.refresh()  # statuses flip to Downloaded
        self._update_action_states()

    def _on_progress(self, done: int, total: int, name: str):
        """KiB units keep the range far under QProgressBar's int32 ceiling."""
        if total <= 0:
            return  # nothing to download: stays indeterminate until the end
        done = min(done, total)
        self._progress_bar.setRange(0, max(1, total // 1024))
        self._progress_bar.setValue(done // 1024)
        mb_total = total / (1024 * 1024)
        if name:
            mb_done = done / (1024 * 1024)
            self._progress_label.setText(
                f"Downloading {name}...  ({mb_done:.1f} of {mb_total:.1f} MB)"
            )
        else:
            self._progress_label.setText(
                f"Downloading trip logs ({mb_total:.1f} MB)..."
            )

    # --- actions --------------------------------------------------------------

    def _on_cancel(self):
        """Non-blocking: the worker exits at the next chunk; completed files
        remain and the next run re-fetches only what is missing."""
        self._progress_label.setText("Cancelling...")
        self._btn_cancel.setEnabled(False)
        self._main_window.wican_log_sync.cancel()

    def _on_open_folder(self):
        logs_dir = Path(self._main_window.settings.get_logs_directory())
        logs_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(logs_dir)))

    # --- state ----------------------------------------------------------------

    def _update_action_states(self, *_args):
        sync = self._main_window.wican_log_sync
        is_wican = self._main_window.settings.is_wican_adapter()

        self._btn_download.setEnabled(
            is_wican and not sync.is_running and not self._ecu_busy
        )
        self._btn_download.setToolTip(
            ECU_BUSY_TIP
            if is_wican and self._ecu_busy
            else "Download every new trip log from the WiCAN's SD card"
        )
        self._btn_refresh.setEnabled(
            is_wican and not sync.is_running and not sync.is_checking
        )
        if not is_wican:
            self._status_label.setText("WiCAN adapter not selected")

    def _populate_table(self):
        self._table.setRowCount(len(self._entries))
        for row, entry in enumerate(self._entries):
            name = QTableWidgetItem(entry.log.name)
            size = QTableWidgetItem(format_size(entry.log.size))
            size.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            mtime = QTableWidgetItem(_format_mtime(entry.log.mtime))
            status = QTableWidgetItem(_STATUS_LABELS.get(entry.status, entry.status))
            if entry.status == STATUS_NEW:
                status.setForeground(QColor(theme.ACCENT))
            elif entry.status == STATUS_ACTIVE:
                status.setForeground(QColor(theme.WARNING_AMBER))
            for col, item in enumerate((name, size, mtime, status)):
                self._table.setItem(row, col, item)
