"""Datalogger owner: host engine on a REAL QThread against a scripted ECU,
engine choice, the start blocker, and the WiCAN push flow."""

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QObject, Signal, qInstallMessageHandler

from src.core.datalog_channels import channels_to_saved, builtin_channels
from src.core.live_csv import TRAIL_MIN_ROWS
from src.ecu import session as session_mod
from src.ecu.protocol import UDSConnection
from src.ui import datalogger as dl_mod
from src.ui import mlv_launch
from src.ui.datalogger import Datalogger, wired_adapter_config
from tests.test_datalog_poller import FakeEcu


@pytest.fixture
def qt_thread_guard():
    messages = []

    def handler(mode, ctx, msg):
        messages.append(msg)

    old = qInstallMessageHandler(handler)
    yield
    qInstallMessageHandler(old)
    aborts = [m for m in messages if "Destroyed while thread" in m]
    assert not aborts, f"QThread lifecycle violation: {aborts}"


@pytest.fixture(autouse=True)
def no_real_mlv(monkeypatch):
    launches = []
    monkeypatch.setattr(mlv_launch, "find_mlv", lambda: Path("mlv.exe"))
    monkeypatch.setattr(
        mlv_launch, "launch_trail", lambda path, exe=None: launches.append(path)
    )
    return launches


def _settings(tmp_path, adapter="j2534", channels=None):
    s = MagicMock()
    s.get_ecu_adapter.return_value = adapter
    s.is_wican_adapter.return_value = adapter == "wican"
    s.get_j2534_dll_path.return_value = ""
    s.get_obdx_port.return_value = "COM9"
    s.get_logs_directory.return_value = str(tmp_path / "logs")
    s.get_live_datalog_auto_trail.return_value = True
    s.get_wican_host.return_value = "127.0.0.1"
    s.get_wican_device_id.return_value = ""
    s.get_datalog_channels.return_value = (
        channels_to_saved(channels) if channels is not None else {}
    )
    return s


class FakeSession(QObject):
    """Stands in for ECUSession inside the worker thread."""

    connection_lost = Signal(str)
    instances = []
    answer = True

    def __init__(self, adapter_config=None):
        super().__init__()
        self.adapter_config = adapter_config
        self.ecu = FakeEcu()
        self.uds = UDSConnection(self.ecu)
        self.is_connected = False
        self.cleaned = 0
        FakeSession.instances.append(self)

    def connect_ecu(self):
        if FakeSession.answer:
            self.is_connected = True
        else:
            self.connection_lost.emit("Connect failed: no reply")

    def cleanup(self):
        self.cleaned += 1
        self.is_connected = False


@pytest.fixture
def fake_session(monkeypatch):
    FakeSession.instances = []
    FakeSession.answer = True
    monkeypatch.setattr(session_mod, "ECUSession", FakeSession)
    return FakeSession


class FakeWican(QObject):
    """Stands in for the WiCAN stream follower (WiCANLiveDatalog)."""

    running_changed = Signal(bool)
    status_changed = Signal(str)
    capture_changed = Signal(str)

    def __init__(self):
        super().__init__()
        self.is_running = False
        self.current_capture = ""
        self.starts = 0
        self.refuse_start = False

    def start(self):
        if self.refuse_start:
            return False
        self.starts += 1
        self.is_running = True
        self.running_changed.emit(True)
        return True

    def shutdown(self, timeout_ms=5000):
        self.end("Stopped")

    def end(self, status):
        """The stream ended: a stop, or on its own with *status*."""
        if self.is_running:
            self.is_running = False
            self.status_changed.emit(status)
            self.running_changed.emit(False)


class FakeSessionClient:
    """Stands in for WiCANSessionList; scripted, records every call."""

    def __init__(self, supported=True):
        self._supported = supported
        self.calls = []
        self.tokens = 0
        self.begin_error = None
        self.apply_error = None
        self.apply_fails = (1,)  # which waits raise apply_error
        self.keepalive_errors = []  # raised by the next keepalives, in order
        self.end_error = None

    def count(self, name):
        return sum(1 for c in self.calls if c[0] == name)

    def supported(self):
        self.calls.append(("supported",))
        return self._supported

    def begin(self, rows, **kw):
        self.calls.append(("begin", rows))
        if self.begin_error is not None:
            raise self.begin_error
        self.tokens += 1
        return self.tokens

    def wait_applied(self, token=None, timeout_s=None, should_stop=None):
        self.calls.append(("wait", token))
        if self.apply_error is not None and self.count("wait") in self.apply_fails:
            raise self.apply_error
        return {"session_list": True}

    def keepalive(self, token, **kw):
        self.calls.append(("keepalive", token))
        if self.keepalive_errors:
            raise self.keepalive_errors.pop(0)
        return 29000

    def end(self, token, **kw):
        self.calls.append(("end", token))
        if self.end_error is not None:
            raise self.end_error
        return True


def _two_channels():
    chans = builtin_channels()
    for c in chans:
        c.enabled = c.name in ("RPM", "KNOCKR")
    return chans


def test_host_run_writes_a_live_csv_and_opens_mlv(
    qtbot, tmp_path, fake_session, no_real_mlv, qt_thread_guard
):
    dl = Datalogger(_settings(tmp_path, channels=_two_channels()))
    running = []
    dl.running_changed.connect(running.append)
    assert dl.engine == "host"
    assert dl.start() is True
    assert dl.is_running and dl.host_running
    qtbot.waitUntil(lambda: len(no_real_mlv) == 1, timeout=5000)
    path = Path(dl.current_capture)
    assert path.parent == tmp_path / "logs" / "live"
    t0 = time.monotonic()
    dl.stop()
    # stop() must not sit out its whole timeout (the GUI freezes meanwhile).
    assert time.monotonic() - t0 < 2.0
    qtbot.waitUntil(lambda: not dl.is_running, timeout=5000)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "Time,RPM,KNOCKR"
    assert len(lines) > TRAIL_MIN_ROWS
    assert lines[1].split(",")[1:] == ["1750", "3.52113"]
    assert running == [True, False]
    session = fake_session.instances[0]
    assert session.adapter_config == {"kind": "j2534", "dll_path": "op20pt32.dll"}
    assert session.cleaned == 1  # the cable is released
    assert dl.current_capture == ""


def test_host_run_reports_a_silent_ecu(qtbot, tmp_path, fake_session, qt_thread_guard):
    fake_session.answer = False
    dl = Datalogger(_settings(tmp_path, channels=_two_channels()))
    statuses = []
    dl.status_changed.connect(statuses.append)
    dl.start()
    qtbot.waitUntil(lambda: not dl.is_running, timeout=5000)
    assert any(s.startswith("Stopped: Connect failed") for s in statuses)
    assert fake_session.instances[0].cleaned == 1


def test_refused_channel_is_reported(qtbot, tmp_path, fake_session, qt_thread_guard):
    chans = _two_channels()
    for c in chans:
        if c.name == "VCT_ACT":
            c.enabled = True  # the fake ECU has no DID 0x16CD
    dl = Datalogger(_settings(tmp_path, channels=chans))
    dropped = []
    dl.channel_dropped.connect(lambda n, why: dropped.append(n))
    dl.start()
    qtbot.waitUntil(lambda: dropped == ["VCT_ACT"], timeout=5000)
    dl.shutdown()
    qtbot.waitUntil(lambda: not dl.is_running, timeout=5000)


def test_blocker_refuses_start(tmp_path, fake_session):
    dl = Datalogger(
        _settings(tmp_path, channels=_two_channels()),
        blocker=lambda: "Disconnect in the ECU Programming window first",
    )
    statuses = []
    dl.status_changed.connect(statuses.append)
    assert dl.start() is False
    assert statuses == ["Disconnect in the ECU Programming window first"]
    assert fake_session.instances == []


def test_no_channel_selected(tmp_path, fake_session):
    chans = builtin_channels()
    for c in chans:
        c.enabled = False
    dl = Datalogger(_settings(tmp_path, channels=chans))
    assert dl.start() is False


def test_wican_adapter_runs_the_wican_engine(qtbot, tmp_path, monkeypatch):
    wican = FakeWican()
    dl = Datalogger(_settings(tmp_path, adapter="wican"), wican=wican)
    monkeypatch.setattr(dl, "_session_client", lambda: FakeSessionClient())
    assert dl.engine == "wican"
    assert dl.start() is True
    assert dl.is_running and not dl.host_running
    qtbot.waitUntil(lambda: wican.starts == 1, timeout=5000)
    dl.stop()
    qtbot.waitUntil(lambda: not dl.is_running, timeout=5000)


def test_wired_adapter_config():
    s = MagicMock()
    s.get_ecu_adapter.return_value = "obdx"
    s.get_obdx_port.return_value = ""
    assert wired_adapter_config(s) == {"kind": "obdx", "port": ""}
    s.get_ecu_adapter.return_value = "j2534"
    s.get_j2534_dll_path.return_value = "C:/x/j2534.dll"
    assert wired_adapter_config(s) == {"kind": "j2534", "dll_path": "C:/x/j2534.dll"}


# --- WiCAN push ---------------------------------------------------------------


class FakeAutoPid:
    def __init__(self, applied="live"):
        self.applied = applied
        self.calls = []

    def push(self, rows, backup_dir):
        self.calls.append(("push", len(rows)))
        result = MagicMock()
        result.applied = self.applied
        result.backup = str(Path(backup_dir) / "auto_pid_x.json")
        result.orphaned_calculated = []
        return result

    def wait_applied(self):
        self.calls.append("wait")
        return {}

    def preview(self, rows):
        self.calls.append("preview")
        return {"removed": ["MAP"], "added": [], "orphaned": [], "rpm": True}

    last_backup = ""


def _wican_dl(tmp_path, monkeypatch, fake):
    wican = MagicMock()
    wican.is_running = False
    wican.current_capture = ""
    dl = Datalogger(_settings(tmp_path, adapter="wican"), wican=wican)
    monkeypatch.setattr(dl, "_wican_client", lambda: fake)
    return dl


def test_push_live(qtbot, tmp_path, monkeypatch, qt_thread_guard):
    fake = FakeAutoPid("live")
    dl = _wican_dl(tmp_path, monkeypatch, fake)
    finished = []
    dl.push_finished.connect(lambda ok, msg: finished.append(ok))
    assert dl.push_to_wican() is True
    assert dl.blocked_reason() is not None  # no run while the table changes
    qtbot.waitUntil(lambda: finished == [True], timeout=5000)
    qtbot.waitUntil(lambda: not dl.push_running, timeout=5000)
    assert fake.calls == [("push", len(builtin_channels())), "wait"]
    assert dl.last_backup.endswith("auto_pid_x.json")


def test_push_deferred_never_restarts_the_trip(
    qtbot, tmp_path, monkeypatch, qt_thread_guard
):
    fake = FakeAutoPid("deferred")
    dl = _wican_dl(tmp_path, monkeypatch, fake)
    finished = []
    dl.push_finished.connect(lambda ok, msg: finished.append((ok, msg)))
    dl.push_to_wican()
    qtbot.waitUntil(lambda: bool(finished) and not dl.push_running, timeout=5000)
    ok, msg = finished[0]
    assert ok and "ignition off" in msg
    assert fake.calls == [("push", len(builtin_channels()))]  # no wait, no restart


def test_preview_writes_nothing(qtbot, tmp_path, monkeypatch, qt_thread_guard):
    fake = FakeAutoPid()
    dl = _wican_dl(tmp_path, monkeypatch, fake)
    previews = []
    dl.push_preview.connect(previews.append)
    assert dl.preview_wican_push() is True
    qtbot.waitUntil(lambda: bool(previews) and not dl.push_running, timeout=5000)
    assert previews[0]["removed"] == ["MAP"]
    assert fake.calls == ["preview"]


def test_push_refused_while_the_ecu_window_holds_the_adapter(tmp_path, monkeypatch):
    fake = FakeAutoPid()
    dl = _wican_dl(tmp_path, monkeypatch, fake)
    dl._blocker = lambda: "Disconnect in the ECU Programming window first"
    results = []
    dl.push_finished.connect(lambda ok, msg: results.append((ok, msg)))
    assert dl.push_to_wican() is False
    assert dl.preview_wican_push() is False
    assert fake.calls == [] and results[0][0] is False


def test_failed_push_after_backup_offers_that_backup(
    qtbot, tmp_path, monkeypatch, qt_thread_guard
):
    fake = FakeAutoPid()
    fake.last_backup = str(tmp_path / "auto_pid_new.json")

    def wait_fails():
        raise RuntimeError("The WiCAN can't poll 1 of the channels")

    fake.wait_applied = wait_fails
    dl = _wican_dl(tmp_path, monkeypatch, fake)
    dl._last_backup = "an older backup"
    results = []
    dl.push_finished.connect(lambda ok, msg: results.append(ok))
    dl.push_to_wican()
    qtbot.waitUntil(lambda: bool(results) and not dl.push_running, timeout=5000)
    assert results == [False]
    assert dl.last_backup == fake.last_backup  # never the older one
    assert dl.failed_push_backup == fake.last_backup


def test_failure_that_changed_nothing_offers_no_restore(
    qtbot, tmp_path, monkeypatch, qt_thread_guard
):
    dl = _wican_dl(tmp_path, monkeypatch, FakeAutoPid())
    dl._last_backup = "backup of an earlier, successful push"

    def offline():
        raise RuntimeError("timed out")

    monkeypatch.setattr(dl, "_wican_client", offline)
    results = []
    dl.push_finished.connect(lambda ok, msg: results.append(ok))
    dl.preview_wican_push()
    qtbot.waitUntil(lambda: bool(results) and not dl.push_running, timeout=5000)
    assert results == [False] and dl.failed_push_backup == ""


def test_trip_opening_during_the_wait_reads_as_deferred(
    qtbot, tmp_path, monkeypatch, qt_thread_guard
):
    fake = FakeAutoPid("live")
    fake.wait_applied = lambda: {"reload_pending": True}
    dl = _wican_dl(tmp_path, monkeypatch, fake)
    finished = []
    dl.push_finished.connect(lambda ok, msg: finished.append((ok, msg)))
    dl.push_to_wican()
    qtbot.waitUntil(lambda: bool(finished) and not dl.push_running, timeout=5000)
    assert finished[0][0] is True and "ignition off" in finished[0][1]


def test_push_failure_is_reported(qtbot, tmp_path, monkeypatch, qt_thread_guard):
    dl = _wican_dl(tmp_path, monkeypatch, FakeAutoPid())

    def boom():
        raise RuntimeError("device unreachable")

    monkeypatch.setattr(dl, "_wican_client", boom)
    results = []
    dl.push_finished.connect(lambda ok, msg: results.append((ok, msg)))
    dl.push_to_wican()
    qtbot.waitUntil(lambda: bool(results), timeout=5000)
    assert results == [(False, "device unreachable")]
    qtbot.waitUntil(lambda: not dl.push_running, timeout=5000)
    assert dl.last_backup == ""  # nothing changed, so no restore is offered


def test_app_close_mid_run_does_not_abort(
    qtbot, tmp_path, fake_session, qt_thread_guard
):
    # The main window calls shutdown() and then destroys its children: the
    # worker thread must be finished by then (Qt aborts otherwise).
    from PySide6.QtCore import QObject

    parent = QObject()
    dl = Datalogger(_settings(tmp_path, channels=_two_channels()), parent=parent)
    dl.start()
    qtbot.waitUntil(lambda: bool(dl.current_capture), timeout=5000)
    dl.shutdown(3000)
    assert dl._thread is None or not dl._thread.isRunning()
    parent.deleteLater()
    qtbot.wait(50)


def test_module_has_no_sender_walks():
    # Architecture rule: signals carry their own context.
    import inspect

    src = inspect.getsource(dl_mod)
    assert "sender()" not in src and ".parent()" not in src


def test_host_run_feeds_live_values_and_clears_them(
    qtbot, tmp_path, fake_session, no_real_mlv, qt_thread_guard
):
    dl = Datalogger(_settings(tmp_path, channels=_two_channels()))
    seen = []
    dl.live_values.changed.connect(seen.append)
    assert dl.start() is True
    qtbot.waitUntil(lambda: any(v for v in seen), timeout=5000)
    first = next(v for v in seen if v)
    assert first["RPM"] == pytest.approx(1750.0)
    assert set(first) == {"RPM", "KNOCKR"}
    dl.stop()
    qtbot.waitUntil(lambda: not dl.is_running, timeout=5000)
    assert seen[-1] == {} and dl.live_values.current() == {}


class FlakyEcu(FakeEcu):
    """Answers, then fails like a USB transfer error after *fail_after* sends."""

    def __init__(self, fail_after):
        super().__init__()
        self.fail_after = fail_after

    def send_message(self, payload, timeout_ms):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise RuntimeError("USB data transfer error: LIBUSB_ERROR_IO")
        return super().send_message(payload, timeout_ms)


@pytest.fixture
def flaky_session(fake_session, monkeypatch):
    """The first session's adapter dies mid-run; later ones work (or, with
    ``answers`` False, never connect)."""
    monkeypatch.setattr(dl_mod, "RECONNECT_DELAY_S", 0.05)
    state = {"answers": True}

    class Flaky(fake_session):
        def __init__(self, adapter_config=None):
            super().__init__(adapter_config)
            if len(fake_session.instances) == 1:
                self.ecu = FlakyEcu(fail_after=200)
                self.uds = UDSConnection(self.ecu)

        def connect_ecu(self):
            if len(fake_session.instances) > 1 and not state["answers"]:
                self.connection_lost.emit("Connect failed: no reply")
                return
            super().connect_ecu()

    monkeypatch.setattr(session_mod, "ECUSession", Flaky)
    fake_session.state = state
    return fake_session


def _rows_in(path):
    return len(Path(path).read_text(encoding="utf-8").splitlines()) - 1


def test_lost_adapter_reconnects_into_the_same_capture(
    qtbot, tmp_path, flaky_session, qt_thread_guard
):
    dl = Datalogger(_settings(tmp_path, channels=_two_channels()))
    statuses, captures = [], []
    dl.status_changed.connect(statuses.append)
    dl.capture_changed.connect(lambda p: p and captures.append(p))
    assert dl.start() is True
    qtbot.waitUntil(lambda: len(flaky_session.instances) >= 2, timeout=5000)
    qtbot.waitUntil(lambda: any("reconnected 1x" in s for s in statuses), timeout=5000)
    first = flaky_session.instances[0]
    assert first.cleaned == 1  # the dead session was released first
    assert dl.is_running
    assert any(s.startswith("Connection lost (USB data transfer") for s in statuses)
    rows_at_reconnect = _rows_in(captures[0])
    qtbot.waitUntil(lambda: _rows_in(captures[0]) > rows_at_reconnect, timeout=5000)
    dl.stop()
    qtbot.waitUntil(lambda: not dl.is_running, timeout=5000)
    assert len(set(captures)) == 1  # one capture across the reconnect
    assert not any(s.startswith("Stopped:") for s in statuses)


def test_reconnect_keeps_trying_until_stopped(
    qtbot, tmp_path, flaky_session, qt_thread_guard
):
    flaky_session.state["answers"] = False
    dl = Datalogger(_settings(tmp_path, channels=_two_channels()))
    statuses = []
    dl.status_changed.connect(statuses.append)
    dl.start()
    qtbot.waitUntil(lambda: len(flaky_session.instances) >= 4, timeout=5000)
    assert dl.is_running
    assert "Reconnecting" in statuses[-1]
    t0 = time.monotonic()
    dl.stop()
    assert time.monotonic() - t0 < 2.0
    qtbot.waitUntil(lambda: not dl.is_running, timeout=5000)
    assert statuses[-1] == "Stopped"  # a user stop, not an error
    assert all(s.cleaned == 1 for s in flaky_session.instances)


def test_a_bug_while_polling_stops_instead_of_reconnecting(
    qtbot, tmp_path, fake_session, monkeypatch, qt_thread_guard
):
    from src.ecu import datalog_poller

    def broken(self):
        raise TypeError("bug")

    monkeypatch.setattr(datalog_poller.DatalogPoller, "sweep", broken)
    dl = Datalogger(_settings(tmp_path, channels=_two_channels()))
    statuses = []
    dl.status_changed.connect(statuses.append)
    dl.start()
    qtbot.waitUntil(lambda: not dl.is_running, timeout=5000)
    assert statuses[-1] == "Stopped: bug"
    assert len(fake_session.instances) == 1


# --- WiCAN run: the channels shown, for this run only ----------------------------


@pytest.fixture
def fast_keepalive(monkeypatch):
    monkeypatch.setattr(dl_mod, "SESSION_KEEPALIVE_S", 0.02)
    monkeypatch.setattr(dl_mod, "SESSION_RETRY_S", 0.02)


def _wican_run(tmp_path, monkeypatch, client, channels=None, **kw):
    wican = FakeWican()
    chans = _two_channels() if channels is None else channels
    dl = Datalogger(
        _settings(tmp_path, adapter="wican", channels=chans), wican=wican, **kw
    )
    monkeypatch.setattr(dl, "_session_client", lambda: client)
    statuses, running = [], []
    dl.status_changed.connect(statuses.append)
    dl.running_changed.connect(running.append)
    return dl, wican, statuses, running


def test_wican_run_sends_the_channels_then_follows_and_ends_on_stop(
    qtbot, tmp_path, monkeypatch, fast_keepalive, qt_thread_guard
):
    client = FakeSessionClient()
    dl, wican, statuses, running = _wican_run(tmp_path, monkeypatch, client)
    assert dl.start() is True
    assert dl.is_running and wican.starts == 0  # running while it sends
    assert dl.start() is False
    qtbot.waitUntil(lambda: client.count("keepalive") >= 2, timeout=5000)
    assert wican.starts == 1
    assert [c[0] for c in client.calls[:3]] == ["supported", "begin", "wait"]
    assert client.calls[2] == ("wait", 1)  # for the list this begin sent
    rows = client.calls[1][1]
    assert len(rows) == len(builtin_channels())  # all of them, with their flag
    assert {r["Name"] for r in rows if r["enabled"]} == {"RPM", "KNOCKR"}
    assert statuses[0] == "Sending the channels to the WiCAN..."
    t0 = time.monotonic()
    dl.stop()
    assert time.monotonic() - t0 < 2.0
    qtbot.waitUntil(lambda: not dl.is_running, timeout=5000)
    assert client.calls[-1] == ("end", 1)  # after the stream stopped
    assert not wican.is_running
    assert running == [True, False]
    assert statuses[-1] == "Stopped"


def test_old_firmware_follows_the_wicans_own_list(
    qtbot, tmp_path, monkeypatch, qt_thread_guard
):
    client = FakeSessionClient(supported=False)
    dl, wican, statuses, running = _wican_run(tmp_path, monkeypatch, client)
    assert dl.start() is True
    qtbot.waitUntil(lambda: wican.starts == 1, timeout=5000)
    qtbot.waitUntil(lambda: dl._session_thread is None, timeout=5000)
    assert dl.is_running  # the stream alone is the run
    assert client.calls == [("supported",)]  # nothing was sent
    said = [s for s in statuses if "its own channel list" in s]
    assert len(said) == 1 and "Update its firmware" in said[0]
    wican.status_changed.emit("Recording (10 rows)")
    assert statuses[-1] == "Recording (10 rows) (the WiCAN's own channel list)"
    dl.stop()
    assert not dl.is_running and running == [True, False]
    assert statuses[-1] == "Stopped"


def test_firmware_that_drops_the_endpoint_also_falls_back(
    qtbot, tmp_path, monkeypatch, qt_thread_guard
):
    from src.ecu.wican_autopid import WiCANSessionUnsupported

    client = FakeSessionClient()
    client.begin_error = WiCANSessionUnsupported("404")
    dl, wican, statuses, _ = _wican_run(tmp_path, monkeypatch, client)
    dl.start()
    qtbot.waitUntil(lambda: wican.starts == 1, timeout=5000)
    assert client.count("end") == 0
    dl.stop()
    qtbot.waitUntil(lambda: not dl.is_running, timeout=5000)


def test_busy_wican_does_not_start(qtbot, tmp_path, monkeypatch, qt_thread_guard):
    from src.ecu.wican_autopid import WiCANSessionBusy

    client = FakeSessionClient()
    client.begin_error = WiCANSessionBusy("host bus claim")
    dl, wican, statuses, running = _wican_run(tmp_path, monkeypatch, client)
    dl.start()
    qtbot.waitUntil(lambda: not dl.is_running, timeout=5000)
    assert wican.starts == 0 and client.count("end") == 0
    assert statuses[-1].startswith("Could not start: the WiCAN's link to the ECU")
    assert running == [True, False]


def test_unreachable_wican_does_not_start(
    qtbot, tmp_path, monkeypatch, qt_thread_guard
):
    dl, wican, statuses, _ = _wican_run(tmp_path, monkeypatch, FakeSessionClient())

    def offline():
        raise RuntimeError("timed out")

    monkeypatch.setattr(dl, "_session_client", offline)
    dl.start()
    qtbot.waitUntil(lambda: not dl.is_running, timeout=5000)
    assert wican.starts == 0 and statuses[-1] == "Could not start: timed out"


def test_rejected_channels_end_the_session_and_the_run(
    qtbot, tmp_path, monkeypatch, qt_thread_guard
):
    client = FakeSessionClient()
    client.apply_error = RuntimeError("The WiCAN can't poll 1 of the channels")
    dl, wican, statuses, _ = _wican_run(tmp_path, monkeypatch, client)
    dl.start()
    qtbot.waitUntil(lambda: not dl.is_running, timeout=5000)
    assert wican.starts == 0
    assert client.calls[-1] == ("end", 1)  # back to its own list at once
    assert statuses[-1] == "Could not start: The WiCAN can't poll 1 of the channels"


def test_dropped_lease_is_begun_again_and_network_errors_are_retried(
    qtbot, tmp_path, monkeypatch, fast_keepalive, qt_thread_guard
):
    from src.ecu.wican_autopid import WiCANSessionStale
    from src.ecu.wican_http import WiCANHttpError

    client = FakeSessionClient()
    client.keepalive_errors = [
        WiCANHttpError("timed out"),
        WiCANHttpError("timed out"),
        WiCANSessionStale("stale"),
    ]
    # The switch after the second begin is not confirmed (flaky link): the
    # run carries on and keeps renewing.
    client.apply_error = RuntimeError("could not switch channels right now")
    client.apply_fails = (2,)
    dl, wican, statuses, _ = _wican_run(tmp_path, monkeypatch, client)
    dl.start()
    qtbot.waitUntil(lambda: ("keepalive", 2) in client.calls, timeout=5000)
    assert client.count("begin") == 2 and client.count("wait") == 2
    assert ("wait", 2) in client.calls  # the second begin's own list
    assert dl.is_running and wican.is_running
    # Quiet: the stream reports the link, the renewals say nothing.
    assert statuses == ["Sending the channels to the WiCAN..."]
    dl.stop()
    qtbot.waitUntil(lambda: not dl.is_running, timeout=5000)
    assert client.calls[-1] == ("end", 2)  # the token of the second begin


def test_stream_ending_on_its_own_ends_the_session(
    qtbot, tmp_path, monkeypatch, fast_keepalive, qt_thread_guard
):
    client = FakeSessionClient()
    dl, wican, statuses, running = _wican_run(tmp_path, monkeypatch, client)
    dl.start()
    qtbot.waitUntil(lambda: client.count("keepalive") >= 1, timeout=5000)
    wican.end("Stopped: another app took over the live stream")
    qtbot.waitUntil(lambda: not dl.is_running, timeout=5000)
    assert client.calls[-1] == ("end", 1)
    renewals = client.count("keepalive")
    qtbot.wait(100)
    assert client.count("keepalive") == renewals  # no orphan renewals
    assert statuses[-1] == "Stopped: another app took over the live stream"
    assert running == [True, False]


def test_stream_that_cannot_start_ends_the_session(
    qtbot, tmp_path, monkeypatch, fast_keepalive, qt_thread_guard
):
    client = FakeSessionClient()
    dl, wican, statuses, _ = _wican_run(tmp_path, monkeypatch, client)
    wican.refuse_start = True
    dl.start()
    qtbot.waitUntil(lambda: not dl.is_running, timeout=5000)
    assert client.calls[-1] == ("end", 1)
    assert statuses[-1].startswith("Could not start: the live stream")


def test_stop_while_sending_never_starts_the_stream(
    qtbot, tmp_path, monkeypatch, qt_thread_guard
):
    import threading

    gate = threading.Event()
    client = FakeSessionClient()
    plain_begin = client.begin

    entered = threading.Event()

    def slow_begin(rows, **kw):
        entered.set()
        gate.wait(5)
        return plain_begin(rows, **kw)

    client.begin = slow_begin
    dl, wican, statuses, _ = _wican_run(tmp_path, monkeypatch, client)
    dl.start()
    assert entered.wait(5)
    dl.stop()
    assert dl.is_running  # until the session is ended
    assert dl.start() is False
    gate.set()
    qtbot.waitUntil(lambda: not dl.is_running, timeout=5000)
    assert wican.starts == 0
    assert client.calls[-1] == ("end", 1)  # the list it had just accepted
    assert statuses[-1] == "Stopped"


def test_wican_start_refused_until_the_previous_run_is_cleaned_up(
    qtbot, tmp_path, monkeypatch, fast_keepalive, qt_thread_guard
):
    client = FakeSessionClient()
    dl, wican, _, _ = _wican_run(tmp_path, monkeypatch, client)
    dl.start()
    qtbot.waitUntil(lambda: wican.starts == 1, timeout=5000)
    dl.stop()
    thread = dl._session_thread
    assert thread is not None
    thread.wait(5000)  # finished, but its queued cleanup has not run
    assert dl.is_running and dl.start() is False
    qtbot.waitUntil(lambda: not dl.is_running, timeout=5000)
    assert dl.start() is True  # and the next run begins a new session
    qtbot.waitUntil(lambda: client.count("begin") == 2, timeout=5000)
    dl.shutdown(3000)
    qtbot.waitUntil(lambda: not dl.is_running, timeout=5000)


def test_wican_run_needs_a_channel_and_a_wican(tmp_path, monkeypatch):
    chans = builtin_channels()
    for c in chans:
        c.enabled = False
    client = FakeSessionClient()
    dl, wican, statuses, _ = _wican_run(tmp_path, monkeypatch, client, channels=chans)
    assert dl.start() is False and not dl.is_running
    assert statuses == ["Select at least one channel."]
    dl, wican, statuses, _ = _wican_run(tmp_path, monkeypatch, client)
    dl._settings.get_wican_host.return_value = ""
    assert dl.start() is False
    assert statuses == ["Could not start: no WiCAN is set up (Settings > ECU)."]
    dl, wican, statuses, _ = _wican_run(
        tmp_path, monkeypatch, client, blocker=lambda: "ECU window is connected"
    )
    assert dl.start() is False and statuses == ["ECU window is connected"]
    assert client.calls == []


def test_app_close_mid_wican_run_ends_the_session(
    qtbot, tmp_path, monkeypatch, fast_keepalive, qt_thread_guard
):
    client = FakeSessionClient()
    client.end_error = RuntimeError("timed out")  # the lease expires by itself
    parent = QObject()
    dl, wican, _, _ = _wican_run(tmp_path, monkeypatch, client, parent=parent)
    dl.start()
    qtbot.waitUntil(lambda: wican.starts == 1, timeout=5000)
    dl.shutdown(3000)
    assert not dl._session_thread.isRunning()
    assert client.calls[-1] == ("end", 1)
    dl.stop()  # safe again, and when idle
    qtbot.waitUntil(lambda: not dl.is_running, timeout=5000)
    dl.stop()
    dl.shutdown(100)


def test_saved_list_is_not_changed_during_a_run(
    qtbot, tmp_path, monkeypatch, fast_keepalive, qt_thread_guard
):
    client = FakeSessionClient()
    dl, wican, _, _ = _wican_run(tmp_path, monkeypatch, client)
    fake = FakeAutoPid()
    monkeypatch.setattr(dl, "_wican_client", lambda: fake)
    results = []
    dl.push_finished.connect(lambda ok, msg: results.append((ok, msg)))
    dl.start()
    assert dl.push_to_wican() is False and dl.preview_wican_push() is False
    assert results[0] == (False, "Stop logging first.") and fake.calls == []
    dl.stop()
    qtbot.waitUntil(lambda: not dl.is_running, timeout=5000)
