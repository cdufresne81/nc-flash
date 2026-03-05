"""
CRC checksum database for ROM patch validation.

Parses romdrop.crc files that contain expected CRC-32 values
for factory ROMs, patches, and patched calibrations.
"""

import struct
from pathlib import Path
from dataclasses import dataclass


@dataclass
class CRCEntry:
    """A single CRC database entry."""

    cal_id: bytes  # 6-byte calibration ID
    factory_crc: int  # CRC-32 of factory calibration region
    patch_crc: int  # CRC-32 of patch file calibration region
    patched_cal_crc: int  # CRC-32 of patched calibration region

    @property
    def cal_id_str(self) -> str:
        return self.cal_id.decode("ascii", errors="replace").rstrip("\x00")


ENTRY_SIZE = 18  # 6 + 4 + 4 + 4
HEADER_SIZE = 4


class CRCDatabase:
    """Parser and lookup for romdrop.crc checksum database."""

    def __init__(self):
        self.header: str = ""
        self.entries: list[CRCEntry] = []

    @classmethod
    def from_file(cls, path: str | Path) -> "CRCDatabase":
        """Load a CRC database from a romdrop.crc file."""
        data = Path(path).read_bytes()
        return cls.from_bytes(data)

    @classmethod
    def from_bytes(cls, data: bytes) -> "CRCDatabase":
        """Parse CRC database from raw bytes."""
        db = cls()
        if len(data) < HEADER_SIZE:
            return db

        # Header: first 4 bytes as hex string
        db.header = data[:HEADER_SIZE].hex()

        # Entries start at offset 4
        payload = data[HEADER_SIZE:]
        count = len(payload) // ENTRY_SIZE

        for i in range(count):
            offset = i * ENTRY_SIZE
            entry_data = payload[offset : offset + ENTRY_SIZE]
            if len(entry_data) < ENTRY_SIZE:
                break

            cal_id = entry_data[0:6]
            factory_crc = int.from_bytes(entry_data[6:10], "big")
            patch_crc = int.from_bytes(entry_data[10:14], "big")
            patched_cal_crc = int.from_bytes(entry_data[14:18], "big")

            db.entries.append(CRCEntry(cal_id, factory_crc, patch_crc, patched_cal_crc))

        return db

    def find_entry(self, cal_id: bytes) -> CRCEntry | None:
        """Look up an entry by 6-byte calibration ID."""
        for entry in self.entries:
            if entry.cal_id == cal_id[:6]:
                return entry
        return None

    def get_factory_crc(self, cal_id: bytes) -> int | None:
        """Get expected factory calibration CRC for a cal-ID."""
        entry = self.find_entry(cal_id)
        return entry.factory_crc if entry else None

    def get_patch_crc(self, cal_id: bytes) -> int | None:
        """Get expected patch CRC for a cal-ID."""
        entry = self.find_entry(cal_id)
        return entry.patch_crc if entry else None

    def get_patched_cal_crc(self, cal_id: bytes) -> int | None:
        """Get expected patched calibration CRC for a cal-ID."""
        entry = self.find_entry(cal_id)
        return entry.patched_cal_crc if entry else None

    def __len__(self) -> int:
        return len(self.entries)

    def __repr__(self) -> str:
        return f"CRCDatabase(header={self.header!r}, entries={len(self.entries)})"
