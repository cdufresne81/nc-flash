"""
Tests for security stub behavior — always runs in CI.

Verifies that when the private _secure module is absent, the stub
functions raise SecureModuleNotAvailable and flash operations are
blocked at the entry point.
"""

from unittest.mock import patch

import pytest

from src.ecu._secure_stub import (
    compute_security_key as stub_compute_key,
    get_sbl_data as stub_get_sbl,
)
from src.ecu.constants import ROM_SIZE
from src.ecu.exceptions import SecureModuleNotAvailable
from src.ecu.flash_manager import FlashManager


class TestSecureStubFunctions:
    """Direct tests of the stub functions in _secure_stub.py."""

    def test_compute_security_key_raises(self):
        """Stub compute_security_key always raises SecureModuleNotAvailable."""
        with pytest.raises(SecureModuleNotAvailable):
            stub_compute_key(b"\xAA\xBB\xCC")

    def test_get_sbl_data_raises(self):
        """Stub get_sbl_data always raises SecureModuleNotAvailable."""
        with pytest.raises(SecureModuleNotAvailable):
            stub_get_sbl(0x2000, "NC1")

    def test_exception_message_contains_guidance(self):
        """Error message should guide the user on what to do."""
        with pytest.raises(SecureModuleNotAvailable, match="Security module"):
            stub_compute_key(b"\x00\x00\x00")


class TestFlashManagerStubGuard:
    """Verify FlashManager blocks operations when _secure is missing.

    Unlike the skip-based tests in test_ecu_flash_manager.py, these
    always run by patching SECURE_MODULE_AVAILABLE to False.
    """

    def test_flash_rom_raises_when_stub(self):
        """flash_rom rejects immediately when secure module unavailable."""
        fm = FlashManager()
        with patch("src.ecu.flash_manager.SECURE_MODULE_AVAILABLE", False):
            with pytest.raises(SecureModuleNotAvailable):
                fm.flash_rom(b"\x00" * ROM_SIZE)

    def test_dynamic_flash_raises_when_stub(self, tmp_path):
        """dynamic_flash rejects immediately when secure module unavailable."""
        fm = FlashManager()
        archive = tmp_path / "archive.bin"
        archive.write_bytes(b"\x00" * ROM_SIZE)
        with patch("src.ecu.flash_manager.SECURE_MODULE_AVAILABLE", False):
            with pytest.raises(SecureModuleNotAvailable):
                fm.dynamic_flash(b"\x00" * ROM_SIZE, str(archive))
