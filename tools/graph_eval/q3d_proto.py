"""Qt Quick 3D prototype of the NC Flash table graph (per-face colors + selection).

Run:  python q3d_proto.py [--bench] [--shot out.png]
"""

import sys
import time

T0 = time.perf_counter()
import numpy as np  # noqa: E402
from PySide6.QtCore import QByteArray, QUrl, Qt, QTimer, Property, Signal  # noqa: E402
from PySide6.QtGui import QVector3D  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QMainWindow,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
)
from PySide6.QtQml import QmlElement  # noqa: E402
from PySide6.QtQuick3D import QQuick3DGeometry  # noqa: E402
from PySide6.QtQuickWidgets import QQuickWidget  # noqa: E402

T_IMPORT = time.perf_counter() - T0

QML_IMPORT_NAME = "NCGraph"
QML_IMPORT_MAJOR_VERSION = 1

ROWS, COLS = 25, 29
BLUE = np.array([0.0, 0.5, 1.0, 1.0], dtype=np.float32)


def make_data():
    load = np.linspace(0.1, 2.4, ROWS)[:, None]
    rpm = np.linspace(600, 7400, COLS)[None, :]
    v = 10 + 28 * np.tanh(rpm / 3000) - 9 * load**1.3 + 2 * np.sin(rpm / 900) * load
    return (
        v.astype(np.float64),
        np.linspace(600, 7400, COLS),
        np.linspace(0.1, 2.4, ROWS),
    )


def thermal(ratio):
    """Blue->cyan->green->yellow->red-ish thermal ramp (stand-in for colormap LUT)."""
    stops = np.array(
        [
            [0.10, 0.20, 0.55],
            [0.0, 0.65, 0.80],
            [0.20, 0.80, 0.35],
            [0.98, 0.85, 0.15],
            [0.90, 0.20, 0.12],
        ],
        dtype=np.float32,
    )
    r = np.clip(ratio, 0, 1) * (len(stops) - 1)
    i = np.minimum(r.astype(int), len(stops) - 2)
    f = (r - i)[..., None]
    rgb = stops[i] * (1 - f) + stops[i + 1] * f
    return np.concatenate([rgb, np.ones(rgb.shape[:-1] + (1,), np.float32)], axis=-1)


@QmlElement
class SurfaceGeometry(QQuick3DGeometry):
    """Flat-shaded quads: 4 unshared vertices per cell so each face owns its color.

    Layout per vertex: pos(3f) normal(3f) color(4f) = 40 bytes.
    Scene units: x in [0, COLS], z in [0, ROWS], y = normalized value * height.
    """

    STRIDE = 40

    def __init__(self, parent=None):
        super().__init__(parent)
        self._values = None
        self._selected = []
        self._pos = None
        self._nrm = None

    # -- public API (called from Python) --
    def set_values(self, values, height=8.0):
        self._values = values
        rows, cols = values.shape
        vmin, vmax = float(values.min()), float(values.max())
        span = (vmax - vmin) or 1.0
        z = np.empty((rows + 1, cols + 1))
        z[:rows, :cols] = values
        z[rows, :cols] = values[-1]
        z[:rows, cols] = values[:, -1]
        z[rows, cols] = values[-1, -1]
        h = ((z - vmin) / span * height).astype(np.float32)
        self._h = h
        gx, gz = np.meshgrid(
            np.arange(cols + 1, dtype=np.float32), np.arange(rows + 1, dtype=np.float32)
        )

        # corners of each cell (r,c): (r,c) (r,c+1) (r+1,c+1) (r+1,c)
        def corner(dr, dc):
            sl = (slice(dr, dr + rows), slice(dc, dc + cols))
            return np.stack([gx[sl], h[sl], gz[sl]], axis=-1)

        quad = np.stack(
            [corner(0, 0), corner(0, 1), corner(1, 1), corner(1, 0)], axis=2
        )  # r,c,4,3
        n = -np.cross(quad[:, :, 3] - quad[:, :, 0], quad[:, :, 1] - quad[:, :, 0])
        n /= np.linalg.norm(n, axis=-1, keepdims=True)
        self._pos = quad.reshape(-1, 4, 3)
        self._nrm = np.repeat(n.reshape(-1, 1, 3), 4, axis=1)
        self._ratio = (values - vmin) / span
        idx = np.arange(rows * cols, dtype=np.uint32)[:, None] * 4
        self._indices = (idx + np.array([0, 1, 2, 0, 2, 3], np.uint32)).ravel()
        self._upload(full=True)

    def set_selection(self, cells):
        self._selected = cells
        self._upload(full=False)

    def _colors(self):
        c = thermal(self._ratio.astype(np.float32))
        for r, col in self._selected:
            c[r, col] = BLUE
        return np.repeat(c.reshape(-1, 1, 4), 4, axis=1)

    def _upload(self, full):
        verts = np.concatenate([self._pos, self._nrm, self._colors()], axis=-1).astype(
            np.float32
        )
        if full:
            self.clear()
            self.setStride(self.STRIDE)
            self.setPrimitiveType(QQuick3DGeometry.PrimitiveType.Triangles)
            A = QQuick3DGeometry.Attribute
            self.addAttribute(A.Semantic.PositionSemantic, 0, A.ComponentType.F32Type)
            self.addAttribute(A.Semantic.NormalSemantic, 12, A.ComponentType.F32Type)
            self.addAttribute(A.Semantic.ColorSemantic, 24, A.ComponentType.F32Type)
            self.addAttribute(A.Semantic.IndexSemantic, 0, A.ComponentType.U32Type)
            self.setIndexData(QByteArray(self._indices.tobytes()))
            p = self._pos.reshape(-1, 3)
            self.setBounds(QVector3D(*p.min(0)), QVector3D(*p.max(0)))
        self.setVertexData(QByteArray(verts.tobytes()))
        self.update()


@QmlElement
class GridGeometry(QQuick3DGeometry):
    """Cell-edge wireframe drawn slightly above the surface."""

    def set_heights(self, h):
        rows1, cols1 = h.shape
        segs = []
        for r in range(rows1):
            p = np.stack([np.arange(cols1), h[r] + 0.01, np.full(cols1, r)], -1)
            segs.append(np.stack([p[:-1], p[1:]], 1))
        for c in range(cols1):
            p = np.stack([np.full(rows1, c), h[:, c] + 0.01, np.arange(rows1)], -1)
            segs.append(np.stack([p[:-1], p[1:]], 1))
        v = np.concatenate(segs).reshape(-1, 3).astype(np.float32)
        self.clear()
        self.setStride(12)
        self.setPrimitiveType(QQuick3DGeometry.PrimitiveType.Lines)
        A = QQuick3DGeometry.Attribute
        self.addAttribute(A.Semantic.PositionSemantic, 0, A.ComponentType.F32Type)
        self.setVertexData(QByteArray(v.tobytes()))
        self.setBounds(QVector3D(*v.min(0)), QVector3D(*v.max(0)))
        self.update()


QML = r"""
import QtQuick
import QtQuick3D
import QtQuick3D.Helpers
import NCGraph

Item {
    id: root
    property var xTicks: []
    property var zTicks: []
    signal frameDone()

    Rectangle { anchors.fill: parent
        gradient: Gradient { GradientStop { position: 0; color: "#20242c" } GradientStop { position: 1; color: "#0e1014" } } }

    View3D {
        id: view
        anchors.fill: parent
        environment: SceneEnvironment {
            backgroundMode: SceneEnvironment.Transparent
            antialiasingMode: SceneEnvironment.MSAA
            antialiasingQuality: SceneEnvironment.Medium
            aoEnabled: false
        }

        Node { id: pivot; objectName: "pivot"; position: Qt.vector3d(14.5, 3, 12.5)
            PerspectiveCamera { id: cam; position: Qt.vector3d(0, 0, 55); clipNear: 1; clipFar: 500 }
            eulerRotation: Qt.vector3d(-30, -40, 0)
        }
        DirectionalLight { eulerRotation: Qt.vector3d(-55, -30, 0); brightness: 0.8; castsShadow: false; shadowFactor: 25; shadowBias: 10; shadowMapQuality: Light.ShadowMapQualityHigh }
        DirectionalLight { eulerRotation: Qt.vector3d(-20, 150, 0); brightness: 0.35 }
        PointLight { position: Qt.vector3d(14, 25, 12); brightness: 0.4 }

        Model {
            geometry: surface
            receivesShadows: false; castsShadows: true
            materials: PrincipledMaterial { vertexColorsEnabled: true; roughness: 0.45; metalness: 0.05; cullMode: Material.NoCulling; lighting: PrincipledMaterial.FragmentLighting }
        }
        Model {
            geometry: grid
            materials: PrincipledMaterial { baseColor: "#e0000000"; lighting: PrincipledMaterial.NoLighting; alphaMode: PrincipledMaterial.Blend }
        }
        // floor
        Model { source: "#Rectangle"; position: Qt.vector3d(14.5, -0.05, 12.5); eulerRotation.x: -90; scale: Qt.vector3d(0.34, 0.30, 1); receivesShadows: true
            materials: PrincipledMaterial { baseColor: "#2a303a"; roughness: 0.9 } }

        Repeater3D { model: root.xTicks
            Node { position: Qt.vector3d(modelData.p, 0, 26.5); eulerRotation: pivot.eulerRotation
                Text { text: modelData.t; color: "#d8dee9"; font.pixelSize: 11; scale: 0.07; anchors.centerIn: parent } } }
        Repeater3D { model: root.zTicks
            Node { position: Qt.vector3d(30.5, 0, modelData.p); eulerRotation: pivot.eulerRotation
                Text { text: modelData.t; color: "#d8dee9"; font.pixelSize: 11; scale: 0.07; anchors.centerIn: parent } } }
    }
    OrbitCameraController { anchors.fill: parent; origin: pivot; camera: cam }
    Text { anchors { left: parent.left; bottom: parent.bottom; margins: 8 }
           text: "X: RPM   Z: Load (g/rev)   drag = orbit, wheel = zoom"; color: "#8a93a3"; font.pixelSize: 11 }
}
"""


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.values, xa, ya = make_data()
        self.table = QTableWidget(ROWS, COLS)
        for r in range(ROWS):
            for c in range(COLS):
                self.table.setItem(r, c, QTableWidgetItem(f"{self.values[r, c]:.1f}"))
        self.surface = SurfaceGeometry()
        self.grid = GridGeometry()
        self.qw = QQuickWidget()
        self.qw.setResizeMode(QQuickWidget.SizeRootObjectToView)
        self.qw.setFormat(self.qw.format())
        ctx = self.qw.rootContext()
        ctx.setContextProperty("surface", self.surface)
        ctx.setContextProperty("grid", self.grid)
        self.qw.setFocusPolicy(Qt.StrongFocus)
        self.surface.set_values(self.values)
        self.grid.set_heights(self.surface._h)
        self.qw.setSource(QUrl())
        from PySide6.QtCore import QTemporaryFile
        import os, tempfile

        p = os.path.join(tempfile.gettempdir(), "ncgraph_proto.qml")
        open(p, "w").write(QML)
        _t = time.perf_counter()
        self.qw.setSource(QUrl.fromLocalFile(p))
        print(f"setSource={1000*(time.perf_counter()-_t):.0f}ms")
        for e in self.qw.errors():
            print("QML:", e.toString())
        root = self.qw.rootObject()
        step = 4
        root.setProperty(
            "xTicks",
            [{"p": i + 0.5, "t": f"{xa[i]:.0f}"} for i in range(0, COLS, step)],
        )
        root.setProperty(
            "zTicks",
            [{"p": i + 0.5, "t": f"{ya[i]:.2f}"} for i in range(0, ROWS, step)],
        )
        sp = QSplitter()
        sp.addWidget(self.table)
        sp.addWidget(self.qw)
        sp.setSizes([600, 700])
        self.setCentralWidget(sp)
        self.resize(1300, 700)
        self.table.itemSelectionChanged.connect(self.on_sel)

    def on_sel(self):
        cells = [(i.row(), i.column()) for i in self.table.selectedIndexes()]
        self.surface.set_selection(cells)


def rss_mb():
    import ctypes
    from ctypes import wintypes

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (n, ctypes.c_size_t)
            for n in ("Peak", "WorkingSetSize", "a", "b", "c", "d", "e", "f")
        ]

    c = PMC()
    c.cb = ctypes.sizeof(c)
    k = ctypes.windll.kernel32
    k.GetCurrentProcess.restype = wintypes.HANDLE
    ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    ctypes.windll.psapi.GetProcessMemoryInfo(
        k.GetCurrentProcess(), ctypes.byref(c), c.cb
    )
    return c.WorkingSetSize / 2**20


def main():
    bench = "--bench" in sys.argv
    shot = sys.argv[sys.argv.index("--shot") + 1] if "--shot" in sys.argv else None
    app = QApplication(sys.argv)
    t_build = time.perf_counter()
    w = Window()
    w.show()
    qwin = w.qw.quickWindow()
    frames = []

    def on_frame():
        frames.append(time.perf_counter())

    qwin.afterFrameEnd.connect(on_frame, Qt.DirectConnection)

    def finish():
        first = frames[0] - t_build if frames else float("nan")
        # selection update timing (CPU side: recolor + upload)
        sel = [(r, c) for r in range(5, 9) for c in range(10, 14)]
        ts = []
        for _ in range(50):
            a = time.perf_counter()
            w.surface.set_selection(sel)
            ts.append(time.perf_counter() - a)
        td = []
        for k in range(20):
            a = time.perf_counter()
            w.surface.set_values(w.values + k * 0.1)
            w.grid.set_heights(w.surface._h)
            td.append(time.perf_counter() - a)
        # orbit fps: rotate pivot every frame for ~2 s
        root = w.qw.rootObject()
        pivot = root.findChild(object, "") if False else None
        n0 = len(frames)
        t0 = time.perf_counter()
        ang = [0]

        from PySide6.QtCore import QObject

        pv = w.qw.rootObject().findChild(QObject, "pivot")

        def spin():
            ang[0] += 2
            pv.setProperty("eulerRotation", QVector3D(-30, -40 + ang[0], 0))

        tm = QTimer()
        tm.timeout.connect(spin)
        tm.start(0)

        def stop():
            tm.stop()
            fps = (len(frames) - n0) / (time.perf_counter() - t0)
            rss = rss_mb()
            print(
                f"import={T_IMPORT*1000:.0f}ms first_frame={first*1000:.0f}ms "
                f"sel_update_med={np.median(ts)*1000:.2f}ms data_update_med={np.median(td)*1000:.2f}ms "
                f"redraw_fps~{fps:.0f} rss={rss:.0f}MB"
            )
            app.quit()

        QTimer.singleShot(2000, stop)

    if bench:
        QTimer.singleShot(1500, finish)
    if shot:

        def snap():
            w.surface.set_selection(
                [(r, c) for r in range(5, 9) for c in range(10, 14)]
            )
            QTimer.singleShot(400, lambda: (w.grab().save(shot), app.quit()))

        QTimer.singleShot(1500, snap)
    app.exec()


if __name__ == "__main__":
    main()
