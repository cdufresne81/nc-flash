"""WiCAN Trip Logs window — the device-utility surface, session-free.

The ECU Programming window is session-gated top to bottom (Connect → operate);
this window is the home of everything that talks to the WiCAN *device* over
plain HTTP/WiFi and needs no ECU session: the trip-log inventory table, the
download, and the live datalog (start/stop/status + MegaLogViewerHD trail);
designed to grow the on-device SD file manager (per-row actions on the same
table) later.

Ownership: all background state stays with the main-window-owned
:class:`~src.ui.wican_log_sync.WiCANLogSync` (single owner; THE one download
path — the startup auto-check prompt funnels through :meth:`start_download`
too) and the main-window-owned
:class:`~src.ui.wican_live_datalog.WiCANLiveDatalog`. This window is a thin
subscriber: closing it mid-download or mid-stream cancels neither; the main
window's closeEvent owns shutdown.

Gating model (one per surface): everything here needs the WiCAN adapter
selected, and the download locks against a busy ECU (flash/read/DTC) exactly
as ECU operations lock against a running download on the other side — the
PR #89 mutual exclusion, rendered from both ends. The live datalog locks
against an ECU session over the WiCAN (which reserves the bus and so pauses
the trip being followed) and against the download.
"""

import logging
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QGroupBox,
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
from src.ui.wican_log_sync import estimate_download_text
from src.utils.transfer import format_rate, format_size, format_time_left

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
ECU_LINK_TIP = "Disconnect the ECU Programming window from the WiCAN first"
LIVE_RUNNING_TIP = "Stop the live datalog first"


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
        self._ecu_link = False
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
        sync.cleanup_status.connect(self._on_cleanup_status)
        sync.inventory_ready.connect(self._on_inventory)
        sync.inventory_failed.connect(self._on_inventory_failed)
        # Refresh re-enables ONLY on this signal: at inventory_ready time
        # is_checking is still True (the check thread's cleanup is queued
        # behind it), so gating recomputed there sees a busy sync.
        sync.checking_changed.connect(self._update_action_states)
        live = main_window.wican_live_datalog
        live.running_changed.connect(self._on_live_running)
        live.status_changed.connect(self._live_status.setText)
        live.capture_changed.connect(self._update_action_states)
        self._on_live_running(live.is_running)

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

        # Inline download progress (shown only while a sync runs): the status
        # text (file, MB, rate, time left) on its own line so it never
        # squeezes the bar.
        self._progress_row = QWidget()
        progress_layout = QVBoxLayout(self._progress_row)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        progress_layout.setSpacing(4)
        self._progress_label = QLabel("")
        progress_layout.addWidget(self._progress_label)
        bar_row = QHBoxLayout()
        self._progress_bar = QProgressBar()
        bar_row.addWidget(self._progress_bar, stretch=1)
        self._btn_cancel = QPushButton("Cancel")
        self._btn_cancel.clicked.connect(self._on_cancel)
        bar_row.addWidget(self._btn_cancel)
        progress_layout.addLayout(bar_row)
        self._progress_row.setVisible(False)
        root.addWidget(self._progress_row)

        # Live datalog: follow the trip the WiCAN is recording, as it records.
        live_box = QGroupBox("Live Datalog")
        live_layout = QVBoxLayout(live_box)
        live_row = QHBoxLayout()
        self._btn_live = QPushButton("Start Live Datalog")
        self._btn_live.setMinimumHeight(32)
        self._btn_live.clicked.connect(self._on_live_toggle)
        live_row.addWidget(self._btn_live)
        self._btn_trail = QPushButton("Open in MegaLogViewerHD")
        self._btn_trail.setToolTip(
            "Follow the live capture in MegaLogViewerHD (Trail Live File mode)"
        )
        self._btn_trail.clicked.connect(self._on_trail)
        live_row.addWidget(self._btn_trail)
        live_row.addStretch()
        live_layout.addLayout(live_row)
        self._live_status = QLabel("")
        self._live_status.setWordWrap(True)
        live_layout.addWidget(self._live_status)
        self._chk_auto_trail = QCheckBox(
            "Open MegaLogViewerHD automatically when a capture starts"
        )
        self._chk_auto_trail.setChecked(
            self._main_window.settings.get_live_datalog_auto_trail()
        )
        self._chk_auto_trail.toggled.connect(
            self._main_window.settings.set_live_datalog_auto_trail
        )
        live_layout.addWidget(self._chk_auto_trail)
        root.addWidget(live_box)

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
        if self._ecu_busy or self._main_window.wican_live_datalog.is_running:
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

    def set_ecu_link(self, linked: bool):
        """The ECU window holds a WiCAN session (it reserves the bus, pausing
        the device's trip) — hold the live datalog start."""
        self._ecu_link = bool(linked)
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

    def _on_progress(self, done: int, total: int, name: str, rate_bps: float):
        """KiB units keep the range far under QProgressBar's int32 ceiling."""
        if total <= 0:
            return  # nothing to download: stays indeterminate until the end
        done = min(done, total)
        self._progress_bar.setRange(0, max(1, total // 1024))
        self._progress_bar.setValue(done // 1024)
        mb_total = total / (1024 * 1024)
        if name:
            mb_done = done / (1024 * 1024)
            text = f"Downloading {name}...  ({mb_done:.1f} of {mb_total:.1f} MB)"
            if rate_bps > 0:
                text += f"  ·  {format_rate(rate_bps)} avg"
                if done < total:
                    text += f"  ·  {format_time_left((total - done) / rate_bps)}"
            self._progress_label.setText(text)
        else:
            self._progress_label.setText(
                f"Downloading trip logs ({mb_total:.1f} MB)..."
            )

    def _on_cleanup_status(self, text: str):
        """Delete-after-download pass (issue 112): no byte total, so indeterminate."""
        self._progress_bar.setRange(0, 0)
        self._progress_label.setText(text)

    # --- actions --------------------------------------------------------------

    def _on_cancel(self):
        """Non-blocking: the worker exits at the next chunk; completed files
        remain and the next run re-fetches only what is missing."""
        self._progress_label.setText("Cancelling...")
        self._btn_cancel.setEnabled(False)
        self._main_window.wican_log_sync.cancel()

    def _on_live_toggle(self):
        live = self._main_window.wican_live_datalog
        if live.is_running:
            self._btn_live.setEnabled(False)  # re-enabled by running_changed
            live.stop()
            return
        if self._ecu_busy or self._ecu_link:
            return  # gated in _update_action_states; backstop only
        if not live.start():
            self._live_status.setText("Could not start (is a WiCAN configured?)")
        self._update_action_states()

    def _on_live_running(self, running: bool):
        self._btn_live.setText("Stop Live Datalog" if running else "Start Live Datalog")
        self._update_action_states()

    def _on_trail(self):
        if not self._main_window.wican_live_datalog.trail_in_mlv():
            self._live_status.setText("MegaLogViewerHD is not installed")

    def _on_open_folder(self):
        logs_dir = Path(self._main_window.settings.get_logs_directory())
        logs_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(logs_dir)))

    # --- state ----------------------------------------------------------------

    def _update_action_states(self, *_args):
        sync = self._main_window.wican_log_sync
        is_wican = self._main_window.settings.is_wican_adapter()

        live = self._main_window.wican_live_datalog
        self._btn_download.setEnabled(
            is_wican
            and not sync.is_running
            and not self._ecu_busy
            and not live.is_running
        )
        settings = self._main_window.settings
        tip = "Download every new trip log from the WiCAN's SD card"
        if settings.get_wican_delete_logs_after_download():
            tip += (
                ", then delete the verified ones from the card (keeping the "
                f"newest {settings.get_wican_keep_newest_logs()})"
            )
        if is_wican and self._ecu_busy:
            tip = ECU_BUSY_TIP
        elif is_wican and live.is_running:
            tip = LIVE_RUNNING_TIP
        self._btn_download.setToolTip(tip)

        if live.is_running:
            self._btn_live.setEnabled(True)  # it must always be stoppable
            self._btn_live.setToolTip("")
        else:
            live_blocked = self._ecu_busy or self._ecu_link or sync.is_running
            self._btn_live.setEnabled(is_wican and not live_blocked)
            if self._ecu_busy:
                live_tip = ECU_BUSY_TIP
            elif self._ecu_link:
                live_tip = ECU_LINK_TIP
            elif sync.is_running:
                live_tip = "Wait for the trip-log download to finish"
            else:
                live_tip = (
                    "Follow the trip the WiCAN is recording, live. Captures go "
                    "to the live folder inside the logs folder."
                )
            self._btn_live.setToolTip(live_tip)
        self._btn_trail.setEnabled(bool(live.current_capture))
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
