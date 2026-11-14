"""Background WiCAN live-datalog stream owner (firmware issue #3).

One :class:`WiCANLiveDatalog` instance is owned by the main window; the Trip
Logs window's Live Datalog button starts/stops it, so at most one live stream
runs at a time (state has ONE owner). The stream is a pure raw-TCP tail of the
device's port-35002 NCDLv1 listener (see :mod:`src.ecu.wican_stream`).

**Tail only.** The device records trips on its own (it follows the ignition);
the stream follows whatever it records and never starts, stops or rotates a
trip. A stream touches no park/claim/lease state on the device.

Captures land in ``{logs_directory}/live/live_<YYYYmmdd_HHMMSS>.csv``: the CSV
header first, then every row appended and **flushed** so MegaLogViewerHD (or
any tailer) sees it at once. When the device opens a new trip file with the
same columns (a new trip, or a size rotation) the rows keep going into the SAME
local file, so a MegaLogViewerHD trailing it keeps following across trips. A
new local file starts only when the column set changes.

Each capture can open MegaLogViewerHD in Trail Live File mode on its own
(setting, on by default), once it holds enough rows for MLV to engage its
trail (it needs 50 and gives up after 60 s). Firmware without
the live stream degrades quietly to a status line, never a dialog. Every
socket read and file write runs on a worker QThread; the GUI thread is never
blocked.
"""

import logging
import threading
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QThread, Qt, Signal

from src.core.datalog_channels import RESERVED_NAMES
from src.core.live_csv import (  # noqa: F401 - TRAIL_MIN_ROWS re-exported
    TRAIL_MIN_ROWS,
    LiveCsvCapture,
    StatusThrottle,
    live_capture_dir,
)
from src.ui.live_values import EMIT_EVERY_S, LiveValues

logger = logging.getLogger(__name__)

#: Pause between reconnect attempts after a working stream drops.
_RECONNECT_DELAY_S = 2.0

#: Silence after which the link counts as dead. The device sends ``#ping``
#: every 3 s when it has nothing else to say, and a sleeping or rebooted
#: device never gets a reset to the PC, so silence is the only signal.
STREAM_IDLE_TIMEOUT_S = 15.0


class _LiveStreamWorker(QObject):
    """Runs one live stream (resolve host -> connect -> read loop) off the GUI
    thread, appending rows to a local ``.csv`` as they arrive."""

    status = Signal(str)  # short human status
    capture_opened = Signal(str)  # a new local capture file was started
    capture_ready = Signal(str)  # that capture reached TRAIL_MIN_ROWS rows
    unsupported = Signal(str)  # firmware without the live stream
    replaced = Signal()  # another client took the device's single stream slot
    values = Signal(dict)  # {channel name: value}, at most every EMIT_EVERY_S
    error = Signal(str)  # unexpected disconnect / other failure
    finished = Signal()  # clean stop (stop() requested)

    def __init__(
        self, host: str, device_id: str, logs_dir: str, *, port: Optional[int] = None
    ):
        super().__init__()
        self._host = host
        self._device_id = device_id
        self._capture = LiveCsvCapture(
            live_capture_dir(logs_dir), label="WiCAN live datalog"
        )
        self._port = port  # None -> the client's default (35002)
        self._client = None
        self._stop_requested = False
        self._lock = threading.Lock()
        # Worker-thread-only capture state.
        self._file_cols = -1
        self._session_cols = 0  # cols= of the latest #session
        self._last_ts: Optional[int] = None  # timestamp_ms of the last row written
        self._replaced = False  # the device gave our slot to a newer client
        self._rows_total = 0
        self._dropped_total = 0
        self._status_throttle = StatusThrottle()
        self._values_throttle = StatusThrottle(every_rows=10**9, every_s=EMIT_EVERY_S)
        # Column names the device sent for the current trip (None after
        # "#nohdr"). The capture may reuse an older header when only the column
        # count matches; that's fine for the CSV, but the live cursor needs the
        # names for sure, or it could read RPM from the LOAD column.
        self._trip_header: Optional[str] = None

    def run(self):
        from src.ecu import wican_discovery
        from src.ecu.wican_stream import (
            WiCANLiveStreamClient,
            WiCANStreamError,
            WiCANStreamUnsupported,
        )

        connected_once = False
        try:
            kwargs = {"read_timeout_s": STREAM_IDLE_TIMEOUT_S}
            if self._port is not None:
                kwargs["port"] = self._port
            host = None
            while True:
                if host is None:
                    # Looked up again only after a connect failed: a device
                    # that rebooted may be on a new DHCP address. Not before
                    # every attempt, since the lookup takes up to 3 s and
                    # can't be interrupted (it delays a stop or app close).
                    host = wican_discovery.resolve_host_with_fallback(
                        self._device_id, self._host
                    )
                client = WiCANLiveStreamClient(host, **kwargs)
                got_in = False
                with self._lock:
                    if self._stop_requested:
                        break
                    self._client = client
                try:
                    self.status.emit(
                        "Reconnecting..." if connected_once else "Connecting..."
                    )
                    client.connect()
                    connected_once = got_in = True
                    client.run(self._on_event)
                    break  # run() returns only on a requested stop
                except _Replaced:
                    break
                except WiCANStreamError as e:
                    if self._stop_was_requested():
                        break
                    if not connected_once:
                        # Never got in: old firmware or device unreachable.
                        raise
                    if not got_in:
                        host = None  # look the device up again next time
                    # Lost a working stream (Wi-Fi drop, device reboot): keep
                    # the capture open and get back in. The device lets the
                    # newest connection win, so a stale socket can't lock us out.
                    logger.warning(
                        "WiCAN live datalog connection lost (%s); reconnecting", e
                    )
                    self.status.emit(self._rows_text("Connection lost, reconnecting"))
                finally:
                    with self._lock:
                        self._client = None
                    client.stop()
                if self._wait_or_stop(_RECONNECT_DELAY_S):
                    break
            if self._replaced:
                self.replaced.emit()
            self.finished.emit()
        except WiCANStreamUnsupported as e:
            # A user stop during connect() closes the socket under the banner
            # read, which reads as "no banner": that is a stop, not old firmware.
            if self._stop_was_requested():
                self.finished.emit()
            else:
                self.unsupported.emit(str(e))
        except Exception as e:  # nothing may raise out of a Qt worker slot
            if self._stop_was_requested():
                self.finished.emit()
            else:
                if not isinstance(e, WiCANStreamError):
                    logger.exception("WiCAN live datalog failed")
                self.error.emit(str(e))
        finally:
            self._close_file()

    def _wait_or_stop(self, seconds: float) -> bool:
        """Sleep up to *seconds*; True as soon as a stop is requested."""
        deadline = time.monotonic() + seconds
        thread = QThread.currentThread()
        while time.monotonic() < deadline:
            if self._stop_was_requested() or (
                thread is not None and thread.isInterruptionRequested()
            ):
                return True
            time.sleep(0.1)
        return self._stop_was_requested()

    def request_stop(self):
        """Interrupt the stream (thread-safe; callable before run() starts)."""
        with self._lock:
            self._stop_requested = True
            client = self._client
        if client is not None:
            client.stop()

    def _stop_was_requested(self) -> bool:
        with self._lock:
            return self._stop_requested

    # --- stream events (worker thread) ---------------------------------------

    def _on_event(self, ev):
        from src.ecu import wican_stream as ws

        thread = QThread.currentThread()
        if thread is not None and thread.isInterruptionRequested():
            self.request_stop()
            return

        if ev.kind == ws.KIND_HELLO:
            logger.info("WiCAN live datalog connected (firmware %s)", ev.fw or "?")
            self.status.emit("Connected, waiting for a trip...")
        elif ev.kind == ws.KIND_IDLE:
            logger.info(
                "WiCAN live datalog: the device is not recording a trip right "
                "now; rows will follow when it starts one"
            )
            self.status.emit("Waiting for a trip (device not recording)...")
        elif ev.kind == ws.KIND_BYE:
            # A newer client (another NC Flash, another tool) took the slot.
            # Reconnecting would take it back and the two would swap forever.
            self._replaced = True
            raise _Replaced()
        elif ev.kind == ws.KIND_SESSION:
            self._session_cols = ev.cols
            self._trip_header = None  # a new trip: its own names must follow
            logger.info(
                "WiCAN live datalog: device trip file %s (%d channels)",
                ev.file or "?",
                ev.cols,
            )
        elif ev.kind == ws.KIND_HEADER:
            self._adopt_header(ev.line)
        elif ev.kind == ws.KIND_NOHDR:
            self._adopt_header(None)
        elif ev.kind == ws.KIND_ROW:
            self._write_row(ev.line)
        elif ev.kind == ws.KIND_DROP:
            self._dropped_total += ev.count
            logger.warning(
                "WiCAN live datalog: %d row(s) skipped (Wi-Fi too slow); the SD "
                "trip log on the device still has them",
                ev.count,
            )
        elif ev.kind == ws.KIND_CLOSE:
            logger.info("WiCAN live datalog: the device closed its trip file")
            self.status.emit(self._rows_text("Trip ended, waiting for the next one"))

    def _adopt_header(self, header: Optional[str]):
        """A device trip file started: keep appending to the open capture when
        the columns match, otherwise start a new local file."""
        self._trip_header = header
        capture = self._capture
        if capture.is_open:
            if header is not None and header == capture.header:
                logger.info(
                    "WiCAN live datalog: same columns, continuing %s",
                    Path(capture.path).name,
                )
                return
            if (
                header is None
                and capture.header is not None
                and self._session_cols == self._file_cols
            ):
                # Header unavailable but the channel count matches the capture
                # we already have a header for: the rows still line up.
                return
        self._open_file(header)

    def _open_file(self, header: Optional[str]):
        path = Path(self._capture.open(header))
        self._file_cols = self._session_cols
        self._last_ts = None
        if header is None:
            logger.warning(
                "WiCAN live datalog: the device could not send the column names; "
                "%s has no header row (the SD trip log on the device is complete)",
                path.name,
            )
        self.capture_opened.emit(str(path))
        self.status.emit(self._rows_text("Recording"))

    def _write_row(self, line: str):
        if not self._capture.is_open:
            # Rows before any #session (should not happen): nowhere to put them.
            return
        ts = _row_timestamp(line)
        if ts is not None:
            if self._last_ts is not None and ts < self._last_ts:
                # timestamp_ms counts from device boot: it went backwards, so
                # the device rebooted between trips. One capture must not run
                # time backwards; start a new one with the same header.
                logger.info(
                    "WiCAN live datalog: the device restarted; starting a new capture"
                )
                self._open_file(self._capture.header)
            self._last_ts = ts
        if self._capture.write_row(line):
            self.capture_ready.emit(self._capture.path)
        self._rows_total += 1
        if self._status_throttle.due(self._rows_total):
            self.status.emit(self._rows_text("Recording"))
        if self._trip_header and self._values_throttle.due(self._rows_total):
            self.values.emit(row_values(self._trip_header, line))

    def _rows_text(self, prefix: str) -> str:
        text = f"{prefix} ({self._rows_total} rows"
        if self._dropped_total:
            text += f", {self._dropped_total} skipped"
        return text + ")"

    def _close_file(self):
        self._capture.close()


def row_values(header: str, line: str) -> dict:
    """``{channel: value}`` of one streamed row; the device's own columns
    (timestamp, battery, mark) and empty or non-numeric cells are left out."""
    values = {}
    for name, cell in zip(header.split(","), line.split(",")):
        name = name.strip()
        if not name or name.lower() in RESERVED_NAMES:
            continue
        try:
            values[name] = float(cell)
        except ValueError:
            continue
    return values


class _Replaced(Exception):
    """Raised from the event callback to end run() on ``#bye replaced``."""


def _row_timestamp(line: str) -> Optional[int]:
    """The leading ``timestamp_ms`` field of a CSV row, or None."""
    head = line.split(",", 1)[0]
    return int(head) if head.isdigit() else None


class WiCANLiveDatalog(QObject):
    """Single owner of the background live-datalog stream state."""

    #: True while a live stream runs.
    running_changed = Signal(bool)
    #: Short human status ("Connecting...", "Recording (N rows)", error, ...).
    status_changed = Signal(str)
    #: Path of the capture being written, or "" when none is open.
    capture_changed = Signal(str)

    def __init__(
        self,
        settings,
        parent=None,
        *,
        port: Optional[int] = None,
        live_values: Optional[LiveValues] = None,
    ):
        """``port`` overrides the device's stream port (tests only);
        *live_values* receives the latest row values (owned by the caller)."""
        super().__init__(parent)
        self._settings = settings
        self._live_values = live_values
        self._port = port
        self._thread = None
        self._worker = None
        self._capture = ""
        self._captured_any = False
        self._replaced = False  # this run ended on "#bye replaced"

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    @property
    def current_capture(self) -> str:
        """The capture file being written, or "" when none is open."""
        return self._capture

    def start(self) -> bool:
        """Start the live stream on a worker thread.

        Returns False (a no-op) when one already runs, the WiCAN adapter is not
        selected, or no WiCAN host/identity is configured.
        """
        if self._thread is not None:
            # Running, or finished but its queued cleanup has not run yet:
            # replacing _thread now would let that cleanup deleteLater() the
            # NEW, running thread (Qt aborts the app).
            return False
        if not self._settings.is_wican_adapter():
            logger.debug("WiCAN live datalog skipped: WiCAN adapter not selected")
            return False
        host = (self._settings.get_wican_host() or "").strip()
        device_id = (self._settings.get_wican_device_id() or "").strip()
        if not host and not device_id:
            logger.info("WiCAN live datalog: no WiCAN configured (Settings > ECU)")
            return False

        logs_dir = self._settings.get_logs_directory()
        worker = _LiveStreamWorker(host, device_id, logs_dir, port=self._port)
        thread = QThread(self)
        worker.moveToThread(thread)
        self._thread = thread
        self._worker = worker

        # Bound-method receivers on this GUI-thread QObject, so the queued
        # slots run on the GUI thread (a lambda would run in the worker).
        worker.status.connect(self._on_status, Qt.QueuedConnection)
        worker.capture_opened.connect(self._on_capture_opened, Qt.QueuedConnection)
        worker.capture_ready.connect(self._on_capture_ready, Qt.QueuedConnection)
        worker.unsupported.connect(self._on_unsupported, Qt.QueuedConnection)
        worker.replaced.connect(self._on_replaced, Qt.QueuedConnection)
        worker.values.connect(self._on_values, Qt.QueuedConnection)
        worker.error.connect(self._on_error, Qt.QueuedConnection)
        worker.finished.connect(self._on_finished, Qt.QueuedConnection)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        worker.unsupported.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(self._on_thread_finished, Qt.QueuedConnection)

        self._captured_any = False
        self._replaced = False
        logger.info(
            "Starting WiCAN live datalog (captures go to %s)",
            live_capture_dir(logs_dir),
        )
        thread.start()
        self.running_changed.emit(True)
        return True

    def stop(self):
        """Stop a running stream (user toggle). Safe when idle."""
        self.shutdown()

    def shutdown(self, timeout_ms: int = 5000):
        """Stop a running stream (user toggle or app exit); safe when idle.

        ``request_stop()`` closes the socket so the blocking read returns within
        half a second, then the thread is joined.
        """
        thread, worker = self._thread, self._worker
        if thread is None:
            return
        thread.requestInterruption()
        if worker is not None:
            worker.request_stop()
        thread.quit()
        if thread.isRunning() and not thread.wait(timeout_ms):
            logger.warning("WiCAN live datalog did not stop within %d ms", timeout_ms)

    def trail_in_mlv(self) -> bool:
        """Open the current capture in MegaLogViewerHD (Trail Live File mode)."""
        if not self._capture:
            return False
        from src.ui import mlv_launch

        return mlv_launch.launch_trail(Path(self._capture))

    # --- worker signal handlers (GUI thread) --------------------------------

    def _on_status(self, text: str):
        self.status_changed.emit(text)

    def _on_capture_opened(self, path: str):
        self._captured_any = True
        self._capture = path
        self.capture_changed.emit(path)

    def _on_capture_ready(self, path: str):
        """Auto-open MegaLogViewerHD on each new capture once it can trail.

        Fires once per capture file: rows continuing the same file (a new
        trip with the same channels, a reconnect) never relaunch it. A new
        file (device restart, new channels) does, since the MegaLogViewerHD
        already open keeps following the old, finished file.
        """
        if path != self._capture:
            return
        from src.ui import mlv_launch

        mlv_launch.auto_trail(self._settings, path)

    def _on_unsupported(self, message: str):
        logger.info("WiCAN live datalog unavailable: %s", message)
        self.status_changed.emit(
            "Could not start: the WiCAN is not reachable, or its firmware has no "
            "live datalog"
        )

    def _on_replaced(self):
        logger.warning(
            "WiCAN live datalog: another app took over the device's live stream; "
            "stopped here (not reconnecting, so the two don't take turns)"
        )
        self._replaced = True  # queued ahead of finished, read there

    def _on_error(self, message: str):
        logger.warning("WiCAN live datalog stopped: %s", message)
        self.status_changed.emit(f"Stopped: {message}")

    def _on_finished(self):
        if self._replaced:
            self.status_changed.emit("Stopped: another app took over the live stream")
            return
        if self._captured_any:
            logger.info(
                "WiCAN live datalog stopped; captures are in %s",
                live_capture_dir(self._settings.get_logs_directory()),
            )
        else:
            logger.info(
                "WiCAN live datalog stopped; the device recorded no trip "
                "meanwhile, so nothing was captured"
            )
        self.status_changed.emit("Stopped")

    def _on_values(self, values: dict):
        if self._live_values is not None and self._thread is not None:
            self._live_values.update(values)

    def _on_thread_finished(self):
        thread, worker = self._thread, self._worker
        self._thread = None
        self._worker = None
        if worker is not None:
            worker.deleteLater()
        if thread is not None:
            thread.deleteLater()
        self._capture = ""
        if self._live_values is not None:
            self._live_values.clear()
        self.capture_changed.emit("")
        self.running_changed.emit(False)
