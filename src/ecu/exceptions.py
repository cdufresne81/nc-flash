"""
ECU Exception Hierarchy

All ECU-related exceptions derive from ECUError, which itself derives from
the application's RomEditorError for unified error handling.
"""

from src.core.exceptions import RomEditorError


class ECUError(RomEditorError):
    """Base exception for all ECU communication and flash errors."""

    pass


# --- J2534 Errors ---


class J2534Error(ECUError):
    """Base exception for J2534 PassThru driver errors."""

    pass


class J2534DLLNotFound(J2534Error):
    """Raised when the J2534 DLL cannot be loaded."""

    pass


class J2534DeviceNotFound(J2534Error):
    """Raised when no J2534 device is detected."""

    pass


class J2534ConnectionError(J2534Error):
    """Raised when connecting to the J2534 device/channel fails."""

    pass


# --- UDS Protocol Errors ---


class UDSError(ECUError):
    """Base exception for UDS diagnostic protocol errors."""

    pass


class NegativeResponseError(UDSError):
    """Raised when the ECU returns a Negative Response Code (NRC)."""

    def __init__(self, nrc: int, description: str = ""):
        self.nrc = nrc
        self.description = description or f"NRC 0x{nrc:02X}"
        super().__init__(f"ECU negative response: {self.description} (0x{nrc:02X})")


class SecurityAccessDenied(UDSError):
    """Raised when security access (seed/key exchange) is rejected."""

    pass


class TransferError(UDSError):
    """Raised when data transfer (SBL or ROM) fails."""

    pass


class UDSTimeoutError(UDSError):
    """Raised when an ECU response times out."""

    pass


# --- Flash Errors ---


class FlashError(ECUError):
    """Base exception for flash orchestration errors."""

    pass


class FlashAbortedError(FlashError):
    """Raised when the user aborts a flash in progress."""

    pass


class ROMValidationError(FlashError):
    """Raised when ROM data fails pre-flash validation."""

    pass


class VehicleGenerationError(ROMValidationError):
    """Raised when the vehicle generation cannot be determined from ROM data."""

    pass


class ChecksumError(FlashError):
    """Raised when ROM checksum validation or correction fails."""

    pass


# --- Secure Module ---


class SecureModuleNotAvailable(ECUError):
    """Raised when the private security module (_secure/) is not installed."""

    def __init__(self):
        super().__init__(
            "Security module not available. "
            "The _secure/ package is required for ECU flashing. "
            "Contact the project maintainer for access."
        )
