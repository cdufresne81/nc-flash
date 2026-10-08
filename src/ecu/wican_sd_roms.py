"""Keep only the newest N staged ROMs on the WiCAN SD card (#139).

Every SD-staged flash uploads ``roms/<stem>_<YYYYMMDD>_<HHMM>.bin`` (~1 MB) and
its ``.json`` manifest (:mod:`src.ecu.wican_sd_package`), and the firmware never
removes them. :func:`trim_staged_roms` deletes the older ones, run by
:class:`~src.ecu.wican_sd_flash.WiCANSdFlasher` only after a successful flash.

What it may delete is deliberately narrow:

- Only names NC Flash itself stages: ``<stem>_<YYYYMMDD>_<HHMM>`` + ``.bin`` or
  ``.json``, plus a leftover ``.part`` of either from an interrupted upload.
  Anything else in ``roms/`` (a file the user put there) is left alone.
- Only entries the file manager lists as plain, unlocked files.
- The image + manifest pair is one unit, ranked by the timestamp in its NAME
  (stamped from the PC clock at staging). The device's mtime is not used: a
  WiCAN may have no clock.
- The pair just flashed (``protect``) is always kept and counts toward ``keep``,
  whatever the sort says (a PC clock that was once set ahead could otherwise
  rank an older image above it).

The firmware refuses every SD delete with ``409`` while an ECU flash runs, and
it reports the flash done just before it lowers that fence, so a 409 on the
first delete is retried once after a short wait. Any other failure stops the
pass; the next successful flash tries again.

Headless: standard library only, no PySide6.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from .wican_http import WiCANHttpError
from .wican_sd_files import DELETABLE_NAME, WiCANSdFiles

logger = logging.getLogger(__name__)

#: The staging folder, relative to /sdcard (the file manager's root).
SD_ROMS_DIR = "roms"

#: A name NC Flash stages: ``<stem>_<YYYYMMDD>_<HHMM>.bin|.json`` (+ ``.part``).
#: ``group`` is the pair's shared name, ``ts`` its sortable timestamp.
_STAGED_NAME = re.compile(
    r"(?P<group>[A-Za-z0-9._-]+?_(?P<ts>\d{8}_\d{4}))\.(?P<ext>bin|json)(?:\.part)?"
)

#: Wait before retrying a delete the firmware refused because its flash fence
#: was still up (it lowers it a moment after reporting the flash done).
FENCE_RETRY_WAIT_S = 2.0


@dataclass(frozen=True)
class RomTrimResult:
    """Outcome of one :func:`trim_staged_roms` pass."""

    deleted: list = field(default_factory=list)  # list[str] file names removed
    kept: list = field(default_factory=list)  # list[str] pair names, newest first
    #: (name, reason) of the delete that stopped the pass, if one failed.
    failed: Optional[tuple] = None


def staged_group(name: str) -> Optional[str]:
    """The pair name of a file NC Flash staged, or None for any other name."""
    m = _STAGED_NAME.fullmatch(name)
    return m.group("group") if m else None


def _delete_order(name: str):
    # The image first (it is what fills the card), then its manifest, then
    # any upload leftovers.
    m = _STAGED_NAME.fullmatch(name)
    return (name.endswith(".part"), m.group("ext") != "bin", name)


def trim_staged_roms(
    files: WiCANSdFiles,
    keep: int,
    *,
    protect: str,
    retry_wait_s: float = FENCE_RETRY_WAIT_S,
) -> RomTrimResult:
    """Delete all but the newest *keep* staged ROM pairs from ``roms/``.

    *protect* is the staged image name just flashed; its pair is always kept.
    Raises ``ValueError`` for ``keep < 1`` or a *protect* name NC Flash would
    not stage (deleting nothing), and an error when the folder cannot be
    listed (usually :class:`WiCANHttpError`). A failed delete does not raise:
    it ends the pass and is reported in :attr:`RomTrimResult.failed`.
    """
    if keep < 1:
        raise ValueError(f"keep must be at least 1, got {keep}")
    protected = staged_group(protect)
    if protected is None:
        raise ValueError(f"not a staged ROM name: {protect!r}")

    groups: dict = {}
    for name, entry in files.list_dir(SD_ROMS_DIR).items():
        if entry.get("type") != "file" or entry.get("locked") or entry.get("active"):
            continue
        group = staged_group(name)
        if group is None or not DELETABLE_NAME.fullmatch(name):
            continue
        groups.setdefault(group, []).append(name)

    # A pair name ends in its 13-char ``YYYYMMDD_HHMM`` timestamp.
    newest_first = sorted(groups, key=lambda g: (g[-13:], g), reverse=True)
    kept = [protected]
    for group in newest_first:
        if len(kept) >= keep:
            break
        if group != protected:
            kept.append(group)
    doomed = [g for g in reversed(newest_first) if g not in kept]  # oldest first

    deleted: list = []
    retried = False
    for group in doomed:
        for name in sorted(groups[group], key=_delete_order):
            while True:
                try:
                    files.delete_file(SD_ROMS_DIR, name)
                    deleted.append(name)
                    break
                except WiCANHttpError as exc:
                    if "HTTP 409" in str(exc) and not retried:
                        retried = True
                        time.sleep(retry_wait_s)
                        continue
                    logger.warning(
                        "Staged-ROM cleanup stopped at %s (retried after the "
                        "next flash): %s",
                        name,
                        exc,
                    )
                    return RomTrimResult(deleted, kept, (name, str(exc)))
    if deleted:
        logger.info(
            "Removed %d old staged ROM file(s) from the WiCAN SD card; kept %d: %s",
            len(deleted),
            len(kept),
            ", ".join(kept),
        )
    return RomTrimResult(deleted, kept, None)
