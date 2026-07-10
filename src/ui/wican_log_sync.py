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

from PySide6.QtCore import QObject, QThread, Qt, Signal

logger = logging.getLogger(__name__)

#: mDNS re-resolve budget — matches the ECU connect path's budget.
_RESOLVE_TIMEOUT_S = 3.0

#: The WiCAN's HTTP device-service port (module-level so tests can inject an
#: ephemeral fake-server port).
_HTTP_PORT = 80


class _LogSyncWorker(QObject):
    """Runs one sync (resolve host → list → download-new) off the GUI thread."""

    finished = Signal(object)  # LogSyncResult
    error = Signal(str)

    def __init__(self, host: str, device_id: str, dest_dir: str):
        super().__init__()
        self._host = host
        self._device_id = device_id
        self._dest_dir = dest_dir

    def run(self):
        try:
            from src.ecu.wican_logs import WiCANLogClient

            client = WiCANLogClient(self._resolve_host(), http_port=_HTTP_PORT)
            result = client.download_new(self._dest_dir, abort_cb=self._abort_requested)
            self.finished.emit(result)
        except Exception as e:
            # Device asleep/unreachable is a normal condition — the owner logs
            # it quietly; nothing may raise out of a Qt worker slot.
            self.error.emit(str(e))

    @staticmethod
    def _abort_requested() -> bool:
        thread = QThread.currentThread()
        return bool(thread and thread.isInterruptionRequested())

    def _resolve_host(self) -> str:
        """Re-resolve the stored device identity to its current IP (best-effort).

        Mirrors the ECU connect path's policy: no identity, discovery
        unavailable, or any failure falls back to the stored static host. No
        settings write-back here — this runs off the GUI thread; the connect
        path stays the one place that caches a fresh IP.
        """
        if not self._device_id:
            return self._host
        try:
            from src.ecu import wican_discovery

            resolved = wican_discovery.resolve_host_for_device_id(
                self._device_id, timeout_s=_RESOLVE_TIMEOUT_S
            )
        except Exception as e:
            logger.debug("WiCAN mDNS re-resolve failed (%s); using stored host", e)
            return self._host
        return resolved or self._host


class WiCANLogSync(QObject):
    """Single owner of the background trip-log download state."""

    #: True while a sync runs (drives the Download Logs button state).
    running_changed = Signal(bool)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self._settings = settings
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
        if self._settings.get_ecu_adapter() != "wican":
            logger.debug("WiCAN log sync skipped: WiCAN adapter not selected")
            return False
        host = (self._settings.get_wican_host() or "").strip()
        device_id = (self._settings.get_wican_device_id() or "").strip()
        if not host and not device_id:
            logger.debug("WiCAN log sync skipped: no host/identity configured")
            return False

        worker = _LogSyncWorker(host, device_id, self._settings.get_logs_directory())
        thread = QThread(self)
        worker.moveToThread(thread)
        self._thread = thread
        self._worker = worker

        # Bound-method receivers (this QObject lives on the GUI thread), so the
        # queued slots run on the GUI thread — same rationale as the ECU
        # window's flash-worker wiring.
        worker.finished.connect(self._on_finished, Qt.QueuedConnection)
        worker.error.connect(self._on_error, Qt.QueuedConnection)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(self._on_thread_finished, Qt.QueuedConnection)

        logger.info("Checking WiCAN for new trip logs...")
        thread.start()
        self.running_changed.emit(True)
        return True

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

    def _on_thread_finished(self):
        thread, worker = self._thread, self._worker
        self._thread = None
        self._worker = None
        if worker is not None:
            worker.deleteLater()
        if thread is not None:
            thread.deleteLater()
        self.running_changed.emit(False)
