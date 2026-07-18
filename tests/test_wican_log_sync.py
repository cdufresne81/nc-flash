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

from src.ecu.wican_logs import STATUS_DOWNLOADED, STATUS_NEW
from src.ui.wican_log_sync import (
    WiCANLogSync,
    estimate_download_text,
    format_size,
)
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


def _wait_check_done(qtbot, sync):
    """Block until the check thread is fully finished AND cleaned up.

    inventory_ready/inventory_failed fire from a queued handler while the
    worker QThread may still be winding down — a test that lets ``sync`` go
    out of scope right then can destroy a running QThread (a hard Qt abort,
    not a failure). The queued cleanup nulling ``_check_thread`` only runs
    after the thread's ``finished``, so waiting for it is the safe barrier.
    """
    qtbot.waitUntil(lambda: sync._check_thread is None, timeout=10000)


class TestInventory:
    """refresh_inventory + the startup auto-check (prompt-first, no silent
    download): real QThreads over the fake device, like the sync tests."""

    def test_manual_refresh_emits_entries_without_announcing(
        self, qtbot, fake_settings, qt_thread_guard
    ):
        dest = Path(fake_settings.get_logs_directory())
        dest.mkdir(parents=True)
        (dest / "old.csv").write_bytes(b"12345")
        files = {"new.csv": b"fresh", "old.csv": b"54321"}
        announced = []
        checking = []
        with _device(files) as (_, httpd):
            sync = WiCANLogSync(fake_settings, http_port=httpd.server_address[1])
            sync.new_logs_available.connect(lambda c, b: announced.append((c, b)))
            sync.checking_changed.connect(checking.append)
            with qtbot.waitSignal(sync.inventory_ready, timeout=10000) as blocker:
                assert sync.refresh_inventory() is True
            _wait_check_done(qtbot, sync)
        entries = blocker.args[0]
        assert [e.status for e in entries] == [STATUS_NEW, STATUS_DOWNLOADED]
        # A manual refresh must NEVER trigger the startup prompt.
        assert announced == []
        assert not sync.is_checking
        # checking_changed(False) is the consumer's re-enable signal — it must
        # arrive even though it fires from the queued cleanup, after ready.
        assert checking == [True, False]

    def test_unreachable_device_emits_failed_quietly(
        self, qtbot, fake_settings, qt_thread_guard
    ):
        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            closed_port = s.getsockname()[1]
        sync = WiCANLogSync(fake_settings, http_port=closed_port)
        with qtbot.waitSignal(sync.inventory_failed, timeout=10000):
            assert sync.refresh_inventory() is True
        _wait_check_done(qtbot, sync)
        assert not sync.is_checking

    def test_refresh_refused_while_download_runs(self, fake_settings, qt_thread_guard):
        # A download owns the device's SD/WiFi; the finished download
        # refreshes anyway (running_changed(False) → window refresh).
        sync = WiCANLogSync(fake_settings)
        sync._thread = MagicMock(isRunning=lambda: True)
        assert sync.refresh_inventory() is False
        sync._thread = None

    def test_auto_check_announces_new_logs_with_totals(
        self, qtbot, fake_settings, qt_thread_guard
    ):
        files = {"a.csv": b"x" * 3000, "b.csv": b"y" * 1000}
        fake_settings.get_wican_auto_download_logs.return_value = True
        with _device(files) as (_, httpd):
            sync = WiCANLogSync(fake_settings, http_port=httpd.server_address[1])
            with qtbot.waitSignal(sync.new_logs_available, timeout=10000) as blocker:
                assert sync.schedule_auto_check(delay_ms=0) is True
            _wait_check_done(qtbot, sync)
        assert blocker.args == [2, 4000]

    def test_auto_check_is_silent_when_nothing_is_new(
        self, qtbot, fake_settings, qt_thread_guard
    ):
        dest = Path(fake_settings.get_logs_directory())
        dest.mkdir(parents=True)
        (dest / "a.csv").write_bytes(b"12345")
        announced = []
        with _device({"a.csv": b"54321"}) as (_, httpd):
            sync = WiCANLogSync(fake_settings, http_port=httpd.server_address[1])
            sync.new_logs_available.connect(lambda c, b: announced.append((c, b)))
            with qtbot.waitSignal(sync.inventory_ready, timeout=10000):
                assert sync.schedule_auto_check(delay_ms=0) is True
            _wait_check_done(qtbot, sync)
        assert announced == []

    def test_auto_check_honors_toggle_and_adapter(self, fake_settings):
        fake_settings.get_wican_auto_download_logs.return_value = False
        sync = WiCANLogSync(fake_settings)
        assert sync.schedule_auto_check(delay_ms=0) is False

        fake_settings.get_wican_auto_download_logs.return_value = True
        fake_settings.is_wican_adapter.return_value = False
        assert sync.schedule_auto_check(delay_ms=0) is False

    def test_shutdown_joins_a_running_check(
        self, qtbot, fake_settings, qt_thread_guard
    ):
        # Hold /csv_list so the check is deterministically mid-flight when
        # shutdown() lands; the guard fixture fails the test on a
        # destroyed-while-running abort.
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
            assert sync.refresh_inventory() is True
            assert sync.refresh_inventory() is False  # one check at a time
            release.set()
            sync.shutdown(timeout_ms=10000)
            # The thread is joined; is_checking stays True until the queued
            # cleanup runs on the next event-loop turn (by design — it blocks
            # a new check from overwriting the refs early).
            _wait_check_done(qtbot, sync)
            assert not sync.is_checking
        finally:
            release.set()
            httpd.shutdown()
            httpd.server_close()
            t.join(timeout=2)


class TestFormattingHelpers:
    def test_format_size(self):
        assert format_size(512) == "1 KB"  # never "0 KB" for a real file
        assert format_size(512 * 1024) == "512 KB"
        assert format_size(int(12.4 * 1024 * 1024)) == "12.4 MB"

    def test_estimate_download_text_buckets(self):
        assert estimate_download_text(100 * 1024) == "under 10 seconds"
        assert "seconds" in estimate_download_text(10 * 1024 * 1024)
        assert "minutes" in estimate_download_text(100 * 1024 * 1024)
