"""
Stub module for when the private security package is not available.

All functions raise SecureModuleNotAvailable to clearly indicate
that the security module must be installed for flash operations.
"""

from .exceptions import SecureModuleNotAvailable


def compute_security_key(seed: bytes) -> bytes:
    """Stub: raises SecureModuleNotAvailable."""
    raise SecureModuleNotAvailable()


def get_sbl_data(flash_start_index: int, vehicle_generation: str) -> bytes:
    """Stub: raises SecureModuleNotAvailable."""
    raise SecureModuleNotAvailable()
