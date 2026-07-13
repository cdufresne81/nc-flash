"""Background WiCAN live-datalog stream owner (fw issue #3).

One :class:`WiCANLiveDatalog` instance is owned by the main window; the ECU
window's "Live Datalog" toggle starts/stops it, so at most one live stream runs
at a time (state has ONE owner). The stream itself is a pure raw-TCP tail of the
device's port-35002 NCDLv1 listener (see :mod:`src.ecu.wican_stream`); the trip
lifecycle around it goes through the SHARED per-host
:class:`~src.ecu.wican_config.WiCANDatalogClient`:

* **Start** = a NEW leased trip: un-park the bus (lifting any ECU-session
  reservation for the stream's duration), ``op=start&rotate=1&lease_ms`` (fresh
  file, firmware dead-man armed), keepalive renews the lease every tick.
* **Stop (user)** = the device stops logging too: take the silent hold (park,
  its own dead-man), then ``op=auto`` — the device stays quiet until the next
  trip, or :meth:`WiCANLiveDatalog.dispose` at app close restores autonomous
  (ignition-follow) trip logging. A stream ERROR instead restores AUTO at once.
* **Stop at the DEVICE (web-UI Stop Trip)** = the operator wins: the keepalive's
  ``op=renew`` 409s, the stream ends cleanly, and the host neither re-parks the
  mode nor restores AUTO — the web Stop stands (no silent hold either).
* **Host crash** anywhere: the firmware reapers restore AUTO on their own.

Each ``#session`` opens a fresh local file
``{logs_directory}/live/live_<YYYYmmdd_HHMMSS>.csv``: the header line is written
first, then every row is appended and **flushed** so MegaLogViewerHD (or any
tailer) sees it live. ``#close`` closes the file; a later ``#session`` opens the
next one. The first capture of each run offers a one-click "Trail in
MegaLogViewerHD?" (see :mod:`src.ui.mlv_trail`); rotated sessions never
re-prompt — the Activity Log names their files instead. The offer is a
NON-MODAL dialog: an app-modal box parented to the main window froze every
window when the user was working in the ECU window (field incident,
2026-07-11 — the dialog opened UNDER the active window, so all input was
blocked with nothing visible to dismiss). Accepting trails the NEWEST open
capture, not the path frozen at offer time — the first capture can rotate
away (0 rows) milliseconds after it opens.

Product decision: the feature is active only while the WiCAN adapter is selected
— :meth:`WiCANLiveDatalog.start` is a no-op with Tactrix/J2534, and the ECU
window hides its button then. Firmware without the live stream degrades quietly
to a status line (:class:`~src.ecu.wican_stream.WiCANStreamUnsupported`), never a
dialog. Every socket read and file write runs on a worker QThread; the GUI
thread is never blocked.
"""

import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QThread, Qt, Signal

logger = logging.getLogger(__name__)

#: Row-count status cadence: emit at most one status update per this many rows
#: or per this many seconds, whichever comes first (never one signal per row).
_STATUS_EVERY_ROWS = 50
_STATUS_EVERY_S = 1.0


def live_capture_dir(logs_dir) -> Path:
    """The folder live captures land in: ``{logs_directory}/live``.

    THE single derivation — the worker writes here, and every Activity Log
    message that names the destination goes through it.
    """
    return Path(logs_dir) / "live"


class _LiveStreamWorker(QObject):
    """Runs one live stream (resolve host -> connect -> read loop) off the GUI
    thread, writing each session to its own local ``.csv`` as rows arrive."""

    status = Signal(str)  # short human status ("streaming (N rows)…", …)
    file_opened = Signal(str)  # local path of the file a #session just opened
    unsupported = Signal(str)  # WiCANStreamUnsupported: fw without live stream
    error = Signal(str)  # unexpected disconnect / other failure
    finished = Signal()  # clean stop (stop() requested)
    # The shared per-host WiCANDatalogClient driving this run's trip lifecycle —
    # emitted before the blocking read loop so the owner can release the silent
    # hold at app close even if this worker is later terminated.
    trip_client = Signal(object)

    def __init__(
        self,
        host: str,
        device_id: str,
        logs_dir: str,
        *,
        port: Optional[int] = None,
    ):
        super().__init__()
        self._host = host
        self._device_id = device_id
        self._live_dir = live_capture_dir(logs_dir)
        self._port = port  # None -> the client's device default
        self._client = None
        self._trip = None  # shared WiCANDatalogClient once the trip began
        self._stop_requested = False
        self._external_stop = False  # web-UI Stop Trip ended the trip, not our user
        self._lock = threading.Lock()
        self._idle_logged = False
        # per-session file state (worker thread only)
        self._file = None
        self._file_path = ""
        self._row_count = 0
        self._last_status_rows = 0
        self._last_status_t = 0.0

    def run(self):
        from src.ecu import wican_discovery
        from src.ecu.wican_stream import (
            WiCANLiveStreamClient,
            WiCANStreamError,
            WiCANStreamUnsupported,
        )

        try:
            # Shared re-resolve fallback policy — no settings write-back here
            # (off the GUI thread); the ECU connect path is the one place that
            # caches a fresh IP.
            host = wican_discovery.resolve_host_with_fallback(
                self._device_id, self._host
            )
            kwargs = {} if self._port is None else {"port": self._port}
            client = WiCANLiveStreamClient(host, **kwargs)
            with self._lock:
                if self._stop_requested:
                    self.finished.emit()
                    return
                self._client = client

            self.status.emit("connecting…")
            client.connect()
            # The banner proved the firmware speaks NCDLv1; now start a NEW leased
            # trip (un-park the bus, rotate to a fresh file, arm the csv dead-man).
            # AFTER connect() so a fw-without-stream never leaves a trip running,
            # and while we already listen, so the fresh #session brings its header.
            from src.ecu.wican_config import get_datalog_client

            trip = get_datalog_client(host)
            self.trip_client.emit(trip)
            with self._lock:
                self._trip = trip
            self.status.emit("starting a new trip…")
            trip.begin_live_trip(on_external_stop=self._on_trip_external_stop)
            self.status.emit("waiting for session…")
            client.run(self._on_event)
            self.finished.emit()
        except WiCANStreamUnsupported as e:
            # A user stop during connect() (socket closed under the banner read)
            # surfaces as "no banner" — that is a clean stop, not unsupported
            # firmware. Only report unsupported when the user did NOT ask to stop.
            if self._stop_was_requested():
                self.finished.emit()
            else:
                self.unsupported.emit(str(e))
        except WiCANStreamError as e:
            if self._stop_was_requested():
                self.finished.emit()
            else:
                self.error.emit(str(e))
        except Exception as e:  # nothing may raise out of a Qt worker slot
            if self._stop_was_requested():
                self.finished.emit()
            else:
                self.error.emit(str(e))
        finally:
            self._close_file()
            # Always release the stream socket. run() does not close it on an
            # error path (only stop() does), and the device's 35002 listener is
            # single-client — a leaked-open socket blocks the next connect until
            # GC. stop() is idempotent, so this is harmless on the clean path.
            with self._lock:
                client, self._client = self._client, None
            if client is not None:
                client.stop()
            self._teardown_trip()

    def _teardown_trip(self):
        """End the leased trip with the user's intent: STOP parks, a failure frees.

        A user stop means "the device stops logging too" — take the silent hold
        FIRST (park while the manual trip still owns the mode; ``op=auto`` right
        after fixes both mode and the restore snapshot, so no instant of un-parked
        AUTO can open a stub trip file). A stream error/unsupported end is not a
        user decision: just restore AUTO. An EXTERNAL stop (web-UI Stop Trip) is
        the device operator's decision: no silent hold, and end_live_trip itself
        skips the mode ops so their Stop stands. Every op soft-degrades; if the
        device is unreachable the firmware csv-lease reaper restores AUTO by
        itself.
        """
        with self._lock:
            trip, self._trip = self._trip, None
            user_stop = self._stop_requested and not self._external_stop
        if trip is None:
            return
        try:
            if user_stop:
                trip.hold_silent()
            trip.end_live_trip()
        except Exception as exc:  # belt+braces: the client itself never raises
            logger.warning(
                "live-trip teardown failed (the device reaper restores auto): %s",
                exc,
            )

    def request_stop(self):
        """Interrupt the stream (thread-safe; callable before run() starts)."""
        with self._lock:
            self._stop_requested = True
            client = self._client
        if client is not None:
            client.stop()

    def _on_trip_external_stop(self):
        """The trip was stopped at the device (web-UI Stop Trip).

        Runs on the trip client's keepalive thread (cross-thread signal emits are
        queued to the GUI). End the stream like a user stop — but flag it external
        FIRST so teardown takes no silent hold and leaves the device's mode to the
        operator who set it.
        """
        with self._lock:
            self._external_stop = True
        logger.info(
            "WiCAN live datalog: trip stopped from the device (web UI); "
            "ending the stream"
        )
        self.status.emit("trip stopped from the device")
        self.request_stop()

    def _stop_was_requested(self) -> bool:
        with self._lock:
            return self._stop_requested

    # --- stream event handling (worker thread) ------------------------------

    def _on_event(self, ev):
        from src.ecu.wican_stream import (
            KIND_CLOSE,
            KIND_DROP,
            KIND_HEADER,
            KIND_HELLO,
            KIND_IDLE,
            KIND_NOHDR,
            KIND_ROW,
            KIND_SESSION,
        )

        # Backstop: honour a Qt interruption even if request_stop() was missed.
        thread = QThread.currentThread()
        if thread is not None and thread.isInterruptionRequested():
            self.request_stop()
            return

        if ev.kind == KIND_HELLO:
            logger.info("WiCAN live datalog connected (fw %s)", ev.fw or "?")
        elif ev.kind == KIND_IDLE:
            if not self._idle_logged:
                # Once per run: connecting with the device idle is the normal
                # case (the datalogger only records while a session runs) and
                # must not read as "streaming".
                self._idle_logged = True
                logger.info(
                    "WiCAN live datalog: the device has no datalog session "
                    "open; waiting (nothing is recorded until one starts)"
                )
            self.status.emit("waiting for session…")
        elif ev.kind == KIND_SESSION:
            self._open_file(ev)
        elif ev.kind == KIND_HEADER:
            self._write_line(ev.line)
        elif ev.kind == KIND_NOHDR:
            logger.warning(
                "WiCAN live datalog joined mid-session without a header copy; "
                "rows will have no column names until the next session"
            )
        elif ev.kind == KIND_ROW:
            self._write_line(ev.line)
            self._row_count += 1
            self._maybe_emit_progress()
        elif ev.kind == KIND_DROP:
            logger.warning(
                "WiCAN live datalog dropped %d row(s) (device ring full)", ev.count
            )
        elif ev.kind == KIND_CLOSE:
            self._close_file()
            self.status.emit("session closed; waiting for next…")

    def _open_file(self, ev):
        self._close_file()
        self._live_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self._live_dir / f"live_{ts}.csv"
        # Rotation (#close + #session) can land inside the same second; never
        # clobber the file we just closed.
        n = 2
        while path.exists():
            path = self._live_dir / f"live_{ts}-{n}.csv"
            n += 1
        self._file = open(path, "w", encoding="utf-8", newline="")
        self._file_path = str(path)
        self._row_count = 0
        self._last_status_rows = 0
        self._last_status_t = time.monotonic()
        logger.info(
            "WiCAN live datalog recording -> %s (device %s, cols=%d)",
            path,
            ev.file or "?",
            ev.cols,
        )
        self.file_opened.emit(self._file_path)
        self.status.emit("streaming (0 rows)…")

    def _write_line(self, line: str):
        if self._file is None:
            logger.warning("WiCAN live datalog row before a session; ignoring")
            return
        self._file.write(line + "\n")
        self._file.flush()  # per-row flush is the point: MLV tails this file

    def _maybe_emit_progress(self):
        now = time.monotonic()
        if (self._row_count - self._last_status_rows) >= _STATUS_EVERY_ROWS or (
            now - self._last_status_t
        ) >= _STATUS_EVERY_S:
            self._last_status_rows = self._row_count
            self._last_status_t = now
            self.status.emit(f"streaming ({self._row_count} rows)…")

    def _close_file(self):
        if self._file is not None:
            try:
                self._file.flush()
                self._file.close()
            except OSError:
                pass
            finally:
                # THE "here is your file" line — full path + rows, so the
                # Activity Log always says where a capture can be found.
                logger.info(
                    "WiCAN live datalog capture saved: %s (%d rows)",
                    self._file_path,
                    self._row_count,
                )
                self._file = None
                self._file_path = ""


class WiCANLiveDatalog(QObject):
    """Single owner of the background live-datalog stream state."""

    #: True while a live stream runs (drives the ECU-window button text/state).
    running_changed = Signal(bool)
    #: Short human status ("connecting…", "streaming (N rows)…", error text, …).
    status_changed = Signal(str)
    #: Current local file path being written, or "" when none is open.
    file_changed = Signal(str)

    def __init__(self, settings, parent=None, *, port: Optional[int] = None):
        """``port`` overrides the stream client's device default (tests only)."""
        super().__init__(parent)
        self._settings = settings
        self._port = port
        self._thread = None
        self._worker = None
        # Whether the current/last run opened at least one capture file —
        # drives the honest "nothing was captured" stop message.
        self._captured_any = False
        # Offer the MLV trail once per run, on the first capture only —
        # rotated sessions must not pop dialogs while the user is driving.
        self._trail_offered = False
        # The open (non-modal) trail-offer dialog, if any, and the newest
        # capture path — the dialog launches THIS path when accepted, so an
        # instantly-rotated first capture never sends MLV to a dead 0-row file.
        self._trail_box = None
        self._latest_capture = ""
        # Shared per-host WiCANDatalogClient of the most recent trip (worker-emitted).
        # Kept ONLY so dispose() can release the silent hold at app close; all other
        # trip choreography lives in the worker/client.
        self._trip_client = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(self) -> bool:
        """Start the live stream on a worker thread.

        Returns False (a complete no-op) when one is already running, when the
        selected ECU adapter is not the WiCAN (feature is WiCAN-only by product
        decision), or when no WiCAN host/identity is configured.
        """
        if self.is_running:
            return False
        if not self._settings.is_wican_adapter():
            logger.debug("WiCAN live datalog skipped: WiCAN adapter not selected")
            return False
        host = (self._settings.get_wican_host() or "").strip()
        device_id = (self._settings.get_wican_device_id() or "").strip()
        if not host and not device_id:
            logger.debug("WiCAN live datalog skipped: no host/identity configured")
            return False

        logs_dir = self._settings.get_logs_directory()
        worker = _LiveStreamWorker(
            host,
            device_id,
            logs_dir,
            port=self._port,
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        self._thread = thread
        self._worker = worker

        # Bound-method receivers (this QObject lives on the GUI thread), so the
        # queued slots run on the GUI thread — same rationale as the ECU
        # window's flash-worker wiring: a bare lambda would run in the worker
        # thread and mutate GUI state cross-thread.
        worker.status.connect(self._on_status, Qt.QueuedConnection)
        worker.trip_client.connect(self._on_trip_client, Qt.QueuedConnection)
        worker.file_opened.connect(self._on_file_opened, Qt.QueuedConnection)
        worker.unsupported.connect(self._on_unsupported, Qt.QueuedConnection)
        worker.error.connect(self._on_error, Qt.QueuedConnection)
        worker.finished.connect(self._on_finished, Qt.QueuedConnection)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        worker.unsupported.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(self._on_thread_finished, Qt.QueuedConnection)

        self._captured_any = False
        self._trail_offered = False
        logger.info(
            "Starting WiCAN live datalog stream (captures go to %s)...",
            live_capture_dir(logs_dir),
        )
        thread.start()
        self.running_changed.emit(True)
        return True

    def stop(self):
        """Stop a running stream (user toggle). Alias of :meth:`shutdown`.

        The worker's teardown takes the silent hold: a user stop also stops the
        DEVICE's logging (parked, dead-man armed) until the next trip, an ECU
        disconnect-free app close, or :meth:`dispose`.
        """
        self.shutdown()

    def dispose(self, timeout_ms: int = 5000):
        """App-close teardown: stop the stream AND give the device back.

        Beyond :meth:`shutdown`, releases the silent hold a previous stop took, so
        closing NC Flash always restores the device's autonomous trip logging
        (the user-facing contract). The HTTP release runs on a short-lived thread
        with a bounded join — app close must not hang on an unreachable device
        (whose firmware reaper restores AUTO by itself anyway).
        """
        self.shutdown(timeout_ms)
        client, self._trip_client = self._trip_client, None
        if client is None:
            return
        releaser = threading.Thread(
            target=client.release_trip_hold,
            name="wican-trip-hold-release",
            daemon=True,
        )
        releaser.start()
        releaser.join(timeout_ms / 1000.0)

    def shutdown(self, timeout_ms: int = 5000):
        """Stop a running stream (user toggle or app exit); safe when idle.

        ``request_stop()`` closes the socket so a blocking read returns
        promptly, then the thread is joined. As a last resort the thread is
        terminated on timeout: a per-row ``flush()`` on a dropped network share
        can block far longer than the socket stop, and app-close must never
        destroy a still-running QThread (Qt ``qFatal``). The live CSV is
        tail-only and flushed per row, so a torn final line is harmless.
        """
        thread, worker = self._thread, self._worker
        if thread is None:
            return  # never started (or already fully torn down)
        thread.requestInterruption()
        if worker is not None:
            worker.request_stop()
        thread.quit()
        if thread.isRunning() and not thread.wait(timeout_ms):
            logger.warning(
                "WiCAN live datalog did not stop within %d ms; terminating",
                timeout_ms,
            )
            thread.terminate()
            thread.wait()

    # --- worker signal handlers (GUI thread) --------------------------------

    def _on_status(self, text: str):
        self.status_changed.emit(text)

    def _on_trip_client(self, client):
        self._trip_client = client

    def _on_file_opened(self, path: str):
        self._captured_any = True
        self._latest_capture = path
        self.file_changed.emit(path)
        self._maybe_offer_trail(path)

    def _maybe_offer_trail(self, path: str):
        """Offer to trail this run's captures in MegaLogViewerHD, once per run.

        Silent no-op when MLV is not installed. The dialog is NON-MODAL and
        parented to the ACTIVE window: a `QMessageBox.question` here froze the
        whole app in the field — app-modal blocks input to every window, and
        parenting to the main window put the box UNDER the ECU window the user
        was actually in, so nothing visible could dismiss it. The worker keeps
        streaming behind the open dialog either way (queued slots).
        """
        from src.ui import mlv_trail

        if self._trail_offered:
            return
        self._trail_offered = True
        exe = mlv_trail.find_mlv()
        if exe is None:
            logger.debug("MegaLogViewerHD not installed; trail offer skipped")
            return
        self._show_trail_offer(path, exe)

    def _show_trail_offer(self, path: str, exe):
        from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

        parent = QApplication.activeWindow()
        if parent is None and isinstance(self.parent(), QWidget):
            parent = self.parent()
        box = QMessageBox(
            QMessageBox.Question,
            "Live Datalog",
            f"Trail in MegaLogViewerHD?\n\n{Path(path).name}",
            QMessageBox.Yes | QMessageBox.No,
            parent,
        )
        box.setDefaultButton(QMessageBox.Yes)
        box.setWindowModality(Qt.NonModal)
        box.finished.connect(lambda result: self._on_trail_answer(exe, result))
        self._trail_box = box
        box.show()
        box.raise_()
        box.activateWindow()

    def _on_trail_answer(self, exe, result: int):
        from PySide6.QtWidgets import QMessageBox

        box, self._trail_box = self._trail_box, None
        if box is not None:
            box.deleteLater()
        if result != int(QMessageBox.StandardButton.Yes):
            logger.info(
                "WiCAN live datalog: MLV trail declined; capture continues "
                "in the background"
            )
            return
        # Trail the NEWEST capture — the offered file may have rotated away
        # while the dialog sat open (the field run's first file lived 3 ms).
        path = self._latest_capture
        if not path:
            logger.info("WiCAN live datalog: no capture is open anymore; trail skipped")
            return
        from src.ui import mlv_trail

        mlv_trail.launch_trail(Path(path), exe)

    def _close_trail_offer(self):
        """Dismiss a still-open offer (run ended; its captures are done)."""
        box, self._trail_box = self._trail_box, None
        if box is not None:
            # Detach the answer slot first: close() emits finished, and a
            # run-end dismissal must not log itself as a user "declined".
            try:
                box.finished.disconnect()
            except (RuntimeError, TypeError):
                pass
            box.close()
            box.deleteLater()

    def _on_unsupported(self, message: str):
        # Firmware without the live stream: quiet status, never a dialog.
        # Detection is banner-based (no #hello NCDLv1), not a version compare —
        # so the status names the missing capability, not a firmware version.
        logger.info("WiCAN live datalog unavailable: %s", message)
        self.status_changed.emit("firmware without a live datalog stream")

    def _on_error(self, message: str):
        logger.info("WiCAN live datalog stopped: %s", message)
        self.status_changed.emit(f"stopped: {message}")

    def _on_finished(self):
        # Every capture already logged its own "capture saved: <path>" line;
        # here we say where they all live — or that there was nothing to save
        # (the field-reported confusion: connect, no session, stop, and the
        # log never said where anything went).
        if self._captured_any:
            logger.info(
                "WiCAN live datalog stopped; captures are in %s",
                live_capture_dir(self._settings.get_logs_directory()),
            )
        else:
            logger.info(
                "WiCAN live datalog stopped; the device never opened a "
                "datalog session, so nothing was captured"
            )
        self.status_changed.emit("stopped")

    def _on_thread_finished(self):
        thread, worker = self._thread, self._worker
        self._thread = None
        self._worker = None
        if worker is not None:
            worker.deleteLater()
        if thread is not None:
            thread.deleteLater()
        self._close_trail_offer()
        self._latest_capture = ""
        self.file_changed.emit("")
        self.running_changed.emit(False)
