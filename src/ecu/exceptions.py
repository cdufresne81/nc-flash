"""
ECU Exception Hierarchy

All ECU-related exceptions derive from ECUError, which itself derives from
the application's RomEditorError for unified error handling.
"""

from typing import Optional

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

    def __init__(
        self, nrc: int, description: str = "", service_id: Optional[int] = None
    ):
        self.nrc = nrc
        # The SID the ECU echoed in its 0x7F reply: which request it refuses.
        self.service_id = service_id
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


class GuardReadError(FlashError):
    """A pre-flash safety check (engine RPM, battery voltage) could not read
    the car: no reply, a corrupt reply, or a refusal meant for another request.
    The flash must not start unless the operator explicitly overrides."""


class ECUStillProgrammingError(FlashError):
    """The ECU refused to start a flash (NRC 0x22 on the flash-counter check or
    RequestDownload) because it is still in programming mode, typically from an
    earlier flash that was interrupted. Raised before anything is written in
    this attempt. A reset (session + security + ECUReset) clears it."""

    def __init__(self):
        super().__init__(
            "The ECU refused to start the flash: it is still in programming "
            "mode, probably from an earlier flash that was interrupted. "
            "Nothing was written this time."
        )


class EngineRunningError(FlashError):
    """Raised by the RPM gate when a flash is attempted with the engine running.

    Flashing with the engine running risks a brick (voltage sag, electrical
    noise, the ECU actively driving actuators), so the gate refuses to enter the
    programming session unless the operator explicitly overrides it. Carries the
    measured RPM so callers can surface it.
    """

    def __init__(self, rpm: float):
        self.rpm = rpm
        super().__init__(
            f"Engine is running ({rpm:.0f} RPM). Turn the engine OFF before "
            "flashing the ECU."
        )


class VehicleGenerationError(ROMValidationError):
    """Raised when the vehicle generation cannot be determined from ROM data."""

    pass


class ECUGenerationMismatchError(FlashError):
    """The ROM file is for the other generation than the ECU (NC1 vs NC2).

    NC1 and NC2 ROMs are not interchangeable, and the SBL NC Flash sends is
    picked from the FILE, so the flash is refused before anything is erased.
    There is no override.
    """

    def __init__(
        self, rom_generation: str, ecu_generation: str, *, from_operator=False
    ):
        self.rom_generation = rom_generation
        self.ecu_generation = ecu_generation
        self.from_operator = from_operator
        where = (
            f"you said this car is an {ecu_generation}"
            if from_operator
            else f"the ECU is an {ecu_generation}"
        )
        super().__init__(
            f"This ROM is for an {rom_generation}, but {where}. NC1 and NC2 "
            "ROMs are not compatible: flashing it can brick the ECU. Nothing "
            "was written to the ECU."
        )


class ECUGenerationUnverifiedError(FlashError):
    """The ECU's generation could not be read before the flash (it refused or
    did not answer), and nobody confirmed which car it is from."""

    def __init__(self, rom_generation: str, reason: str):
        self.rom_generation = rom_generation
        self.reason = reason
        super().__init__(
            f"Could not check that this {rom_generation} ROM matches the ECU: "
            f"{reason}. Nothing was written to the ECU."
        )


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
