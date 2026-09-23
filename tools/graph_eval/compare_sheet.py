"""Side-by-side before/after sheets for the graph baseline screenshots.

Pairs docs/screenshots/graph_before/X.png with graph_after/X.png and writes
docs/screenshots/graph_compare_NN.png (before left, after right, labelled).

    python tools/graph_eval/compare_sheet.py
"""

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[2] / "docs" / "screenshots"
MAX_H = 700


def main():
    QApplication.instance() or QApplication(sys.argv)
    before = ROOT / "graph_before"
    after = ROOT / "graph_after"
    names = sorted(p.name for p in before.glob("graph_*.png"))
    for n, name in enumerate(names, 1):
        a, b = QImage(str(before / name)), QImage(str(after / name))
        if b.isNull():
            print(f"MISSING after: {name}")
            continue
        a = a.scaledToHeight(min(MAX_H, a.height()), Qt.SmoothTransformation)
        b = b.scaledToHeight(min(MAX_H, b.height()), Qt.SmoothTransformation)
        head = 40
        img = QImage(
            a.width() + b.width() + 30,
            max(a.height(), b.height()) + head,
            QImage.Format_RGB32,
        )
        img.fill(QColor(255, 255, 255))
        p = QPainter(img)
        p.setFont(QFont("Segoe UI", 14, QFont.Bold))
        p.drawText(10, 28, f"BEFORE  {name}")
        p.drawText(a.width() + 40, 28, "AFTER")
        p.drawImage(10, head, a)
        p.drawImage(a.width() + 20, head, b)
        p.end()
        out = ROOT / f"graph_compare_{n:02d}.png"
        img.save(str(out))
        print(out)


if __name__ == "__main__":
    main()
