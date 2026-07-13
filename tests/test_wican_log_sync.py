"""WiCANLogSync collaborator: real-QThread lifecycle over a fake device.

Per the repo's QThread rule these tests run the worker on a REAL QThread —
a destroyed-while-running abort or a cross-thread slot bug does not reproduce
under a mocked thread. A qInstallMessageHandler guard fails the test if Qt
reports a thread destroyed while still running.

The fake csv_logger device (and its endpoint emulation) is shared with
``test_ecu_wican_logs`` via pytest's ``pythonpath = tests``.
"""

import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import qInstallMessageHandler

import src.ui.wican_log_sync as wican_log_sync_module
from src.ui.wican_log_sync import WiCANLogSync
from test_ecu_wican_logs import _Handler, _device

from http.server import ThreadingHTTPServer


@pytest.fixture
def qt_thread_guard():
    """Fail the test if Qt reports a QThread destroyed while running."""
    messages = []

    def handler(mode, ctx, msg):
        messages.append(msg)

    old = qInstallMessageHandler(handler)
    yield
    qInstallMessageHandler(old)
    aborts = [m for m in messages if "Destroyed while thread" in m]
    assert not aborts, f"QThread lifecycle violation: {aborts}"


@pytest.fixture
def fake_settings(tmp_path):
    s = MagicMock()
    s.is_wican_adapter.return_value = True  # feature is WiCAN-adapter-only
    s.get_wican_host.return_value = "127.0.0.1"
    s.get_wican_device_id.return_value = ""  # no mDNS resolve in tests
    s.get_logs_directory.return_value = str(tmp_path / "logs")
    return s


def _wait_sync_done(qtbot, sync):
    """Block until the sync's running_changed(False) fires."""
    with qtbot.waitSignal(
        sync.running_changed,
        timeout=10000,
        check_params_cb=lambda running: running is False,
    ):
        pass


class TestWiCANLogSync:
    def test_start_downloads_and_returns_to_idle(
        self, qtbot, fake_settings, qt_thread_guard
    ):
        data = b"time,rpm\n1,800\n"
        with _device({"trip.csv": data}) as (_, httpd):
            sync = WiCANLogSync(fake_settings, http_port=httpd.server_address[1])
            assert sync.start() is True
            _wait_sync_done(qtbot, sync)
        assert not sync.is_running
        dest = Path(fake_settings.get_logs_directory())
        assert (dest / "trip.csv").read_bytes() == data
        # Idle shutdown is a no-op (app close with no sync running).
        sync.shutdown()

    def test_second_start_while_running_is_refused(
        self, qtbot, fake_settings, qt_thread_guard
    ):
        # Hold /csv_list until released so the first sync is deterministically
        # still running when the second start() lands.
        release = threading.Event()

        class _SlowHandler(_Handler):
            def do_GET(self):
                release.wait(timeout=10)
                super().do_GET()

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _SlowHandler)
        httpd.files = {}
        httpd.active = None
        httpd.advertised = {}
        httpd.status_error = False
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            sync = WiCANLogSync(fake_settings, http_port=httpd.server_address[1])
            assert sync.start() is True
            assert sync.start() is False  # single owner: one sync at a time
            release.set()
            _wait_sync_done(qtbot, sync)
            assert not sync.is_running
        finally:
            release.set()
            httpd.shutdown()
            httpd.server_close()
            t.join(timeout=2)

    def test_no_host_or_identity_is_a_noop(self, fake_settings, qt_thread_guard):
        fake_settings.get_wican_host.return_value = ""
        fake_settings.get_wican_device_id.return_value = ""
        sync = WiCANLogSync(fake_settings)
        assert sync.start() is False
        assert not sync.is_running

    def test_j2534_adapter_is_a_noop(self, fake_settings, qt_thread_guard):
        # Product decision: the whole sync is dormant unless the WiCAN adapter
        # is selected — even with a valid host configured.
        fake_settings.is_wican_adapter.return_value = False
        sync = WiCANLogSync(fake_settings)
        assert sync.start() is False
        assert not sync.is_running

    def test_unreachable_device_is_quiet_and_returns_to_idle(
        self, qtbot, fake_settings, qt_thread_guard
    ):
        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            closed_port = s.getsockname()[1]
        sync = WiCANLogSync(fake_settings, http_port=closed_port)
        assert sync.start() is True
        _wait_sync_done(qtbot, sync)  # error path also returns to idle
        assert not sync.is_running
        # Nothing was created locally.
        assert not Path(fake_settings.get_logs_directory()).exists()

    def test_shutdown_stops_running_sync(self, qtbot, fake_settings, qt_thread_guard):
        # Many files so the sync is still mid-run when shutdown() lands; the
        # interruption flag is polled between files/chunks, so shutdown joins
        # the thread promptly and completed files remain.
        files = {f"trip{i}.csv": b"x" * 2048 for i in range(50)}
        with _device(files) as (_, httpd):
            sync = WiCANLogSync(fake_settings, http_port=httpd.server_address[1])
            assert sync.start() is True
            sync.shutdown(timeout_ms=10000)
            assert not sync.is_running
        dest = Path(fake_settings.get_logs_directory())
        if dest.exists():
            assert list(dest.rglob("*.part")) == []

    def test_progress_signal_spans_the_run(self, qtbot, fake_settings, qt_thread_guard):
        # progress_changed must arrive on the GUI thread: determinate total
        # up front, monotonic bytes, and a final done == total emit.
        files = {"b.csv": b"n" * 3000, "a.csv": b"o" * 1000}
        events = []
        with _device(files) as (_, httpd):
            sync = WiCANLogSync(fake_settings, http_port=httpd.server_address[1])
            sync.progress_changed.connect(lambda d, t, n: events.append((d, t, n)))
            assert sync.start() is True
            _wait_sync_done(qtbot, sync)
        assert events, "no progress events reached the GUI thread"
        total = 4000
        assert events[0] == (0, total, "")
        assert events[-1] == (total, total, "a.csv")
        dones = [d for d, _, _ in events]
        assert dones == sorted(dones)

    def test_cancel_stops_mid_run_without_blocking(
        self, qtbot, fake_settings, qt_thread_guard, caplog
    ):
        # cancel() (the dialog's Cancel button) must return immediately and
        # let the worker wind down through the normal finished path: back to
        # idle, no .part residue, completed files remain — and NEVER through
        # the error path (a deliberate cancel must not log "check skipped",
        # whether it lands between files or mid-file between chunks).
        files = {f"trip{i}.csv": b"x" * 2048 for i in range(50)}
        with caplog.at_level("INFO", logger="src.ui.wican_log_sync"):
            with _device(files) as (_, httpd):
                sync = WiCANLogSync(fake_settings, http_port=httpd.server_address[1])
                assert sync.start() is True
                sync.cancel()  # non-blocking; teardown rides running_changed
                _wait_sync_done(qtbot, sync)
                assert not sync.is_running
        assert "WiCAN trip-log check skipped" not in caplog.text
        dest = Path(fake_settings.get_logs_directory())
        if dest.exists():
            assert list(dest.rglob("*.part")) == []
        # Idle cancel is a no-op.
        sync.cancel()

    def test_bulk_quiet_window_wraps_the_download(
        self, qtbot, fake_settings, monkeypatch, qt_thread_guard
    ):
        # While the sync monopolizes the device httpd, an existing datalog
        # client's keepalive logging is quieted — set before the transfer,
        # cleared after, even though no client is ever CREATED by the sync.
        calls = []

        class _FakeDatalogClient:
            def set_bulk_transfer(self, active):
                calls.append(active)

        import src.ecu.wican_config as wican_config

        fake = _FakeDatalogClient()
        monkeypatch.setattr(wican_config, "peek_datalog_client", lambda *a, **k: fake)
        with _device({"trip.csv": b"data\n"}) as (_, httpd):
            sync = WiCANLogSync(fake_settings, http_port=httpd.server_address[1])
            assert sync.start() is True
            _wait_sync_done(qtbot, sync)
        assert calls == [True, False]

    def test_schedule_auto_start_defers_when_enabled(
        self, fake_settings, monkeypatch, qt_thread_guard
    ):
        # The owner (not main.py) owns the launch policy: the enable toggle
        # and the defer both live in schedule_auto_start.
        fake_settings.get_wican_auto_download_logs.return_value = True
        scheduled = []

        class _FakeTimer:
            @staticmethod
            def singleShot(ms, cb):
                scheduled.append((ms, cb))

        monkeypatch.setattr(wican_log_sync_module, "QTimer", _FakeTimer)
        sync = WiCANLogSync(fake_settings)
        sync.schedule_auto_start()
        assert scheduled == [(WiCANLogSync._AUTO_START_DELAY_MS, sync.start)]

    def test_schedule_auto_start_honors_disabled_toggle(
        self, fake_settings, monkeypatch, qt_thread_guard
    ):
        fake_settings.get_wican_auto_download_logs.return_value = False
        scheduled = []

        class _FakeTimer:
            @staticmethod
            def singleShot(ms, cb):
                scheduled.append((ms, cb))

        monkeypatch.setattr(wican_log_sync_module, "QTimer", _FakeTimer)
        sync = WiCANLogSync(fake_settings)
        sync.schedule_auto_start()
        assert scheduled == []
