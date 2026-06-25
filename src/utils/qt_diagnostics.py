"""Qt + native crash diagnostics.

Routes Qt's own console warnings into our logger and, for the small set of
threading/painting warnings that have preceded a hard crash on the WiCAN
flash-completion path, captures the **Python stack** at the instant Qt emits the
warning. An intermittent, hardware-only crash (e.g. 2026-06-23: a dynamic flash
that completed on the ECU but closed the app during the completion UI, preceded by
``QObject::setParent: Cannot set parent, new parent is in a different thread`` and
``QBackingStore::endPaint() called with active painter``) therefore leaves an
actionable trace in the session log instead of a bare, un-attributable console
line. ``faulthandler`` is armed so a genuine native fault also dumps a C-level
traceback.

Install once at startup (see ``main.py``) — it is a pure diagnostic and never
changes behaviour. Stdlib + PySide6 only.
"""

from __future__ import annotations

import faulthandler
import logging
import traceback
from typing import Optional

from PySide6.QtCore import QtMsgType, qInstallMessageHandler

logger = logging.getLogger("qt")

#: Substrings of Qt warnings that have preceded a hard crash. When one is seen we
#: dump the current Python stack so the triggering slot/widget op is identifiable.
#: Kept narrow on purpose — a normal session emits none of these.
_STACK_TRIGGERS = (
    "Cannot set parent",
    "different thread",
    "endPaint",
    "active painter",
    "Timers cannot be",
    "QObject::~QObject",
)

_LEVELS = {
    QtMsgType.QtDebugMsg: logging.DEBUG,
    QtMsgType.QtInfoMsg: logging.INFO,
    QtMsgType.QtWarningMsg: logging.WARNING,
    QtMsgType.QtCriticalMsg: logging.ERROR,
    QtMsgType.QtFatalMsg: logging.CRITICAL,
}


def _qt_message_handler(mode, context, message: str) -> None:
    """Forward a Qt message to the logger; attach a Python stack for known triggers.

    Runs in whatever thread Qt emits from; ``logging`` and ``traceback`` are both
    thread-safe. Must never raise — a throwing handler during a fatal Qt message
    would mask the very crash we are trying to capture.
    """
    try:
        level = _LEVELS.get(mode, logging.WARNING)
        logger.log(level, "Qt: %s", message)
        if any(trigger in message for trigger in _STACK_TRIGGERS):
            # format_stack() excludes this frame's own tail well enough; the chain
            # back through the event loop names the offending slot/widget op.
            stack = "".join(traceback.format_stack())
            logger.error("Qt diagnostic — stack at %r:\n%s", message, stack)
    except Exception:  # pragma: no cover - diagnostics must not destabilise Qt
        pass


def install_qt_diagnostics(fault_file: Optional[object] = None) -> None:
    """Arm ``faulthandler`` and install the Qt message handler.

    Args:
        fault_file: Open file object for native fault tracebacks (e.g. the session
            log stream). ``None`` uses the default (``sys.stderr``), which is
            visible when the app is launched from a terminal.
    """
    try:
        if fault_file is not None:
            faulthandler.enable(file=fault_file, all_threads=True)
        else:
            faulthandler.enable(all_threads=True)
    except Exception:  # pragma: no cover - never block startup on diagnostics
        logger.debug("faulthandler.enable failed", exc_info=True)
    qInstallMessageHandler(_qt_message_handler)
    logger.debug("Qt diagnostics installed")
