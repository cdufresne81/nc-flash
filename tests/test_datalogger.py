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


def test_wican_adapter_runs_the_wican_engine(tmp_path):
    wican = MagicMock()
    wican.is_running = False
    wican.current_capture = ""
    dl = Datalogger(_settings(tmp_path, adapter="wican"), wican=wican)
    assert dl.engine == "wican"
    assert dl.start() is True
    wican.start.assert_called_once()
    assert not dl.host_running


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
