"""Background WiCAN trip-log sync owner (issue #83).

One :class:`WiCANLogSync` instance is owned by the main window; the Trip Logs
window drives it, so at most one sync runs at a time (state has ONE owner).
The download itself is pure HTTP against the WiCAN's port-80 csv_logger
endpoints — fully decoupled from the CAN bus / SLCAN session / ECU: it never
opens an ECU connection and works whether the car is on or off.

Besides the download it owns the read-only device *inventory* (list + status
+ local classification, :meth:`WiCANLogSync.refresh_inventory`) used by the
Trip Logs table and the startup new-log check
(:meth:`WiCANLogSync.schedule_auto_check`): the check never downloads by
itself — when new logs exist it emits ``new_logs_available`` and the main
window asks the user; a confirmed download runs through the Trip Logs window
like any manual one (single download path).

Product decision (2026-07-10): the WHOLE feature is active only while the
WiCAN adapter is selected — :meth:`WiCANLogSync.start` is a no-op with
Tactrix/J2534, and the Trip Logs entry points (menu item + toolbar icon) are
hidden then. (The underlying client would work regardless of adapter; this is
a deliberate UX gate, not a technical constraint.)

Quiet by contract: a sleeping/unreachable WiCAN (car off is the normal case)
is an info line in the Activity Log, never a dialog. Every HTTP call runs on
a worker QThread; the GUI thread is never blocked.
"""

import logging
import math
import time
from typing import Optional

from PySide6.QtCore import QObject, QThread, Qt, QTimer, Signal

logger = logging.getLogger(__name__)

#: Progress emits are throttled to this period (a 27 MB trip log arrives in
#: ~420 chunks; flooding queued cross-thread signals helps nobody).
_PROGRESS_MIN_INTERVAL_S = 0.1

#: Startup new-log check waits this long after launch so it never competes
#: with session restore / ROM parsing for the first paint.
_AUTO_CHECK_DELAY_MS = 3000

#: Rough sustained rate of the WiCAN serving CSVs from SD over HTTP (ESP32
#: class hardware). Only feeds the human-facing time estimate — being
#: conservative beats being optimistic; tune from real transfers if needed.
_EST_DOWNLOAD_BYTES_PER_S = 400 * 1024


def format_size(num_bytes: int) -> str:
    """Human size for status lines and prompts: ``512 KB`` / ``12.4 MB``."""
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    return f"{max(1, round(num_bytes / 1024))} KB"


def estimate_download_text(total_bytes: int) -> str:
    """Rough human estimate of a download's duration ("about 15 seconds")."""
    seconds = math.ceil(total_bytes / _EST_DOWNLOAD_BYTES_PER_S)
    if seconds < 10:
        return "under 10 seconds"
    if seconds < 90:
        return f"about {5 * math.ceil(seconds / 5)} seconds"
    return f"about {math.ceil(seconds / 60)} minutes"


class _DeviceWorker(QObject):
    """Shared scaffolding for one-shot WiCAN HTTP jobs off the GUI thread."""

    error = Signal(str)

    def __init__(
        self,
        host: str,
        device_id: str,
        dest_dir: str,
        http_port: Optional[int] = None,
    ):
        super().__init__()
        self._host = host
        self._device_id = device_id
        self._dest_dir = dest_dir
        self._http_port = http_port  # None -> the client's device default

    def _make_client(self):
        """Resolve the device's current IP and build the log client.

        Shared re-resolve fallback policy. No settings write-back here — this
        runs off the GUI thread; the ECU connect path stays the one place that
        caches a fresh IP.
        """
        from src.ecu import wican_discovery
        from src.ecu.wican_logs import WiCANLogClient

        host = wican_discovery.resolve_host_with_fallback(self._device_id, self._host)
        kwargs = {} if self._http_port is None else {"http_port": self._http_port}
        return WiCANLogClient(host, **kwargs)


class _LogSyncWorker(_DeviceWorker):
    """Runs one sync (resolve host → list → download-new) off the GUI thread."""

    finished = Signal(object)  # LogSyncResult
    #: (bytes_done, bytes_total, filename currently transferring)
    progress = Signal(int, int, str)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_progress_emit = 0.0

    def run(self):
        try:
            result = self._make_client().download_new(
                self._dest_dir,
                abort_cb=self._abort_requested,
                progress_cb=self._on_progress,
            )
            self.finished.emit(result)
        except Exception as e:
            # Device asleep/unreachable is a normal condition — the owner logs
            # it quietly; nothing may raise out of a Qt worker slot.
            self.error.emit(str(e))

    def _on_progress(self, done: int, total: int, name: str):
        """Forward download progress as a signal, throttled to ~10 Hz.

        Boundary emits (run start, file completion) always pass so the display
        never misses the determinate total or the final 100%.
        """
        now = time.monotonic()
        boundary = done == 0 or done >= total
        if not boundary and now - self._last_progress_emit < _PROGRESS_MIN_INTERVAL_S:
            return
        self._last_progress_emit = now
        self.progress.emit(done, total, name)

    @staticmethod
    def _abort_requested() -> bool:
        thread = QThread.currentThread()
        return bool(thread and thread.isInterruptionRequested())


class _InventoryWorker(_DeviceWorker):
    """Runs one read-only device inventory (list + classify) off the GUI thread."""

    finished = Signal(object)  # list[LogInventoryEntry], device order

    def run(self):
        try:
            self.finished.emit(self._make_client().classify(self._dest_dir))
        except Exception as e:
            # Device asleep/unreachable is a normal condition (car off).
            self.error.emit(str(e))


class WiCANLogSync(QObject):
    """Single owner of the background trip-log download state."""

    #: True while a sync runs (drives the Trip Logs window's download UI and
    #: the ECU window's operation locks).
    running_changed = Signal(bool)

    #: Byte progress of the running sync: (bytes_done, bytes_total, filename).
    #: Fires (0, total, "") as soon as the plan is known — a consumer can show
    #: a determinate bar before the first byte — then ~10 Hz during transfers.
    progress_changed = Signal(int, int, str)

    #: True while an inventory check runs (drives the Refresh button).
    #: The False emit comes from the check thread's QUEUED cleanup — i.e.
    #: strictly after inventory_ready/inventory_failed — because is_checking
    #: stays True until then; a consumer that re-renders only on the ready/
    #: failed signals would otherwise never see the button re-enable.
    checking_changed = Signal(bool)

    #: Result of a :meth:`refresh_inventory` run: list[LogInventoryEntry] in
    #: device order (fires for manual and auto checks alike).
    inventory_ready = Signal(object)

    #: A :meth:`refresh_inventory` run could not reach the device (car off is
    #: the normal case — consumers show a quiet status line, never a dialog).
    inventory_failed = Signal(str)

    #: The STARTUP check found new logs: (count, total_bytes). Only ever fires
    #: for :meth:`schedule_auto_check` runs — the main window prompts the user
    #: and a confirmed download goes through the Trip Logs window.
    new_logs_available = Signal(int, int)

    def __init__(self, settings, parent=None, *, http_port: Optional[int] = None):
        """``http_port`` overrides the client's device default (tests only)."""
        super().__init__(parent)
        self._settings = settings
        self._http_port = http_port
        self._thread = None
        self._worker = None
        self._check_thread = None
        self._check_worker = None
        self._auto_check_pending = False

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    @property
    def is_checking(self) -> bool:
        # Non-None until _on_check_thread_finished, NOT isRunning(): the
        # ready/failed signals land while the thread is still winding down,
        # and a new check must not start (and overwrite the refs) before the
        # old one's queued cleanup has run.
        return self._check_thread is not None

    def _resolve_config(self) -> Optional[tuple]:
        """``(host, device_id)`` when the WiCAN feature is usable, else None.

        The single gate shared by downloads and inventory checks: the feature
        is WiCAN-only by product decision, and needs a host or an mDNS
        identity to talk to.
        """
        if not self._settings.is_wican_adapter():
            logger.debug("WiCAN log sync skipped: WiCAN adapter not selected")
            return None
        host = (self._settings.get_wican_host() or "").strip()
        device_id = (self._settings.get_wican_device_id() or "").strip()
        if not host and not device_id:
            logger.debug("WiCAN log sync skipped: no host/identity configured")
            return None
        return host, device_id

    def start(self) -> bool:
        """Kick a background sync.

        Returns False (a complete no-op) when one is already running, when the
        selected ECU adapter is not the WiCAN (feature is WiCAN-only by product
        decision), or when no WiCAN host/identity is configured.
        """
        if self.is_running:
            return False
        config = self._resolve_config()
        if config is None:
            return False
        host, device_id = config

        worker = _LogSyncWorker(
            host,
            device_id,
            self._settings.get_logs_directory(),
            http_port=self._http_port,
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        self._thread = thread
        self._worker = worker

        # Bound-method receivers (this QObject lives on the GUI thread), so the
        # queued slots run on the GUI thread — same rationale as the ECU
        # window's flash-worker wiring.
        worker.finished.connect(self._on_finished, Qt.QueuedConnection)
        worker.error.connect(self._on_error, Qt.QueuedConnection)
        worker.progress.connect(self._on_progress, Qt.QueuedConnection)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(self._on_thread_finished, Qt.QueuedConnection)

        logger.info("Checking WiCAN for new trip logs...")
        thread.start()
        self.running_changed.emit(True)
        return True

    def refresh_inventory(self) -> bool:
        """Kick a background device inventory (list + classify, no download).

        The result arrives as ``inventory_ready`` (or ``inventory_failed``).
        Returns False without doing anything when a check or a download is
        already running (a finished download refreshes anyway, and the two
        would contend for the device's SD/WiFi), or when the WiCAN feature is
        not usable (:meth:`_resolve_config`).
        """
        if self.is_checking or self.is_running:
            return False
        config = self._resolve_config()
        if config is None:
            return False
        host, device_id = config

        worker = _InventoryWorker(
            host,
            device_id,
            self._settings.get_logs_directory(),
            http_port=self._http_port,
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        self._check_thread = thread
        self._check_worker = worker

        worker.finished.connect(self._on_inventory_finished, Qt.QueuedConnection)
        worker.error.connect(self._on_inventory_error, Qt.QueuedConnection)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(self._on_check_thread_finished, Qt.QueuedConnection)

        thread.start()
        self.checking_changed.emit(True)
        return True

    def schedule_auto_check(self, delay_ms: Optional[int] = None) -> bool:
        """Arm the startup new-log check (the auto-download feature's front).

        A few seconds after launch (*delay_ms* overrides, tests pass 0) the
        device inventory runs quietly; when it finds new logs,
        ``new_logs_available(count, total_bytes)`` fires so the main window
        can ask the user — nothing is EVER downloaded without confirmation.
        Honors the Settings toggle and the WiCAN-adapter gate; returns whether
        the check was armed.
        """
        if not self._settings.get_wican_auto_download_logs():
            logger.debug("WiCAN startup log check disabled in settings")
            return False
        if self._resolve_config() is None:
            return False
        if delay_ms is None:
            delay_ms = _AUTO_CHECK_DELAY_MS
        QTimer.singleShot(delay_ms, self._run_auto_check)
        return True

    def _run_auto_check(self):
        self._auto_check_pending = True
        if not self.refresh_inventory():
            self._auto_check_pending = False

    def cancel(self):
        """Request a running sync to stop, without blocking (Cancel button).

        Sets the interruption flag and returns at once; the worker exits at the
        next chunk/file boundary and the normal ``finished`` →
        ``running_changed(False)`` teardown follows. Completed files remain
        (idempotent re-run); a partial ``.part`` file is cleaned up. No-op when
        nothing runs.
        """
        thread = self._thread
        if thread is not None:
            logger.info("Cancelling WiCAN trip-log sync...")
            thread.requestInterruption()

    def shutdown(self, timeout_ms: int = 15000):
        """Stop a running sync (and inventory check) before app exit.

        The download worker polls the interruption flag between files and
        between 64 KiB chunks, so this returns quickly (worst case one socket
        timeout); the atomic ``.part`` contract means an interrupted file never
        looks complete and is simply re-fetched next run. An inventory check is
        two short GETs — it is simply joined.
        """
        for thread in (self._thread, self._check_thread):
            if thread is None:
                continue
            thread.requestInterruption()
            thread.quit()
            if thread.isRunning() and not thread.wait(timeout_ms):
                logger.warning("WiCAN log sync did not stop within %d ms", timeout_ms)

    # --- worker signal handlers (GUI thread) --------------------------------

    def _on_finished(self, result):
        if result.downloaded:
            logger.info(
                "Downloaded %d new trip log(s) to %s (%d skipped)",
                len(result.downloaded),
                self._settings.get_logs_directory(),
                len(result.skipped),
            )
        else:
            logger.info("No new WiCAN trip logs")

    def _on_error(self, message: str):
        # Sleeping/unreachable device is normal (car off): quiet info, no dialog.
        logger.info("WiCAN trip-log check skipped: %s", message)

    def _on_progress(self, done: int, total: int, name: str):
        self.progress_changed.emit(done, total, name)

    @staticmethod
    def _dispose(thread, worker):
        """Common (thread, worker) teardown once a run's QThread has finished."""
        if worker is not None:
            worker.deleteLater()
        if thread is not None:
            thread.deleteLater()

    def _on_thread_finished(self):
        thread, worker = self._thread, self._worker
        self._thread = None
        self._worker = None
        self._dispose(thread, worker)
        self.running_changed.emit(False)

    # --- inventory-check signal handlers (GUI thread) -----------------------

    def _on_inventory_finished(self, entries):
        from src.ecu.wican_logs import STATUS_NEW

        auto = self._auto_check_pending
        self._auto_check_pending = False
        self.inventory_ready.emit(entries)

        new = [e for e in entries if e.status == STATUS_NEW]
        if not auto:
            return
        if not new:
            logger.info("No new WiCAN trip logs")
            return
        total = sum(e.log.size for e in new)
        logger.info(
            "%d new trip log(s) on the WiCAN (%s)", len(new), format_size(total)
        )
        self.new_logs_available.emit(len(new), total)

    def _on_inventory_error(self, message: str):
        self._auto_check_pending = False
        # Sleeping/unreachable device is normal (car off): quiet info, no dialog.
        logger.info("WiCAN trip-log check skipped: %s", message)
        self.inventory_failed.emit(message)

    def _on_check_thread_finished(self):
        thread, worker = self._check_thread, self._check_worker
        self._check_thread = None
        self._check_worker = None
        self._dispose(thread, worker)
        self.checking_changed.emit(False)
