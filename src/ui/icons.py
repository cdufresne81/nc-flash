"""
Shared toolbar icon factory.

Creates crisp QPainter-drawn icons for toolbars across the application.
All icons are 20x20 logical pixels, rendered at the widget's device pixel ratio.
"""

from math import cos, sin, pi

from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import (
    QColor,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)


def make_icon(widget, name: str) -> QIcon:
    """Create a toolbar icon by name using QPainter.

    Args:
        widget: QWidget used for devicePixelRatioF() and palette().
        name: Icon identifier (e.g. 'open', 'save', 'copy', 'graph').

    Returns:
        QIcon with the drawn icon.
    """
    s = 20
    dpr = widget.devicePixelRatioF()
    pm = QPixmap(int(s * dpr), int(s * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)

    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    c = widget.palette().windowText().color()
    pen = QPen(c, 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)

    drawer = _ICON_DRAWERS.get(name)
    if drawer:
        drawer(p, c)

    p.end()
    return QIcon(pm)


# -- Main window icons --


def _draw_open(p, c):
    p.drawLine(2, 7, 2, 17)
    p.drawLine(2, 17, 17, 17)
    p.drawLine(17, 17, 17, 7)
    p.drawLine(17, 7, 10, 7)
    p.drawLine(10, 7, 8, 4)
    p.drawLine(8, 4, 2, 4)
    p.drawLine(2, 4, 2, 7)


def _draw_save(p, c):
    p.drawRect(3, 2, 14, 16)
    p.drawRect(6, 2, 8, 6)
    p.drawRect(6, 11, 8, 5)


def _draw_compare(p, c):
    p.drawRect(2, 4, 7, 12)
    p.drawRect(11, 4, 7, 12)
    p.setPen(QPen(c, 1.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.drawLine(9, 8, 11, 8)
    p.drawLine(11, 12, 9, 12)


def _draw_flash(p, c):
    bolt = QPolygonF(
        [
            QPointF(11, 2),
            QPointF(6, 10),
            QPointF(10, 10),
            QPointF(9, 18),
            QPointF(14, 9),
            QPointF(10, 9),
        ]
    )
    p.setBrush(c)
    p.drawPolygon(bolt)


def _draw_settings(p, c):
    cx, cy, r_out, r_in = 10, 10, 8.5, 5.5
    teeth = 6
    tooth_half = 0.28
    pts = []
    for i in range(teeth):
        a = 2 * pi * i / teeth - pi / 2
        pts.append(
            QPointF(cx + r_out * cos(a - tooth_half), cy + r_out * sin(a - tooth_half))
        )
        pts.append(
            QPointF(cx + r_out * cos(a + tooth_half), cy + r_out * sin(a + tooth_half))
        )
        a_next = 2 * pi * (i + 0.5) / teeth - pi / 2
        pts.append(
            QPointF(cx + r_in * cos(a + tooth_half), cy + r_in * sin(a + tooth_half))
        )
        pts.append(
            QPointF(
                cx + r_in * cos(a_next + tooth_half),
                cy + r_in * sin(a_next + tooth_half),
            )
        )
    p.setBrush(Qt.NoBrush)
    p.drawPolygon(QPolygonF(pts))
    p.drawEllipse(QPointF(cx, cy), 2.5, 2.5)


def _draw_history(p, c):
    p.setPen(QPen(c, 1.6, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(7, 2, 7, 18)
    p.setBrush(c)
    p.setPen(QPen(c, 1.2))
    p.drawEllipse(QPointF(7, 5), 2.2, 2.2)
    p.drawEllipse(QPointF(7, 10), 2.2, 2.2)
    p.drawEllipse(QPointF(7, 15), 2.2, 2.2)
    p.setPen(QPen(c, 1.2, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(9, 5, 16, 5)
    p.drawLine(9, 10, 14, 10)
    p.drawLine(9, 15, 17, 15)


def _draw_mcp_on(p, c):
    cx, cy = 10, 14
    green = QColor(76, 175, 80)
    p.setBrush(green)
    p.setPen(QPen(green, 1.6, Qt.SolidLine, Qt.RoundCap))
    p.drawEllipse(QPointF(cx, cy), 2, 2)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(green, 1.4, Qt.SolidLine, Qt.RoundCap))
    p.drawArc(QRectF(3, 7, 14, 14), 45 * 16, 90 * 16)
    p.drawArc(QRectF(0, 4, 20, 20), 45 * 16, 90 * 16)


def _draw_mcp_off(p, c):
    cx, cy = 10, 14
    p.setBrush(c)
    p.drawEllipse(QPointF(cx, cy), 2, 2)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(c, 1.4, Qt.SolidLine, Qt.RoundCap))
    p.drawArc(QRectF(3, 7, 14, 14), 45 * 16, 90 * 16)
    p.drawArc(QRectF(0, 4, 20, 20), 45 * 16, 90 * 16)


# -- Table viewer window icons --


def _draw_copy(p, c):
    p.drawRect(3, 4, 14, 14)
    p.drawRect(7, 1, 6, 5)
    p.drawLine(6, 9, 14, 9)
    p.drawLine(6, 12, 14, 12)
    p.drawLine(6, 15, 11, 15)


def _draw_export(p, c):
    p.drawLine(10, 2, 10, 11)
    p.drawLine(7, 8, 10, 11)
    p.drawLine(13, 8, 10, 11)
    p.drawLine(3, 13, 3, 18)
    p.drawLine(3, 18, 17, 18)
    p.drawLine(17, 18, 17, 13)


def _draw_increment(p, c):
    p.setPen(QPen(c, 2.0, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(10, 4, 10, 16)
    p.drawLine(4, 10, 16, 10)


def _draw_decrement(p, c):
    p.setPen(QPen(c, 2.0, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(4, 10, 16, 10)


def _draw_add(p, c):
    p.drawEllipse(2, 2, 16, 16)
    p.drawLine(10, 6, 10, 14)
    p.drawLine(6, 10, 14, 10)


def _draw_multiply(p, c):
    p.setPen(QPen(c, 1.8, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(10, 4, 10, 16)
    p.drawLine(5, 7, 15, 13)
    p.drawLine(15, 7, 5, 13)


def _draw_set_value(p, c):
    p.setPen(QPen(c, 2.0, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(4, 8, 16, 8)
    p.drawLine(4, 12, 16, 12)


def _draw_interp_v(p, c):
    p.drawLine(10, 3, 10, 17)
    p.drawLine(7, 6, 10, 3)
    p.drawLine(13, 6, 10, 3)
    p.drawLine(7, 14, 10, 17)
    p.drawLine(13, 14, 10, 17)


def _draw_interp_h(p, c):
    p.drawLine(3, 10, 17, 10)
    p.drawLine(6, 7, 3, 10)
    p.drawLine(6, 13, 3, 10)
    p.drawLine(14, 7, 17, 10)
    p.drawLine(14, 13, 17, 10)


def _draw_interp_2d(p, c):
    p.drawRect(3, 3, 14, 14)
    p.drawLine(10, 3, 10, 17)
    p.drawLine(3, 10, 17, 10)


def _draw_smooth(p, c):
    path = QPainterPath()
    path.moveTo(3, 14)
    path.cubicTo(7, 2, 13, 18, 17, 6)
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)


def _draw_graph(p, c):
    p.setPen(QPen(c, 3.0, Qt.SolidLine, Qt.FlatCap))
    p.drawLine(4, 17, 4, 12)
    p.drawLine(8, 17, 8, 7)
    p.drawLine(12, 17, 12, 10)
    p.drawLine(16, 17, 16, 4)
    p.setPen(QPen(c, 1.4, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(2, 18, 18, 18)


def _draw_round(p, c):
    # Stepped staircase being rounded into a curve
    # Draw the smooth curve
    path = QPainterPath()
    path.moveTo(3, 16)
    path.cubicTo(6, 16, 7, 10, 10, 10)
    path.cubicTo(13, 10, 14, 4, 17, 4)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(c, 1.8, Qt.SolidLine, Qt.RoundCap))
    p.drawPath(path)
    # Draw tiny dots at rounded positions
    p.setBrush(c)
    p.setPen(Qt.NoPen)
    p.drawEllipse(QPointF(3, 16), 1.5, 1.5)
    p.drawEllipse(QPointF(10, 10), 1.5, 1.5)
    p.drawEllipse(QPointF(17, 4), 1.5, 1.5)


# Dispatch table
_ICON_DRAWERS = {
    # Main window
    "open": _draw_open,
    "save": _draw_save,
    "compare": _draw_compare,
    "flash": _draw_flash,
    "settings": _draw_settings,
    "history": _draw_history,
    "mcp_on": _draw_mcp_on,
    "mcp_off": _draw_mcp_off,
    # Table viewer window
    "copy": _draw_copy,
    "export": _draw_export,
    "increment": _draw_increment,
    "decrement": _draw_decrement,
    "add": _draw_add,
    "multiply": _draw_multiply,
    "set_value": _draw_set_value,
    "interp_v": _draw_interp_v,
    "interp_h": _draw_interp_h,
    "interp_2d": _draw_interp_2d,
    "smooth": _draw_smooth,
    "round": _draw_round,
    "graph": _draw_graph,
}
