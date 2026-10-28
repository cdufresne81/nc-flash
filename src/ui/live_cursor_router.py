"""
Routes the Datalogger's live values to the open table windows.

A main-window collaborator (not a mixin): it reads the live values from their
one owner (:class:`~src.ui.live_values.LiveValues`) and calls
``set_live_values`` on each open table window whose axes have logged channels
behind them. It never holds or mutates the window list; it asks for it.
"""

import logging
from typing import Callable, Iterable

from PySide6.QtCore import QObject

from .live_values import LiveValues

logger = logging.getLogger(__name__)


class LiveCursorRouter(QObject):
    """Live table cursor on/off and the fan-out to table windows."""

    def __init__(
        self,
        live_values: LiveValues,
        windows: Callable[[], Iterable],
        settings,
        parent=None,
    ):
        super().__init__(parent)
        self._live = live_values
        self._windows = windows
        self._settings = settings
        self._enabled = settings.get_live_cursor_enabled()
        self._failed = set()  # id() of windows already logged as failing
        live_values.changed.connect(self._on_values)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool):
        """Show or hide the cursor on every open table (saved)."""
        enabled = bool(enabled)
        if enabled == self._enabled:
            return
        self._enabled = enabled
        self._settings.set_live_cursor_enabled(enabled)
        self._push(self.current())

    def current(self) -> dict:
        """The values a table window should show now ({} = no cursor)."""
        return self._live.current() if self._enabled else {}

    def attach(self, window):
        """A table window just opened: show the cursor at once if logging."""
        values = self.current()
        if values and window.has_live_cursor_axes:
            self._show(window, values)

    def _on_values(self, values: dict):
        if self._enabled:
            self._push(values)

    def _push(self, values: dict):
        for window in list(self._windows()):
            if window.has_live_cursor_axes:
                self._show(window, values)

    def _show(self, window, values: dict):
        """Update one window; a failure there must not freeze the others."""
        try:
            window.set_live_values(values)
        except Exception:  # noqa: BLE001 - the cursor is display-only
            if id(window) not in self._failed:
                self._failed.add(id(window))
                logger.exception("Live table cursor failed on a table window")
            try:
                window.set_live_values({})  # never leave a stale cursor up
            except Exception:  # noqa: BLE001
                pass
