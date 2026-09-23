"""Tests for the engine-neutral graph model (src/ui/graph_model.py).

Pure numpy: colors must match the table gradient exactly (same LUT, scaling
range, clamp, degenerate → midpoint), the surface grid must cover every cell,
and labels must use real axis values and "Name (units)" titles. Ported from the
old ``_GraphPlotMixin`` color tests plus new coverage.
"""

from unittest.mock import patch

import numpy as np
import pytest

from src.core.rom_definition import (
    AxisType,
    RomDefinition,
    RomID,
    Scaling,
    Table,
    TableType,
)
from src.ui import graph_model as gm
from src.utils.colormap import ColorMap
from src.utils.formatting import get_scaling_range


def _romid():
    return RomID(
        xmlid="test",
        internalidaddress="0x0",
        internalidstring="T",
        ecuid="",
        make="",
        model="",
        flashmethod="",
        memmodel="",
        checksummodule="",
    )


def _scaling(name="S", lo=0.0, hi=100.0, units=""):
    return Scaling(
        name=name,
        units=units,
        toexpr="x",
        frexpr="x",
        format="%0.2f",
        min=lo,
        max=hi,
        inc=1.0,
        storagetype="float",
        endian="big",
    )


def _defn(**scalings):
    return RomDefinition(romid=_romid(), scalings=scalings)


def _t(name, kind, elements, scaling, **kw):
    return Table(
        name=name,
        address="0x100",
        type=kind,
        elements=elements,
        scaling=scaling,
        **kw,
    )


def _table_3d(xname="RPM", yname="Load", scaling="S", xs="SX", ys="SY"):
    x = _t(xname, TableType.THREE_D, 3, xs, axis_type=AxisType.X_AXIS)
    y = _t(yname, TableType.THREE_D, 2, ys, axis_type=AxisType.Y_AXIS)
    return _t("T3", TableType.THREE_D, 6, scaling, children=[x, y])


def _table_2d(scaling="S"):
    y = _t("APP", TableType.TWO_D, 3, "SY", axis_type=AxisType.Y_AXIS)
    return _t("T2", TableType.TWO_D, 3, scaling, children=[y])


@pytest.fixture(autouse=True)
def builtin_colormap():
    cmap = ColorMap()  # built-in LUT, independent of user settings
    with patch("src.ui.graph_model.get_colormap", return_value=cmap):
        yield cmap


# --- colors (ported from test_graph_viewer.py) -------------------------------
class TestCellColors:
    def test_shape_2d_values(self):
        assert gm.cell_colors(np.array([[1.0, 2.0], [3.0, 4.0]]), (0, 10)).shape == (
            2,
            2,
            4,
        )

    def test_shape_1d_values(self):
        assert gm.cell_colors(np.array([1.0, 2.0, 3.0]), (0, 10)).shape == (3, 4)

    def test_alpha_is_one(self):
        c = gm.cell_colors(np.array([[1.0, 50.0]]), (0, 100))
        np.testing.assert_array_equal(c[..., 3], 1.0)

    def test_min_is_low_color_max_is_high_color(self):
        c = gm.cell_colors(np.array([0.0, 100.0]), (0, 100))
        assert c[0, 2] == pytest.approx(1.0, abs=0.01)  # blue end
        assert c[1, 0] == pytest.approx(1.0, abs=0.01)  # red end

    def test_matches_colormap_lut_exactly(self, builtin_colormap):
        values = np.array([[0.0, 12.5, 50.0], [77.0, 99.9, 100.0]])
        c = gm.cell_colors(values, (0.0, 100.0))
        lut = np.array(builtin_colormap.colors) / 255.0
        for (r, k), v in np.ndenumerate(values):
            idx = int(np.clip(v / 100.0 * 255, 0, 255))
            np.testing.assert_allclose(c[r, k, :3], lut[idx])

    def test_uniform_values_midpoint_color(self, builtin_colormap):
        c = gm.cell_colors(np.full((2, 2), 7.0), None)
        mid = np.array(builtin_colormap.colors[127]) / 255.0
        np.testing.assert_allclose(c[0, 0, :3], mid)
        assert np.all(c[0, 0] == c[1, 1])

    def test_without_scaling_range_uses_data_range(self):
        c = gm.cell_colors(np.array([5.0, 6.0]), None)
        assert not np.array_equal(c[0], c[1])

    def test_values_outside_range_are_clamped(self):
        a = gm.cell_colors(np.array([-50.0, 200.0]), (0, 100))
        b = gm.cell_colors(np.array([0.0, 100.0]), (0, 100))
        np.testing.assert_allclose(a, b)

    def test_nan_does_not_crash_and_maps_mid(self, builtin_colormap):
        c = gm.cell_colors(np.array([[np.nan, 1.0], [2.0, 3.0]]), None)
        mid = np.array(builtin_colormap.colors[127]) / 255.0
        np.testing.assert_allclose(c[0, 0, :3], mid)

    def test_all_nan_does_not_crash(self):
        c = gm.cell_colors(np.full((2, 2), np.nan), None)
        assert c.shape == (2, 2, 4)


class TestScalingRange:
    """get_scaling_range (moved out of the old mixin, behavior unchanged)."""

    def test_returns_min_max(self):
        assert get_scaling_range(_defn(S=_scaling("S", 10, 200)), "S") == (10.0, 200.0)

    def test_zero_range_is_none(self):
        assert get_scaling_range(_defn(S=_scaling("S", 0, 0)), "S") is None

    def test_equal_min_max_is_none(self):
        assert get_scaling_range(_defn(S=_scaling("S", 5, 5)), "S") is None

    def test_missing_scaling_is_none(self):
        assert get_scaling_range(_defn(), "S") is None


# --- geometry ------------------------------------------------------------------
class TestGrid:
    def test_face_count_equals_cells_and_last_row_col_present(self):
        values = np.arange(12, dtype=float).reshape(3, 4)
        z = gm.extend_grid(values)
        assert z.shape == (4, 5)  # (rows+1) x (cols+1) → rows*cols quads
        assert (z.shape[0] - 1) * (z.shape[1] - 1) == values.size
        np.testing.assert_array_equal(z[3, :4], values[-1])
        np.testing.assert_array_equal(z[:3, 4], values[:, -1])
        assert z[3, 4] == values[-1, -1]


# --- labels --------------------------------------------------------------------
class TestLabels:
    def _model(self, **kw):
        defn = _defn(
            S=_scaling("S", 0, 100, "deg"),
            SX=_scaling("SX", 0, 8000, "RPM"),
            SY=_scaling("SY", 0, 3, ""),
        )
        data = {
            "values": np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]),
            "x_axis": np.array([1000.0, 2500.0, 7250.0]),
            "y_axis": np.array([0.125, 0.25]),
        }
        data.update(kw)
        return gm.build_model(_table_3d(), data, defn)

    def test_tick_labels_are_real_axis_values_4g(self):
        m = self._model()
        assert [t.label for t in m.x_ticks] == ["1000", "2500", "7250"]
        assert [t.label for t in m.y_ticks] == ["0.125", "0.25"]
        assert [t.position for t in m.x_ticks] == [0.5, 1.5, 2.5]

    def test_axis_titles_have_units_and_fallback(self):
        m = self._model()
        assert m.x_title == "RPM (RPM)"
        assert m.y_title == "Load"  # scaling without units → bare name
        assert m.value_title == "Value (deg)"

    def test_missing_axes_fall_back_to_column_row(self):
        m = self._model(x_axis=None, y_axis=None)
        assert m.x_title == "Column" and m.y_title == "Row"
        assert [t.label for t in m.x_ticks] == ["0", "1", "2"]

    def test_ticks_thinned_for_long_axes(self):
        idx = gm.tick_indices(29)
        assert 5 <= len(idx) <= 8 and idx[0] == 0

    def test_axis_title_without_axis_table(self):
        t = _t("T", TableType.THREE_D, 1, "S")
        assert gm.axis_title(t, None, AxisType.X_AXIS) == "X Axis"
        assert gm.axis_title(t, None, AxisType.Y_AXIS) == "Y Axis"

    def test_2d_model_uses_real_x_values(self):
        defn = _defn(S=_scaling("S", 0, 100), SY=_scaling("SY", 0, 100, "%"))
        data = {"values": np.array([1.0, 5.0, 2.0]), "y_axis": np.array([0.0, 2.5, 90])}
        m = gm.build_model(_table_2d(), data, defn)
        np.testing.assert_array_equal(m.x_values, [0.0, 2.5, 90.0])
        assert m.x_title == "APP (%)"

    def test_nice_value_ticks_cover_range(self):
        ticks = gm.nice_value_ticks(-20.0, 59.9, 5)
        assert ticks[0] >= -20.0 and ticks[-1] <= 59.9
        assert ticks == [-20.0, 0.0, 20.0, 40.0]


# --- selection + hover -----------------------------------------------------------
class TestSelectionAndHover:
    def test_selection_colors_only_selected_blue(self):
        values = np.arange(6, dtype=float).reshape(2, 3)
        m = gm.GraphModel(TableType.THREE_D, values, gm.cell_colors(values, None))
        c = gm.selection_colors(m, [(1, 2)])
        np.testing.assert_array_equal(c[1, 2], gm.SELECTION_RGBA)
        assert (c[0, 0] != gm.SELECTION_RGBA).any()

    def test_out_of_range_selection_ignored(self):
        values = np.ones((2, 2))
        m = gm.GraphModel(TableType.THREE_D, values, gm.cell_colors(values, None))
        gm.selection_colors(m, [(5, 5), (-1, 0)])  # no IndexError

    def test_hover_text_3d(self):
        defn = _defn(
            S=_scaling("S"), SX=_scaling("SX", 0, 1, "RPM"), SY=_scaling("SY", 0, 1)
        )
        data = {
            "values": np.array([[1.0, 2.0, 3.0], [4.0, 5.5, 6.0]]),
            "x_axis": np.array([1000.0, 2000.0, 3000.0]),
            "y_axis": np.array([0.5, 1.0]),
        }
        m = gm.build_model(_table_3d(), data, defn)
        text = gm.hover_text(m, 1, 1)
        assert "2000" in text and "1" in text and "5.5" in text
        assert gm.hover_text(m, 9, 9) == ""

    def test_hover_text_2d(self):
        defn = _defn(S=_scaling("S"), SY=_scaling("SY"))
        data = {"values": np.array([1.0, 7.25]), "y_axis": np.array([10.0, 20.0])}
        m = gm.build_model(_table_2d(), data, defn)
        assert gm.hover_text(m, 1) == "APP 20 → 7.25"


def test_nice_value_ticks_never_negative_zero():
    ticks = gm.nice_value_ticks(-19.5, 68.0, 5)
    assert ticks[0] == 0.0 and gm.format_tick(ticks[0]) == "0"
    assert all(gm.format_tick(t) != "-0" for t in ticks)
