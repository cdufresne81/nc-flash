"""
Shared error-handling helpers for UI code.

Provides a single ``handle_rom_operation_error`` function used by both
``main.py`` and the project mixin to log errors and show message boxes, and
``ecu_error_message`` which turns an ECU operation error into the text shown
to the user.
"""

from PySide6.QtWidgets import QMessageBox

from src.ecu.constants import NRC_CONDITIONS_NOT_CORRECT, SID_DIAGNOSTIC_SESSION
from src.ecu.exceptions import NegativeResponseError
from src.utils.logging_config import get_logger

logger = get_logger(__name__)


def handle_rom_operation_error(parent, operation: str, exception: Exception):
    """
    Handle common ROM operation errors with consistent logging and user feedback.

    Args:
        parent: Parent widget for message box
        operation: Description of operation that failed (e.g., "open ROM file")
        exception: The exception that was raised
    """
    error_msg = f"Failed to {operation}:\n{str(exception)}"
    logger.error(error_msg.replace("\n", " "))
    QMessageBox.critical(parent, "Error", error_msg)


#: Shown when the ECU answers the programming-session request (SID 0x10) with
#: NRC 0x22 during authentication. EcuTek-tuned ECUs do this every time (their
#: ROM is locked), but so does a stock ECU asked again within a few seconds of a
#: previous session attempt, so the text must not claim the ECU is locked.
SESSION_REFUSED_MESSAGE = (
    "The ECU refused the programming session (Conditions not correct).\n\n"
    "This usually means the ECU is locked by another tuning tool such as "
    "EcuTek, which NC Flash can't read or flash.\n\n"
    "If it isn't, check the ignition is ON and the engine is OFF, wait a few "
    "seconds (the ECU briefly refuses a new session right after a previous "
    "attempt), then try again."
)


def _session_refused(exc: BaseException) -> bool:
    """True if ``exc`` (or an exception it wraps) is the ECU refusing the
    programming session with NRC 0x22."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if (
            isinstance(exc, NegativeResponseError)
            and exc.nrc == NRC_CONDITIONS_NOT_CORRECT
            and exc.service_id == SID_DIAGNOSTIC_SESSION
        ):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def ecu_error_message(exc: BaseException) -> str:
    """Text to show the user for a failed ECU operation (read, RAM scan, flash).

    Known ECU refusals get a plain-words explanation; anything else keeps the
    exception's own message. The caller still logs the full traceback.
    """
    if _session_refused(exc):
        return SESSION_REFUSED_MESSAGE
    return str(exc)
