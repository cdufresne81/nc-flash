"""
Patch-state check for NC ROMs: is the ROM patched, and does its patch revision
match the definition?

A RomDrop/NC Flash patch writes its patch-engine revision as 4 BCD-style bytes
at 0xFFC4C (e.g. ``21 05 30 00`` = revision 21053000). Factory ROMs leave those
bytes erased (``FF FF FF FF``; checked against all 121 OEM ROMs). Definitions
record the revision they were written for in ``<checksummodule>``.
"""

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

# Same address as src.ecu.constants.ROM_ID_OFFSET (core may not import ecu;
# tests/test_patch_check.py keeps the two equal).
PATCH_REVISION_OFFSET = 0xFFC4C
_PATCH_REVISION_LEN = 4
_REVISION_RE = re.compile(r"^[0-9A-Fa-f]{8}$")
_ERASED = (b"\xff\xff\xff\xff", b"\x00\x00\x00\x00")


class PatchState(Enum):
    OK = "ok"  # patched, revision matches the definition
    UNPATCHED = "unpatched"  # factory ROM: no patch revision stamped
    MISMATCH = "mismatch"  # patched, but a different revision than the definition
    NOT_APPLICABLE = "n/a"  # definition names no patch revision (TCM, "None", ...)


@dataclass(frozen=True)
class PatchStatus:
    state: PatchState
    expected: Optional[str] = None  # revision the definition expects
    found: Optional[str] = None  # revision stamped in the ROM

    def warning(self, rom_name: str, definition_name: str) -> Optional[str]:
        """User-facing warning text, or None when there is nothing to warn about."""
        if self.state is PatchState.UNPATCHED:
            return (
                f"{rom_name} looks like an unpatched (factory) ROM: it has no patch "
                f"revision, but the {definition_name} definition expects patch "
                f"revision {self.expected}.\n\n"
                "You can view and edit it, but patch the ROM (Tools > Patch ROM...) "
                "before flashing it to a car."
            )
        if self.state is PatchState.MISMATCH:
            return (
                f"{rom_name} was patched with revision {self.found}, but the "
                f"{definition_name} definition was written for revision "
                f"{self.expected}.\n\n"
                "Table addresses may differ between patch revisions, so some "
                "tables could show or write the wrong data. Check you have the "
                "right definition before editing or flashing."
            )
        return None


def check_patch_state(rom_data: bytes, checksummodule: Optional[str]) -> PatchStatus:
    """Compare the ROM's stamped patch revision with the definition's.

    Args:
        rom_data: Full ROM image.
        checksummodule: The definition's ``<checksummodule>`` text.
    """
    expected = (checksummodule or "").strip().upper()
    if not _REVISION_RE.match(expected):
        return PatchStatus(PatchState.NOT_APPLICABLE)
    if len(rom_data) < PATCH_REVISION_OFFSET + _PATCH_REVISION_LEN:
        return PatchStatus(PatchState.NOT_APPLICABLE, expected=expected)

    raw = bytes(
        rom_data[PATCH_REVISION_OFFSET : PATCH_REVISION_OFFSET + _PATCH_REVISION_LEN]
    )
    if raw in _ERASED:
        return PatchStatus(PatchState.UNPATCHED, expected=expected)
    found = raw.hex().upper()
    if found != expected:
        return PatchStatus(PatchState.MISMATCH, expected=expected, found=found)
    return PatchStatus(PatchState.OK, expected=expected, found=found)
