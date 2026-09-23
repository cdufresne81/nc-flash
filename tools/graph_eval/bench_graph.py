"""Benchmark the real GPU graph in TableViewerWindow (goal criterion A10).

Prints: GUI-thread max stall during warm-up, first/second graph open → first
frame, selection→frame median, orbit fps. Run from the repo root:

    python tools/graph_eval/bench_graph.py
"""

import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np  # noqa: E402
from PySide6.QtWidgets import QApplication, QMainWindow  # noqa: E402

from src.core.rom_definition import (
    AxisType,
    RomDefinition,
    RomID,
    Scaling,
    Table,
    TableType,
)  # noqa: E402,E501
from src.ui import gpu_runtime  # noqa: E402

ROWS, COLS = 25, 29


def _defn():
    rid = RomID(
        xmlid="b",
        internalidaddress="0",
        internalidstring="B",
        ecuid="",
        make="",
        model="",
        flashmethod="",
        memmodel="",
        checksummodule="",
    )
    sc = {
        n: Scaling(
            name=n,
            units=u,
            toexpr="x",
            frexpr="x",
            format="%0.2f",
            min=lo,
            max=hi,
            inc=1.0,
            storagetype="float",
            endian="big",
        )
        for n, u, lo, hi in (
            ("S", "deg", -20, 60),
            ("SX", "RPM", 0, 8000),
            ("SY", "g/rev", 0, 3),
        )
    }
    return RomDefinition(romid=rid, scalings=sc)


def _table():
    x = Table(
        name="RPM",
        address="1",
        type=TableType.THREE_D,
        elements=COLS,
        scaling="SX",
        axis_type=AxisType.X_AXIS,
    )
    y = Table(
        name="Load",
        address="2",
        type=TableType.THREE_D,
        elements=ROWS,
        scaling="SY",
        axis_type=AxisType.Y_AXIS,
    )
    return Table(
        name="Spark",
        address="3",
        type=TableType.THREE_D,
        elements=ROWS * COLS,
        scaling="S",
        children=[x, y],
    )


def _data():
    r, c = np.meshgrid(np.linspace(0, 1, ROWS), np.linspace(0, 1, COLS), indexing="ij")
    return {
        "values": 12 + 30 * (1 - np.exp(-3 * c)) - 22 * r**1.3,
        "x_axis": np.linspace(600, 7400, COLS),
        "y_axis": np.linspace(0.1, 2.5, ROWS),
    }


def main():
    app = QApplication(sys.argv)
    from src.ui.table_viewer_window import TableViewerWindow

    idle = QMainWindow()
    idle.resize(400, 200)
    idle.show()

    # No background warm-up (it crashed the interpreter; see gpu_runtime):
    # the first graph pays probe + device + shader compile on the GUI thread.
    print(f"engine_requested={gpu_runtime.requested_engine()}")

    def open_graph():
        win = TableViewerWindow(_table(), _data(), _defn(), rom_path="/tmp/b.bin")
        win.resize(1300, 760)
        win.show()
        app.processEvents()
        t = time.perf_counter()
        win._toggle_graph()
        gw = win.graph_widget
        while gw.backend is None or getattr(gw.backend, "n_draws", 1) < 1:
            app.processEvents()
            if gw.backend is not None and gw.backend.engine_name == "gpu":
                gw.backend.widget.force_draw()
        total = (time.perf_counter() - t) * 1000
        # graph-only share: rebuild + draw once more on the settled window
        t2 = time.perf_counter()
        gw.set_data(gw.table, gw.data, gw.rom_definition, [])
        if gw.backend.engine_name == "gpu":
            gw.backend.widget.force_draw()
        else:
            gw.backend.widget.draw()
        return win, total, (time.perf_counter() - t2) * 1000

    w1, first, g1 = open_graph()
    w2, second, g2 = open_graph()
    print(
        f"first_graph_frame={first:.0f}ms (budget < 1000)  "
        f"second_graph_frame={second:.0f}ms (budget < 600)  "
        f"graph_rebuild+draw={g2:.0f}ms"
    )

    b = w1.graph_widget.backend
    gw = w1.graph_widget
    if b.engine_name != "gpu":
        print("classic engine: skipping GPU-only timings")
        return
    sel = []
    for i in range(30):
        cells = [(i % ROWS, (i * 3) % COLS), ((i + 4) % ROWS, (i * 5) % COLS)]
        t = time.perf_counter()
        gw.update_selection(cells)
        b.widget.force_draw()
        sel.append((time.perf_counter() - t) * 1000)
    print(f"selection_to_frame_median={statistics.median(sel):.1f}ms (budget < 60)")

    n, t = 90, time.perf_counter()
    for i in range(n):
        gw.set_view(30, -60 + i * 2)
        b.widget.force_draw()
        app.processEvents()
    fps = n / (time.perf_counter() - t)
    print(f"orbit_fps={fps:.1f} (budget >= 20)")
    w1.close()
    w2.close()


if __name__ == "__main__":
    main()
