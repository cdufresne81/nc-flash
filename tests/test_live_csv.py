"""The shared live CSV capture both Datalogger engines write through."""

from src.core.live_csv import TRAIL_MIN_ROWS, LiveCsvCapture, StatusThrottle


def test_header_rows_flush_and_ready_once(tmp_path):
    cap = LiveCsvCapture(tmp_path / "live")
    path = cap.open("Time,RPM")
    ready = [cap.write_row(f"{i},800") for i in range(TRAIL_MIN_ROWS + 5)]
    assert ready.count(True) == 1 and ready[TRAIL_MIN_ROWS - 1] is True
    # Flushed per row: readable while still open (MLV tails the file).
    lines = open(path, encoding="utf-8").read().splitlines()
    assert lines[0] == "Time,RPM" and len(lines) == TRAIL_MIN_ROWS + 6
    cap.close()
    assert not cap.is_open and cap.path == ""


def test_new_file_never_clobbers_one_from_the_same_second(tmp_path):
    cap = LiveCsvCapture(tmp_path)
    first = cap.open("A")
    second = cap.open("A")
    third = cap.open(None)
    assert len({first, second, third}) == 3
    cap.write_row("1")
    cap.close()
    assert open(third, encoding="utf-8").read() == "1\n"  # no header row


def test_rows_before_open_are_ignored(tmp_path):
    assert LiveCsvCapture(tmp_path).write_row("x") is False


def test_status_throttle_rows_or_seconds():
    now = [100.0]
    t = StatusThrottle(every_rows=10, every_s=1.0, clock=lambda: now[0])
    assert t.due(1)  # first call
    assert not t.due(5)
    assert t.due(11)  # 10 rows since
    now[0] += 1.5
    assert t.due(12)  # a second since
