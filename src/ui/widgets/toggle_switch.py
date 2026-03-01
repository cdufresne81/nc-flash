"""
Toggle Switch Widget

A custom ON/OFF toggle switch styled like modern mobile toggles.
Used for binary table values (e.g., DTC Activation Flags).
"""

from PySide6.QtCore import Property, QPropertyAnimation, QEasingCurve, Qt, QRect, QSize
from PySide6.QtGui import QPainter, QColor, QPen
from PySide6.QtWidgets import QAbstractButton, QSizePolicy


class ToggleSwitch(QAbstractButton):
    """Animated toggle switch widget with ON/OFF states"""

    _TRACK_COLOR_ON = QColor("#4CAF50")
    _TRACK_COLOR_OFF = QColor("#B0B0B0")
    _HANDLE_COLOR = QColor("#FFFFFF")
    _TRACK_HEIGHT = 22
    _TRACK_WIDTH = 44
    _HANDLE_SIZE = 18
    _HANDLE_MARGIN = 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        self._handle_position = self._HANDLE_MARGIN

        self._animation = QPropertyAnimation(self, b"handle_position", self)
        self._animation.setDuration(150)
        self._animation.setEasingCurve(QEasingCurve.InOutCubic)

    def sizeHint(self):
        return QSize(self._TRACK_WIDTH, self._TRACK_HEIGHT)

    def minimumSizeHint(self):
        return self.sizeHint()

    def _get_handle_position(self):
        return self._handle_position

    def _set_handle_position(self, pos):
        self._handle_position = pos
        self.update()

    handle_position = Property(float, _get_handle_position, _set_handle_position)

    def setChecked(self, checked):
        super().setChecked(checked)
        # Snap position immediately (no animation) when set programmatically
        if checked:
            self._handle_position = (
                self._TRACK_WIDTH - self._HANDLE_SIZE - self._HANDLE_MARGIN
            )
        else:
            self._handle_position = self._HANDLE_MARGIN
        self.update()

    def checkStateSet(self):
        super().checkStateSet()
        self._animate_toggle()

    def nextCheckState(self):
        super().nextCheckState()
        self._animate_toggle()

    def _animate_toggle(self):
        self._animation.stop()
        if self.isChecked():
            end = self._TRACK_WIDTH - self._HANDLE_SIZE - self._HANDLE_MARGIN
        else:
            end = self._HANDLE_MARGIN
        self._animation.setStartValue(self._handle_position)
        self._animation.setEndValue(end)
        self._animation.start()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # Draw track
        if self.isChecked():
            track_color = self._TRACK_COLOR_ON
        else:
            track_color = self._TRACK_COLOR_OFF

        radius = self._TRACK_HEIGHT / 2
        track_rect = QRect(0, 0, self._TRACK_WIDTH, self._TRACK_HEIGHT)
        painter.setPen(Qt.NoPen)
        painter.setBrush(track_color)
        painter.drawRoundedRect(track_rect, radius, radius)

        # Draw handle
        handle_y = (self._TRACK_HEIGHT - self._HANDLE_SIZE) / 2
        handle_rect = QRect(
            int(self._handle_position),
            int(handle_y),
            self._HANDLE_SIZE,
            self._HANDLE_SIZE,
        )
        painter.setBrush(self._HANDLE_COLOR)
        # Subtle shadow via border
        painter.setPen(QPen(QColor(0, 0, 0, 30), 1))
        painter.drawEllipse(handle_rect)

        painter.end()
