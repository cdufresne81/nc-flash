"""WiCANLiveDatalog collaborator: real-QThread lifecycle over a fake device.

Per the repo's QThread rule these tests run the worker on a REAL QThread — a
destroyed-while-running abort or a cross-thread slot bug does not reproduce under
a mocked thread. A qInstallMessageHandler guard fails the test if Qt reports a
thread destroyed while still running.

The fake NCDLv1 device (``MockStreamServer``) is shared with
``test_ecu_wican_stream`` via pytest's ``pythonpath = tests``.
"""

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import qInstallMessageHandler

from src.ui import mlv_trail
from src.ui.wican_live_datalog import WiCANLiveDatalog
from test_ecu_wican_stream import MockStreamServer


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


@pytest.fixture
def server():
    srv = None

    def _make(**kwargs):
        nonlocal srv
        srv = MockStreamServer(**kwargs)
        srv.start()
        return srv

    yield _make
    if srv is not None:
        srv.stop()


class _FakeTripClient:
    """Stands in for the shared WiCANDatalogClient: records the live-trip
    lifecycle calls the worker/owner make, in order."""

    def __init__(self):
        self.calls = []
        self.external_stop_cb = None  # what begin_live_trip registered

    def begin_live_trip(self, on_external_stop=None):
        self.external_stop_cb = on_external_stop
        self.calls.append("begin")

    def end_live_trip(self):
        self.calls.append("end")

    def hold_silent(self):
        self.calls.append("hold")

    def release_trip_hold(self):
        self.calls.append("release_hold")


class TestWiCANLiveDatalog:
    @pytest.fixture(autouse=True)
    def no_mlv(self, monkeypatch):
        """No test may depend on (or pop a dialog from) a real MLV install
        on the dev machine; the trail tests override find_mlv explicitly."""
        monkeypatch.setattr(mlv_trail, "find_mlv", lambda: None)

    @pytest.fixture(autouse=True)
    def fake_trip(self, monkeypatch):
        """The worker drives the device's trip lifecycle through the shared
        WiCANDatalogClient factory; stub it so no HTTP ever leaves the box and
        the choreography (begin/hold/end/release) is assertable."""
        from src.ecu import wican_config

        client = _FakeTripClient()
        monkeypatch.setattr(
            wican_config,
            "get_datalog_client",
            lambda host, http_port=80: client,
        )
        return client

    def test_streams_rows_into_live_csv(
        self, qtbot, fake_settings, server, qt_thread_guard, caplog
    ):
        caplog.set_level(logging.INFO, logger="src.ui.wican_live_datalog")
        # Session + header arrive on connect; rows are fed live afterwards.
        srv = server(
            initial=[
                b"#session file=trip.csv cols=2\n",
                b"time,rpm\n",
            ]
        )
        live = WiCANLiveDatalog(fake_settings, port=srv.port)
        files = []
        live.file_changed.connect(files.append)

        assert live.start() is True
        # The #session opens a local file and emits its path (non-empty).
        qtbot.waitUntil(lambda: any(files), timeout=5000)
        path = Path(next(p for p in files if p))
        assert path.parent.name == "live"
        assert path.name.startswith("live_") and path.suffix == ".csv"
        # The session announcement names the FULL local path (not the
        # device-side name) — that line is how the user finds a live capture.
        assert f"recording -> {path}" in caplog.text

        # Feed rows over the live connection; per-row flush must make them
        # visible to a concurrent reader (that is how MLV tails the file).
        srv.feed(b"1,800\n")
        srv.feed(b"2,850\n")
        qtbot.waitUntil(
            lambda: "2,850" in path.read_text(encoding="utf-8"), timeout=5000
        )

        lines = path.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "time,rpm"  # header written first
        assert lines[1:] == ["1,800", "2,850"]

        # Clean stop: running_changed(False) fires and the file ref clears.
        with qtbot.waitSignal(
            live.running_changed,
            timeout=5000,
            check_params_cb=lambda running: running is False,
        ):
            live.stop()
        assert not live.is_running
        assert files[-1] == ""  # file_changed("") on teardown

        # The Activity Log names the capture: full local path + row count on
        # close, and the stop line points at the capture folder.
        assert f"capture saved: {path} (2 rows)" in caplog.text
        assert f"captures are in {path.parent}" in caplog.text

    def test_stop_without_session_explains_nothing_captured(
        self, qtbot, fake_settings, server, qt_thread_guard, caplog
    ):
        # The field-reported confusion: start, connect, no session, stop — the
        # Activity Log must name the capture folder up front and say nothing
        # was captured, not end on a bare "stopped".
        caplog.set_level(logging.INFO, logger="src.ui.wican_live_datalog")
        srv = server(initial=[b"#idle\n"])
        live = WiCANLiveDatalog(fake_settings, port=srv.port)
        statuses = []
        live.status_changed.connect(statuses.append)

        assert live.start() is True
        live_dir = Path(fake_settings.get_logs_directory()) / "live"
        assert str(live_dir) in caplog.text  # start line names the destination
        qtbot.waitUntil(lambda: "waiting for session…" in statuses, timeout=5000)

        # Repeated #idle (a keepalive-style device) must not spam the Activity
        # Log: the "no datalog session open" line is one-shot per run.
        srv.feed(b"#idle\n")
        qtbot.waitUntil(
            lambda: statuses.count("waiting for session…") >= 2, timeout=5000
        )
        assert caplog.text.count("has no datalog session open") == 1

        with qtbot.waitSignal(
            live.running_changed,
            timeout=5000,
            check_params_cb=lambda running: running is False,
        ):
            live.stop()
        assert "nothing was captured" in caplog.text

    def test_restart_with_idle_device_reports_nothing_captured(
        self, qtbot, fake_settings, server, qt_thread_guard, caplog
    ):
        # _captured_any is per RUN, not per instance: the owner lives for the
        # whole app, so a run that captured a session must not make a later
        # idle run claim "captures are in …" (that would reintroduce the
        # field-reported confusion the message exists to fix).
        caplog.set_level(logging.INFO, logger="src.ui.wican_live_datalog")
        srv1 = server(initial=[b"#session file=a.csv cols=1\n", b"x\n"])
        live = WiCANLiveDatalog(fake_settings, port=srv1.port)
        files = []
        statuses = []
        live.file_changed.connect(files.append)
        live.status_changed.connect(statuses.append)

        assert live.start() is True
        qtbot.waitUntil(lambda: any(files), timeout=5000)
        with qtbot.waitSignal(
            live.running_changed,
            timeout=5000,
            check_params_cb=lambda running: running is False,
        ):
            live.stop()
        assert "captures are in" in caplog.text  # run 1 captured a session
        srv1.stop()

        # Run 2 on the SAME owner against an idle device (fresh server; the
        # fixture tears this one down). Re-point the tests-only port seam.
        caplog.clear()
        srv2 = server(initial=[b"#idle\n"])
        live._port = srv2.port
        assert live.start() is True
        qtbot.waitUntil(lambda: "waiting for session…" in statuses, timeout=5000)
        with qtbot.waitSignal(
            live.running_changed,
            timeout=5000,
            check_params_cb=lambda running: running is False,
        ):
            live.stop()
        assert "nothing was captured" in caplog.text
        assert "captures are in" not in caplog.text  # no stale run-1 claim

    def test_first_capture_offers_mlv_trail_once(
        self, qtbot, fake_settings, server, qt_thread_guard, monkeypatch
    ):
        # One prompt per run, on the FIRST capture only; a rotation mid-run
        # must not pop a second dialog (the user may be driving). The dialog
        # must be NON-MODAL (an app-modal box froze every window in the field,
        # 2026-07-11) and accepting trails the NEWEST capture — the offered
        # file can rotate away (0 rows) before the user answers.
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QMessageBox

        fake_exe = Path("X:/MLV/MegaLogViewerHD.exe")
        launches = []
        monkeypatch.setattr(mlv_trail, "find_mlv", lambda: fake_exe)
        monkeypatch.setattr(
            mlv_trail,
            "launch_trail",
            lambda path, exe: launches.append((path, exe)) or True,
        )
        srv = server(initial=[b"#session file=a.csv cols=2\n", b"time,rpm\n"])
        live = WiCANLiveDatalog(fake_settings, port=srv.port)
        offers = []
        real_offer = live._show_trail_offer
        monkeypatch.setattr(
            live,
            "_show_trail_offer",
            lambda path, exe: offers.append(path) or real_offer(path, exe),
        )
        files = []
        live.file_changed.connect(files.append)

        assert live.start() is True
        qtbot.waitUntil(lambda: live._trail_box is not None, timeout=5000)
        first = next(p for p in files if p)
        box = live._trail_box
        # THE freeze regression: the offer must never block input to the app.
        assert box.windowModality() == Qt.NonModal
        # Rotate to a second session BEFORE answering: no second prompt, and
        # the eventual Yes must follow the rotation to the newest file.
        srv.feed(b"#session file=b.csv cols=2\ntime,rpm\n")
        qtbot.waitUntil(lambda: len([p for p in files if p]) >= 2, timeout=5000)
        second = [p for p in files if p][1]
        box.button(QMessageBox.StandardButton.Yes).click()
        qtbot.waitUntil(lambda: len(launches) == 1, timeout=5000)
        with qtbot.waitSignal(
            live.running_changed,
            timeout=5000,
            check_params_cb=lambda running: running is False,
        ):
            live.stop()

        assert offers == [first]  # offered once, on the first capture
        assert launches == [(Path(second), fake_exe)]  # newest, not stalest
        assert live._trail_box is None

    def test_declined_trail_never_launches(
        self, qtbot, fake_settings, server, qt_thread_guard, monkeypatch, caplog
    ):
        caplog.set_level(logging.INFO, logger="src.ui.wican_live_datalog")
        launches = []
        monkeypatch.setattr(mlv_trail, "find_mlv", lambda: Path("X:/MLV.exe"))
        monkeypatch.setattr(
            mlv_trail, "launch_trail", lambda path, exe: launches.append(path) or True
        )
        srv = server(initial=[b"#session file=a.csv cols=1\n", b"x\n"])
        live = WiCANLiveDatalog(fake_settings, port=srv.port)

        assert live.start() is True
        qtbot.waitUntil(lambda: live._trail_box is not None, timeout=5000)
        from PySide6.QtWidgets import QMessageBox

        live._trail_box.button(QMessageBox.StandardButton.No).click()
        with qtbot.waitSignal(
            live.running_changed,
            timeout=5000,
            check_params_cb=lambda running: running is False,
        ):
            live.stop()

        assert launches == []
        # Declining must not read as an error — the capture keeps going.
        assert "MLV trail declined" in caplog.text

    def test_run_end_dismisses_an_unanswered_offer(
        self, qtbot, fake_settings, server, qt_thread_guard, monkeypatch, caplog
    ):
        # Stop the stream while the offer sits open: the box must close by
        # itself (its captures are done) without logging a user "declined".
        caplog.set_level(logging.INFO, logger="src.ui.wican_live_datalog")
        launches = []
        monkeypatch.setattr(mlv_trail, "find_mlv", lambda: Path("X:/MLV.exe"))
        monkeypatch.setattr(
            mlv_trail, "launch_trail", lambda path, exe: launches.append(path) or True
        )
        srv = server(initial=[b"#session file=a.csv cols=1\n", b"x\n"])
        live = WiCANLiveDatalog(fake_settings, port=srv.port)

        assert live.start() is True
        qtbot.waitUntil(lambda: live._trail_box is not None, timeout=5000)
        with qtbot.waitSignal(
            live.running_changed,
            timeout=5000,
            check_params_cb=lambda running: running is False,
        ):
            live.stop()

        assert live._trail_box is None
        assert launches == []
        assert "MLV trail declined" not in caplog.text

    def test_no_mlv_installed_never_prompts(
        self, qtbot, fake_settings, server, qt_thread_guard, monkeypatch
    ):
        monkeypatch.setattr(mlv_trail, "find_mlv", lambda: None)
        srv = server(initial=[b"#session file=a.csv cols=1\n", b"x\n"])
        live = WiCANLiveDatalog(fake_settings, port=srv.port)
        offers = []
        monkeypatch.setattr(
            live, "_show_trail_offer", lambda path, exe: offers.append(path)
        )
        files = []
        live.file_changed.connect(files.append)

        assert live.start() is True
        qtbot.waitUntil(lambda: any(files), timeout=5000)
        with qtbot.waitSignal(
            live.running_changed,
            timeout=5000,
            check_params_cb=lambda running: running is False,
        ):
            live.stop()

        assert offers == []  # no install -> no dialog, ever

    def test_session_rotation_opens_a_second_file(
        self, qtbot, fake_settings, server, qt_thread_guard
    ):
        # A bare #session (no #close guaranteed) rotates to a NEW local file
        # without clobbering or truncating the first one.
        srv = server(initial=[b"#session file=a.csv cols=2\n", b"time,rpm\n"])
        live = WiCANLiveDatalog(fake_settings, port=srv.port)
        files = []
        live.file_changed.connect(files.append)

        assert live.start() is True
        qtbot.waitUntil(lambda: any(files), timeout=5000)
        first = Path(next(p for p in files if p))
        srv.feed(b"1,800\n")
        qtbot.waitUntil(
            lambda: "1,800" in first.read_text(encoding="utf-8"), timeout=5000
        )

        # Rotate: new session + header + row on the same socket.
        srv.feed(b"#session file=b.csv cols=2\ntime,rpm\n2,850\n")
        qtbot.waitUntil(lambda: len([p for p in files if p]) >= 2, timeout=5000)
        second = Path([p for p in files if p][1])
        assert second != first  # never reuse/clobber the just-closed file
        qtbot.waitUntil(
            lambda: "2,850" in second.read_text(encoding="utf-8"), timeout=5000
        )

        # Session 1's file is intact; session 2 has its own header + row.
        assert first.read_text(encoding="utf-8").splitlines() == ["time,rpm", "1,800"]
        assert second.read_text(encoding="utf-8").splitlines() == ["time,rpm", "2,850"]

        with qtbot.waitSignal(
            live.running_changed,
            timeout=5000,
            check_params_cb=lambda running: running is False,
        ):
            live.stop()
        assert not live.is_running

    def test_unexpected_disconnect_reports_and_returns_to_idle(
        self, qtbot, fake_settings, server, qt_thread_guard
    ):
        # Device reboots / WiFi drops mid-stream: the worker's error path must
        # emit a "stopped: …" status, return is_running to False, and clear the
        # file (a regression that skips thread.quit would strand is_running=True).
        srv = server(
            initial=[b"#session file=t.csv cols=1\n", b"x\n", b"1\n"],
            close_after_initial=True,
        )
        live = WiCANLiveDatalog(fake_settings, port=srv.port)
        statuses = []
        files = []
        live.status_changed.connect(statuses.append)
        live.file_changed.connect(files.append)

        assert live.start() is True
        qtbot.waitUntil(
            lambda: any(s.startswith("stopped:") for s in statuses), timeout=5000
        )
        qtbot.waitUntil(lambda: not live.is_running, timeout=5000)
        assert files[-1] == ""  # file_changed("") on teardown

    def test_unsupported_firmware_is_quiet(
        self, qtbot, fake_settings, server, qt_thread_guard
    ):
        # A device that does not send the NCDLv1 banner -> quiet status, no
        # dialog, and the stream returns to idle.
        srv = server(banner=b"WELCOME not-ncdl\n")
        live = WiCANLiveDatalog(fake_settings, port=srv.port)
        statuses = []
        live.status_changed.connect(statuses.append)

        assert live.start() is True
        qtbot.waitUntil(
            lambda: any("live datalog stream" in s for s in statuses),
            timeout=5000,
        )
        qtbot.waitUntil(lambda: not live.is_running, timeout=5000)

    def test_stop_during_connect_reports_stopped_not_unsupported(
        self, qtbot, fake_settings, server, qt_thread_guard
    ):
        # Server accepts but never sends the banner; the user stops mid-connect.
        # That is a clean stop — it must NOT be reported as unsupported firmware
        # or as an error (the socket closing under the banner read looks like
        # "no banner", which the worker must recognize as a requested stop).
        srv = server(banner=None)
        live = WiCANLiveDatalog(fake_settings, port=srv.port)
        statuses = []
        live.status_changed.connect(statuses.append)

        assert live.start() is True
        qtbot.waitUntil(lambda: "connecting…" in statuses, timeout=5000)
        assert srv.wait_connected(2.0)  # worker is inside connect()'s banner read

        with qtbot.waitSignal(
            live.running_changed,
            timeout=5000,
            check_params_cb=lambda running: running is False,
        ):
            live.stop()
        assert "stopped" in statuses  # clean finish
        assert not any("firmware without" in s for s in statuses)  # not unsupported
        assert not any(s.startswith("stopped:") for s in statuses)  # not an error

    def test_user_stop_takes_the_silent_hold(
        self, qtbot, fake_settings, server, qt_thread_guard, fake_trip
    ):
        # The user's contract: Stop Live Datalog also stops the DEVICE's logging.
        # Order matters — hold (park) BEFORE end (op=auto), so the device never
        # passes through an un-parked AUTO instant.
        srv = server(initial=[b"#session file=a.csv cols=1\n", b"x\n"])
        live = WiCANLiveDatalog(fake_settings, port=srv.port)
        files = []
        live.file_changed.connect(files.append)

        assert live.start() is True
        qtbot.waitUntil(lambda: any(files), timeout=5000)
        assert fake_trip.calls == ["begin"]  # a NEW leased trip started the run
        with qtbot.waitSignal(
            live.running_changed,
            timeout=5000,
            check_params_cb=lambda running: running is False,
        ):
            live.stop()
        assert fake_trip.calls == ["begin", "hold", "end"]

    def test_stream_error_frees_the_device_without_hold(
        self, qtbot, fake_settings, server, qt_thread_guard, fake_trip
    ):
        # A device reboot / WiFi drop is not a user decision: restore AUTO at
        # once, and do NOT leave the device parked behind the user's back.
        srv = server(
            initial=[b"#session file=t.csv cols=1\n", b"x\n", b"1\n"],
            close_after_initial=True,
        )
        live = WiCANLiveDatalog(fake_settings, port=srv.port)

        assert live.start() is True
        qtbot.waitUntil(lambda: not live.is_running, timeout=5000)
        assert fake_trip.calls == ["begin", "end"]

    def test_external_stop_ends_the_stream_without_parking(
        self, qtbot, fake_settings, server, qt_thread_guard, fake_trip, caplog
    ):
        # Web-UI Stop Trip mid-stream: the keepalive's op=renew 409 fires the
        # registered callback (on the keepalive thread). The stream must end
        # cleanly, take NO silent hold (the device operator owns the mode now),
        # and still run end_live_trip (re-park for ref holders + lease cleanup).
        caplog.set_level(logging.INFO, logger="src.ui.wican_live_datalog")
        srv = server(initial=[b"#session file=a.csv cols=1\n", b"x\n"])
        live = WiCANLiveDatalog(fake_settings, port=srv.port)
        statuses = []
        live.status_changed.connect(statuses.append)
        files = []
        live.file_changed.connect(files.append)

        assert live.start() is True
        qtbot.waitUntil(lambda: any(files), timeout=5000)
        assert fake_trip.external_stop_cb is not None  # begin registered it
        # Simulate the keepalive thread's 409 detection.
        import threading

        t = threading.Thread(target=fake_trip.external_stop_cb)
        t.start()
        t.join()
        qtbot.waitUntil(lambda: not live.is_running, timeout=5000)

        assert fake_trip.calls == ["begin", "end"]  # no "hold"
        assert "trip stopped from the device" in statuses
        assert "stopped from the device (web UI)" in caplog.text

    def test_unsupported_firmware_never_starts_a_trip(
        self, qtbot, fake_settings, server, qt_thread_guard, fake_trip
    ):
        # No NCDLv1 banner -> the trip must never have begun (begin runs only
        # AFTER connect() proves the firmware speaks the protocol).
        srv = server(banner=b"WELCOME not-ncdl\n")
        live = WiCANLiveDatalog(fake_settings, port=srv.port)

        assert live.start() is True
        qtbot.waitUntil(lambda: not live.is_running, timeout=5000)
        assert fake_trip.calls == []

    def test_dispose_releases_the_silent_hold(
        self, qtbot, fake_settings, server, qt_thread_guard, fake_trip
    ):
        # Closing NC Flash restores the device's autonomous trip logging: the
        # app-close dispose() drops the hold the earlier user stop took.
        srv = server(initial=[b"#session file=a.csv cols=1\n", b"x\n"])
        live = WiCANLiveDatalog(fake_settings, port=srv.port)
        files = []
        live.file_changed.connect(files.append)

        assert live.start() is True
        qtbot.waitUntil(lambda: any(files), timeout=5000)
        with qtbot.waitSignal(
            live.running_changed,
            timeout=5000,
            check_params_cb=lambda running: running is False,
        ):
            live.stop()
        live.dispose()
        assert fake_trip.calls == ["begin", "hold", "end", "release_hold"]

    def test_non_wican_adapter_is_a_noop(self, fake_settings, qt_thread_guard):
        # Product decision: dormant unless the WiCAN adapter is selected, even
        # with a valid host configured.
        fake_settings.is_wican_adapter.return_value = False
        live = WiCANLiveDatalog(fake_settings)
        assert live.start() is False
        assert not live.is_running

    def test_no_host_or_identity_is_a_noop(self, fake_settings, qt_thread_guard):
        fake_settings.get_wican_host.return_value = ""
        fake_settings.get_wican_device_id.return_value = ""
        live = WiCANLiveDatalog(fake_settings)
        assert live.start() is False
        assert not live.is_running

    def test_idle_shutdown_is_a_noop(self, fake_settings, qt_thread_guard):
        # App close with no stream running must not raise.
        live = WiCANLiveDatalog(fake_settings)
        live.shutdown()
        assert not live.is_running
