"""Package for the WiCAN fast flash: ``<stem>.ffp`` + ``<stem>.json``.

The WiCAN firmware (nc-flash-wican-fw, the ``K`` command) runs the fast-flash
sequence by itself from a package the PC uploaded to its SD card. This module
builds that package. Its output is byte-identical to the firmware project's
reference packer (``tools/fast_flash_package.py``) for the same image and
kernel; ``tests/test_ecu_fast_flash_package.py`` runs both and compares.

What it holds:

* package = kernel | one raw LZMA1 stream per program range | commit page;
* manifest = where each section sits, its CRC-32, and the CRC-32 the ECU must
  report for every compare range before and after the flash.

Every block 2..15 is packed (the PC cannot know which blocks differ until the
ECU reports its CRCs). Ranges, streams and kernel rules come from
:mod:`src.ecu.fast_flash` (one copy). The firmware re-checks every rule and CRC
before it contacts the ECU; the wire contract is the firmware's
``docs/internals/fast_flash.md``.

Headless: standard library only.
"""

from __future__ import annotations

import json
import re
import time
import zlib
from dataclasses import dataclass
from typing import Optional

from .constants import ROM_FLASH_START_MIN, ROM_SIZE
from .exceptions import FlashError
from .fast_flash import (
    COMMIT_PAGE,
    COMMIT_PAGE_SIZE,
    ENTRY_POINTER,
    FIRST_APP_BLOCK,
    LAST_BLOCK,
    LZ_LP,
    LZ_PB,
    LZ_PIECE,
    _lz_ranges,
    compare_ranges,
    lzma_stream,
    validate_kernel,
)

#: Kernel version the firmware requires (self CRC, block-15-first rule).
MIN_KERNEL_VERSION = 3
#: Largest manifest the firmware reads.
MANIFEST_MAX = 8192
#: Largest package the firmware loads into PSRAM.
PACKAGE_MAX = 2 * 1024 * 1024
#: Boot-block policies the firmware knows.
BOOT_POLICIES = ("refuse", "allow")
#: Longest SD leaf name the firmware accepts (``ff_name_is_safe``).
_NAME_MAX = 96


@dataclass(frozen=True)
class FastFlashPackage:
    """A package ready to upload."""

    #: SD leaf name of the package (``<stem>.ffp``); the manifest is ``<stem>.json``.
    name: str
    data: bytes
    manifest: dict
    manifest_bytes: bytes

    @property
    def stem(self) -> str:
        return self.name.rsplit(".", 1)[0]

    @property
    def manifest_name(self) -> str:
        return f"{self.stem}.json"


def manifest_bytes(manifest: dict) -> bytes:
    """The manifest as the firmware tool writes it (insertion order, compact)."""
    return json.dumps(manifest, separators=(",", ":")).encode("ascii")


def build(
    image: bytes,
    kernel: bytes,
    boot_policy: str = "refuse",
    rewrite_all: bool = False,
    cal_id: str = "",
    generation: str = "",
    kernel_version: int = MIN_KERNEL_VERSION,
) -> tuple[bytes, dict]:
    """Return ``(package bytes, manifest dict)`` for a checksum-corrected image.

    Same arguments and output as the firmware tool's ``build()``.
    """
    if len(image) != ROM_SIZE:
        raise FlashError(f"Image must be {ROM_SIZE} bytes, got {len(image)}")
    if image[ENTRY_POINTER : ENTRY_POINTER + 4] == b"\xff" * 4:
        raise FlashError("This ROM has no entry pointer; it would never start")
    if boot_policy not in BOOT_POLICIES:
        raise FlashError("boot policy must be 'refuse' or 'allow'")
    if kernel_version < MIN_KERNEL_VERSION:
        raise FlashError(
            f"Dynamic Flash over the WiCAN needs flash program v{MIN_KERNEL_VERSION} "
            "or later"
        )
    validate_kernel(kernel)

    pkg = bytearray()
    kernel_off = len(pkg)
    pkg += kernel
    blocks = []
    for block in range(FIRST_APP_BLOCK, LAST_BLOCK + 1):
        entry = {
            "block": block,
            "cmp": [
                [a, n, zlib.crc32(image[a : a + n])]
                for a, n in compare_ranges(block, True)
            ],
        }
        if block == LAST_BLOCK:
            entry["cmp_pre"] = [
                [a, n, zlib.crc32(image[a : a + n])]
                for a, n in compare_ranges(block, False)
            ]
        ranges = []
        for a, n in _lz_ranges(block):
            stream = lzma_stream(image[a : a + n])
            ranges.append(
                {
                    "addr": a,
                    "size": n,
                    "off": len(pkg),
                    "len": len(stream),
                    "crc32": zlib.crc32(stream),
                }
            )
            pkg += stream
        entry["ranges"] = ranges
        blocks.append(entry)
    commit_off = len(pkg)
    page = image[COMMIT_PAGE : COMMIT_PAGE + COMMIT_PAGE_SIZE]
    pkg += page

    manifest = {
        "kind": "nc_fast_flash",
        "manifest_version": 1,
        "package_len": len(pkg),
        "package_crc32": zlib.crc32(pkg),
        "kernel": {
            "off": kernel_off,
            "len": len(kernel),
            "crc32": zlib.crc32(kernel),
            "version": kernel_version,
        },
        "lz": {"lp": LZ_LP, "pb": LZ_PB, "piece": LZ_PIECE},
        "blocks": blocks,
        "boot": {
            "crc32": zlib.crc32(image[:ROM_FLASH_START_MIN]),
            "policy": boot_policy,
        },
        "commit": {
            "addr": COMMIT_PAGE,
            "off": commit_off,
            "len": COMMIT_PAGE_SIZE,
            "crc32": zlib.crc32(page),
        },
        "rewrite_all": bool(rewrite_all),
        "cal_id": cal_id,
        "generation": generation,
    }
    if len(manifest_bytes(manifest)) > MANIFEST_MAX:
        raise FlashError(
            "The Dynamic Flash package description is larger than the WiCAN accepts"
        )
    if len(pkg) > PACKAGE_MAX:
        raise FlashError("The Dynamic Flash package is larger than the WiCAN accepts")
    return bytes(pkg), manifest


def staged_name(cal_id: Optional[str], when: Optional[float] = None) -> str:
    """SD leaf name for a package: ``ff_<cal id>_<YYYYmmdd-HHMMSS>.ffp``.

    Only characters the firmware accepts (letters, digits, ``-``, ``_``, ``.``).
    """
    label = re.sub(r"[^A-Za-z0-9_-]+", "-", (cal_id or "rom").strip()).strip("-")
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(when))
    name = f"ff_{label or 'rom'}_{stamp}"
    return name[: _NAME_MAX - 4] + ".ffp"


def package_image(
    corrected: bytes,
    kernel: bytes,
    kernel_version: int,
    *,
    boot_policy: str = "refuse",
    rewrite_all: bool = False,
    name: Optional[str] = None,
) -> FastFlashPackage:
    """Package an image ALREADY prepared by
    :func:`src.ecu.flash_prep.prepare_flash_image` (its ``corrected_rom``), so
    the caller's archive and the package are the same bytes."""
    from .flash_manager import rom_cal_id_text
    from .rom_utils import rom_hardware_generation

    cal_id = rom_cal_id_text(corrected) or ""
    data, manifest = build(
        corrected,
        kernel,
        boot_policy=boot_policy,
        rewrite_all=rewrite_all,
        cal_id=cal_id,
        generation=rom_hardware_generation(corrected),
        kernel_version=kernel_version,
    )
    return FastFlashPackage(
        name=name or staged_name(cal_id),
        data=data,
        manifest=manifest,
        manifest_bytes=manifest_bytes(manifest),
    )


def package_rom(
    rom_data: bytes,
    kernel: bytes,
    kernel_version: int,
    *,
    boot_policy: str = "refuse",
    rewrite_all: bool = False,
    name: Optional[str] = None,
) -> FastFlashPackage:
    """Prepare ``rom_data`` the way every flash path does, then package it.

    ``rom_data`` is the raw ROM; it is checksum-corrected by
    :func:`src.ecu.flash_prep.prepare_flash_image` (the one prep pipeline).
    ``kernel`` / ``kernel_version`` come from
    :func:`src.ecu.fast_flash_kernel.load_kernel` (its bytes and pin version).
    """
    from .flash_prep import prepare_flash_image

    corrected, _sbl, _gen, _corrections = prepare_flash_image(
        rom_data, ROM_FLASH_START_MIN
    )
    return package_image(
        corrected,
        kernel,
        kernel_version,
        boot_policy=boot_policy,
        rewrite_all=rewrite_all,
        name=name,
    )
