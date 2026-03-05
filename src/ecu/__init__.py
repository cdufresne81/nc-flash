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

__all__ = [
    # Exceptions
    "ECUError",
    "J2534Error",
    "J2534DLLNotFound",
    "J2534DeviceNotFound",
    "J2534ConnectionError",
    "UDSError",
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
]
