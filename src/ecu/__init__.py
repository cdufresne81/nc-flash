"""
ECU Flash Module

Native Python implementation of Mazda NC Miata ECU communication,
replacing the external romdrop.exe dependency.

Provides J2534 PassThru communication, UDS diagnostic services,
ROM validation, checksum correction, and flash orchestration.
"""

from .exceptions import (
    ECUError,
    J2534Error,
    J2534DLLNotFound,
    J2534DeviceNotFound,
    J2534ConnectionError,
    UDSError,
    UDSTimeoutError,
    NegativeResponseError,
    SecurityAccessDenied,
    TransferError,
    FlashError,
    FlashAbortedError,
    ROMValidationError,
    VehicleGenerationError,
    ChecksumError,
    SecureModuleNotAvailable,
)
from .checksum import mazda_checksum, correct_rom_checksums, crc32
from .rom_utils import (
    detect_vehicle_generation,
    get_rom_id,
    get_cal_id,
    get_calibration_crc,
    validate_rom_size,
    find_first_difference,
    calculate_flash_start_index,
    patch_rom,
    PatchResult,
)
from .dtc import get_dtc_description, get_dtc_prefix, format_dtc
from .flash_manager import FlashManager, FlashState, SECURE_MODULE_AVAILABLE

# Transports are PySide6-free core modules; guard imports anyway so a broken
# optional dependency (e.g. WiCAN's stack) never takes down the package.
from .transport import (
    EcuTransport,
    J2534Transport,
    FakeTransport,
    create_ecu_transport,
)

try:
    from .wican_transport import WiCANTransport, WiCANError
except ImportError:
    # Optional WiCAN transport stack unavailable.
    WiCANTransport = None  # type: ignore[misc, assignment]
    WiCANError = None  # type: ignore[misc, assignment]

try:
    from .session import ECUSession, ECUSessionState
except ImportError:
    # PySide6 not available (e.g., test environment)
    ECUSession = None  # type: ignore[misc, assignment]
    ECUSessionState = None  # type: ignore[misc, assignment]

__all__ = [
    # Exceptions
    "ECUError",
    "J2534Error",
    "J2534DLLNotFound",
    "J2534DeviceNotFound",
    "J2534ConnectionError",
    "UDSError",
    "UDSTimeoutError",
    "NegativeResponseError",
    "SecurityAccessDenied",
    "TransferError",
    "FlashError",
    "FlashAbortedError",
    "ROMValidationError",
    "VehicleGenerationError",
    "ChecksumError",
    "SecureModuleNotAvailable",
    # Checksum
    "mazda_checksum",
    "correct_rom_checksums",
    "crc32",
    # ROM Utils
    "detect_vehicle_generation",
    "get_rom_id",
    "get_cal_id",
    "validate_rom_size",
    "find_first_difference",
    "calculate_flash_start_index",
    "get_calibration_crc",
    "patch_rom",
    "PatchResult",
    # DTC
    "get_dtc_description",
    "get_dtc_prefix",
    "format_dtc",
    # Flash
    "FlashManager",
    "FlashState",
    "SECURE_MODULE_AVAILABLE",
    # Transports
    "EcuTransport",
    "J2534Transport",
    "FakeTransport",
    "WiCANTransport",
    "WiCANError",
    "create_ecu_transport",
    # Session
    "ECUSession",
    "ECUSessionState",
]
