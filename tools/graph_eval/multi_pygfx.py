import sys, time, gc

sys.argv = [sys.argv[0], "--backend", sys.argv[1] if len(sys.argv) > 1 else "pygfx"]
import pygfx_proto as p
import psutil
from PySide6 import QtWidgets

app = QtWidgets.QApplication([])
proc = psutil.Process()
wins = []
out = []
for i in range(4):
    t = time.perf_counter()
    w = p.Main(p.ARGS.backend)
    w.show()
    w.graph.force_frame()
    app.processEvents()
    out.append(
        f"w{i+1}: {1000*(time.perf_counter()-t):.0f}ms rss={proc.memory_info().rss/2**20:.0f}MB"
    )
    wins.append(w)
    t2 = time.perf_counter()
    while time.perf_counter() - t2 < 0.3:
        app.processEvents()
for w in wins[1:]:
    w.close()
    w.deleteLater()
wins = wins[:1]
for _ in range(20):
    app.processEvents()
    time.sleep(0.02)
gc.collect()
app.processEvents()
out.append(f"after closing 3: rss={proc.memory_info().rss/2**20:.0f}MB")
print(p.ARGS.backend, " | ".join(out))
