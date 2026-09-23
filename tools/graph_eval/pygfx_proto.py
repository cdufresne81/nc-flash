"""pygfx vs matplotlib mplot3d prototype for the NC ROM editor 3D table graph.

Usage:
  python pygfx_proto.py --backend pygfx|mpl [--bench] [--shot out.png] [--show]

--bench prints one JSON line with timings (all ms) and exits.
"""

import argparse
import importlib.util
import json
import os
import sys
import time
import weakref

T_PROC0 = time.perf_counter()

import numpy as np  # noqa: E402

REPO = r"C:\Users\dufre\Projets\nc-rom-editor"

ap = argparse.ArgumentParser()
ap.add_argument("--backend", default="pygfx", choices=["pygfx", "mpl"])
ap.add_argument("--bench", action="store_true")
ap.add_argument("--shot", default=None)
ap.add_argument("--show", action="store_true")
ap.add_argument("--keytest", action="store_true")
ARGS = ap.parse_args()

t0 = time.perf_counter()
from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

T_IMPORT_QT = (time.perf_counter() - t0) * 1000

# --- app colormap (loaded straight from the repo file; src/ is not modified) ---
_spec = importlib.util.spec_from_file_location(
    "nc_colormap", os.path.join(REPO, "src", "utils", "colormap.py")
)
_cm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cm)
CMAP = _cm.ColorMap(os.path.join(REPO, "colormaps", "default.map"))
LUT = np.array(CMAP.colors, dtype=np.float64) / 255.0

# --- realistic ECU map: 25 load rows x 29 rpm cols ignition timing ---
ROWS, COLS = 25, 29
RPM = np.linspace(600, 7400, COLS).round(-1)
LOAD = np.linspace(0.10, 2.50, ROWS).round(2)  # g/rev


def make_values(bump=0.0):
    r = (RPM - RPM.min()) / (RPM.max() - RPM.min())
    lo = (LOAD - LOAD.min()) / (LOAD.max() - LOAD.min())
    R, L = np.meshgrid(r, lo)
    base = 12 + 30 * (1 - np.exp(-3.2 * R)) - 22 * L**1.3 + 4 * np.sin(6 * R) * (1 - L)
    base -= 6 * np.exp(
        -((R - 0.35) ** 2) / 0.01 - ((L - 0.8) ** 2) / 0.02
    )  # knock notch
    return np.round((base + bump) * 2) / 2  # 0.5 deg resolution


def calc_colors(values):
    """Same maths as GraphWidget._calculate_colors."""
    mn, mx = values.min(), values.max()
    ratios = (
        np.clip((values - mn) / (mx - mn), 0, 1)
        if mx != mn
        else np.full_like(values, 0.5)
    )
    idx = np.clip(np.nan_to_num(ratios * 255, nan=127.0), 0, 255).astype(np.intp)
    c = np.empty((*values.shape, 4))
    c[..., :3] = LUT[idx]
    c[..., 3] = 1.0
    return c


BLUE = np.array([0.0, 0.5, 1.0, 1.0])


def extend_z(values):
    rows, cols = values.shape
    Z = np.zeros((rows + 1, cols + 1))
    Z[:rows, :cols] = values
    Z[rows, :cols] = values[-1, :]
    Z[:rows, cols] = values[:, -1]
    Z[rows, cols] = values[-1, -1]
    return Z


def tick_idx(n):
    return np.arange(0, n, max(1, n // 6)) if n > 6 else np.arange(n)


# =============================================================================
# pygfx backend
# =============================================================================
class PygfxGraph:
    Z_WORLD = 14.0  # world height for full data range

    def __init__(self, parent):
        t = time.perf_counter()
        import pygfx as gfx
        import pylinalg as la
        from rendercanvas.qt import QRenderWidget

        self.import_ms = (time.perf_counter() - t) * 1000
        self.gfx, self.la = gfx, la
        self.selected = []

        self.widget = KeyAwareRenderWidget.make(QRenderWidget, parent, self)
        self.renderer = gfx.renderers.WgpuRenderer(
            self.widget,
            pixel_ratio=(
                float(os.environ["NCG_PR"]) if os.environ.get("NCG_PR") else None
            ),
        )
        self.scene = gfx.Scene()

        # --- "lavish" look: gradient background, key+fill+rim light, phong ---
        self.scene.add(
            gfx.Background(
                None,
                gfx.BackgroundMaterial("#1b2130", "#1b2130", "#3a4760", "#3a4760"),
            )
        )
        self.scene.add(gfx.AmbientLight("#ffffff", 0.9))
        self.values = make_values()
        self._build_surface()
        self._build_axes()

        self.camera = gfx.PerspectiveCamera(35, 16 / 9)
        self.camera.world.reference_up = (0, 0, 1)
        self.camera.show_object(
            self.scene_bounds_obj, view_dir=(1.0, 1.25, -1.0), up=(0, 0, 1), scale=1.45
        )
        key = gfx.DirectionalLight("#fff4e0", 1.6)
        key.local.position = (-1, -2, 3)
        fill = gfx.DirectionalLight("#cfe0ff", 0.5)
        fill.local.position = (2, 1, 1)
        self.camera.add(key)
        self.camera.add(fill)
        self.scene.add(self.camera)
        self.controller = gfx.OrbitController(
            self.camera, register_events=self.renderer
        )
        self.widget.request_draw(self._animate)

    # geometry ---------------------------------------------------------------
    def _positions(self):
        rows, cols = self.values.shape
        X, Y = np.meshgrid(np.arange(cols + 1), np.arange(rows + 1))
        Z = extend_z(self.values)
        self.zmin, self.zmax = float(self.values.min()), float(self.values.max())
        zs = (Z - self.zmin) / (self.zmax - self.zmin) * self.Z_WORLD
        return np.column_stack([X.ravel(), Y.ravel(), zs.ravel()]).astype(np.float32)

    def _face_colors(self):
        c = calc_colors(self.values)
        for r, cc in self.selected:
            if r < c.shape[0] and cc < c.shape[1]:
                c[r, cc] = BLUE
        # two triangles per cell, cell-major order -> repeat each cell colour twice
        # pygfx treats buffer colours as linear -> convert sRGB LUT colours first
        rgb = c[..., :3]
        c[..., :3] = np.where(
            rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4
        )
        return np.repeat(c.reshape(-1, 4), 2, axis=0).astype(np.float32)

    def _build_surface(self):
        gfx = self.gfx
        rows, cols = self.values.shape
        W = cols + 1
        r, c = np.meshgrid(np.arange(rows), np.arange(cols), indexing="ij")
        v0 = (r * W + c).ravel()
        v1, v2, v3 = v0 + 1, v0 + W + 1, v0 + W
        idx = np.empty((rows * cols * 2, 3), np.int32)
        idx[0::2] = np.column_stack([v0, v1, v2])
        idx[1::2] = np.column_stack([v0, v2, v3])
        pos = self._positions()
        self.geom = gfx.Geometry(
            positions=pos,
            indices=idx,
            normals=np.tile([0, 0, 1], (len(pos), 1)).astype(np.float32),
            colors=self._face_colors(),
        )
        mat = gfx.MeshPhongMaterial(
            color_mode="face",
            flat_shading=True,
            shininess=40,
            specular="#555555",
            side="both",
        )
        self.mesh = gfx.Mesh(self.geom, mat)
        self.scene.add(self.mesh)

        # grey cell edges, lifted a hair to avoid z-fighting
        self.edges = gfx.Line(
            gfx.Geometry(positions=self._edge_positions(pos)),
            gfx.LineSegmentMaterial(color="#20242c", thickness=1.0, aa=True),
        )
        self.scene.add(self.edges)

    def _edge_positions(self, pos):
        rows, cols = self.values.shape
        P = pos.reshape(rows + 1, cols + 1, 3).copy()
        P[..., 2] += 0.02
        segs = []
        a, b = P[:, :-1].reshape(-1, 3), P[:, 1:].reshape(-1, 3)  # along cols
        segs.append(np.stack([a, b], 1).reshape(-1, 3))
        a, b = P[:-1, :].reshape(-1, 3), P[1:, :].reshape(-1, 3)  # along rows
        segs.append(np.stack([a, b], 1).reshape(-1, 3))
        return np.concatenate(segs).astype(np.float32)

    def _build_axes(self):
        gfx = self.gfx
        rows, cols = self.values.shape
        grp = gfx.Group()
        axis_col, txt_col = "#c8d0e0", "#e8ecf4"
        # floor grid + box edges
        floor = []
        for i in range(0, cols + 1, 2):
            floor += [(i, 0, 0), (i, rows, 0)]
        for j in range(0, rows + 1, 2):
            floor += [(0, j, 0), (cols, j, 0)]
        grp.add(
            gfx.Line(
                gfx.Geometry(positions=np.array(floor, np.float32)),
                gfx.LineSegmentMaterial(color="#56627a", thickness=1),
            )
        )
        box = [
            (0, 0, 0),
            (cols, 0, 0),
            (0, 0, 0),
            (0, rows, 0),
            (0, rows, 0),
            (0, rows, self.Z_WORLD),
        ]
        grp.add(
            gfx.Line(
                gfx.Geometry(positions=np.array(box, np.float32)),
                gfx.LineSegmentMaterial(color=axis_col, thickness=2),
            )
        )

        def label(text, pos, anchor, size=12, color=txt_col, weight=None):
            t = gfx.Text(
                text=text,
                font_size=size,
                screen_space=True,
                anchor=anchor,
                material=gfx.TextMaterial(
                    color=color,
                    outline_color="#000000",
                    outline_thickness=0.15,
                    weight_offset=weight or 0,
                ),
            )
            t.local.position = pos
            grp.add(t)

        for i in tick_idx(cols):
            label(f"{RPM[i]:.4g}", (i + 0.5, -0.8, 0), "top-center")
        for j in tick_idx(rows):
            label(f"{LOAD[j]:.4g}", (-0.8, j + 0.5, 0), "middle-right")
        for zv in np.linspace(self.zmin, self.zmax, 6):
            zw = (zv - self.zmin) / (self.zmax - self.zmin) * self.Z_WORLD
            label(f"{zv:.3g}", (-0.6, rows + 0.6, zw), "middle-right")
        label(
            "Engine Speed (RPM)", (cols / 2, -3.5, 0), "top-center", 14, "#ffffff", 150
        )
        label(
            "Engine Load (g/rev)", (-5, rows / 2, 0), "middle-right", 14, "#ffffff", 150
        )
        label(
            "Ignition Timing (deg)",
            (-1.5, rows, self.Z_WORLD + 1.5),
            "bottom-center",
            14,
            "#ffffff",
            150,
        )
        self.axes = grp
        self.scene.add(grp)
        # invisible box used for framing
        self.scene_bounds_obj = gfx.Group()
        self.scene_bounds_obj.add(
            self.mesh.__class__(
                gfx.box_geometry(cols, rows, self.Z_WORLD), gfx.MeshBasicMaterial()
            )
        )
        self.scene_bounds_obj.children[0].local.position = (
            cols / 2,
            rows / 2,
            self.Z_WORLD / 2,
        )
        self.scene_bounds_obj.visible = False

    # API mirroring GraphWidget --------------------------------------------------
    def update_selection(self, cells):
        self.selected = cells
        self.geom.colors.data[:] = self._face_colors()
        self.geom.colors.update_full()
        self.widget.request_draw()

    def update_values(self, values):
        self.values = values
        pos = self._positions()
        self.geom.positions.data[:] = pos
        self.geom.positions.update_full()
        self.edges.geometry.positions.data[:] = self._edge_positions(pos)
        self.edges.geometry.positions.update_full()
        self.geom.colors.data[:] = self._face_colors()
        self.geom.colors.update_full()
        self.widget.request_draw()

    def orbit(self, d_az, d_el=0.0):
        self.controller.rotate((np.radians(d_az), np.radians(d_el)), (0, 0, 1, 1))
        self.widget.request_draw()

    def zoom(self, factor):
        self.controller.quickzoom(0) if False else None
        self.camera.zoom *= factor
        self.widget.request_draw()

    def force_frame(self):
        self.widget.force_draw()

    def _animate(self):
        self.renderer.render(self.scene, self.camera)

    def grab(self, path):
        self.widget.force_draw()
        img = self.renderer.snapshot()  # RGBA ndarray from the GPU
        from PySide6.QtGui import QImage

        h, w = img.shape[:2]
        QImage(
            np.ascontiguousarray(img).data, w, h, 4 * w, QImage.Format_RGBA8888
        ).save(path)


class KeyAwareRenderWidget:
    """Factory: subclass QRenderWidget so Qt keyPressEvent reaches our handler."""

    _cls = None  # built ONCE: Shiboken never frees Python subclass types, so a
    # per-window subclass whose closure captures `graph` leaks every graph.

    @staticmethod
    def make(base, parent, graph):
        if KeyAwareRenderWidget._cls is None:
            KeyAwareRenderWidget._cls = KeyAwareRenderWidget._build(base)
        w = KeyAwareRenderWidget._cls(parent)
        w._graph = weakref.ref(graph)
        return w

    @staticmethod
    def _build(base):
        class _W(base):
            keys_seen = []

            def keyPressEvent(self, ev):  # noqa: N802
                self.keys_seen.append(ev.key())
                graph = self._graph()
                if graph is None:
                    return super().keyPressEvent(ev)
                k = ev.key()
                if k == QtCore.Qt.Key_Left:
                    graph.orbit(-10)
                elif k == QtCore.Qt.Key_Right:
                    graph.orbit(10)
                elif k == QtCore.Qt.Key_Up:
                    graph.orbit(0, 10)
                elif k == QtCore.Qt.Key_Down:
                    graph.orbit(0, -10)
                elif k in (QtCore.Qt.Key_Plus, QtCore.Qt.Key_Equal):
                    graph.zoom(1.1)
                elif k == QtCore.Qt.Key_Minus:
                    graph.zoom(0.9)
                else:
                    super().keyPressEvent(ev)

        return _W


# =============================================================================
# matplotlib backend (mirrors src/ui/graph_viewer.py)
# =============================================================================
class MplGraph:
    def __init__(self, parent):
        t = time.perf_counter()
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from matplotlib.figure import Figure

        self.import_ms = (time.perf_counter() - t) * 1000
        self.selected = []
        self.values = make_values()
        self.figure = Figure(figsize=(8, 6), dpi=100)
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.canvas.setParent(parent)
        self.canvas.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.widget = self.canvas
        self.keys_seen = []
        self.canvas.keyPressEvent = lambda ev: self.keys_seen.append(ev.key())
        self._plot()

    def _colors(self):
        c = calc_colors(self.values)
        for r, cc in self.selected:
            c[r, cc] = BLUE
        return c

    def _plot(self):
        ax = self.figure.add_subplot(111, projection="3d")
        rows, cols = self.values.shape
        X, Y = np.meshgrid(np.arange(cols + 1), np.arange(rows + 1))
        ax.plot_surface(
            X,
            Y,
            extend_z(self.values),
            facecolors=self._colors(),
            linewidth=0.5,
            edgecolor="gray",
            antialiased=True,
            shade=False,
        )
        ti = tick_idx(cols)
        ax.set_xticks(ti + 0.5)
        ax.set_xticklabels([f"{RPM[i]:.4g}" for i in ti])
        ti = tick_idx(rows)
        ax.set_yticks(ti + 0.5)
        ax.set_yticklabels([f"{LOAD[i]:.4g}" for i in ti])
        ax.set_xlabel("Engine Speed (RPM)")
        ax.set_ylabel("Engine Load (g/rev)")
        ax.set_zlabel("Ignition Timing (deg)")
        ax.mouse_init()
        self.ax = ax

    def update_selection(self, cells):
        self.selected = cells
        self.ax.collections[0].set_facecolors(self._colors().reshape(-1, 4))
        self.canvas.draw_idle()

    def update_values(self, values):
        # same as GraphWidget._update_3d_surface
        self.values = values
        ax = self.ax
        xl, yl, zl = ax.get_xlim(), ax.get_ylim(), ax.get_zlim()
        while ax.collections:
            ax.collections[0].remove()
        rows, cols = values.shape
        X, Y = np.meshgrid(np.arange(cols + 1), np.arange(rows + 1))
        ax.plot_surface(
            X,
            Y,
            extend_z(values),
            facecolors=self._colors(),
            linewidth=0.5,
            edgecolor="gray",
            antialiased=True,
            shade=False,
        )
        ax.set_xlim(xl), ax.set_ylim(yl), ax.set_zlim(zl)
        self.canvas.draw_idle()

    def orbit(self, d_az, d_el=0.0):
        self.ax.view_init(elev=self.ax.elev + d_el, azim=self.ax.azim + d_az)
        self.canvas.draw_idle()

    def force_frame(self):
        self.canvas.draw()
        self.canvas.repaint()

    def grab(self, path):
        self.canvas.draw()
        self.widget.grab().save(path)


# =============================================================================
class Main(QtWidgets.QMainWindow):
    def __init__(self, backend):
        super().__init__()
        self.setWindowTitle(f"3D graph prototype - {backend}")
        self.resize(1400, 760)
        split = QtWidgets.QSplitter()
        self.table = QtWidgets.QTableWidget(ROWS, COLS)
        self.table.setHorizontalHeaderLabels([f"{v:.0f}" for v in RPM])
        self.table.setVerticalHeaderLabels([f"{v:.2f}" for v in LOAD])
        vals = make_values()
        cols = calc_colors(vals)
        for r in range(ROWS):
            for c in range(COLS):
                it = QtWidgets.QTableWidgetItem(f"{vals[r, c]:.1f}")
                it.setBackground(QtGui.QColor.fromRgbF(*cols[r, c, :3]))
                it.setForeground(QtGui.QColor("black"))
                self.table.setItem(r, c, it)
        self.table.horizontalHeader().setDefaultSectionSize(40)
        split.addWidget(self.table)
        self.graph = (PygfxGraph if backend == "pygfx" else MplGraph)(split)
        split.addWidget(self.graph.widget)
        split.setSizes([520, 880])
        self.setCentralWidget(split)
        self.table.itemSelectionChanged.connect(self._sel)

    def _sel(self):
        cells = [(i.row(), i.column()) for i in self.table.selectedIndexes()]
        self.graph.update_selection(cells)


def main():
    t_app = time.perf_counter()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    t_build = time.perf_counter()
    win = Main(ARGS.backend)
    g = win.graph
    win.show()
    # first rendered frame
    g.force_frame()
    app.processEvents()
    t_first = time.perf_counter()

    # preset selection (the few blue cells)
    sel = [(10, 12), (10, 13), (11, 12), (11, 13), (12, 14)]
    win.table.clearSelection()
    for r, c in sel:
        win.table.item(r, c).setSelected(True)
    g.force_frame()
    app.processEvents()

    out = {}
    if ARGS.bench:
        import psutil

        out["backend"] = ARGS.backend
        out["import_qt_ms"] = T_IMPORT_QT
        out["import_backend_ms"] = g.import_ms
        out["first_frame_from_window_ctor_ms"] = (t_first - t_build) * 1000
        out["first_frame_from_process_start_ms"] = (t_first - T_PROC0) * 1000
        out["rss_mb_after_first_frame"] = psutil.Process().memory_info().rss / 2**20
        rng = np.random.default_rng(0)

        def med(fn, n=30):
            ts = []
            for i in range(n):
                t = time.perf_counter()
                fn(i)
                ts.append((time.perf_counter() - t) * 1000)
            return float(np.median(ts))

        def rand_sel(i):
            r, c = rng.integers(0, ROWS - 3), rng.integers(0, COLS - 3)
            return [(r + a, c + b) for a in range(3) for b in range(3)]

        # selection: update-only (API call, draw deferred) and update+frame on screen
        out["sel_update_only_ms"] = med(lambda i: g.update_selection(rand_sel(i)))
        app.processEvents()
        out["sel_update_plus_frame_ms"] = med(
            lambda i: (g.update_selection(rand_sel(i)), g.force_frame())
        )
        out["zdata_update_only_ms"] = med(
            lambda i: g.update_values(make_values(bump=(i % 5) * 0.5))
        )
        app.processEvents()
        out["zdata_update_plus_frame_ms"] = med(
            lambda i: (
                g.update_values(make_values(bump=(i % 5) * 0.5)),
                g.force_frame(),
            )
        )
        g.update_values(make_values())
        # orbit framerate: synchronous rotate + full frame to screen
        N = 120
        g.force_frame()
        t = time.perf_counter()
        for i in range(N):
            g.orbit(3)
            g.force_frame()
            app.processEvents()
        out["orbit_fps"] = N / (time.perf_counter() - t)
        out["rss_mb_end"] = psutil.Process().memory_info().rss / 2**20
        g.orbit(-3 * N)
        g.force_frame()

    if ARGS.keytest:
        g.widget.setFocus()
        app.processEvents()
        from PySide6.QtTest import QTest

        QTest.keyClick(g.widget, QtCore.Qt.Key_Left)
        QTest.keyClick(g.widget, QtCore.Qt.Key_Up)
        app.processEvents()
        seen = getattr(g.widget, "keys_seen", None)
        if seen is None:
            seen = g.keys_seen
        out["keys_seen"] = [int(k) for k in seen]
        out["focus_widget_is_graph"] = QtWidgets.QApplication.focusWidget() is g.widget
        out["graph_is_plain_qwidget_child"] = (
            g.widget.parent() is not None and not g.widget.isWindow()
        )
        out["native_window"] = g.widget.testAttribute(QtCore.Qt.WA_NativeWindow)
        # undo rotation for screenshot consistency
        g.orbit(10, -10)
        g.force_frame()

    if ARGS.shot:
        g.grab(ARGS.shot)
        win.grab().save(ARGS.shot.replace(".png", "_window.png"))
        out["shot"] = ARGS.shot

    if ARGS.backend == "pygfx":
        try:
            out["adapter"] = g.renderer.device.adapter.summary
        except Exception as e:  # noqa: BLE001
            out["adapter"] = repr(e)
    if out:
        print("RESULT " + json.dumps(out))
    if ARGS.show:
        app.exec()


if __name__ == "__main__":
    main()
