import sys, time, gc

sys.argv = [sys.argv[0]]
import q3d_proto as q
from PySide6.QtWidgets import QApplication

app = QApplication([])
wins = []
out = []
for i in range(4):
    t = time.perf_counter()
    w = q.Window()
    w.show()
    app.processEvents()
    out.append(f"w{i+1}: {1000*(time.perf_counter()-t):.0f}ms rss={q.rss_mb():.0f}MB")
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
out.append(f"after closing 3: rss={q.rss_mb():.0f}MB")
print("q3d", " | ".join(out))
