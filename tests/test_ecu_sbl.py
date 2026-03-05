"""Tests for src/ecu/_secure/_sbl.py — SBL data preparation.

These tests only run when the private _secure module is available.
"""

import pytest
from src.ecu.constants import SBL_SIZE

try:
    from src.ecu._secure._sbl import get_sbl_data

    SECURE_AVAILABLE = True
except ImportError:
    SECURE_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not SECURE_AVAILABLE,
    reason="Private _secure module not available",
)


class TestGetSblData:
    """Test SBL data preparation."""

    def test_nc1_size(self):
        """NC1 SBL is exactly 0x1800 bytes."""
        sbl = get_sbl_data(0x2000, "NC1")
        assert len(sbl) == SBL_SIZE

    def test_nc2_size(self):
        """NC2 SBL is exactly 0x1800 bytes."""
        sbl = get_sbl_data(0x2000, "NC2")
        assert len(sbl) == SBL_SIZE

    def test_nc1_returns_bytes(self):
        """SBL data is returned as immutable bytes."""
        sbl = get_sbl_data(0x2000, "NC1")
        assert isinstance(sbl, bytes)

    def test_nc2_returns_bytes(self):
        """SBL data is returned as immutable bytes."""
        sbl = get_sbl_data(0x2000, "NC2")
        assert isinstance(sbl, bytes)

    def test_nc1_nc2_differ(self):
        """NC1 and NC2 SBLs are different."""
        sbl_nc1 = get_sbl_data(0x2000, "NC1")
        sbl_nc2 = get_sbl_data(0x2000, "NC2")
        assert sbl_nc1 != sbl_nc2

    def test_different_flash_start_produces_different_sbl(self):
        """Different flash start indices produce different SBL data."""
        sbl_a = get_sbl_data(0x2000, "NC1")
        sbl_b = get_sbl_data(0x8000, "NC1")
        assert sbl_a != sbl_b

    def test_all_valid_flash_start_indices(self):
        """All 14 valid flash start indices produce valid SBL data."""
        valid_starts = [
            0x2000,
            0x3000,
            0x4000,
            0x5000,
            0x6000,
            0x7000,
            0x8000,
            0x20000,
            0x40000,
            0x60000,
            0x80000,
            0xA0000,
            0xC0000,
            0xE0000,
        ]
        for start in valid_starts:
            sbl = get_sbl_data(start, "NC1")
            assert len(sbl) == SBL_SIZE, f"Bad SBL size for start=0x{start:X}"

    def test_invalid_generation_raises(self):
        """Invalid generation string raises ValueError."""
        with pytest.raises(ValueError, match="Invalid vehicle generation"):
            get_sbl_data(0x2000, "NC3")

    def test_invalid_flash_start_raises(self):
        """Invalid flash start index raises ValueError."""
        with pytest.raises(ValueError, match="Invalid flash start index"):
            get_sbl_data(0x1234, "NC1")
