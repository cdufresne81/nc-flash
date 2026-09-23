import sys, time

T0 = time.perf_counter()
import numpy as np
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
)
from PySide6.QtCore import QTimer
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FC
from matplotlib.figure import Figure

TI = time.perf_counter() - T0
sys.path.insert(0, ".")
from q3d_proto import make_data, thermal, ROWS, COLS, rss_mb

app = QApplication(sys.argv)
t = time.perf_counter()
v, xa, ya = make_data()
tab = QTableWidget(ROWS, COLS)
for r in range(ROWS):
    for c in range(COLS):
        tab.setItem(r, c, QTableWidgetItem(f"{v[r,c]:.1f}"))
fig = Figure(figsize=(8, 6))
can = FC(fig)
drawn = []
can.mpl_connect("draw_event", lambda e: drawn.append(time.perf_counter()))


def plot(vals, sel=()):
    fig.clear()
    ax = fig.add_subplot(111, projection="3d")
    X, Y = np.meshgrid(np.arange(COLS + 1), np.arange(ROWS + 1))
    Z = np.zeros((ROWS + 1, COLS + 1))
    Z[:ROWS, :COLS] = vals
    Z[ROWS, :COLS] = vals[-1]
    Z[:ROWS, COLS] = vals[:, -1]
    Z[ROWS, COLS] = vals[-1, -1]
    col = thermal(((vals - vals.min()) / np.ptp(vals)).astype(np.float32))
    for r, c in sel:
        col[r, c] = [0, 0.5, 1, 1]
    ax.plot_surface(
        X,
        Y,
        Z,
        facecolors=col,
        linewidth=0.5,
        edgecolor="gray",
        antialiased=True,
        shade=False,
    )
    return ax


ax = plot(v)
sp = QSplitter()
sp.addWidget(tab)
sp.addWidget(can)
w = QMainWindow()
w.setCentralWidget(sp)
w.resize(1300, 700)
w.show()


def fin():
    first = drawn[0] - t
    sel = [(r, c) for r in range(5, 9) for c in range(10, 14)]
    ts = []
    for _ in range(20):
        a = time.perf_counter()
        col = thermal(((v - v.min()) / np.ptp(v)).astype(np.float32))
        for r, c in sel:
            col[r, c] = [0, 0.5, 1, 1]
        ax.collections[0].set_facecolors(col.reshape(-1, 4))
        can.draw()
        ts.append(time.perf_counter() - a)
    td = []
    for k in range(10):
        a = time.perf_counter()
        plot(v + k * 0.1)
        can.draw()
        td.append(time.perf_counter() - a)
    fr = []
    for k in range(20):
        a = time.perf_counter()
        fig.axes[0].view_init(30, -60 + k * 2)
        can.draw()
        fr.append(time.perf_counter() - a)
    print(
        f"MPL import={TI*1000:.0f}ms first_frame={first*1000:.0f}ms sel_update+draw_med={np.median(ts)*1000:.1f}ms data_update+draw_med={np.median(td)*1000:.1f}ms orbit_fps~{1/np.median(fr):.0f} rss={rss_mb():.0f}MB"
    )
    app.quit()


QTimer.singleShot(1500, fin)
app.exec()
