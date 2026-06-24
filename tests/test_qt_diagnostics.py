"""Qt message-handler diagnostics (src/utils/qt_diagnostics.py).

The handler must forward Qt console warnings to our ``qt`` logger and, for the
narrow set of threading/painting warnings that have preceded hard crashes, attach
a Python stack so an intermittent, hardware-only crash leaves an actionable trace.
"""

import logging

from PySide6.QtCore import QtMsgType, qInstallMessageHandler

from src.utils.qt_diagnostics import (
    _STACK_TRIGGERS,
    _qt_message_handler,
    install_qt_diagnostics,
)


def test_forwards_warning_to_logger(caplog):
    with caplog.at_level(logging.WARNING, logger="qt"):
        _qt_message_handler(QtMsgType.QtWarningMsg, None, "some benign Qt warning")
    assert "some benign Qt warning" in caplog.text


def test_benign_message_attaches_no_stack(caplog):
    with caplog.at_level(logging.DEBUG, logger="qt"):
        _qt_message_handler(QtMsgType.QtWarningMsg, None, "ordinary repaint notice")
    assert "diagnostic — stack" not in caplog.text


def test_known_trigger_attaches_python_stack(caplog):
    msg = "QObject::setParent: Cannot set parent, new parent is in a different thread"
    assert any(t in msg for t in _STACK_TRIGGERS)
    with caplog.at_level(logging.ERROR, logger="qt"):
        _qt_message_handler(QtMsgType.QtWarningMsg, None, msg)
    assert "diagnostic — stack" in caplog.text
    # The captured stack names this test frame (proves a real traceback was dumped).
    assert "test_known_trigger_attaches_python_stack" in caplog.text


def test_endpaint_trigger_also_captured(caplog):
    msg = "QBackingStore::endPaint() called with active painter"
    with caplog.at_level(logging.ERROR, logger="qt"):
        _qt_message_handler(QtMsgType.QtWarningMsg, None, msg)
    assert "diagnostic — stack" in caplog.text


def test_handler_never_raises_on_bad_input():
    # A diagnostic handler must not raise (it would mask the crash it captures).
    _qt_message_handler(QtMsgType.QtWarningMsg, None, None)  # type: ignore[arg-type]
    _qt_message_handler(999, None, "weird mode")  # unknown mode -> WARNING default


def test_install_is_safe_and_restorable():
    # Installing arms faulthandler + the message handler without error; restore the
    # previous handler afterwards so the test suite's Qt output is untouched.
    install_qt_diagnostics()
    try:
        # The installed handler routes a trigger message through our logger.
        _qt_message_handler(QtMsgType.QtCriticalMsg, None, "endPaint active painter")
    finally:
        qInstallMessageHandler(None)
