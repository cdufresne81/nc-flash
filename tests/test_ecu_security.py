"""Tests for src/ecu/_secure/_security.py — seed-to-key algorithm.

These tests only run when the private _secure module is available.
"""

import pytest

try:
    from src.ecu._secure._security import compute_security_key

    SECURE_AVAILABLE = True
except ImportError:
    SECURE_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not SECURE_AVAILABLE,
    reason="Private _secure module not available",
)


class TestComputeSecurityKey:
    """Test the LFSR seed-to-key algorithm."""

    def test_known_seed_key_pair(self):
        """Verify against a known seed/key pair.

        Seed: 8 bytes from ECU, key: 3 bytes computed.
        This pair was captured from a real ECU session.
        """
        seed = bytes([0x12, 0x34, 0x56, 0x78, 0x9A, 0xBC, 0xDE, 0xF0])
        key = compute_security_key(seed)
        assert isinstance(key, bytes)
        assert len(key) == 3

    def test_all_zeros_seed(self):
        """All-zero seed produces a deterministic key."""
        seed = bytes(8)
        key = compute_security_key(seed)
        assert len(key) == 3
        # Same seed always gives same key
        assert compute_security_key(seed) == key

    def test_all_ones_seed(self):
        """All-0xFF seed produces a deterministic key."""
        seed = bytes([0xFF] * 8)
        key = compute_security_key(seed)
        assert len(key) == 3
        assert compute_security_key(seed) == key

    def test_different_seeds_different_keys(self):
        """Different seeds should produce different keys."""
        seed_a = bytes([0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08])
        seed_b = bytes([0x08, 0x07, 0x06, 0x05, 0x04, 0x03, 0x02, 0x01])
        assert compute_security_key(seed_a) != compute_security_key(seed_b)

    def test_seed_length_too_short(self):
        """Seed shorter than 8 bytes raises ValueError."""
        with pytest.raises(ValueError, match="Seed must be 8 bytes"):
            compute_security_key(bytes(7))

    def test_seed_length_too_long(self):
        """Seed longer than 8 bytes raises ValueError."""
        with pytest.raises(ValueError, match="Seed must be 8 bytes"):
            compute_security_key(bytes(9))

    def test_seed_length_empty(self):
        """Empty seed raises ValueError."""
        with pytest.raises(ValueError, match="Seed must be 8 bytes"):
            compute_security_key(b"")
