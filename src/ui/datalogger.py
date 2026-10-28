"""The Datalogger: one owner for live logging, whatever the adapter.

The user picks channels once (saved in settings, see
:mod:`src.core.datalog_channels`); the engine follows the selected adapter:

- **Tactrix / OBDX (host engine):** NC Flash polls the ECU itself on a worker
  thread (:class:`~src.ecu.datalog_poller.DatalogPoller`) and appends a row per
  sweep to a live CSV.
- **WiCAN:** the WiCAN polls the ECU itself, which is much faster than NC Flash
  polling over Wi-Fi. The channel list is sent to its firmware
  (:meth:`Datalogger.push_to_wican`) and the run follows the trip it records
  through the existing :class:`~src.ui.wican_live_datalog.WiCANLiveDatalog`.

Both engines write through :class:`~src.core.live_csv.LiveCsvCapture` and
open MegaLogViewerHD through :func:`src.ui.mlv_launch.auto_trail`.

Owned by the main window (one instance, state has ONE owner). The ECU
Programming window locks itself while a run is active, and a run won't start
while that window holds the ECU link (``blocker``).
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, QThread, Qt, Signal

from src.core.datalog_channels import (
    Channel,
    channels_from_saved,
    to_firmware_row,
)
from src.core.live_csv import LiveCsvCapture, StatusThrottle, live_capture_dir
from src.ui.live_values import EMIT_EVERY_S, LiveValues
from src.ui.wican_live_datalog import WiCANLiveDatalog

logger = logging.getLogger(__name__)

ENGINE_HOST = "host"
ENGINE_WICAN = "wican"

#: Where the WiCAN's previous channel file is saved before each change.
BACKUP_SUBDIR = "wican_channel_backups"
#: App close waits this long for a WiCAN channel job: an HTTP call in flight
#: (10 s timeout) after a 3 s mDNS lookup.
JOB_SHUTDOWN_MS = 15000


def wired_adapter_config(settings) -> dict:
    """The ECUSession config for the selected wired adapter (Tactrix / OBDX).

    THE builder for the wired kinds; the ECU window uses it too.
    """
    from src.ecu.constants import DEFAULT_J2534_DLL

    if settings.get_ecu_adapter() == "obdx":
        return {"kind": "obdx", "port": settings.get_obdx_port()}
    return {
        "kind": "j2534",
        "dll_path": settings.get_j2534_dll_path() or DEFAULT_J2534_DLL,
    }


#: After a lost connection mid-run, the next reconnect attempt waits this long.
RECONNECT_DELAY_S = 2.0


def _not_a_connection_loss():
    from src.ecu.datalog_poller import DatalogNothingToPoll

    # Programming errors must stop the run, not loop on reconnects.
    return (
        DatalogNothingToPoll,
        TypeError,
        AttributeError,
        NameError,
        AssertionError,
        ImportError,
    )


class _HostPollWorker(QObject):
    """Connects to the ECU, polls the channels, writes the capture."""

    status = Signal(str)
    capture_opened = Signal(str)
    capture_ready = Signal(str)
    channel_dropped = Signal(str, str)  # channel name, why
    values = Signal(dict)  # {channel name: value}, at most every EMIT_EVERY_S
    error = Signal(str)
    finished = Signal()

    def __init__(self, adapter_config: dict, channels: list[Channel], logs_dir: str):
        super().__init__()
        self._adapter_config = dict(adapter_config)
        self._channels = channels
        self._live_dir = live_capture_dir(logs_dir)
        self._stop = threading.Event()

    def request_stop(self):
        """Thread-safe; the loop ends after the sweep in flight."""
        self._stop.set()

    def _stopping(self) -> bool:
        thread = QThread.currentThread()
        return self._stop.is_set() or (
            thread is not None and thread.isInterruptionRequested()
        )

    def _connect(self):
        """Open a session to the ECU, or raise with a reason a user can act on."""
        from src.ecu.session import ECUSession

        lost: list[str] = []
        session = ECUSession(adapter_config=self._adapter_config)
        session.connection_lost.connect(lost.append, Qt.DirectConnection)
        try:
            session.connect_ecu()
        except Exception:
            self._close(session)
            raise
        if not session.is_connected:
            self._close(session)
            raise RuntimeError(
                (lost[0] if lost else "Could not connect to the ECU")
                + ". Check the ignition is on and the cable is plugged in."
            )
        return session

    @staticmethod
    def _close(session):
        try:
            session.cleanup()
        except Exception:
            logger.debug("Datalog session cleanup failed", exc_info=True)

    def _reconnect(self, reason: str):
        """Retry every RECONNECT_DELAY_S until connected; None once stopped."""
        attempt = 0
        while not self._stopping():
            attempt += 1
            self.status.emit(f"Connection lost ({reason}). Reconnecting...")
            if self._stop.wait(RECONNECT_DELAY_S) or self._stopping():
                return None
            try:
                session = self._connect()
            except Exception as exc:  # still gone: try again
                reason = str(exc) or type(exc).__name__
                logger.info("Datalog reconnect attempt %d failed: %s", attempt, reason)
                continue
            logger.info("Datalog reconnected after %d attempt(s)", attempt)
            return session
        return None

    def run(self):
        from src.ecu.datalog_poller import DatalogPoller, format_row

        session = None
        capture = LiveCsvCapture(self._live_dir, label="Datalog")
        failure: Optional[str] = None
        try:
            self.status.emit("Connecting to the ECU...")
            # A first connect that fails stops the run (ignition off, wrong
            # cable): the user gets the reason at once, not endless retries.
            session = self._connect()
            poller = DatalogPoller(session.uds, self._channels)
            path = capture.open(poller.header)
            self.capture_opened.emit(path)
            reported: set[str] = set()
            throttle = StatusThrottle(every_rows=10**9, every_s=1.0)
            values_throttle = StatusThrottle(every_rows=10**9, every_s=EMIT_EVERY_S)
            names = [c.name for c in poller.channels]
            t0 = time.monotonic()
            window_t, window_rows, rate = t0, 0, 0.0
            rows = 0
            reconnects = 0
            while not self._stopping():
                try:
                    result = poller.sweep()
                except _not_a_connection_loss():
                    raise  # every channel refused, or a bug: stop, don't loop
                except Exception as exc:
                    # Lost the adapter or the ECU mid-run (USB hiccup, ECU
                    # silent): reconnect and keep growing the same capture.
                    reason = str(exc) or type(exc).__name__
                    logger.warning("Datalog connection lost: %s; reconnecting", reason)
                    self._close(session)
                    session = self._reconnect(reason)
                    if session is None:
                        break  # stopped while reconnecting
                    reconnects += 1
                    dropped = dict(poller.unsupported)
                    poller = DatalogPoller(session.uds, self._channels)
                    poller.unsupported.update(dropped)  # same columns, same drops
                    window_t, window_rows = time.monotonic(), 0
                    self.status.emit("Reconnected, logging again")
                    continue
                now = time.monotonic()
                if capture.write_row(format_row(now - t0, result.values)):
                    self.capture_ready.emit(path)
                rows += 1
                window_rows += 1
                if values_throttle.due(rows):
                    self.values.emit(
                        {n: v for n, v in zip(names, result.values) if v is not None}
                    )
                for name, why in poller.unsupported.items():
                    if name not in reported:
                        reported.add(name)
                        self.channel_dropped.emit(name, why)
                if throttle.due(rows):
                    if now - window_t > 0:
                        rate = window_rows / (now - window_t)
                    window_t, window_rows = now, 0
                    text = f"Logging: {rate:.1f} rows/s, {rows} rows"
                    if reconnects:
                        text += f" (reconnected {reconnects}x)"
                    self.status.emit(text)
            logger.info("Datalog stopped after %d rows", rows)
        except Exception as exc:  # nothing may raise out of a Qt worker slot
            failure = str(exc) or type(exc).__name__
            logger.warning("Datalog stopped: %s", failure)
        finally:
            capture.close()
            if session is not None:
                self._close(session)
        if failure is not None and not self._stopping():
            self.error.emit(failure)
        else:
            self.finished.emit()


class _Job(QObject):
    """Runs one blocking call off the GUI thread (WiCAN HTTP)."""

    done = Signal(object)
    failed = Signal(str, str)  # message, backup written before the failure ("")

    def __init__(self, fn: Callable[[], object]):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            result = self._fn()
        except Exception as exc:  # reported, never raised out of the slot
            logger.warning("WiCAN channel update failed: %s", exc)
            self.failed.emit(str(exc) or type(exc).__name__, getattr(exc, "backup", ""))
            return
        self.done.emit(result)


class Datalogger(QObject):
    """Single owner of the live Datalogger run and the WiCAN channel push."""

    #: True while a run is active (either engine).
    running_changed = Signal(bool)
    #: Short human status for the run.
    status_changed = Signal(str)
    #: Path of the capture being written, or "".
    capture_changed = Signal(str)
    #: A channel the ECU refused: (name, why). Host engine only.
    channel_dropped = Signal(str, str)
    #: True while a WiCAN channel push/apply/restore runs.
    push_running_changed = Signal(bool)
    #: What sending the list would change on the WiCAN (see
    #: :meth:`~src.ecu.wican_autopid.WiCANAutoPid.preview`); the window asks
    #: the user, then calls :meth:`push_to_wican`.
    push_preview = Signal(dict)
    #: A push/apply/restore ended: (ok, message).
    push_finished = Signal(bool, str)

    def __init__(
        self,
        settings,
        parent=None,
        *,
        blocker: Optional[Callable[[], Optional[str]]] = None,
        wican: Optional[WiCANLiveDatalog] = None,
    ):
        super().__init__(parent)
        self._settings = settings
        self._blocker = blocker
        #: Latest logged values (both engines), for the live table cursor.
        self.live_values = LiveValues(self)
        self.wican = wican or WiCANLiveDatalog(
            settings, parent=self, live_values=self.live_values
        )
        self.wican.running_changed.connect(self._on_wican_running)
        self.wican.status_changed.connect(self.status_changed)
        self.wican.capture_changed.connect(self.capture_changed)
        self._thread: Optional[QThread] = None
        self._worker: Optional[_HostPollWorker] = None
        self._capture = ""
        self._job_thread: Optional[QThread] = None
        self._job: Optional[_Job] = None
        self._job_client = None  # the WiCANAutoPid of the job in flight
        self._last_backup = ""
        #: Backup written by the push that just failed ("" if it changed
        #: nothing); only that one may be offered for a restore.
        self.failed_push_backup = ""

    # --- state ------------------------------------------------------------

    @property
    def engine(self) -> str:
        return ENGINE_WICAN if self._settings.is_wican_adapter() else ENGINE_HOST

    @property
    def is_running(self) -> bool:
        return self._thread is not None or self.wican.is_running

    @property
    def host_running(self) -> bool:
        """A host-engine run (it holds the Tactrix/OBDX cable) is active."""
        return self._thread is not None

    @property
    def current_capture(self) -> str:
        return self.wican.current_capture or self._capture

    @property
    def push_running(self) -> bool:
        return self._job_thread is not None

    @property
    def last_backup(self) -> str:
        """The WiCAN channel file saved before the last push, or ""."""
        return self._last_backup

    def channels(self) -> list[Channel]:
        return channels_from_saved(self._settings.get_datalog_channels())

    def blocked_reason(self) -> Optional[str]:
        """Why a run can't start now, or None."""
        if self.push_running:
            return "Wait for the WiCAN channel update to finish."
        return self._blocker() if self._blocker is not None else None

    # --- run --------------------------------------------------------------

    def start(self) -> bool:
        """Start a run with the saved channels; False (and a status) if not."""
        if self.is_running:
            return False
        reason = self.blocked_reason()
        if reason:
            self.status_changed.emit(reason)
            return False
        if self.engine == ENGINE_WICAN:
            if not self.wican.start():
                self.status_changed.emit(
                    "Could not start: no WiCAN is set up (Settings > ECU)."
                )
                return False
            return True
        return self._start_host()

    def _start_host(self) -> bool:
        if self._thread is not None:
            return False  # its queued cleanup has not run yet
        channels = [c for c in self.channels() if c.enabled]
        if not channels:
            self.status_changed.emit("Select at least one channel.")
            return False
        worker = _HostPollWorker(
            wired_adapter_config(self._settings),
            channels,
            self._settings.get_logs_directory(),
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        self._thread, self._worker = thread, worker
        # Bound-method receivers on this GUI-thread QObject, so the queued
        # slots run on the GUI thread (a lambda would run in the worker).
        worker.status.connect(self._on_status, Qt.QueuedConnection)
        worker.capture_opened.connect(self._on_capture_opened, Qt.QueuedConnection)
        worker.capture_ready.connect(self._on_capture_ready, Qt.QueuedConnection)
        worker.channel_dropped.connect(self._on_channel_dropped, Qt.QueuedConnection)
        worker.values.connect(self._on_values, Qt.QueuedConnection)
        worker.error.connect(self._on_error, Qt.QueuedConnection)
        worker.finished.connect(self._on_finished, Qt.QueuedConnection)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(self._on_thread_finished, Qt.QueuedConnection)
        logger.info(
            "Starting datalog: %d channel(s) over %s",
            len(channels),
            self._settings.get_ecu_adapter(),
        )
        thread.start()
        self.running_changed.emit(True)
        return True

    def stop(self):
        """Stop a run (user toggle). Safe when idle; leaves a WiCAN channel
        job running (it ends on its own)."""
        self.wican.shutdown()
        self._stop_host(10000)

    def shutdown(self, timeout_ms: int = 10000):
        """Stop any run and channel job (also on app exit); safe when idle."""
        self.wican.shutdown(timeout_ms)
        self._stop_host(timeout_ms)
        self.wait_for_wican_job(max(timeout_ms, JOB_SHUTDOWN_MS))

    def _stop_host(self, timeout_ms: int):
        thread, worker = self._thread, self._worker
        if thread is not None:
            thread.requestInterruption()
            if worker is not None:
                worker.request_stop()
            # The worker's finished -> quit is queued to THIS (blocked) thread,
            # so ask the event loop to exit directly; it does once run() ends.
            thread.quit()
            if thread.isRunning() and not thread.wait(timeout_ms):
                logger.warning("Datalog did not stop within %d ms", timeout_ms)

    def wait_for_wican_job(self, timeout_ms: int):
        """App close: let a WiCAN channel job end (its thread must not be
        destroyed running). It stops waiting for the reload at once; an HTTP
        call in flight can still take its own timeout."""
        job_thread = self._job_thread
        if job_thread is not None and job_thread.isRunning():
            client = self._job_client
            if client is not None:
                client.cancel()
            job_thread.quit()  # exits once the call in flight returns
            if not job_thread.wait(timeout_ms):
                logger.warning("WiCAN channel update did not finish in time")

    def trail_in_mlv(self) -> bool:
        """Open the current capture in MegaLogViewerHD (Trail Live File mode)."""
        path = self.current_capture
        if not path:
            return False
        from src.ui import mlv_launch

        return mlv_launch.launch_trail(Path(path))

    # --- host worker handlers (GUI thread) --------------------------------

    def _on_status(self, text: str):
        self.status_changed.emit(text)

    def _on_capture_opened(self, path: str):
        self._capture = path
        self.capture_changed.emit(path)

    def _on_capture_ready(self, path: str):
        if path != self._capture:
            return
        from src.ui import mlv_launch

        mlv_launch.auto_trail(self._settings, path)

    def _on_channel_dropped(self, name: str, why: str):
        self.channel_dropped.emit(name, why)

    def _on_values(self, values: dict):
        if self._thread is not None:  # not a late one after the stop
            self.live_values.update(values)

    def _on_error(self, message: str):
        self.status_changed.emit(f"Stopped: {message}")

    def _on_finished(self):
        self.status_changed.emit("Stopped")

    def _on_thread_finished(self):
        thread, worker = self._thread, self._worker
        self._thread = None
        self._worker = None
        if worker is not None:
            worker.deleteLater()
        if thread is not None:
            thread.deleteLater()
        self._capture = ""
        self.live_values.clear()
        self.capture_changed.emit(self.wican.current_capture)
        self.running_changed.emit(self.is_running)

    def _on_wican_running(self, running: bool):
        self.running_changed.emit(self.is_running)

    # --- WiCAN channel push -----------------------------------------------

    def _finish(self, ok: bool, message: str, failed_backup: str = ""):
        self.failed_push_backup = failed_backup
        self.push_finished.emit(ok, message)

    def _firmware_rows(self) -> Optional[list]:
        try:
            return [to_firmware_row(c) for c in self.channels()]
        except ValueError as exc:
            self._finish(False, str(exc))
            return None

    def _push_blocked(self) -> bool:
        if self.push_running:
            return True
        reason = self._blocker() if self._blocker is not None else None
        if reason:
            self._finish(False, reason)
            return True
        return False

    def preview_wican_push(self) -> bool:
        """Read the WiCAN's list and report what sending ours would change
        (``push_preview``); nothing is written."""
        if self._push_blocked():
            return False
        rows = self._firmware_rows()
        if rows is None:
            return False

        def preview():
            return ("preview", self._new_client().preview(rows))

        return self._run_job(preview, self._on_push_done)

    def push_to_wican(self) -> bool:
        """Send the saved channel list to the WiCAN's poller (background).

        Ends with ``push_finished``. The previous list is saved first; after a
        failure ``last_backup`` names it (or is "" when nothing was changed).
        """
        if self._push_blocked():
            return False
        rows = self._firmware_rows()
        if rows is None:
            return False
        backup_dir = Path(self._settings.get_logs_directory()) / BACKUP_SUBDIR

        def push():
            client = self._new_client()
            try:
                result = client.push(rows, backup_dir)
                if result.applied in ("live", ""):
                    if client.wait_applied().get("reload_pending"):
                        result.applied = "deferred"  # a trip opened meanwhile
            except Exception as exc:
                exc.backup = client.last_backup  # the device may have changed
                raise
            return ("pushed", result)

        self._last_backup = ""
        return self._run_job(push, self._on_push_done)

    def restore_wican_backup(self, backup_path: str) -> bool:
        """Put back the channel file saved before a push (background)."""
        if self._push_blocked():
            return False

        def restore():
            self._new_client().restore(backup_path)
            return ("restored", backup_path)

        return self._run_job(restore, self._on_push_done)

    def _new_client(self):
        client = self._wican_client()
        self._job_client = client
        return client

    def _wican_client(self):
        from src.ecu import wican_discovery
        from src.ecu.wican_autopid import WiCANAutoPid

        host = (self._settings.get_wican_host() or "").strip()
        device_id = (self._settings.get_wican_device_id() or "").strip()
        if not host and not device_id:
            raise RuntimeError("No WiCAN is set up (Settings > ECU).")
        resolved = wican_discovery.resolve_host_with_fallback(device_id, host)
        return WiCANAutoPid(resolved or host)

    def _run_job(self, fn, on_done) -> bool:
        if self._job_thread is not None:
            return False
        job = _Job(fn)
        thread = QThread(self)
        job.moveToThread(thread)
        self._job_thread, self._job = thread, job
        job.done.connect(on_done, Qt.QueuedConnection)
        job.failed.connect(self._on_job_failed, Qt.QueuedConnection)
        thread.started.connect(job.run)
        job.done.connect(thread.quit)
        job.failed.connect(thread.quit)
        thread.finished.connect(self._on_job_thread_finished, Qt.QueuedConnection)
        thread.start()
        self.push_running_changed.emit(True)
        return True

    def _on_push_done(self, outcome):
        kind, value = outcome
        if kind == "preview":
            self.push_preview.emit(value)
            return
        if kind == "restored":
            self._last_backup = ""
            self._finish(True, "The WiCAN's previous channel list is back.")
            return
        self._last_backup = value.backup
        if value.applied == "deferred":
            text = (
                "Sent. The WiCAN is recording a trip, so it switches to the new "
                "channels when that trip ends: turn the ignition off, then on again."
            )
        elif value.applied == "reboot":
            text = (
                "Sent. Restart the WiCAN (or turn the ignition off and on) for it "
                "to start polling the new channels."
            )
        else:
            text = "The WiCAN now polls the new channel list."
        if value.orphaned_calculated:
            text += (
                " These calculated channels lost an input and will stay empty: "
                + ", ".join(value.orphaned_calculated)
                + "."
            )
        self._finish(True, text)

    def _on_job_failed(self, message: str, backup: str):
        if backup:
            self._last_backup = backup
        self._finish(False, message, backup)

    def _on_job_thread_finished(self):
        thread, job = self._job_thread, self._job
        self._job_thread = None
        self._job = None
        self._job_client = None
        if job is not None:
            job.deleteLater()
        if thread is not None:
            thread.deleteLater()
        self.push_running_changed.emit(False)
