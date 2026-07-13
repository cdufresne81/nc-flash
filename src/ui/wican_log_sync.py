"""Background WiCAN trip-log sync owner (issue #83).

One :class:`WiCANLogSync` instance is owned by the main window; the ECU
window's "Download Logs" button and the launch-time auto-download both go
through it, so at most one sync runs at a time (state has ONE owner). The
download itself is pure HTTP against the WiCAN's port-80 csv_logger endpoints
— fully decoupled from the CAN bus / SLCAN session / ECU: it never opens an
ECU connection and works whether the car is on or off.

Product decision (2026-07-10): the WHOLE feature is active only while the
WiCAN adapter is selected — :meth:`WiCANLogSync.start` is a no-op with
Tactrix/J2534, and the ECU window hides its button then. (The underlying
client would work regardless of adapter; this is a deliberate UX gate, not a
technical constraint.)

Quiet by contract: a sleeping/unreachable WiCAN (car off is the normal case)
is an info line in the Activity Log, never a dialog. Every HTTP call runs on
a worker QThread; the GUI thread is never blocked.
"""

import logging
import time
from typing import Optional

from PySide6.QtCore import QObject, QThread, Qt, Signal

logger = logging.getLogger(__name__)

#: Progress emits are throttled to this period (a 27 MB trip log arrives in
#: ~420 chunks; flooding queued cross-thread signals helps nobody).
_PROGRESS_MIN_INTERVAL_S = 0.1


class _LogSyncWorker(QObject):
    """Runs one sync (resolve host → list → download-new) off the GUI thread."""

    finished = Signal(object)  # LogSyncResult
    error = Signal(str)
    #: (bytes_done, bytes_total, filename currently transferring)
    progress = Signal(int, int, str)

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
        self._last_progress_emit = 0.0

    def run(self):
        try:
            from src.ecu import wican_discovery
            from src.ecu.wican_logs import WiCANLogClient

            # Shared re-resolve fallback policy. No settings write-back here —
            # this runs off the GUI thread; the ECU connect path stays the one
            # place that caches a fresh IP.
            host = wican_discovery.resolve_host_with_fallback(
                self._device_id, self._host
            )
            kwargs = {} if self._http_port is None else {"http_port": self._http_port}
            client = WiCANLogClient(host, **kwargs)

            result = client.download_new(
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


class WiCANLogSync(QObject):
    """Single owner of the background trip-log download state."""

    #: True while a sync runs (drives the Download Logs button state).
    running_changed = Signal(bool)

    #: Byte progress of the running sync: (bytes_done, bytes_total, filename).
    #: Fires (0, total, "") as soon as the plan is known — a consumer can show
    #: a determinate bar before the first byte — then ~10 Hz during transfers.
    progress_changed = Signal(int, int, str)

    def __init__(self, settings, parent=None, *, http_port: Optional[int] = None):
        """``http_port`` overrides the client's device default (tests only)."""
        super().__init__(parent)
        self._settings = settings
        self._http_port = http_port
        self._thread = None
        self._worker = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(self) -> bool:
        """Kick a background sync.

        Returns False (a complete no-op) when one is already running, when the
        selected ECU adapter is not the WiCAN (feature is WiCAN-only by product
        decision), or when no WiCAN host/identity is configured.
        """
        if self.is_running:
            return False
        if not self._settings.is_wican_adapter():
            logger.debug("WiCAN log sync skipped: WiCAN adapter not selected")
            return False
        host = (self._settings.get_wican_host() or "").strip()
        device_id = (self._settings.get_wican_device_id() or "").strip()
        if not host and not device_id:
            logger.debug("WiCAN log sync skipped: no host/identity configured")
            return False

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
        """Stop a running sync before app exit.

        The worker polls the interruption flag between files and between 64 KiB
        chunks, so this returns quickly (worst case one socket timeout); the
        atomic ``.part`` contract means an interrupted file never looks
        complete and is simply re-fetched next run.
        """
        thread = self._thread
        if thread is None:
            return
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

    def _on_thread_finished(self):
        thread, worker = self._thread, self._worker
        self._thread = None
        self._worker = None
        if worker is not None:
            worker.deleteLater()
        if thread is not None:
            thread.deleteLater()
        self.running_changed.emit(False)
