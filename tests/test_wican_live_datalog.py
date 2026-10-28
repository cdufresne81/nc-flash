"""WiCANLiveDatalog owner: real-QThread lifecycle over a fake NCDLv1 device.

Per the repo's QThread rule the worker runs on a REAL QThread — a
destroyed-while-running abort or a cross-thread slot bug does not reproduce
under a mocked thread. ``qt_thread_guard`` fails a test if Qt reports one.

``_Device`` is a loopback server that accepts one connection per script, so a
test can drop the stream and watch the worker reconnect.
"""

import socket
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import qInstallMessageHandler

from src.ui import mlv_launch
from src.ui.wican_live_datalog import TRAIL_MIN_ROWS, WiCANLiveDatalog

HELLO = b"#hello NCDLv1 fw=test\n"
HEADER = b"timestamp_ms,datetime,BATT_V,RPM,mark\n"


class _Device:
    """Accepts len(scripts) connections in turn. Each script is a list of
    byte lines sent after the banner; ``None`` in a script means "hang up
    here". After its script a connection stays open until stop()."""

    def __init__(self, *scripts):
        self._scripts = list(scripts)
        self._stop = threading.Event()
        self._conns = []
        self.accepted = 0
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(2)
        self.port = self._srv.getsockname()[1]
        self._live = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        self._srv.settimeout(0.2)
        while self._scripts and not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except OSError:
                continue
            self.accepted += 1
            self._conns.append(conn)
            script = self._scripts.pop(0)
            try:
                conn.sendall(HELLO)
                for line in script:
                    if line is None:
                        conn.shutdown(socket.SHUT_RDWR)
                        conn.close()
                        break
                    conn.sendall(line)
                else:
                    self._live = conn
            except OSError:
                pass

    def feed(self, *lines):
        for line in lines:
            self._live.sendall(line)

    def stop(self):
        self._stop.set()
        for c in self._conns:
            try:
                c.close()
            except OSError:
                pass
        self._srv.close()


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


@pytest.fixture
def settings(tmp_path):
    s = MagicMock()
    s.is_wican_adapter.return_value = True
    s.get_wican_host.return_value = "127.0.0.1"
    s.get_wican_device_id.return_value = ""  # no mDNS resolve in tests
    s.get_logs_directory.return_value = str(tmp_path / "logs")
    s.get_live_datalog_auto_trail.return_value = False
    return s


@pytest.fixture
def device():
    made = []

    def _make(*scripts):
        d = _Device(*scripts)
        made.append(d)
        return d

    yield _make
    for d in made:
        d.stop()


@pytest.fixture(autouse=True)
def no_real_mlv(monkeypatch):
    """No test may launch (or depend on) a real MegaLogViewerHD install."""
    launches = []
    monkeypatch.setattr(mlv_launch, "find_mlv", lambda: Path("mlv.exe"))
    monkeypatch.setattr(
        mlv_launch, "launch_trail", lambda path, exe=None: launches.append(path)
    )
    return launches


def _live(qtbot, settings, dev):
    live = WiCANLiveDatalog(settings, port=dev.port)
    captures = []
    statuses = []
    live.capture_changed.connect(captures.append)
    live.status_changed.connect(statuses.append)
    return live, captures, statuses


def _stop(qtbot, live):
    # running_changed(False) comes from the queued thread-finished cleanup,
    # after which the owner's state is final.
    with qtbot.waitSignal(live.running_changed, timeout=5000):
        live.stop()


def _rows(n, start=0):
    return [
        f"{i},2026-10-15 12:00:00,13.9,{800 + i},\n".encode()
        for i in range(start, start + n)
    ]


def test_rows_stream_into_a_flushed_live_csv(qtbot, settings, device, qt_thread_guard):
    dev = device([b"#session file=t1.csv cols=2\n", HEADER])
    live, captures, _ = _live(qtbot, settings, dev)
    assert live.start() is True
    qtbot.waitUntil(lambda: any(captures), timeout=5000)
    path = Path(captures[-1])
    assert path.parent.name == "live" and path.name.startswith("live_")

    dev.feed(*_rows(3))
    qtbot.waitUntil(lambda: path.read_text().count("\n") == 4, timeout=5000)
    lines = path.read_text().splitlines()
    assert lines[0] == HEADER.decode().strip()
    assert lines[3].startswith("2,")
    assert live.current_capture == str(path)
    _stop(qtbot, live)
    assert live.current_capture == ""
    assert captures[-1] == ""


def test_same_columns_continue_the_same_file(qtbot, settings, device, qt_thread_guard):
    # Device size rotation / next trip: #close + #session + identical header.
    # MLV trails ONE file, so the capture must keep growing, not split.
    dev = device([b"#session file=t1.csv cols=2\n", HEADER, *_rows(2)])
    live, captures, _ = _live(qtbot, settings, dev)
    live.start()
    qtbot.waitUntil(lambda: any(captures), timeout=5000)
    path = Path(captures[-1])
    dev.feed(b"#close\n", b"#session file=t2.csv cols=2\n", HEADER, *_rows(2, 10))
    qtbot.waitUntil(lambda: path.read_text().count("\n") == 5, timeout=5000)
    text = path.read_text()
    assert text.count("timestamp_ms") == 1
    assert [c for c in captures if c] == [str(path)]
    _stop(qtbot, live)


def test_new_columns_start_a_new_file(qtbot, settings, device, qt_thread_guard):
    other = b"timestamp_ms,datetime,BATT_V,RPM,ECT,mark\n"
    dev = device([b"#session file=t1.csv cols=2\n", HEADER, *_rows(1)])
    live, captures, _ = _live(qtbot, settings, dev)
    live.start()
    qtbot.waitUntil(lambda: any(captures), timeout=5000)
    first = Path(captures[-1])
    qtbot.waitUntil(lambda: first.read_text().count("\n") == 2, timeout=5000)
    dev.feed(
        b"#close\n", b"#session file=t2.csv cols=3\n", other, b"5,x,13.9,900,80,\n"
    )
    qtbot.waitUntil(lambda: len([c for c in captures if c]) == 2, timeout=5000)
    second = Path(captures[-1])
    assert second != first
    qtbot.waitUntil(lambda: second.read_text().count("\n") == 2, timeout=5000)
    assert second.read_text().splitlines()[0] == other.decode().strip()
    _stop(qtbot, live)


def test_reconnects_after_a_drop_and_keeps_the_capture(
    qtbot, settings, device, qt_thread_guard
):
    # Wi-Fi drop mid-trip: the device hangs up, the worker reconnects and the
    # resync (#session + same header) keeps appending to the same file.
    dev = device(
        [b"#session file=t1.csv cols=2\n", HEADER, *_rows(2), None],
        [b"#session file=t1.csv cols=2\n", HEADER, *_rows(2, 50)],
    )
    live, captures, statuses = _live(qtbot, settings, dev)
    live.start()
    qtbot.waitUntil(lambda: dev.accepted == 2, timeout=10000)
    path = Path([c for c in captures if c][0])
    qtbot.waitUntil(lambda: path.read_text().count("\n") == 5, timeout=5000)
    assert live.is_running
    assert any("reconnecting" in s for s in statuses)
    assert [c for c in captures if c] == [str(path)]
    _stop(qtbot, live)


def test_unsupported_firmware_is_a_quiet_status(qtbot, settings, qt_thread_guard):
    # Nothing listens on the port: firmware without the stream (or device off).
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    live = WiCANLiveDatalog(settings, port=port)
    statuses = []
    live.status_changed.connect(statuses.append)
    live.start()
    qtbot.waitUntil(lambda: not live.is_running, timeout=10000)
    assert any("no live datalog" in s for s in statuses)


def test_auto_trail_launches_once_when_the_capture_can_trail(
    qtbot, settings, device, no_real_mlv, qt_thread_guard
):
    settings.get_live_datalog_auto_trail.return_value = True
    dev = device([b"#session file=t1.csv cols=2\n", HEADER])
    live, captures, _ = _live(qtbot, settings, dev)
    live.start()
    qtbot.waitUntil(lambda: any(captures), timeout=5000)
    dev.feed(*_rows(TRAIL_MIN_ROWS - 1))
    qtbot.wait(300)
    assert no_real_mlv == []  # MLV gives up on a file under 50 rows
    dev.feed(*_rows(TRAIL_MIN_ROWS, 100))
    qtbot.waitUntil(lambda: len(no_real_mlv) == 1, timeout=5000)
    assert no_real_mlv[0] == Path(captures[-1])
    _stop(qtbot, live)
    assert len(no_real_mlv) == 1


def test_auto_trail_off_never_launches(
    qtbot, settings, device, no_real_mlv, qt_thread_guard
):
    dev = device([b"#session file=t1.csv cols=2\n", HEADER, *_rows(TRAIL_MIN_ROWS + 5)])
    live, captures, _ = _live(qtbot, settings, dev)
    live.start()
    qtbot.waitUntil(lambda: any(captures), timeout=5000)
    path = Path(captures[-1])
    qtbot.waitUntil(
        lambda: path.read_text().count("\n") == TRAIL_MIN_ROWS + 6, timeout=5000
    )
    qtbot.wait(200)
    assert no_real_mlv == []
    _stop(qtbot, live)


def test_start_is_a_noop_without_the_wican_adapter(settings):
    settings.is_wican_adapter.return_value = False
    assert WiCANLiveDatalog(settings).start() is False


def test_shutdown_when_idle_is_safe(settings):
    WiCANLiveDatalog(settings).shutdown()


# --- review follow-ups ------------------------------------------------------


def test_silent_dead_link_is_detected_and_reconnected(
    qtbot, settings, device, monkeypatch, qt_thread_guard
):
    # The device slept or rebooted: the socket stays "open" on the PC but
    # nothing ever arrives (the reset never got out). Silence past the idle
    # timeout must count as a dead link and feed the reconnect path.
    from src.ui import wican_live_datalog as mod

    monkeypatch.setattr(mod, "STREAM_IDLE_TIMEOUT_S", 0.8)
    dev = device(
        [b"#session file=t1.csv cols=2\n", HEADER, *_rows(2)],  # then silent
        [b"#session file=t1.csv cols=2\n", HEADER, *_rows(2, 50)],
    )
    live, captures, _ = _live(qtbot, settings, dev)
    live.start()
    qtbot.waitUntil(lambda: dev.accepted == 2, timeout=10000)
    path = Path([c for c in captures if c][0])
    qtbot.waitUntil(lambda: path.read_text().count("\n") == 5, timeout=5000)
    assert live.is_running
    _stop(qtbot, live)


def test_bye_replaced_stops_without_reconnecting(
    qtbot, settings, device, qt_thread_guard
):
    dev = device(
        [b"#session file=t1.csv cols=2\n", HEADER, *_rows(1), b"#bye replaced\n"],
        [b"#idle\n"],  # a reconnect would land here
    )
    live, _captures, statuses = _live(qtbot, settings, dev)
    with qtbot.waitSignal(live.running_changed, timeout=5000) as blocker:
        live.start()
    with qtbot.waitSignal(live.running_changed, timeout=8000):
        pass
    assert blocker.args == [True]
    qtbot.wait(2500)  # longer than the reconnect delay
    assert dev.accepted == 1
    assert statuses[-1] == "Stopped: another app took over the live stream"


def test_device_restart_starts_a_new_capture(qtbot, settings, device, qt_thread_guard):
    # timestamp_ms counts from device boot; it going backwards means the
    # device restarted between trips, so time must not run backwards in a file.
    dev = device([b"#session file=t1.csv cols=2\n", HEADER, *_rows(2, 500)])
    live, captures, _ = _live(qtbot, settings, dev)
    live.start()
    qtbot.waitUntil(lambda: any(captures), timeout=5000)
    first = Path(captures[-1])
    qtbot.waitUntil(lambda: first.read_text().count("\n") == 3, timeout=5000)
    dev.feed(b"#close\n", b"#session file=t2.csv cols=2\n", HEADER, *_rows(2, 10))
    qtbot.waitUntil(lambda: len([c for c in captures if c]) == 2, timeout=5000)
    second = Path(captures[-1])
    qtbot.waitUntil(lambda: second.read_text().count("\n") == 3, timeout=5000)
    assert second.read_text().splitlines()[0] == HEADER.decode().strip()
    assert first.read_text().count("\n") == 3
    _stop(qtbot, live)


def test_nohdr_never_continues_a_headerless_capture(
    qtbot, settings, device, qt_thread_guard
):
    dev = device([b"#session file=t1.csv cols=2\n", b"#nohdr\n", *_rows(1)])
    live, captures, _ = _live(qtbot, settings, dev)
    live.start()
    qtbot.waitUntil(lambda: any(captures), timeout=5000)
    dev.feed(b"#close\n", b"#session file=t2.csv cols=2\n", b"#nohdr\n", *_rows(1, 5))
    qtbot.waitUntil(lambda: len([c for c in captures if c]) == 2, timeout=5000)
    _stop(qtbot, live)


def test_start_refused_until_the_previous_run_is_cleaned_up(settings):
    # is_running goes false before the queued cleanup runs; a start in that
    # gap would let the old cleanup deleteLater() the new, running thread.
    live = WiCANLiveDatalog(settings)
    live._thread = object()  # finished, cleanup still queued
    assert live.start() is False


def test_auto_trail_follows_a_new_capture_after_a_device_restart(
    qtbot, settings, device, no_real_mlv, qt_thread_guard
):
    # The MLV already open keeps following the old, finished file, so a new
    # capture (timestamps went backwards) gets its own trail.
    settings.get_live_datalog_auto_trail.return_value = True
    dev = device(
        [b"#session file=t1.csv cols=2\n", HEADER, *_rows(TRAIL_MIN_ROWS, 1000)]
    )
    live, captures, _ = _live(qtbot, settings, dev)
    live.start()
    qtbot.waitUntil(lambda: len(no_real_mlv) == 1, timeout=5000)
    dev.feed(b"#close\n", b"#session file=t2.csv cols=2\n", HEADER)
    dev.feed(*_rows(TRAIL_MIN_ROWS, 10))
    qtbot.waitUntil(lambda: len(no_real_mlv) == 2, timeout=5000)
    caps = [Path(c) for c in captures if c]
    assert no_real_mlv == caps
    _stop(qtbot, live)


def test_a_clean_reconnect_skips_the_host_lookup(
    qtbot, settings, device, monkeypatch, qt_thread_guard
):
    # The lookup takes up to 3 s and can't be interrupted, so it runs only at
    # start and after a connect that failed, not on every reconnect.
    from src.ecu import wican_discovery

    lookups = []

    def fake_resolve(device_id, fallback_host, *a, **k):
        lookups.append(fallback_host)
        return fallback_host

    monkeypatch.setattr(wican_discovery, "resolve_host_with_fallback", fake_resolve)
    dev = device(
        [b"#session file=t1.csv cols=2\n", HEADER, *_rows(1), None],
        [b"#session file=t1.csv cols=2\n", HEADER, *_rows(1, 50)],
    )
    live, _captures, _ = _live(qtbot, settings, dev)
    live.start()
    qtbot.waitUntil(lambda: dev.accepted == 2, timeout=10000)
    _stop(qtbot, live)
    assert len(lookups) == 1


def test_rows_feed_the_live_table_cursor(qtbot, settings, device, qt_thread_guard):
    from src.ui.live_values import LiveValues

    values = LiveValues()
    seen = []
    values.changed.connect(seen.append)
    dev = device([b"#session file=t1.csv cols=2\n", HEADER])
    live = WiCANLiveDatalog(settings, port=dev.port, live_values=values)
    assert live.start() is True
    qtbot.waitUntil(lambda: bool(live.current_capture), timeout=5000)
    dev.feed(*_rows(3))
    qtbot.waitUntil(lambda: any(seen), timeout=5000)
    assert next(v for v in seen if v) == {"RPM": 800.0}  # no device columns
    _stop(qtbot, live)
    assert seen[-1] == {}


def test_no_column_names_no_live_cursor(qtbot, settings, device, qt_thread_guard):
    # "#nohdr": the capture may reuse an older header when the count matches,
    # but the cursor must not guess which column is RPM.
    from src.ui.live_values import LiveValues

    values = LiveValues()
    seen = []
    values.changed.connect(seen.append)
    dev = device([b"#session file=t1.csv cols=2\n", b"#nohdr\n"])
    live = WiCANLiveDatalog(settings, port=dev.port, live_values=values)
    assert live.start() is True
    qtbot.waitUntil(lambda: bool(live.current_capture), timeout=5000)
    dev.feed(*_rows(3))
    qtbot.wait(400)
    _stop(qtbot, live)
    assert not any(seen)
