"""Live table cursor: position maths, axis map, live values, routing."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from src.core import axis_channels
from src.core.axis_channels import AxisMapping, load_map, resolve
from src.core.datalog_channels import BUILTIN_CHANNELS
from src.core.definition_parser import DefinitionParser
from src.core.live_cursor import (
    axis_position,
    locate,
    table_value,
)
from src.core.rom_definition import AxisType, Table, TableType
from src.ui import live_values as live_values_mod
from src.ui.live_cursor_router import LiveCursorRouter
from src.ui.live_values import LiveValues
from src.ui.wican_live_datalog import row_values
from src.utils.paths import get_app_root


def _ident(x):
    return x


RPM = AxisMapping("RPM", _ident)
LOAD = AxisMapping("LOAD", _ident, approx=True)


# --- position maths -----------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [(1000, (0.0, False)), (2500, (1.5, False)), (4000, (3.0, False))],
)
def test_axis_position_ascending(value, expected):
    assert axis_position([1000, 2000, 3000, 4000], value) == expected


def test_axis_position_descending_axis():
    fr, clamped = axis_position([100.0, 50.0, 0.0], 25.0)
    assert fr == pytest.approx(1.5) and not clamped


def test_axis_position_clamps_off_either_end():
    assert axis_position([1, 2, 3], -5) == (0.0, True)
    assert axis_position([1, 2, 3], 99) == (2.0, True)
    assert axis_position([3, 2, 1], 99) == (0.0, True)


def test_axis_position_degenerate():
    assert axis_position([], 1.0) is None
    assert axis_position([1, 2], float("nan")) is None
    assert axis_position([5.0], 5.0) == (0.0, False)
    assert axis_position([5.0], 7.0) == (0.0, True)


@pytest.mark.parametrize(
    "axis",
    [
        [float("nan")] * 4,  # unpatched ROM: 0xFF floats read as NaN
        [500.0, float("nan"), 1000.0],
        [float("nan"), 500.0, 1000.0],
        [500.0, float("inf"), 1000.0],
    ],
)
def test_an_axis_with_a_non_finite_breakpoint_places_nothing(axis):
    assert axis_position(axis, 700.0) is None


def test_an_axis_that_goes_up_and_down_places_nothing():
    assert axis_position([0.0, 50.0, 25.0, 100.0], 40.0) is None
    assert axis_position([0.0, 6400.0, 6400.0], 6400.0) is not None  # flat is ok


def test_locate_on_a_nan_axis_table_gives_no_point():
    data = dict(DATA_3D, x_axis=np.full(4, np.nan))
    assert locate(data, True, LOAD, RPM, {"RPM": 2500, "LOAD": 0.5}).point is None


DATA_3D = {
    "values": np.arange(12.0).reshape(3, 4),
    "x_axis": np.array([0.2, 0.4, 0.6, 0.8]),  # LOAD (columns)
    "y_axis": np.array([1000.0, 2000.0, 3000.0]),  # RPM (rows)
}


def test_table_value_blends_the_four_surrounding_cells():
    values = np.array([[1.0, 2.0], [3.0, 5.0]])
    assert table_value(values, 0.0, 0.0) == 1.0  # on a cell: that cell
    assert table_value(values, 0.0, 0.5) == pytest.approx(1.5)
    assert table_value(values, 0.5, 0.5) == pytest.approx((1 + 2 + 3 + 5) / 4)
    assert table_value(values, 1.0, 1.0) == 5.0  # last cell, no overrun


def test_table_value_2d_and_edges():
    values = np.array([10.0, 20.0, 40.0])
    assert table_value(values, 1.5) == pytest.approx(30.0)
    assert table_value(values, 2.0) == 40.0
    assert table_value(values, -1.0) == 10.0  # clamped like the cursor


def test_table_value_with_a_non_number_nearby_is_none():
    values = np.array([[1.0, np.nan], [3.0, 4.0]])
    assert table_value(values, 0.5, 0.5) is None
    assert table_value(values, 1.0, 0.0) == 3.0  # the NaN isn't involved


def test_locate_3d_places_the_point_and_reads_each_axis():
    p = locate(DATA_3D, True, LOAD, RPM, {"RPM": 2500, "LOAD": 0.5}).point
    assert (p.fr, p.fc) == pytest.approx((1.5, 1.5))
    assert p.readings == (("RPM", 2500, False, False), ("LOAD", 0.5, True, False))


def test_locate_reports_missing_channels():
    status = locate(DATA_3D, True, LOAD, RPM, {"RPM": 2500})
    assert status.point is None and status.missing == ("LOAD",)


def test_locate_flags_a_value_off_the_table():
    status = locate(DATA_3D, True, LOAD, RPM, {"RPM": 9000, "LOAD": 0.5})
    assert status.point.fr == 2.0
    assert status.point.readings[0] == ("RPM", 9000, False, True)


def test_locate_2d_uses_the_y_axis_only():
    data = {"values": np.array([1.0, 2.0, 3.0]), "y_axis": np.array([0, 50, 100])}
    p = locate(data, False, None, AxisMapping("ECT", _ident), {"ECT": 75}).point
    assert (p.fr, p.fc) == (1.5, 0.0)
    assert len(p.readings) == 1


def test_locate_converts_with_the_mapping_expr():
    to_kpa = AxisMapping("BARO", lambda x: x / 10)
    data = {"values": np.array([1.0, 2.0]), "y_axis": np.array([90.0, 100.0])}
    p = locate(data, False, None, to_kpa, {"BARO": 950}).point
    assert p.fr == pytest.approx(0.5)


def test_locate_needs_both_axes_on_a_3d_table():
    assert locate(DATA_3D, True, None, RPM, {"RPM": 1}).point is None


# --- axis map -----------------------------------------------------------------


def _axis(name, logparam=""):
    return Table(
        name=name,
        address="0x0",
        elements=3,
        scaling="S",
        type=TableType.THREE_D,
        axis_type=AxisType.X_AXIS,
        logparam=logparam,
    )


def test_load_map_reads_channel_expr_and_approx(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(
        json.dumps(
            {
                "axes": {
                    "A": {"channel": "RPM"},
                    "B": {"channel": "LOAD", "approx": True, "expr": "x*2"},
                    "C": {"channel": "X", "expr": "__import__('os')"},
                    "D": {"nochannel": 1},
                }
            }
        ),
        encoding="utf-8",
    )
    m = load_map(path)
    assert set(m) == {"A", "B"}  # bad entries skipped, not fatal
    assert m["B"].approx and m["B"].convert(3) == 6
    assert not m["A"].approx and m["A"].convert(3) == 3


@pytest.mark.parametrize("text", ["", "not json", "[]", '{"axes": 3}'])
def test_bad_map_file_is_empty_not_fatal(tmp_path, text):
    path = tmp_path / "m.json"
    path.write_text(text, encoding="utf-8")
    assert load_map(path) == {}
    assert load_map(tmp_path / "missing.json") == {}


def test_tcm_definitions_use_only_their_own_logparam():
    m = {"RPM": AxisMapping("RPM", _ident)}
    tcm = SimpleNamespace(model="MX5 NC TCM")
    assert not axis_channels.uses_shared_map(tcm)
    assert axis_channels.uses_shared_map(SimpleNamespace(model="MX5"))
    assert axis_channels.uses_shared_map(None)
    assert resolve(_axis("RPM"), m, shared=False) is None
    assert resolve(_axis("RPM", logparam="RPM"), m, shared=False).channel == "RPM"


def test_resolve_xml_logparam_wins_over_the_map():
    m = {"RPM_6DD8": AxisMapping("RPM", _ident)}
    assert resolve(_axis("RPM_6DD8"), m).channel == "RPM"
    assert resolve(_axis("RPM_6DD8", logparam="RPM_RAW"), m).channel == "RPM_RAW"
    assert resolve(_axis("Filter Q"), m) is None
    assert resolve(None, m) is None


def test_shipped_map_loads_and_names_only_builtin_channels():
    shipped = load_map(get_app_root() / axis_channels.MAP_FILE)
    assert shipped["RPM_6DD8"].channel == "RPM"
    assert shipped["LOAD_6F44"].approx
    builtin = {c.name for c in BUILTIN_CHANNELS}
    assert {m.channel for m in shipped.values()} <= builtin


def test_parser_reads_logparam_on_an_axis(tmp_path):
    xml = tmp_path / "d.xml"
    xml.write_text(
        """<roms><rom>
  <romid><xmlid>t</xmlid><internalidaddress>0</internalidaddress>
    <internalidstring>T</internalidstring></romid>
  <scaling name="S" units="" toexpr="x" frexpr="x" format="%0.2f" min="0"
    max="1" inc="1" storagetype="float" endian="big"/>
  <table type="3D" name="T" address="100" elements="4" scaling="S">
    <table name="LOAD_6F78" address="200" elements="2" scaling="S"
      type="X Axis" logparam="LOAD_RAM"/>
    <table name="RPM_6DD8" address="300" elements="2" scaling="S" type="Y Axis"/>
  </table>
</rom></roms>""",
        encoding="utf-8",
    )
    table = DefinitionParser(str(xml)).parse().get_table_by_name("T")
    assert table.x_axis.logparam == "LOAD_RAM"
    assert table.y_axis.logparam == ""


# --- WiCAN rows -----------------------------------------------------------------


def test_wican_row_values_skip_device_columns_and_bad_cells():
    header = "timestamp_ms,RPM,LOAD,batt_v,KNOCKR,mark"
    assert row_values(header, "1234,2500.5,0.42,13.9,,1") == {
        "RPM": 2500.5,
        "LOAD": 0.42,
    }


# --- live values owner + router -------------------------------------------------


def test_live_values_go_stale_and_clear(qtbot, monkeypatch):
    monkeypatch.setattr(live_values_mod, "STALE_MS", 50)
    live = LiveValues()
    seen = []
    live.changed.connect(seen.append)
    live.update({"RPM": 1000.0})
    assert live.current() == {"RPM": 1000.0}
    qtbot.waitUntil(lambda: seen[-1] == {}, timeout=2000)
    assert live.current() == {}


def test_live_values_clear_is_quiet_when_empty(qtbot):
    live = LiveValues()
    seen = []
    live.changed.connect(seen.append)
    live.clear()
    assert seen == []


class _Win:
    def __init__(self, axes=True, focus_at=0.0):
        self.has_live_cursor_axes = axes
        self.live_focus_at = focus_at
        self.calls = []
        self.foreground = []

        self.estimate = []

    def set_live_values(self, values, foreground=True):
        self.calls.append(values)
        self.foreground.append(foreground)

    def set_live_estimate(self, enabled):
        self.estimate.append(enabled)


def _router(windows, enabled=True, estimate=False):
    settings = MagicMock()
    settings.get_live_cursor_enabled.return_value = enabled
    settings.get_live_value_estimate.return_value = estimate
    live = LiveValues()
    return LiveCursorRouter(live, lambda: windows, settings), live, settings


def test_router_feeds_only_tables_with_mapped_axes(qtbot):
    mapped, unmapped = _Win(), _Win(axes=False)
    router, live, _ = _router([mapped, unmapped])
    live.update({"RPM": 1.0})
    live.clear()
    assert mapped.calls == [{"RPM": 1.0}, {}]
    assert unmapped.calls == []


def test_router_attach_shows_the_cursor_on_a_table_opened_mid_run(qtbot):
    windows = []
    router, live, _ = _router(windows)
    live.update({"RPM": 1.0})
    late = _Win()
    windows.append(late)
    router.attach(late)
    assert late.calls == [{"RPM": 1.0}]


def test_router_disabled_hides_and_saves(qtbot):
    win = _Win()
    router, live, settings = _router([win])
    live.update({"RPM": 1.0})
    router.set_enabled(False)
    settings.set_live_cursor_enabled.assert_called_once_with(False)
    live.update({"RPM": 2.0})
    assert win.calls == [{"RPM": 1.0}, {}]  # hidden, then left alone
    router.set_enabled(True)
    assert win.calls[-1] == {"RPM": 2.0}


class _Broken(_Win):
    def set_live_values(self, values, foreground=True):
        self.calls.append(values)
        if values:
            raise ValueError("bad axis")


def test_one_failing_table_never_freezes_the_others(qtbot):
    broken, good = _Broken(), _Win()
    router, live, _ = _router([broken, good])
    live.update({"RPM": 1.0})
    live.update({"RPM": 2.0})
    assert good.calls == [{"RPM": 1.0}, {"RPM": 2.0}]
    assert broken.calls[-1] == {}  # its cursor is taken down, not left stale
    router.attach(_Broken())  # opening one mid-run doesn't raise either


def test_a_few_tables_all_get_the_full_rate(qtbot):
    from src.ui import live_cursor_router as lcr

    wins = [_Win(focus_at=float(i)) for i in range(lcr.FULL_RATE_TABLES)]
    router, live, _ = _router(wins)
    live.update({"RPM": 1.0})
    assert all(w.foreground == [True] for w in wins)


def test_with_many_tables_the_one_clicked_last_gets_the_full_rate(qtbot):
    # Even while MegaLogViewerHD or the Datalogger has focus: the router picks
    # the table window activated most recently, not the active window.
    from src.ui import live_cursor_router as lcr

    wins = [_Win(focus_at=float(i)) for i in range(lcr.FULL_RATE_TABLES + 1)]
    old, latest = wins[0], wins[-1]
    router, live, _ = _router(wins)
    live.update({"RPM": 1.0})
    assert old.foreground == [False] and latest.foreground == [True]
    router.attach(_Win(focus_at=0.0))  # a window just opened shows at once


def test_router_never_mutates_the_window_list(qtbot):
    windows = [_Win()]
    snapshot = list(windows)
    router, live, _ = _router(windows)
    live.update({"RPM": 1.0})
    assert windows == snapshot
    assert isinstance(router.current(), dict)


def test_a_new_table_gets_the_estimate_setting(qtbot):
    window = _Win()
    router, _, _ = _router([window], estimate=True)
    router.attach(window)
    assert window.estimate == [True]


def test_saved_settings_turn_the_estimate_on_for_open_tables(qtbot):
    mapped, unmapped = _Win(), _Win(axes=False)
    router, _, settings = _router([mapped, unmapped])
    router.reload_settings()  # unchanged: nothing to do
    assert mapped.estimate == []
    settings.get_live_value_estimate.return_value = True
    router.reload_settings()
    assert mapped.estimate == [True]
    assert unmapped.estimate == []
