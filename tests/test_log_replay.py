"""Log replay: reading a log, column mapping, pacing, and the Datalogger path."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core.live_csv import TRAIL_MIN_ROWS
from src.core.log_replay import (
    DEFAULT_ROW_S,
    GAP_PLAY_S,
    ReplayLogError,
    ReplayPlayer,
    load_log,
    map_columns,
)

FIXTURES = Path(__file__).parent / "fixtures" / "replay"
KNOWN = ["RPM", "LOAD", "ECT", "IAT", "APP", "TPS", "VSS", "MAF_V"]


def _write(tmp_path, text, name="log.csv"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# --- reading a log --------------------------------------------------------------


def test_ncflash_capture_times_are_seconds():
    log = load_log(FIXTURES / "ncflash_capture.csv", KNOWN)
    assert log.has_time
    assert log.columns[:2] == ("RPM", "LOAD")
    assert "Time" not in log.columns
    assert log.row_count == 120
    assert log.offsets[0] == 0.0
    assert log.offsets[1] == pytest.approx(0.05)
    assert log.duration_s == pytest.approx(119 * 0.05)
    assert log.values(0)["RPM"] == pytest.approx(1500.0)


def test_wican_trip_log_times_are_milliseconds_and_gap_is_shortened():
    log = load_log(FIXTURES / "wican_trip.csv", KNOWN)
    # datetime and mark are not channels; the battery voltage is kept.
    assert log.columns == ("BATT_V", "RPM", "LOAD", "ECT")
    assert log.offsets[1] == pytest.approx(0.045)
    # The 45 s stop in the middle plays as a short pause.
    assert log.gaps == 1
    assert log.offsets[30] - log.offsets[29] == pytest.approx(GAP_PLAY_S)
    assert log.duration_s == pytest.approx(58 * 0.045 + GAP_PLAY_S)


def test_empty_cell_keeps_the_last_logged_value():
    log = load_log(FIXTURES / "wican_trip.csv", KNOWN)
    assert log.cells[3][3] == ""  # ECT is logged every 8th row
    assert log.values(3)["ECT"] == 70.0
    assert log.values(9)["ECT"] == 71.0


def test_log_without_time_column_plays_at_the_default_pace(tmp_path):
    log = load_log(_write(tmp_path, "RPM,LOAD\n1000,0.3\n1100,0.4\n1200,0.5\n"))
    assert not log.has_time
    assert log.offsets == pytest.approx((0.0, DEFAULT_ROW_S, 2 * DEFAULT_ROW_S))


def test_time_going_backwards_counts_as_a_gap(tmp_path):
    text = "timestamp_ms,RPM\n9000,1000\n9100,1100\n200,1200\n300,1300\n"
    log = load_log(_write(tmp_path, text))
    assert log.gaps == 1
    assert log.offsets == pytest.approx((0.0, 0.1, 0.1 + GAP_PLAY_S, 0.2 + GAP_PLAY_S))


def test_unreadable_lines_are_skipped_and_counted(tmp_path):
    text = (
        "Time,RPM,LOAD\n"
        "s,rpm,g/rev\n"  # a units row
        "0.000,1000,0.3\n"
        "0.100,1100\n"  # cut off
        "oops,1200,0.5\n"  # no time
        "0.300,1300,0.6\n"
    )
    log = load_log(_write(tmp_path, text))
    assert log.row_count == 2
    assert log.skipped_rows == 3
    assert log.offsets == pytest.approx((0.0, 0.3))


@pytest.mark.parametrize(
    "text, words",
    [
        ("", "empty"),
        ("1,2,3\n4,5,6\n", "no header"),
        ("Time,datetime\n0.1,now\n", "no channel columns"),
        ("Time,RPM\nabc,def\n", "no rows"),
        ("Time,RPM\n", "no rows"),
    ],
)
def test_malformed_file_is_refused_with_a_reason(tmp_path, text, words):
    with pytest.raises(ReplayLogError, match=words):
        load_log(_write(tmp_path, text))


def test_missing_and_binary_files_are_refused(tmp_path):
    with pytest.raises(ReplayLogError):
        load_log(tmp_path / "nope.csv")
    blob = tmp_path / "rom.bin"
    blob.write_bytes(bytes(range(256)) * 8)
    with pytest.raises(ReplayLogError):
        load_log(blob)


# --- column names ---------------------------------------------------------------


def test_columns_map_to_known_channels():
    names = ["rpm", "Engine Load (%)", "Coolant Temp", "KNOCKR", "Boost"]
    assert map_columns(names, KNOWN) == ["RPM", "LOAD", "ECT", "KNOCKR", "Boost"]


def test_a_name_already_in_the_log_is_not_given_to_another_column():
    assert map_columns(["RPM", "Engine RPM"], KNOWN) == ["RPM", "Engine RPM"]
    assert map_columns(["Speed", "Vehicle Speed"], KNOWN) == ["VSS", "Vehicle Speed"]


def test_renamed_columns_are_reported(tmp_path):
    log = load_log(_write(tmp_path, "Time,Engine RPM,LOAD\n0,1000,0.3\n"), KNOWN)
    assert log.columns == ("RPM", "LOAD")
    assert log.renamed == (("Engine RPM", "RPM"),)
    assert log.header == "Time,RPM,LOAD"


# --- pacing ---------------------------------------------------------------------


def _log(tmp_path, times):
    rows = "".join(f"{t},{1000 + i}\n" for i, t in enumerate(times))
    return load_log(_write(tmp_path, "Time,RPM\n" + rows))


def test_rows_come_due_at_their_recorded_pace(tmp_path):
    player = ReplayPlayer(_log(tmp_path, [0, 0.1, 0.2, 0.3, 0.4]))
    assert player.advance(0.0) == [0]
    assert player.advance(0.05) == []
    assert player.advance(0.06) == [1]
    assert player.advance(0.2) == [2, 3]
    assert not player.at_end
    assert player.advance(0.2) == [4]
    assert player.at_end
    assert player.advance(1.0) == []


def test_speed_plays_faster(tmp_path):
    player = ReplayPlayer(_log(tmp_path, [0, 0.1, 0.2, 0.3, 0.4]), speed=2.0)
    assert player.advance(0.1) == [0, 1, 2]
    player.speed = 5.0
    assert player.advance(0.04) == [3, 4]
    with pytest.raises(ValueError):
        player.speed = 0


def test_seek_continues_from_the_chosen_row(tmp_path):
    player = ReplayPlayer(_log(tmp_path, [0, 0.1, 0.2, 0.3, 0.4]))
    player.advance(0.4)
    player.seek(1)
    assert player.position == 1
    assert player.advance(0.0) == [1]
    assert player.advance(0.1) == [2]
    player.seek(99)  # past the end: the last row
    assert player.advance(0.0) == [4]


def test_loop_starts_over_at_the_end(tmp_path):
    player = ReplayPlayer(_log(tmp_path, [0, 0.1, 0.2]), loop=True)
    assert player.advance(0.3) == [0, 1, 2]
    assert player.advance(0.3) == [0]  # nothing carries across the end
    assert player.advance(0.1) == [1]


def test_a_stalled_clock_does_not_release_a_burst(tmp_path):
    player = ReplayPlayer(_log(tmp_path, [i * 0.1 for i in range(100)]))
    assert len(player.advance(30.0)) <= 6


# --- the replay source (Qt) -----------------------------------------------------


class _Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


@pytest.fixture
def replay(qapp, tmp_path):
    from src.ui.live_values import LiveValues
    from src.ui.log_replay import LogReplay

    settings = MagicMock()
    settings.get_logs_directory.return_value = str(tmp_path / "logs")
    clock = _Clock()
    live = LiveValues()
    source = LogReplay(settings, live_values=live, clock=clock)
    source.clock = clock
    source.live = live
    yield source
    source.stop()


def _step(source, seconds):
    source.clock.now += seconds
    source._on_tick()


def _csv_rows(path):
    return Path(path).read_text(encoding="utf-8").splitlines()


def test_replay_feeds_live_values_and_grows_a_live_csv(replay, tmp_path):
    states = []
    replay.running_changed.connect(states.append)
    assert replay.start(FIXTURES / "ncflash_capture.csv", KNOWN)
    assert replay.is_running and states == [True]
    capture = replay.current_capture
    assert Path(capture).parent == tmp_path / "logs" / "live"
    # The first row shows at once, and is already on disk for a tailer.
    assert replay.live.current()["RPM"] == pytest.approx(1500.0)
    assert _csv_rows(capture)[0] == "Time,RPM,LOAD,MAF,SPARKADV,ECT,IAT,APP"
    assert len(_csv_rows(capture)) == 2

    _step(replay, 0.2)
    rows = _csv_rows(capture)
    assert len(rows) == 6  # header + rows at 0, 0.05, 0.10, 0.15, 0.20 s
    assert rows[-1].startswith("0.200,")
    assert replay.position == 4

    replay.stop()
    assert not replay.is_running and states == [True, False]
    assert replay.live.current() == {}
    assert replay.current_capture == ""
    assert len(_csv_rows(capture)) == 6  # the file stays, closed


def test_replay_values_do_not_go_stale_while_paused(replay):
    replay.start(FIXTURES / "ncflash_capture.csv", KNOWN)
    replay.set_paused(True)
    assert replay.is_paused
    assert not replay.live._stale.isActive()
    assert replay.live.current()


def test_pause_holds_the_position_and_seek_moves_the_cursor(replay):
    replay.start(FIXTURES / "ncflash_capture.csv", KNOWN)
    capture = replay.current_capture
    positions = []
    replay.position_changed.connect(lambda i, s: positions.append((i, s)))
    replay.set_paused(True)
    written = len(_csv_rows(capture))

    replay.seek(70)
    assert replay.position == 70
    assert positions[-1] == (70, pytest.approx(3.5))
    log_rpm = replay.log.values(70)["RPM"]
    assert replay.live.current()["RPM"] == log_rpm
    assert len(_csv_rows(capture)) == written  # only played rows are written

    replay.set_paused(False)
    _step(replay, 0.125)  # rows 70, 71 and 72 are played from the new position
    assert replay.position == 72
    assert len(_csv_rows(capture)) == written + 3


def test_csv_time_keeps_rising_across_a_jump_back(replay):
    replay.start(FIXTURES / "ncflash_capture.csv", KNOWN)
    _step(replay, 0.3)
    replay.seek(2)
    _step(replay, 0.1)
    times = [float(r.split(",")[0]) for r in _csv_rows(replay.current_capture)[1:]]
    assert times == sorted(times)
    assert len(set(times)) == len(times)


def test_replay_pauses_at_the_end_and_play_starts_again(replay):
    replay.set_speed(5.0)
    replay.start(FIXTURES / "wican_trip.csv", KNOWN)
    for _ in range(40):
        _step(replay, 0.5)
    assert replay.is_running and replay.is_paused
    assert replay.position == replay.log.row_count - 1
    assert replay.live.current()  # the last point stays up
    replay.set_paused(False)
    _step(replay, 0.0)
    assert replay.position == 0


def test_loop_keeps_playing_into_the_same_file(replay):
    replay.set_speed(5.0)
    replay.set_loop(True)
    replay.start(FIXTURES / "wican_trip.csv", KNOWN)
    capture = replay.current_capture
    for _ in range(40):
        _step(replay, 0.5)
    assert not replay.is_paused
    assert replay.current_capture == capture
    assert len(_csv_rows(capture)) > replay.log.row_count + 1


def test_capture_ready_fires_once_for_megalogviewer(replay):
    ready = []
    replay.capture_ready.connect(ready.append)
    replay.set_speed(5.0)
    replay.start(FIXTURES / "ncflash_capture.csv", KNOWN)
    for _ in range(20):
        _step(replay, 0.5)
    assert ready == [ready[0]]
    assert len(_csv_rows(ready[0])) > TRAIL_MIN_ROWS


def test_malformed_file_does_not_start_and_says_why(replay, tmp_path):
    statuses = []
    replay.status_changed.connect(statuses.append)
    bad = _write(tmp_path, "1,2,3\n4,5,6\n")
    assert not replay.start(bad)
    assert not replay.is_running
    assert "no header" in statuses[-1]
    assert not (tmp_path / "logs" / "live").exists()  # no empty capture left


def test_second_start_is_refused_while_one_runs(replay):
    assert replay.start(FIXTURES / "ncflash_capture.csv")
    assert not replay.start(FIXTURES / "wican_trip.csv")


# --- through the Datalogger -----------------------------------------------------


@pytest.fixture
def datalogger(qapp, tmp_path):
    from src.ui.datalogger import Datalogger

    settings = MagicMock()
    settings.get_logs_directory.return_value = str(tmp_path / "logs")
    settings.get_datalog_channels.return_value = []
    settings.is_wican_adapter.return_value = False
    settings.get_live_datalog_auto_trail.return_value = False
    blocker = MagicMock(return_value="The ECU window is connected.")
    dl = Datalogger(settings, blocker=blocker)
    yield dl
    dl.shutdown(1000)


def test_replay_needs_no_ecu_and_is_not_a_live_run(datalogger):
    captures = []
    datalogger.capture_changed.connect(captures.append)
    # The ECU window holds the link (blocker): a replay starts anyway.
    assert datalogger.start_replay(FIXTURES / "ncflash_capture.csv")
    assert datalogger.replay.is_running
    assert not datalogger.is_running  # the ECU window stays free
    assert datalogger.current_capture == captures[-1] != ""
    assert datalogger.live_values.current()["RPM"] == pytest.approx(1500.0)
    datalogger.replay.stop()
    assert datalogger.current_capture == "" == captures[-1]


def test_live_run_and_replay_exclude_each_other(datalogger):
    statuses = []
    datalogger.status_changed.connect(statuses.append)
    assert datalogger.start_replay(FIXTURES / "ncflash_capture.csv")
    assert not datalogger.start()
    assert statuses[-1] == "Stop the log replay first."
    datalogger.replay.stop()

    datalogger._thread = object()  # a host run is active
    try:
        assert not datalogger.start_replay(FIXTURES / "ncflash_capture.csv")
        assert statuses[-1] == "Stop logging first."
    finally:
        datalogger._thread = None


def test_alias_columns_reach_the_axis_channels(datalogger, tmp_path):
    log = _write(tmp_path, "Time,Engine RPM,Engine Load\n0,3000,0.5\n")
    assert datalogger.start_replay(log)
    assert datalogger.live_values.current() == {"RPM": 3000.0, "LOAD": 0.5}
