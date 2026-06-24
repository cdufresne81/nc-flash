"""
Tests for LogConsole console-scope filtering (src/ui/log_console.py).

Covers:
  * Qt-logger records are dropped from the console but still reach other
    root handlers (e.g. the session file handler).
  * Optional logger-name allowlist shows only matching loggers.
  * Default (no allowlist, drop_qt_logger=False) is unchanged: any logger
    at INFO+ is shown (back-compat for the main-window LogConsole).
  * min_level (INFO) still applies on top of the allowlist.

Requires a QApplication for widget instantiation.
"""

import logging

import pytest
from PySide6.QtWidgets import QApplication

from src.ui.log_console import LogConsole, _ConsoleScopeFilter


@pytest.fixture(scope="module")
def qapp():
    """Create QApplication for Qt widget tests."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture(autouse=True)
def root_at_debug():
    """Ensure root passes INFO/DEBUG records to handlers during these tests."""
    root = logging.getLogger()
    prev = root.level
    root.setLevel(logging.DEBUG)
    yield
    root.setLevel(prev)


def _shown(console, monkeypatch):
    """Spy on console.append_log and return the list of records it *shows*.

    append_log is the slot the QtLogHandler signal is connected to; a record
    that passes the handler's filter reaches it, a filtered one never does.
    The spy mirrors append_log's own min_level gate so the captured list
    reflects what is actually rendered (not merely what reached the slot).
    """
    captured = []
    orig = console.append_log

    def _spy(message, level):
        if level >= console.min_level:
            captured.append((message, level))
        return orig(message, level)

    monkeypatch.setattr(console, "append_log", _spy)
    # Reconnect the handler signal to the spy (the original was connected at
    # setup_logging time to the bound method we just shadowed).
    console.log_handler.log_message.disconnect()
    console.log_handler.log_message.connect(console.append_log)
    return captured


def _emit(logger_name, level, msg):
    logging.getLogger(logger_name).log(level, msg)


# ---------------------------------------------------------------------------
# _ConsoleScopeFilter (unit, no Qt needed but kept here for cohesion)
# ---------------------------------------------------------------------------


class TestConsoleScopeFilter:
    def _record(self, name):
        return logging.LogRecord(name, logging.INFO, __file__, 1, "x", None, None)

    def test_drop_qt_logger(self):
        f = _ConsoleScopeFilter(drop_qt_logger=True)
        assert f.filter(self._record("qt")) is False
        assert f.filter(self._record("qt.core")) is False
        assert f.filter(self._record("src.ecu.protocol")) is True

    def test_allowlist_prefix_match(self):
        f = _ConsoleScopeFilter(allowed_prefixes=["src.ecu", "__main__"])
        assert f.filter(self._record("src.ecu")) is True
        assert f.filter(self._record("src.ecu.protocol")) is True
        assert f.filter(self._record("__main__")) is True
        assert f.filter(self._record("src.rom.reader")) is False
        # Guard against loose substring matching: "src.ecu_foo" must NOT match.
        assert f.filter(self._record("src.ecu_foo")) is False

    def test_no_constraints_passes_all(self):
        f = _ConsoleScopeFilter()
        assert f.filter(self._record("anything")) is True


# ---------------------------------------------------------------------------
# CHANGE #2 — qt records dropped from console, kept on root file handler
# ---------------------------------------------------------------------------


class TestQtDropFromConsole:
    def test_qt_records_dropped_from_console(self, qapp, monkeypatch):
        console = LogConsole(auto_register=True, drop_qt_logger=True)
        try:
            captured = _shown(console, monkeypatch)
            _emit(
                "qt",
                logging.WARNING,
                "Qt: QObject::setParent: Cannot set parent, "
                "new parent is in a different thread",
            )
            assert not any("Cannot set parent" in m for m, _ in captured)
            # A relevant logger still reaches the console.
            _emit("src.ecu.protocol", logging.INFO, "ECU >> connected")
            assert any("ECU >> connected" in m for m, _ in captured)
        finally:
            console.unregister_logging()

    def test_qt_still_reaches_root_file_handler(self, qapp):
        """Dropping qt from the console must NOT disable propagation: a
        separately-attached root handler still receives the qt record
        (proves the session file handler keeps getting Qt diagnostics)."""
        console = LogConsole(auto_register=True, drop_qt_logger=True)
        seen = []

        class _Spy(logging.Handler):
            def emit(self, record):
                seen.append(record.name)

        spy = _Spy()
        root = logging.getLogger()
        root.addHandler(spy)
        prev_level = root.level
        root.setLevel(logging.DEBUG)
        try:
            _emit("qt", logging.WARNING, "Qt: something")
            assert "qt" in seen, "qt record must still reach other root handlers"
        finally:
            root.removeHandler(spy)
            root.setLevel(prev_level)
            console.unregister_logging()


# ---------------------------------------------------------------------------
# CHANGE #3 — logger-name allowlist
# ---------------------------------------------------------------------------


class TestAllowlist:
    def test_allowlist_blocks_unrelated_loggers(self, qapp, monkeypatch):
        console = LogConsole(
            auto_register=True,
            allowed_logger_prefixes=["src.ecu", "src.ui.ecu_window", "__main__"],
        )
        try:
            captured = _shown(console, monkeypatch)
            _emit("src.rom.reader", logging.INFO, "rom loaded")
            assert not any("rom loaded" in m for m, _ in captured)
            _emit("src.ecu.protocol", logging.INFO, "ecu line")
            _emit("__main__", logging.INFO, "flash line")
            shown = [m for m, _ in captured]
            assert any("ecu line" in m for m in shown)
            assert any("flash line" in m for m in shown)
        finally:
            console.unregister_logging()

    def test_default_no_allowlist_unchanged(self, qapp, monkeypatch):
        """Default console (main-window) still shows ANY logger at INFO+."""
        console = LogConsole(auto_register=True)
        try:
            captured = _shown(console, monkeypatch)
            _emit("some.unrelated.subsystem", logging.INFO, "hello world")
            assert any("hello world" in m for m, _ in captured)
        finally:
            console.unregister_logging()

    def test_min_level_filtering_still_applies(self, qapp, monkeypatch):
        """DEBUG on an allowed logger is still suppressed by min_level=INFO."""
        console = LogConsole(auto_register=True, allowed_logger_prefixes=["src.ecu"])
        try:
            captured = _shown(console, monkeypatch)
            # Root is at DEBUG (autouse fixture), so the record propagates to
            # the handler; append_log drops it because min_level == INFO.
            _emit("src.ecu.protocol", logging.DEBUG, "debug noise")
            assert not any("debug noise" in m for m, _ in captured)
            _emit("src.ecu.protocol", logging.INFO, "info kept")
            assert any("info kept" in m for m, _ in captured)
        finally:
            console.unregister_logging()
