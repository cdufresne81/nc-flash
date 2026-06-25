"""Tests for the WiCAN mDNS Scan flow in ``SettingsDialog``.

The scan runs OFF the GUI thread (a ``_WiCANScanWorker`` in a ``QThread``)
behind an elapsed-seconds progress dialog with a Cancel button. The flow is
decomposed so the parts that matter are unit-testable without real threading:

* ``_present_scan_results`` — synchronous picker + stage-identity (what the user
  actually acts on).
* ``_on_scan_finished`` / ``_on_scan_error`` / ``_on_scan_cancel`` /
  ``_on_scan_tick`` — GUI-thread slots driven by the worker.
* ``_teardown_scan`` — releases scan state / disposes the dialog + timer.
* ``_WiCANScanWorker.run`` — the off-thread body.
* ``_scan_wican_devices`` — orchestration (Qt heavies patched out).

A QApplication must exist for QObject/QWidget construction.
"""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.ecu.wican_discovery import DiscoveryUnavailable, WiCANDevice
from src.ui.settings_dialog import (
    SETTINGS_REGISTRY,
    SettingsDialog,
    _WiCANScanWorker,
)


@pytest.fixture(autouse=True)
def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _device(host="192.168.1.169", device_id="dcb4d91511b9", mac="DC:B4:D9:15:11:B8"):
    return WiCANDevice(
        name="WiCAN-WebServer",
        host=host,
        port=80,
        hostname="wican.local",
        device_id=device_id,
        mac=mac,
    )


# --------------------------------------------------------------------------- #
# _present_scan_results — synchronous picker
# --------------------------------------------------------------------------- #


class TestPresentScanResults:
    def _self(self):
        return SimpleNamespace(_pending_wican_device_id=None)

    def _present(self, fake, host_edit, devices):
        SettingsDialog._present_scan_results(fake, devices, host_edit)

    def test_no_devices_shows_info(self):
        fake, edit = self._self(), MagicMock()
        with patch("PySide6.QtWidgets.QMessageBox") as MB:
            self._present(fake, edit, [])
        MB.information.assert_called_once()
        edit.setText.assert_not_called()
        assert fake._pending_wican_device_id is None

    def test_user_cancels_picker(self):
        fake, edit = self._self(), MagicMock()
        dev = _device()
        with (
            patch("PySide6.QtWidgets.QInputDialog") as QID,
            patch("PySide6.QtWidgets.QMessageBox"),
        ):
            QID.getItem.return_value = (dev.label, False)  # ok=False -> cancelled
            self._present(fake, edit, [dev])
        edit.setText.assert_not_called()
        assert fake._pending_wican_device_id is None

    def test_pick_sets_host_and_stages_identity(self):
        fake, edit = self._self(), MagicMock()
        dev = _device(host="192.168.1.77")
        with (
            patch("PySide6.QtWidgets.QInputDialog") as QID,
            patch("PySide6.QtWidgets.QMessageBox"),
        ):
            QID.getItem.return_value = (dev.label, True)
            self._present(fake, edit, [dev])
        # Host filled with the IP only (never the port — mDNS=80 vs SLCAN=35000).
        edit.setText.assert_called_once_with("192.168.1.77")
        # Stable identity (mac preferred) staged for persistence on apply.
        assert fake._pending_wican_device_id == "DC:B4:D9:15:11:B8"

    def test_pick_among_several_uses_chosen_label(self):
        fake, edit = self._self(), MagicMock()
        a = _device(host="192.168.1.10", device_id="aaa", mac="AA:AA:AA:AA:AA:AA")
        b = _device(host="192.168.1.20", device_id="bbb", mac="BB:BB:BB:BB:BB:BB")
        with (
            patch("PySide6.QtWidgets.QInputDialog") as QID,
            patch("PySide6.QtWidgets.QMessageBox"),
        ):
            QID.getItem.return_value = (b.label, True)  # pick the second
            self._present(fake, edit, [a, b])
        edit.setText.assert_called_once_with("192.168.1.20")
        assert fake._pending_wican_device_id == "BB:BB:BB:BB:BB:BB"

    def test_pick_without_stable_id_stages_empty(self):
        # No mac/device_id -> stable_id is None -> staged as "" (clear), not None.
        fake, edit = self._self(), MagicMock()
        dev = _device(device_id=None, mac=None)
        with (
            patch("PySide6.QtWidgets.QInputDialog") as QID,
            patch("PySide6.QtWidgets.QMessageBox"),
        ):
            QID.getItem.return_value = (dev.label, True)
            self._present(fake, edit, [dev])
        edit.setText.assert_called_once_with("192.168.1.169")
        assert fake._pending_wican_device_id == ""


# --------------------------------------------------------------------------- #
# GUI-thread slots driven by the worker
# --------------------------------------------------------------------------- #


class TestScanSlots:
    def _self(self, **over):
        base = dict(
            _scan_cancelled=False,
            _scan_thread=MagicMock(),  # an active scan
            _scan_timer=MagicMock(),
            _scan_host_edit=MagicMock(),
            _scan_cancel_event=MagicMock(),
            _scan_progress=MagicMock(),
            _scan_timeout_s=4.0,
            _teardown_scan=MagicMock(),
            _present_scan_results=MagicMock(),
        )
        base.update(over)
        return SimpleNamespace(**base)

    def test_finished_presents_when_not_cancelled(self):
        fake = self._self()
        host_edit = fake._scan_host_edit
        devices = [_device()]
        SettingsDialog._on_scan_finished(fake, devices)
        fake._teardown_scan.assert_called_once()
        fake._present_scan_results.assert_called_once_with(devices, host_edit)

    def test_finished_skips_present_when_cancelled(self):
        fake = self._self(_scan_cancelled=True)
        SettingsDialog._on_scan_finished(fake, [_device()])
        fake._teardown_scan.assert_called_once()
        fake._present_scan_results.assert_not_called()

    def test_finished_drops_stale_signal_when_no_active_scan(self):
        # A late/duplicate signal after teardown (no active scan) is ignored.
        fake = self._self(_scan_thread=None)
        SettingsDialog._on_scan_finished(fake, [_device()])
        fake._teardown_scan.assert_not_called()
        fake._present_scan_results.assert_not_called()

    def test_error_drops_stale_signal_when_no_active_scan(self):
        fake = self._self(_scan_thread=None)
        with patch("PySide6.QtWidgets.QMessageBox") as MB:
            SettingsDialog._on_scan_error(fake, OSError("late"))
        fake._teardown_scan.assert_not_called()
        MB.warning.assert_not_called()

    def test_error_discovery_unavailable_warns(self):
        fake = self._self()
        with patch("PySide6.QtWidgets.QMessageBox") as MB:
            SettingsDialog._on_scan_error(fake, DiscoveryUnavailable("no zc"))
        MB.warning.assert_called_once()
        assert "Discovery Unavailable" in MB.warning.call_args[0][1]
        fake._teardown_scan.assert_called_once()

    def test_error_oserror_warns_with_firewall_hint(self):
        fake = self._self()
        with patch("PySide6.QtWidgets.QMessageBox") as MB:
            SettingsDialog._on_scan_error(fake, OSError("socket"))
        msg = MB.warning.call_args[0][2]
        assert "5353" in msg  # firewall/UDP hint preserved
        fake._teardown_scan.assert_called_once()

    def test_error_generic_warns(self):
        fake = self._self()
        with patch("PySide6.QtWidgets.QMessageBox") as MB:
            SettingsDialog._on_scan_error(fake, RuntimeError("weird"))
        MB.warning.assert_called_once()

    def test_error_silent_when_cancelled(self):
        # A cancel that races with a late error must not pop a dialog.
        fake = self._self(_scan_cancelled=True)
        with patch("PySide6.QtWidgets.QMessageBox") as MB:
            SettingsDialog._on_scan_error(fake, OSError("socket"))
        MB.warning.assert_not_called()
        fake._teardown_scan.assert_called_once()

    def test_cancel_sets_event_flag_and_stops_timer(self):
        fake = self._self()
        SettingsDialog._on_scan_cancel(fake)
        assert fake._scan_cancelled is True
        fake._scan_cancel_event.set.assert_called_once()
        fake._scan_timer.stop.assert_called_once()  # ticker can't overwrite label
        fake._scan_progress.setLabelText.assert_called_once()

    def test_tick_skipped_after_cancel(self):
        # Once cancelled, the ticker must not overwrite the "Cancelling…" label.
        prog = MagicMock()
        fake = self._self(_scan_cancelled=True, _scan_progress=prog)
        SettingsDialog._on_scan_tick(fake)
        prog.setValue.assert_not_called()
        prog.setLabelText.assert_not_called()

    def test_tick_advances_value_and_label(self):
        prog = MagicMock()
        prog.value.return_value = 5
        prog.maximum.return_value = 40
        fake = self._self(_scan_progress=prog)
        SettingsDialog._on_scan_tick(fake)
        prog.setValue.assert_called_once_with(6)
        prog.setLabelText.assert_called_once()

    def test_tick_caps_below_maximum(self):
        # Never lets the bar reach max (which would auto-close before the worker).
        prog = MagicMock()
        prog.value.return_value = 39
        prog.maximum.return_value = 40
        fake = self._self(_scan_progress=prog)
        SettingsDialog._on_scan_tick(fake)
        prog.setValue.assert_called_once_with(39)

    def test_tick_noop_without_progress(self):
        fake = self._self(_scan_progress=None)
        SettingsDialog._on_scan_tick(fake)  # must not raise


# --------------------------------------------------------------------------- #
# _teardown_scan
# --------------------------------------------------------------------------- #


class TestTeardownScan:
    def _fake(self, **over):
        base = dict(
            _scan_timer=MagicMock(),
            _scan_progress=MagicMock(),
            _scan_thread=MagicMock(),
            _scan_worker=MagicMock(),
            _scan_cancel_event=MagicMock(),
            _scan_host_edit=MagicMock(),
            _scan_cancelled=True,
            _cleanup_scan_thread=MagicMock(),
        )
        base.update(over)
        return SimpleNamespace(**base)

    def test_resets_state_and_disposes_widgets(self):
        fake = self._fake()
        timer, progress = fake._scan_timer, fake._scan_progress
        thread, worker = fake._scan_thread, fake._scan_worker
        SettingsDialog._teardown_scan(fake)
        timer.stop.assert_called_once()
        timer.deleteLater.assert_called_once()
        progress.close.assert_called_once()
        progress.deleteLater.assert_called_once()
        assert fake._scan_thread is None
        assert fake._scan_worker is None
        assert fake._scan_cancel_event is None
        assert fake._scan_host_edit is None
        assert fake._scan_cancelled is False

    def test_blocking_joins_thread_synchronously(self):
        fake = self._fake()
        thread, worker = fake._scan_thread, fake._scan_worker
        SettingsDialog._teardown_scan(fake, blocking=True)
        # The captured thread/worker are cleaned synchronously (not deferred).
        fake._cleanup_scan_thread.assert_called_once_with(thread, worker)

    def test_teardown_is_safe_when_nothing_running(self):
        fake = self._fake(
            _scan_timer=None,
            _scan_progress=None,
            _scan_thread=None,
            _scan_worker=None,
            _scan_cancel_event=None,
            _scan_host_edit=None,
            _scan_cancelled=False,
        )
        SettingsDialog._teardown_scan(fake)  # must not raise
        assert fake._scan_thread is None
        fake._cleanup_scan_thread.assert_not_called()  # no thread to clean


class TestCleanupScanThread:
    def test_quits_waits_and_disposes_running_thread(self):
        thread, worker = MagicMock(), MagicMock()
        thread.isRunning.return_value = True
        SettingsDialog._cleanup_scan_thread(thread, worker)
        thread.quit.assert_called_once()
        thread.wait.assert_called_once()
        thread.deleteLater.assert_called_once()
        worker.deleteLater.assert_called_once()

    def test_skips_quit_for_already_finished_thread(self):
        thread, worker = MagicMock(), MagicMock()
        thread.isRunning.return_value = False
        SettingsDialog._cleanup_scan_thread(thread, worker)
        thread.quit.assert_not_called()
        thread.wait.assert_not_called()
        thread.deleteLater.assert_called_once()
        worker.deleteLater.assert_called_once()


# --------------------------------------------------------------------------- #
# _WiCANScanWorker.run (off-thread body)
# --------------------------------------------------------------------------- #


class TestScanWorker:
    def test_run_emits_finished_with_devices(self):
        ev = threading.Event()
        worker = _WiCANScanWorker(ev, 4.0)
        got = []
        worker.finished.connect(got.append)
        with patch(
            "src.ecu.wican_discovery.discover", return_value=[_device()]
        ) as disc:
            worker.run()
        disc.assert_called_once_with(timeout_s=4.0, cancel_event=ev)
        assert got and got[0][0].host == "192.168.1.169"

    def test_run_emits_error_on_exception(self):
        worker = _WiCANScanWorker(threading.Event(), 4.0)
        errs = []
        worker.error.connect(errs.append)
        with patch("src.ecu.wican_discovery.discover", side_effect=OSError("boom")):
            worker.run()
        assert len(errs) == 1 and isinstance(errs[0], OSError)


# --------------------------------------------------------------------------- #
# _scan_wican_devices orchestration (Qt heavies patched out)
# --------------------------------------------------------------------------- #


class TestScanOrchestration:
    def _fake(self):
        return SimpleNamespace(
            _scan_thread=None,
            _scan_worker=None,
            _scan_progress=None,
            _scan_timer=None,
            _scan_cancel_event=None,
            _scan_host_edit=None,
            _scan_cancelled=False,
            _scan_timeout_s=None,
            _on_scan_tick=MagicMock(),
            _on_scan_cancel=MagicMock(),
            _on_scan_finished=MagicMock(),
            _on_scan_error=MagicMock(),
        )

    def test_zeroconf_unavailable_warns_and_starts_nothing(self):
        fake = self._fake()
        with (
            patch("src.ecu.wican_discovery.zeroconf_available", return_value=False),
            patch("PySide6.QtWidgets.QMessageBox") as MB,
        ):
            SettingsDialog._scan_wican_devices(fake, MagicMock())
        MB.warning.assert_called_once()
        assert fake._scan_thread is None

    def test_reentrancy_guard_returns_immediately(self):
        fake = self._fake()
        sentinel = object()
        fake._scan_thread = sentinel
        with patch("src.ecu.wican_discovery.zeroconf_available", return_value=True):
            SettingsDialog._scan_wican_devices(fake, MagicMock())
        assert fake._scan_thread is sentinel  # untouched
        assert fake._scan_worker is None  # never created a worker

    def test_starts_worker_thread_progress_and_timer(self):
        from PySide6.QtCore import Qt

        fake = self._fake()
        with (
            patch("src.ecu.wican_discovery.zeroconf_available", return_value=True),
            patch("src.ui.settings_dialog.QThread") as QThreadM,
            patch("src.ui.settings_dialog.QTimer") as QTimerM,
            patch("src.ui.settings_dialog.QProgressDialog") as QPDM,
            patch("src.ui.settings_dialog._WiCANScanWorker") as WorkerM,
        ):
            SettingsDialog._scan_wican_devices(fake, MagicMock())
        thread, timer, progress, worker = (
            QThreadM.return_value,
            QTimerM.return_value,
            QPDM.return_value,
            WorkerM.return_value,
        )
        thread.start.assert_called_once()
        timer.start.assert_called_once()
        progress.show.assert_called_once()
        assert fake._scan_thread is thread
        assert fake._scan_worker is worker
        assert isinstance(fake._scan_cancel_event, threading.Event)
        assert fake._scan_cancelled is False
        # Signal wiring — a typo in any signal name would slip past start()/show().
        # The thread/worker are disposed by _teardown_scan (quit+wait+deleteLater),
        # NOT self-disposed here, so we assert only the live-scan connections.
        worker.moveToThread.assert_called_once_with(thread)
        timer.timeout.connect.assert_called_once_with(fake._on_scan_tick)
        progress.canceled.connect.assert_called_once_with(fake._on_scan_cancel)
        worker.finished.connect.assert_called_once_with(
            fake._on_scan_finished, Qt.QueuedConnection
        )
        worker.error.connect.assert_called_once_with(
            fake._on_scan_error, Qt.QueuedConnection
        )
        thread.started.connect.assert_called_once_with(worker.run)


# --------------------------------------------------------------------------- #
# Host field manual-edit guard (built in _create_setting_widget)
# --------------------------------------------------------------------------- #


class TestIdentityClearGuard:
    """The host field's textEdited wiring (built in _create_setting_widget)."""

    def _host_descriptor(self):
        return next(d for d in SETTINGS_REGISTRY if d.key == "ecu.wican.host")

    def _build_host_widget(self):
        fake = SimpleNamespace(
            _widgets={},
            _pending_wican_device_id="DC:B4:D9:15:11:B8",
            _scan_wican_devices=lambda *a, **k: None,
        )
        # Retain the container so Qt doesn't GC it (and its child QLineEdit).
        fake._container = SettingsDialog._create_setting_widget(
            fake, self._host_descriptor()
        )
        return fake, fake._widgets["ecu.wican.host"]

    def test_programmatic_setText_keeps_identity(self):
        # Scan/load fill the field via setText, which must NOT fire textEdited
        # and so must NOT clear the staged identity.
        fake, edit = self._build_host_widget()
        edit.setText("192.168.1.50")
        assert fake._pending_wican_device_id == "DC:B4:D9:15:11:B8"

    def test_user_edit_clears_identity(self):
        # A real user keystroke (textEdited) detaches the stored identity so the
        # connect-time re-resolve won't override a manually-typed IP.
        fake, edit = self._build_host_widget()
        edit.textEdited.emit("192.168.1.51")
        assert fake._pending_wican_device_id == ""


# --------------------------------------------------------------------------- #
# apply / close persistence
# --------------------------------------------------------------------------- #


class TestApplyPersistsIdentity:
    """End-to-end: apply_settings writes the staged identity exactly once."""

    def _dialog(self, monkeypatch):
        from src.utils.settings import AppSettings

        store = {}
        qs = MagicMock()
        qs.value = lambda key, default=None, **k: store.get(key, default)
        qs.setValue = lambda key, value: store.__setitem__(key, value)
        qs.sync = MagicMock()
        monkeypatch.setattr("src.utils.settings.QSettings", lambda *a, **k: qs)
        settings = AppSettings()
        monkeypatch.setattr("src.ui.settings_dialog.get_settings", lambda: settings)
        return SettingsDialog(), settings, store

    def test_staged_identity_is_persisted_then_cleared(self, monkeypatch):
        dialog, settings, store = self._dialog(monkeypatch)
        dialog._pending_wican_device_id = "DC:B4:D9:15:11:B8"
        dialog.apply_settings()
        assert settings.get_wican_device_id() == "DC:B4:D9:15:11:B8"
        assert dialog._pending_wican_device_id is None  # reset after write

    def test_no_pending_identity_is_left_untouched(self, monkeypatch):
        dialog, settings, store = self._dialog(monkeypatch)
        store["ecu/wican_device_id"] = "preexisting"
        dialog._pending_wican_device_id = None  # user never scanned/edited
        dialog.apply_settings()
        assert settings.get_wican_device_id() == "preexisting"

    def test_done_cancels_inflight_scan(self, monkeypatch):
        from PySide6.QtWidgets import QDialog

        dialog, settings, store = self._dialog(monkeypatch)
        ev = threading.Event()
        thread = MagicMock()
        thread.isRunning.return_value = True
        dialog._scan_cancel_event = ev
        dialog._scan_thread = thread  # an active scan
        dialog.done(QDialog.Rejected)
        assert ev.is_set()  # close signalled the worker to stop early
        # ...and joined the thread synchronously so it isn't destroyed running.
        thread.quit.assert_called_once()
        thread.wait.assert_called_once()
        assert dialog._scan_thread is None

    def test_done_without_scan_is_noop_for_cancel_flag(self, monkeypatch):
        from PySide6.QtWidgets import QDialog

        dialog, settings, store = self._dialog(monkeypatch)
        dialog._scan_thread = None
        dialog.done(QDialog.Rejected)
        assert dialog._scan_cancelled is False


# --------------------------------------------------------------------------- #
# Real-thread end-to-end — catches QThread lifecycle bugs the mocked
# orchestration test can't (e.g. "QThread: Destroyed while thread is running").
# --------------------------------------------------------------------------- #


class TestScanRealThread:
    def _dialog(self, monkeypatch):
        from src.utils.settings import AppSettings

        store = {}
        qs = MagicMock()
        qs.value = lambda key, default=None, **k: store.get(key, default)
        qs.setValue = lambda key, value: store.__setitem__(key, value)
        qs.sync = MagicMock()
        monkeypatch.setattr("src.utils.settings.QSettings", lambda *a, **k: qs)
        settings = AppSettings()
        monkeypatch.setattr("src.ui.settings_dialog.get_settings", lambda: settings)
        return SettingsDialog()

    def _run_scan(self, dialog, qapp):
        import time

        host_edit = dialog._widgets.get("ecu.wican.host")
        if host_edit is None:  # ECU page not built in this env -> use a bare edit
            from PySide6.QtWidgets import QLineEdit

            host_edit = QLineEdit()
        dialog._scan_wican_devices(host_edit)

        deadline = time.monotonic() + 5.0
        while dialog._scan_thread is not None and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.005)
        # Let the deferred quit+join (QTimer.singleShot) and any wrapper GC run.
        import gc

        for _ in range(40):
            qapp.processEvents()
            gc.collect()
            time.sleep(0.005)
        return host_edit

    def test_real_scan_completes_without_qt_thread_warning(self, monkeypatch, _qapp):
        from PySide6.QtCore import qInstallMessageHandler

        dev = _device(host="192.168.1.55")
        monkeypatch.setattr("src.ecu.wican_discovery.zeroconf_available", lambda: True)
        monkeypatch.setattr("src.ecu.wican_discovery.discover", lambda *a, **k: [dev])
        # Auto-pick the single discovered device (no modal block).
        monkeypatch.setattr(
            "PySide6.QtWidgets.QInputDialog.getItem",
            lambda *a, **k: (dev.label, True),
        )

        messages = []
        prev = qInstallMessageHandler(lambda mode, ctx, msg: messages.append(msg))
        try:
            dialog = self._dialog(monkeypatch)
            host_edit = self._run_scan(dialog, _qapp)
        finally:
            qInstallMessageHandler(prev)

        assert dialog._scan_thread is None, "scan thread was not torn down"
        assert host_edit.text() == "192.168.1.55"
        bad = [
            m
            for m in messages
            if "Destroyed while" in m or "still running" in m.lower()
        ]
        assert not bad, f"QThread lifecycle warning(s) emitted: {bad}"

    def test_real_scan_cancelled_on_close_without_warning(self, monkeypatch, _qapp):
        # Close the dialog while the scan is in flight: the worker must be joined
        # synchronously (done -> blocking teardown), never destroyed-while-running.
        import threading

        from PySide6.QtCore import qInstallMessageHandler
        from PySide6.QtWidgets import QDialog

        started = threading.Event()
        release = threading.Event()

        def slow_discover(*a, **k):
            started.set()
            release.wait(2.0)  # hold the worker "in flight" until we close
            return []

        monkeypatch.setattr("src.ecu.wican_discovery.zeroconf_available", lambda: True)
        monkeypatch.setattr("src.ecu.wican_discovery.discover", slow_discover)

        messages = []
        prev = qInstallMessageHandler(lambda mode, ctx, msg: messages.append(msg))
        try:
            dialog = self._dialog(monkeypatch)
            from PySide6.QtWidgets import QLineEdit

            dialog._scan_wican_devices(QLineEdit())
            assert started.wait(2.0), "worker never started"
            release.set()  # let discover return as soon as it's joined
            dialog.done(QDialog.Rejected)  # close mid-scan
            import gc

            for _ in range(20):
                _qapp.processEvents()
                gc.collect()
        finally:
            release.set()
            qInstallMessageHandler(prev)

        assert dialog._scan_thread is None
        bad = [
            m
            for m in messages
            if "Destroyed while" in m or "still running" in m.lower()
        ]
        assert not bad, f"QThread lifecycle warning(s) emitted: {bad}"
