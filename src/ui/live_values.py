"""
The latest logged values, for the live table cursor.

One owner (:class:`LiveValues`, held by the Datalogger). Both engines call
:meth:`LiveValues.update` from their GUI-thread handlers; table windows read
the values through :attr:`LiveValues.changed`. Values that stop arriving go
stale after :data:`STALE_MS` and are cleared, so a cursor never sits on an old
position after the logger stops or the ECU goes quiet.
"""

from typing import Dict

from PySide6.QtCore import QObject, QTimer, Signal

#: Values older than this are dropped (the cursor hides).
STALE_MS = 1500
#: Engines send values at most this often (the cursor needs no more).
EMIT_EVERY_S = 0.1


class LiveValues(QObject):
    """Latest ``{channel name: value}`` from the running Datalogger."""

    #: The new values; ``{}`` when cleared (stopped or stale).
    changed = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._values: Dict[str, float] = {}
        self._stale = QTimer(self)
        self._stale.setSingleShot(True)
        self._stale.setInterval(STALE_MS)
        self._stale.timeout.connect(self.clear)

    def current(self) -> Dict[str, float]:
        return dict(self._values)

    def update(self, values: Dict[str, float], *, expire: bool = True):
        """Replace the values (GUI thread). ``expire=False`` keeps them until
        the next update or clear (UI test runner only)."""
        self._values = dict(values)
        if expire:
            self._stale.start()
        else:
            self._stale.stop()
        self.changed.emit(dict(self._values))

    def clear(self):
        self._stale.stop()
        if self._values:
            self._values = {}
            self.changed.emit({})
