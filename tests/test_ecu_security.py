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

    def test_known_pair_1(self):
        """Verified against romdrop.log capture (romdrop-master)."""
        seed = bytes([0xB3, 0xA8, 0x4A])
        key = compute_security_key(seed)
        assert key == bytes([0x44, 0x70, 0xE8])

    def test_known_pair_2(self):
        """Verified against romdrop.log capture (romdrop_rev_210202)."""
        seed = bytes([0xAA, 0xC1, 0x02])
        key = compute_security_key(seed)
        assert key == bytes([0x7F, 0x26, 0xF4])

    def test_key_is_3_bytes(self):
        key = compute_security_key(bytes([0x12, 0x34, 0x56]))
        assert len(key) == 3

    def test_deterministic(self):
        seed = bytes([0xAB, 0xCD, 0xEF])
        assert compute_security_key(seed) == compute_security_key(seed)

    def test_different_seeds_different_keys(self):
        seed_a = bytes([0x01, 0x02, 0x03])
        seed_b = bytes([0x03, 0x02, 0x01])
        assert compute_security_key(seed_a) != compute_security_key(seed_b)

    def test_seed_length_too_short(self):
        with pytest.raises(ValueError, match="Seed must be 3 bytes"):
            compute_security_key(bytes(2))

    def test_seed_length_too_long(self):
        with pytest.raises(ValueError, match="Seed must be 3 bytes"):
            compute_security_key(bytes(4))

    def test_seed_length_empty(self):
        with pytest.raises(ValueError, match="Seed must be 3 bytes"):
            compute_security_key(b"")
