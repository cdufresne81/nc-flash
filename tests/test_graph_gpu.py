"""Graph engine integration tests — run against BOTH renderers.

Every test is parametrized over ``gpu`` (pygfx) and ``classic`` (matplotlib).
GPU runs skip when no WebGPU adapter is available, unless
``NCFLASH_REQUIRE_GPU=1`` (set on the CI job that installs lavapipe), which
turns the skip into a failure. Setting ``NCFLASH_GRAPH_ENGINE=classic`` in the
environment forces the classic engine for the whole run (GPU params skip).

Regression-checklist items are referenced as [Rn] (goal doc
.claude/plans/graph-engine-pygfx-goal.md).
"""

import gc
import os
import weakref

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QTableWidgetSelectionRange

from src.core.rom_definition import (
    AxisType,
    RomDefinition,
    RomID,
    Scaling,
    Table,
    TableType,
)
from src.ui import gpu_runtime
from src.ui.graph_model import SELECTION_RGBA
from src.ui.table_viewer_window import TableViewerWindow

_FORCED_CLASSIC = os.environ.get("NCFLASH_GRAPH_ENGINE", "").lower() == "classic"
BLUE = np.array(SELECTION_RGBA)


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
def _romid():
    return RomID(
        xmlid="t",
        internalidaddress="0x0",
        internalidstring="T",
        ecuid="",
        make="",
        model="",
        flashmethod="",
        memmodel="",
        checksummodule="",
    )


def _scaling(name, lo=0.0, hi=100.0, units=""):
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


def _defn():
    return RomDefinition(
        romid=_romid(),
        scalings={
            "S": _scaling("S", -30, 70, "deg"),
            "SX": _scaling("SX", 0, 8000, "RPM"),
            "SY": _scaling("SY", 0, 3, "g/rev"),
        },
    )


def _t(name, kind, n, scaling, **kw):
    return Table(
        name=name, address="0x100", type=kind, elements=n, scaling=scaling, **kw
    )


ROWS, COLS = 7, 9


def _table_3d(**kw):
    x = _t("Engine Speed", TableType.THREE_D, COLS, "SX", axis_type=AxisType.X_AXIS)
    y = _t("Load", TableType.THREE_D, ROWS, "SY", axis_type=AxisType.Y_AXIS)
    return _t("Spark", TableType.THREE_D, ROWS * COLS, "S", children=[x, y], **kw)


def _data_3d():
    r, c = np.meshgrid(np.linspace(0, 1, ROWS), np.linspace(0, 1, COLS), indexing="ij")
    return {
        "values": np.round(10 + 30 * c - 15 * r + 3 * np.sin(6 * c), 2),
        "x_axis": np.linspace(800, 7200, COLS),
        "y_axis": np.round(np.linspace(0.2, 2.4, ROWS), 3),
    }


def _table_2d():
    y = _t("Pedal", TableType.TWO_D, 6, "SY", axis_type=AxisType.Y_AXIS)
    return _t("Throttle", TableType.TWO_D, 6, "S", children=[y])


def _data_2d():
    return {
        "values": np.array([5.0, 12.0, 30.0, 44.0, 60.0, 61.0]),
        "y_axis": np.array([0.0, 10.0, 25.0, 50.0, 75.0, 100.0]),
    }


@pytest.fixture(params=["gpu", "classic"])
def engine(request, monkeypatch):
    if request.param == "classic":
        monkeypatch.setenv("NCFLASH_GRAPH_ENGINE", "classic")
        return "classic"
    if _FORCED_CLASSIC:
        pytest.skip("NCFLASH_GRAPH_ENGINE=classic forces the classic engine")
    monkeypatch.setenv("NCFLASH_GRAPH_ENGINE", "auto")
    gpu_runtime.wait_until_ready()
    eng, reason = gpu_runtime.engine_decision()
    if eng != gpu_runtime.ENGINE_GPU:
        if os.environ.get("NCFLASH_REQUIRE_GPU"):
            pytest.fail(f"GPU required but unavailable: {reason}")
        pytest.skip(f"no GPU graph: {reason}")
    return "gpu"


def _open(qtbot, table, data, engine, show_graph=True, **kw):
    win = TableViewerWindow(table, data, _defn(), rom_path="/tmp/t.bin", **kw)
    qtbot.addWidget(win)
    win.resize(900, 520)
    win.show()
    qtbot.waitExposed(win)
    if show_graph:
        win._toggle_graph()
        qtbot.waitUntil(lambda: win.graph_widget.backend is not None, timeout=180000)
        _settle(qtbot, win)
        assert win.graph_widget.engine_name == engine
    return win


def _settle(qtbot, win, ms=150):
    qtbot.wait(ms)
    b = win.graph_widget.backend
    if b is not None and b.engine_name == "gpu":
        b.widget.force_draw()


def _layout(qtbot, win, graph_w, height):
    """Give the graph pane exactly ``graph_w`` px next to an unsqueezed table.

    Don't rely on the window's own sizing: headless CI screens are small
    (800 px), so opening the graph caps the window and squeezes the table pane,
    and Qt then restores a squeezed pane before stretching the other one.
    """
    table_w = max(win.splitter.widget(0).sizeHint().width(), win.splitter.sizes()[0])
    win.resize(table_w + win.splitter.handleWidth() + graph_w, height)
    win.splitter.setSizes([table_w, graph_w])
    qtbot.wait(200)


def _select(win, cells):
    tw = win.viewer.table_widget
    tw.clearSelection()
    for r, c in cells:
        ur, uc = win.viewer._data_to_ui_coords(r, c)
        tw.setRangeSelected(QTableWidgetSelectionRange(ur, uc, ur, uc), True)


def _heights(backend):
    """Rendered surface heights, read back from the backend (not the table)."""
    if backend.engine_name == "gpu":
        return np.array(backend.surface.mesh.geometry.positions.data[:, 2])
    return np.array(backend.model.values).ravel()  # classic re-plots from model


def _face_rgba(backend):
    """Per-cell RGBA as rendered (sRGB), shape (rows, cols, 4)."""
    rows, cols = backend.model.values.shape
    if backend.engine_name == "gpu":
        lin = np.array(backend.surface.mesh.geometry.colors.data)[::2]
        rgb = np.where(
            lin[:, :3] <= 0.0031308,
            lin[:, :3] * 12.92,
            1.055 * np.power(lin[:, :3], 1 / 2.4) - 0.055,
        )
        return np.column_stack([rgb, lin[:, 3]]).reshape(rows, cols, 4)
    # mpl depth-sorts polygons at draw time; the backend's model holds the
    # colors it handed to set_facecolors (row-major, one per cell).
    return np.array(backend.model.colors).reshape(rows, cols, 4)


def _is_blue(rgba):
    return np.allclose(rgba, BLUE, atol=0.01)


# ---------------------------------------------------------------------------
# [R1, R27] live refresh on edits / undo  (A3)
# ---------------------------------------------------------------------------
class TestLiveRefresh:
    def test_cell_edit_bulk_edit_axis_edit_and_undo_refresh(
        self, qtbot, engine, isolated_settings
    ):
        data = _data_3d()
        win = _open(qtbot, _table_3d(), data, engine)
        b = win.graph_widget.backend

        before = b.model.values.copy()
        z0 = _heights(b)
        # single cell edit (programmatic, same path undo uses)
        n = b.n_data_updates
        win.viewer.update_cell_value(2, 3, float(before[2, 3] + 25))
        qtbot.waitUntil(lambda: b.n_data_updates > n, timeout=300)
        assert b.model.values[2, 3] == before[2, 3] + 25
        assert not np.array_equal(_heights(b), z0)

        # bulk edit (one refresh for many cells)
        z1 = _heights(b)
        win.viewer.begin_bulk_update()
        for c in range(COLS):
            win.viewer.update_cell_value(0, c, 50.0)
        n = b.n_data_updates
        win.viewer.end_bulk_update()
        qtbot.waitUntil(lambda: b.n_data_updates > n, timeout=300)
        assert b.model.values[0, 0] == 50.0
        assert not np.array_equal(_heights(b), z1)

        # axis edit changes the tick labels (H3)
        old = [t.label for t in b.model.x_ticks]
        win.viewer.update_axis_cell_value("x_axis", 0, 555.0)
        qtbot.waitUntil(lambda: b.model.x_ticks[0].label == "555", timeout=300)
        assert [t.label for t in b.model.x_ticks] != old

        # "undo": restore original values in one bulk op
        win.viewer.begin_bulk_update()
        for (r, c), v in np.ndenumerate(before):
            win.viewer.update_cell_value(r, c, float(v))
        win.viewer.end_bulk_update()
        qtbot.waitUntil(lambda: np.array_equal(b.model.values, before), timeout=300)


# ---------------------------------------------------------------------------
# [R2, R3, R5] debounce: one update per action  (A4)
# ---------------------------------------------------------------------------
class TestDebounce:
    def test_bulk_undo_is_one_update(self, qtbot, engine, isolated_settings):
        data = _data_3d()
        win = _open(qtbot, _table_3d(), data, engine)
        b = win.graph_widget.backend
        n0 = b.n_data_updates
        win.viewer.begin_bulk_update()
        for (r, c), v in np.ndenumerate(data["values"]):
            win.viewer.update_cell_value(r, c, float(v) + 1)  # 63 cells
        win.viewer.end_bulk_update()
        qtbot.wait(200)
        assert b.n_data_updates - n0 == 1

    def test_selection_then_edit_is_one_draw_path(
        self, qtbot, engine, isolated_settings
    ):
        win = _open(qtbot, _table_3d(), _data_3d(), engine)
        b = win.graph_widget.backend
        nd, nc = b.n_data_updates, b.n_color_updates
        _select(win, [(1, 1)])  # starts the 100 ms selection timer
        win.viewer.update_cell_value(1, 1, 33.0)  # 50 ms refresh supersedes it
        qtbot.wait(300)
        assert b.n_data_updates - nd == 1
        assert b.n_color_updates - nc == 0

    def test_arrow_key_burst_is_debounced(self, qtbot, engine, isolated_settings):
        win = _open(qtbot, _table_3d(), _data_3d(), engine)
        b = win.graph_widget.backend
        nc = b.n_color_updates
        for c in range(COLS - 1):  # 8 rapid selection changes
            _select(win, [(0, c)])
            qtbot.wait(10)
        qtbot.wait(250)
        assert b.n_color_updates - nc <= 2


# ---------------------------------------------------------------------------
# [R4, R21] selection highlight  (A5, A11)
# ---------------------------------------------------------------------------
class TestSelection:
    @pytest.mark.parametrize("flip", [False, True])
    def test_exactly_selected_faces_blue(self, qtbot, engine, isolated_settings, flip):
        table = _table_3d(flipx=flip, flipy=flip)
        win = _open(qtbot, table, _data_3d(), engine)
        b = win.graph_widget.backend
        cells = [(0, 0), (3, 4), (6, 8)]
        _select(win, cells)
        qtbot.waitUntil(lambda: b.n_color_updates >= 1, timeout=1000)
        rgba = _face_rgba(b)
        for r in range(ROWS):
            for c in range(COLS):
                assert _is_blue(rgba[r, c]) == ((r, c) in cells), (r, c)

    def test_axis_cells_are_excluded(self, qtbot, engine, isolated_settings):
        win = _open(qtbot, _table_3d(), _data_3d(), engine)
        tw = win.viewer.table_widget
        tw.clearSelection()
        tw.setRangeSelected(
            QTableWidgetSelectionRange(0, 0, 0, tw.columnCount() - 1), True
        )
        assert win._get_selected_data_cells() == []

    def test_selection_only_update_keeps_geometry_buffer(
        self, qtbot, engine, isolated_settings
    ):
        win = _open(qtbot, _table_3d(), _data_3d(), engine)
        b = win.graph_widget.backend
        nd = b.n_data_updates
        if engine == "gpu":
            buf = b.surface.mesh.geometry.positions
        _select(win, [(2, 2)])
        qtbot.waitUntil(lambda: b.n_color_updates >= 1, timeout=1000)
        assert b.n_data_updates == nd
        if engine == "gpu":
            assert b.surface.mesh.geometry.positions is buf

    def test_2d_selection_markers(self, qtbot, engine, isolated_settings):
        win = _open(qtbot, _table_2d(), _data_2d(), engine)
        b = win.graph_widget.backend
        _select(win, [(2, 0), (4, 0)])
        qtbot.waitUntil(lambda: b.n_color_updates >= 1, timeout=1000)
        from src.ui.graph_model import selected_points

        sx, sy = selected_points(b.model, win.graph_widget.selected_cells)
        np.testing.assert_array_equal(sx, [25.0, 75.0])
        if engine == "gpu":
            pos = b.line._sel_obj.geometry.positions.data
            np.testing.assert_allclose(pos[:, :2], [[25.0, 30.0], [75.0, 60.0]])


# ---------------------------------------------------------------------------
# [R7, R8] view preservation  (A6)
# ---------------------------------------------------------------------------
class TestView:
    def test_default_view(self, qtbot, engine, isolated_settings):
        win = _open(qtbot, _table_3d(), _data_3d(), engine)
        elev, azim = win.graph_widget.get_view()
        assert elev == pytest.approx(30, abs=0.5)
        assert azim == pytest.approx(-60, abs=0.5)

    def test_view_and_zoom_survive_selection_edit_undo(
        self, qtbot, engine, isolated_settings
    ):
        data = _data_3d()
        win = _open(qtbot, _table_3d(), data, engine)
        gw, b = win.graph_widget, win.graph_widget.backend
        gw.set_view(45, 30)
        b.zoom(1.3)
        zoom = b.get_zoom()
        _select(win, [(1, 2)])
        qtbot.wait(200)
        win.viewer.update_cell_value(1, 2, 60.0)
        qtbot.wait(200)
        win.viewer.begin_bulk_update()
        win.viewer.update_cell_value(1, 2, float(data["values"][1, 2]))
        win.viewer.end_bulk_update()
        qtbot.wait(200)
        _settle(qtbot, win)
        elev, azim = gw.get_view()
        assert elev == pytest.approx(45, abs=0.5)
        assert azim == pytest.approx(30, abs=0.5)
        assert b.get_zoom() == pytest.approx(zoom, rel=1e-3)

    def test_z_range_follows_data(self, qtbot, engine, isolated_settings):  # H4
        data = _data_3d()
        win = _open(qtbot, _table_3d(), data, engine)
        b = win.graph_widget.backend
        new = data["values"] * 3
        win.viewer.begin_bulk_update()
        for (r, c), v in np.ndenumerate(new):
            win.viewer.update_cell_value(r, c, float(v))
        win.viewer.end_bulk_update()
        qtbot.waitUntil(lambda: b.model.values.max() == new.max(), timeout=500)
        if engine == "gpu":
            assert b.surface.zhi == pytest.approx(new.max())
        else:
            lo, hi = b.ax.get_zlim()
            assert hi >= new.max() - 1e-6


# ---------------------------------------------------------------------------
# [R9, R10] keys — and the +/-/= data-edit bug H1  (A7)
# ---------------------------------------------------------------------------
class TestKeys:
    def _activate(self, qtbot, win, widget):
        win.activateWindow()
        widget.setFocus()
        qtbot.waitUntil(lambda: QApplication.focusWidget() is widget, timeout=2000)

    def test_arrow_rotates(self, qtbot, engine, isolated_settings):
        win = _open(qtbot, _table_3d(), _data_3d(), engine)
        gw = win.graph_widget
        self._activate(qtbot, win, gw.backend.widget)
        _, azim = gw.get_view()
        QTest.keyClick(win.windowHandle(), Qt.Key_Left)
        qtbot.wait(50)
        assert gw.get_view()[1] == pytest.approx(azim - 10, abs=0.5)

    def test_plus_with_graph_focused_zooms_and_never_edits(
        self, qtbot, engine, isolated_settings
    ):
        data = _data_3d()
        win = _open(qtbot, _table_3d(), data, engine)
        gw = win.graph_widget
        _select(win, [(0, 0)])
        qtbot.wait(150)
        before = data["values"].copy()
        z0 = gw.backend.get_zoom()
        self._activate(qtbot, win, gw.backend.widget)
        for key in (Qt.Key_Plus, Qt.Key_Minus, Qt.Key_Plus):
            QTest.keyClick(win.windowHandle(), key)
            qtbot.wait(30)
        np.testing.assert_array_equal(data["values"], before)  # H1: no edit
        assert gw.backend.get_zoom() != pytest.approx(z0)

    def test_plus_with_table_focused_still_increments(
        self, qtbot, engine, isolated_settings
    ):
        data = _data_3d()
        win = _open(qtbot, _table_3d(), data, engine)
        _select(win, [(0, 0)])
        before = data["values"][0, 0]
        self._activate(qtbot, win, win.viewer.table_widget)
        QTest.keyClick(win.windowHandle(), Qt.Key_Plus)
        qtbot.wait(50)
        assert data["values"][0, 0] > before


# ---------------------------------------------------------------------------
# [R12, R14] toggle, sizing, persistence  (A1, A8, A17, A18)
# ---------------------------------------------------------------------------
class TestSizing:
    def test_toggle_actions_in_sync(self, qtbot, engine, isolated_settings):
        win = _open(qtbot, _table_3d(), _data_3d(), engine, show_graph=False)
        win._toggle_graph()
        assert win.graph_action.isChecked() and win._tb_graph_action.isChecked()
        win._toggle_graph()
        assert not win.graph_action.isChecked()
        assert not win._tb_graph_action.isChecked()

    @pytest.mark.parametrize("kind", ["3d", "2d_narrow"])
    def test_hide_restores_width_repeatedly(
        self, qtbot, engine, isolated_settings, kind
    ):
        if kind == "3d":
            win = _open(qtbot, _table_3d(), _data_3d(), engine, show_graph=False)
        else:  # narrower than table + graph minimum: stale-minimum regression
            win = _open(qtbot, _table_2d(), _data_2d(), engine, show_graph=False)
            win._auto_size_window()
            qtbot.wait(100)
            assert win.width() < 340
        w0 = win.width()
        for _ in range(3):
            win._toggle_graph()
            qtbot.wait(100)
            win._toggle_graph()
            qtbot.wait(100)
            assert abs(win.width() - w0) <= 5

    def test_window_resize_grows_graph_pane_not_table(
        self, qtbot, engine, isolated_settings
    ):
        win = _open(qtbot, _table_3d(), _data_3d(), engine)
        _layout(qtbot, win, 400, 520)
        t0, g0 = win.splitter.sizes()
        w0 = win.width()
        win.resize(w0 + 120, win.height())
        qtbot.wait(200)
        t1, g1 = win.splitter.sizes()
        grew = win.width() - w0
        assert grew >= 100
        assert abs(t1 - t0) <= 2  # table pane untouched
        assert abs((g1 - g0) - grew) <= 2  # graph pane absorbed all of it

    def test_pane_width_persists(self, qtbot, engine, isolated_settings):
        win = _open(qtbot, _table_3d(), _data_3d(), engine)
        _layout(qtbot, win, 480, win.height())
        width = win.graph_widget.width()
        needed = win._table_only_size.width() + win.splitter.handleWidth() + width
        cap = QApplication.primaryScreen().availableGeometry().width() * 0.95
        if needed > cap:
            pytest.skip(
                f"screen too small to re-show a {width}px pane ({needed} > {cap:.0f})"
            )
        win._toggle_graph()  # hiding the graph persists the pane width
        qtbot.wait(100)
        assert isolated_settings.get_graph_pane_width() == width
        remembered = width
        win._toggle_graph()
        qtbot.wait(200)
        assert abs(win.graph_widget.width() - remembered) <= 5


# ---------------------------------------------------------------------------
# [R13] fit-to-pane: nothing clipped, plot not tiny  (A16)
# ---------------------------------------------------------------------------
def _frame(backend):
    if backend.engine_name == "gpu":
        return backend.snapshot()[..., :3].astype(int)
    img = backend.grab().toImage().convertToFormat(4)  # RGB32
    ptr = img.constBits()
    arr = np.frombuffer(ptr, np.uint8).reshape(img.height(), img.bytesPerLine() // 4, 4)
    return arr[:, : img.width(), [2, 1, 0]].astype(int)


@pytest.mark.parametrize(
    "kind,size",
    [
        ("3d", (300, 300)),
        ("3d", (600, 900)),
        ("3d", (1600, 500)),
        ("3d", (900, 600)),
        ("2d", (300, 300)),
        ("2d", (1600, 500)),
    ],
)
def test_fit_to_pane_no_clipping(qtbot, engine, isolated_settings, kind, size):
    if engine == "classic":
        pytest.skip("fit-to-pane is a GPU-engine feature (classic keeps mpl layout)")
    table, data = (
        (_table_3d(), _data_3d()) if kind == "3d" else (_table_2d(), _data_2d())
    )
    win = _open(qtbot, table, data, engine)
    b = win.graph_widget.backend
    _layout(qtbot, win, size[0], size[1] + 60)
    qtbot.wait(100)
    img = _frame(b)
    h, w = img.shape[:2]
    # Background = the stage gradient: per row, the median of the outer columns.
    bg_left = img[:, :3].reshape(h, -1, 3).mean(axis=1)
    diff = np.abs(img - bg_left[:, None, :]).max(axis=2)
    content = diff > 40
    border = 3 * max(1, int(round(h / max(1, win.graph_widget.height()))))
    edge = np.concatenate(
        [
            content[:border].ravel(),
            content[-border:].ravel(),
            content[:, :border].ravel(),
            content[:, -border:].ravel(),
        ]
    )
    assert edge.mean() < 0.002, f"plot touches the edge ({edge.mean():.4f})"
    ys, xs = np.nonzero(content)
    # A fitted plot fills its LIMITING dimension (width in a tall pane, height
    # in a wide one); area share alone is naturally small at extreme aspects.
    fill = max((xs.max() - xs.min()) / w, (ys.max() - ys.min()) / h)
    assert fill >= 0.7, f"plot too small in pane ({fill:.2f} of limiting side)"


# ---------------------------------------------------------------------------
# [R25] screenshots capture the graph  (A14)
# ---------------------------------------------------------------------------
def test_grab_is_not_black(qtbot, engine, isolated_settings):
    win = _open(qtbot, _table_3d(), _data_3d(), engine)
    _settle(qtbot, win)
    img = win.graph_widget.grab().toImage()
    colors = {
        img.pixel(x, y)
        for x in range(0, img.width(), 3)
        for y in range(0, img.height(), 3)
    }
    assert len(colors) > (1000 if engine == "gpu" else 200)
    whole = win.grab().toImage()  # what F12 / test_runner capture
    gx = win.graph_widget.mapTo(win, win.graph_widget.rect().center())
    assert (
        whole.pixelColor(gx) != whole.pixelColor(0, whole.height() - 1)
        or len(colors) > 200
    )


# ---------------------------------------------------------------------------
# [R23] no leak over repeated open/close  (A12)
# ---------------------------------------------------------------------------
def test_open_close_cycles_release_gpu(qtbot, engine, isolated_settings):
    if engine == "classic":
        pytest.skip("GPU resource accounting")
    import wgpu

    def gpu_mem():
        gc.collect()
        return wgpu.diagnostics.object_counts.get_dict()["total"]["resource_mem"]

    refs, baseline = [], None
    for i in range(10):
        win = _open(qtbot, _table_3d(), _data_3d(), engine)
        refs.append(weakref.ref(win.graph_widget.backend))
        win.close()
        del win
        for _ in range(5):
            qtbot.wait(30)
            QApplication.sendPostedEvents(None, 52)  # DeferredDelete
        gc.collect()
        if i == 0:
            baseline = gpu_mem()
    assert sum(r() is not None for r in refs) == 0
    assert gpu_mem() - baseline < 5 * 2**20


# ---------------------------------------------------------------------------
# H2: scaling edit refreshes colors without toggling G  (A20)
# ---------------------------------------------------------------------------
def test_scaling_change_recolors(qtbot, engine, isolated_settings, monkeypatch):
    """Drive the real _edit_scaling (dialog + XML write stubbed)."""
    import src.ui.table_viewer_window as tvw
    from unittest.mock import MagicMock

    win = _open(qtbot, _table_3d(), _data_3d(), engine)
    b = win.graph_widget.backend
    c0 = _face_rgba(b).copy()
    scaling = win.rom_definition.scalings["S"]
    dialog = MagicMock()
    dialog.exec.return_value = True
    dialog.get_all_updates.return_value = {"S": ({"max": "1000"}, scaling)}
    monkeypatch.setattr(tvw, "TableScalingDialog", lambda *a, **k: dialog)
    monkeypatch.setattr(tvw, "update_scaling", lambda *a, **k: True)
    monkeypatch.setattr(tvw.QMessageBox, "information", lambda *a, **k: None)
    win.rom_definition.xml_path = "/tmp/fake.xml"
    n = b.n_data_updates
    win._edit_scaling()
    qtbot.waitUntil(lambda: b.n_data_updates > n, timeout=1000)
    _settle(qtbot, win)
    assert scaling.max == 1000.0
    assert not np.allclose(_face_rgba(b), c0)


# ---------------------------------------------------------------------------
# hover readout  (A21)
# ---------------------------------------------------------------------------
def test_hover_face_maps_to_cell(qtbot, engine, isolated_settings):
    if engine == "classic":
        pytest.skip("hover readout is a GPU-engine feature")
    data = _data_3d()
    win = _open(qtbot, _table_3d(), data, engine)
    _layout(qtbot, win, 500, 520)
    b = win.graph_widget.backend
    b.widget.force_draw()
    for face in (0, 1, 2 * (3 * COLS + 4), 2 * (ROWS * COLS) - 1):
        r, c = b.surface.cell_at_face(face)
        from src.ui.graph_model import format_tick, hover_text

        text = hover_text(b.model, r, c)
        assert format_tick(data["values"][r, c]) in text
        assert format_tick(data["x_axis"][c]) in text
        assert format_tick(data["y_axis"][r]) in text
    # and a real pick at the centre of the pane lands on the surface
    w, h = b.widget.width(), b.widget.height()
    found = any(
        b.hover_text_at(w * fx, h * fy)
        for fx in (0.4, 0.5, 0.6)
        for fy in (0.4, 0.5, 0.6)
    )
    assert found


# ---------------------------------------------------------------------------
# fallback  (A19)
# ---------------------------------------------------------------------------
def test_gpu_init_failure_falls_back_to_classic(qtbot, isolated_settings, monkeypatch):
    import src.ui.graph_gpu as graph_gpu

    monkeypatch.setenv("NCFLASH_GRAPH_ENGINE", "auto")
    monkeypatch.setattr(gpu_runtime, "engine_decision", lambda: ("gpu", "test"))
    monkeypatch.setattr(gpu_runtime, "is_ready", lambda: True)

    def boom(*a, **k):
        raise RuntimeError("simulated device loss")

    monkeypatch.setattr(graph_gpu, "GpuGraphView", boom)
    win = TableViewerWindow(_table_3d(), _data_3d(), _defn(), rom_path="/tmp/t.bin")
    qtbot.addWidget(win)
    win.show()
    win._toggle_graph()
    qtbot.waitUntil(lambda: win.graph_widget.backend is not None, timeout=5000)
    assert win.graph_widget.engine_name == "classic"


def test_pygfx_unavailable_decides_classic(monkeypatch):
    monkeypatch.setitem(gpu_runtime._state, "probed", True)
    monkeypatch.setitem(gpu_runtime._state, "available", False)
    monkeypatch.setitem(gpu_runtime._state, "reason", "ImportError: no pygfx")
    monkeypatch.setenv("NCFLASH_GRAPH_ENGINE", "auto")
    eng, reason = gpu_runtime.engine_decision()
    assert eng == "classic" and "pygfx" in reason


# ---------------------------------------------------------------------------
# selected-cell axis highlight (table) + crosshair/callouts (graph)
# ---------------------------------------------------------------------------
class TestAxisHighlight:
    def test_table_highlights_selected_rows_and_columns(
        self, qtbot, engine, isolated_settings
    ):
        win = _open(qtbot, _table_3d(), _data_3d(), engine, show_graph=False)
        v = win.viewer
        _select(win, [(2, 5)])
        assert v.is_axis_highlighted("x_axis", 5) and v.is_axis_highlighted("y_axis", 2)
        assert not v.is_axis_highlighted("x_axis", 2)
        assert not v.is_axis_highlighted("y_axis", 5)
        _select(win, [(1, 1), (3, 4)])
        assert {i for i in range(COLS) if v.is_axis_highlighted("x_axis", i)} == {1, 4}
        assert {i for i in range(ROWS) if v.is_axis_highlighted("y_axis", i)} == {1, 3}
        win.viewer.table_widget.clearSelection()
        assert not any(v.is_axis_highlighted("y_axis", i) for i in range(ROWS))

    def test_2d_table_highlights_row_only(self, qtbot, engine, isolated_settings):
        win = _open(qtbot, _table_2d(), _data_2d(), engine, show_graph=False)
        _select(win, [(3, 0)])
        assert win.viewer.is_axis_highlighted("y_axis", 3)
        assert not win.viewer.is_axis_highlighted("x_axis", 0)

    def test_graph_crosshair_single_cell_only(self, qtbot, engine, isolated_settings):
        if engine == "classic":
            pytest.skip("crosshair is a GPU-engine feature")
        data = _data_3d()
        win = _open(qtbot, _table_3d(), data, engine)
        b = win.graph_widget.backend
        surf = b.surface
        _select(win, [(3, 4)])
        qtbot.waitUntil(lambda: b.n_color_updates >= 1, timeout=1000)
        assert all(line.visible for line in surf._xhair)
        callouts = {rec[1]: rec for rec in surf._labels if rec[1][0] == "h"}
        from src.ui.graph_model import format_tick

        assert callouts["hx"][3] == format_tick(data["x_axis"][4])
        assert callouts["hy"][3] == format_tick(data["y_axis"][3])
        assert callouts["hz"][3] == format_tick(data["values"][3, 4])
        # the guides lie on the surface: every sample is within the lift of it
        row_pts, col_pts = surf._crosshair_paths(3, 4)
        g = surf._zgrid
        rows, cols = data["values"].shape
        for x, y, z in np.vstack([row_pts, col_pts]):
            ci = min(int(x / 10.0 * cols), cols - 1)
            ri = min(int(y / 10.0 * rows), rows - 1)
            lo = g[ri : ri + 2, ci : ci + 2].min()
            hi = g[ri : ri + 2, ci : ci + 2].max()
            assert lo - 1e-6 <= z - 0.03 <= hi + 1e-6
        _select(win, [(1, 1), (2, 2)])
        n = b.n_color_updates
        qtbot.waitUntil(lambda: b.n_color_updates > n, timeout=1000)
        assert not any(line.visible for line in surf._xhair)
        assert not any(rec[0].visible for rec in callouts.values())

    def test_2d_crosshair_point(self, qtbot, engine, isolated_settings):
        if engine == "classic":
            pytest.skip("crosshair is a GPU-engine feature")
        win = _open(qtbot, _table_2d(), _data_2d(), engine)
        b = win.graph_widget.backend
        _select(win, [(3, 0)])
        qtbot.waitUntil(lambda: b.n_color_updates >= 1, timeout=1000)
        assert b.line.cross == (50.0, 44.0)
        _select(win, [(1, 0), (2, 0)])
        n = b.n_color_updates
        qtbot.waitUntil(lambda: b.n_color_updates > n, timeout=1000)
        assert b.line.cross is None


# ---------------------------------------------------------------------------
# adversarial-review regressions
# ---------------------------------------------------------------------------
def test_nan_cell_crosshair_keeps_graph_fitted(qtbot, engine, isolated_settings):
    if engine == "classic":
        pytest.skip("crosshair is a GPU-engine feature")
    data = _data_3d()
    data["values"][3, 4] = np.nan  # erased 0xFFFFFFFF float cells read as NaN
    win = _open(qtbot, _table_3d(), data, engine)
    b = win.graph_widget.backend
    _select(win, [(3, 4)])
    qtbot.waitUntil(lambda: b.n_color_updates >= 1, timeout=1000)
    _settle(qtbot, win)
    s, cx, cy = b._fit
    assert np.isfinite([s, cx, cy]).all() and s > 0.01


def test_pane_width_stable_across_toggles(qtbot, engine, isolated_settings):
    win = _open(qtbot, _table_3d(), _data_3d(), engine)
    first = win.graph_widget.width()
    for _ in range(4):
        win._toggle_graph()
        qtbot.wait(100)
        win._toggle_graph()
        qtbot.wait(200)
    assert abs(win.graph_widget.width() - first) <= 2


@pytest.mark.parametrize("key", [Qt.Key_BracketRight, Qt.Key_V, Qt.Key_S])
def test_single_key_edit_shortcuts_blocked_while_graph_focused(
    qtbot, engine, isolated_settings, key
):
    data = _data_3d()
    win = _open(qtbot, _table_3d(), data, engine)
    _select(win, [(1, 1), (1, 2), (2, 1), (2, 2)])
    qtbot.wait(150)
    before = data["values"].copy()
    w = win.graph_widget.backend.widget
    win.activateWindow()
    w.setFocus()
    qtbot.waitUntil(lambda: QApplication.focusWidget() is w, timeout=2000)
    QTest.keyClick(win.windowHandle(), key)
    qtbot.wait(100)
    np.testing.assert_array_equal(data["values"], before)
