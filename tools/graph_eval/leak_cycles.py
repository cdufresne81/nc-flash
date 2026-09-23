"""pygfx graph memory: leak vs. driver-held reuse.

Opens a table+graph window, renders, closes it; repeats. After every cycle
prints process working set / private bytes, live PygfxGraph objects (weakref),
and wgpu live object counts + resource_mem.

  python leak_cycles.py naive|teardown [cycles] [batch]

naive    : close() + deleteLater, drop Python refs (what the prototype does)
teardown : also canvas.close(), drop renderer/scene/geometry refs explicitly
closeonly: canvas.close() only (minimum fix candidate)
batch    : windows opened per cycle (default 1)
"""

import gc
import sys
import time
import weakref

MODE = sys.argv[1] if len(sys.argv) > 1 else "naive"
CYCLES = int(sys.argv[2]) if len(sys.argv) > 2 else 12
BATCH = int(sys.argv[3]) if len(sys.argv) > 3 else 1
sys.argv = [sys.argv[0], "--backend", "pygfx"]

import psutil  # noqa: E402
import pygfx_proto as p  # noqa: E402
from PySide6 import QtCore, QtWidgets  # noqa: E402
import wgpu  # noqa: E402

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
proc = psutil.Process()
alive = weakref.WeakSet()


def pump(sec=0.3):
    t = time.perf_counter()
    while time.perf_counter() - t < sec:
        app.processEvents()
        app.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        time.sleep(0.01)


def teardown(w):
    g = w.graph
    if MODE == "teardown":
        try:
            g.widget.close()  # rendercanvas: releases the canvas context
        except Exception as e:  # noqa: BLE001
            print("canvas close err", e)
        g.controller = None
        g.scene.clear()
        g.scene = g.renderer = g.camera = g.geom = g.mesh = g.edges = g.axes = None
        g.scene_bounds_obj = None
    if MODE == "closeonly":
        g.widget.close()
    w.graph = None
    w.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
    w.close()


def stats(tag):
    gc.collect()
    pump(0.2)
    gc.collect()
    mi = proc.memory_full_info()
    oc = wgpu.diagnostics.object_counts.get_dict()
    tot = oc.get("total", {})
    keys = (
        "Buffer",
        "Texture",
        "TextureView",
        "BindGroup",
        "RenderPipeline",
        "CanvasContext",
    )
    live = " ".join(f"{k}={oc.get(k, {}).get('count', 0)}" for k in keys)
    print(
        f"{tag:>10} ws={mi.rss / 2**20:6.0f}MB private={mi.private / 2**20:6.0f}MB "
        f"graphs_alive={len(alive)} gpu_mem={tot.get('resource_mem', 0) / 2**20:6.1f}MB {live}",
        flush=True,
    )


stats("start")
for cyc in range(1, CYCLES + 1):
    wins = []
    for _ in range(BATCH):
        w = p.Main("pygfx")
        alive.add(w.graph)
        w.show()
        w.graph.force_frame()
        wins.append(w)
    pump(0.3)
    stats(f"open{cyc}x{BATCH}")
    for w in wins:
        teardown(w)
    del wins, w
    stats(f"cycle{cyc}")
