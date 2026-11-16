"""Where the fast-flash kernel comes from, and when NC Flash may use it.

The kernel is a RAM program built in the nc-flash-re project. NC Flash looks
for it, in order:

1. ``get_fast_flash_kernel()`` in the private security module (release builds);
2. the file ``fast_flash/kernel.bin`` under the app root (bench and development
   only; the path is git-ignored). A release build (frozen) never reads it.

A kernel is used only if its size and SHA-256 are pinned in
:data:`FAST_FLASH_KERNELS` below. A kernel that is not pinned is ignored, unless
the environment variable ``NCFLASH_FAST_FLASH_UNPINNED=1`` is set (bench use:
testing a kernel CI has not built yet).

Even with a kernel, the fast flash runs only on the adapters, hardware
generations and chip classes proven on the bench; everything else uses the
normal flash. Headless: no PySide6.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from .exceptions import FlashError, ROMValidationError
from .fast_flash import KernelPolicy, validate_kernel
from .rom_utils import rom_hardware_generation

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KernelPin:
    """One accepted kernel build: its size, SHA-256 and the version it reports
    when it identifies itself. (A CRC-32 can be forged in four bytes; the
    kernel runs with the right to erase the boot block.)"""

    size: int
    sha256: str
    version: int


#: Kernels NC Flash accepts. To add one, paste the line that
#: ``python -m src.ecu.fast_flash_kernel kernel.bin 3`` prints.
FAST_FLASH_KERNELS: Tuple[KernelPin, ...] = (
    # v3 ("NF03*SBL_END"), nc-flash-re branch claude/clever-feynman-nutih0 (WIP),
    # CI run 43772197996; bench-proven 2026-11-15 (Tactrix, chip class 1).
    # (CRC-32 0xD0556A29.)
    KernelPin(
        size=0x1800,
        sha256="8f21aa273495cc3d2cb1fd5927c5928f1867b4cacb814ede25e841e8eb63a62c",
        version=3,
    ),
)

#: The only kernel version the app uses: v3 checks its own image in RAM and
#: refuses every range until block 15 was erased in the session. Older
#: versions stay supported by ``fast_flash.py`` for its tests only.
APP_KERNEL_VERSIONS = frozenset({3})

#: Set to 1 to accept a kernel that is not pinned (bench only).
UNPINNED_ENV = "NCFLASH_FAST_FLASH_UNPINNED"

#: Development / bench location of the kernel, relative to the app root.
KERNEL_FILE = Path("fast_flash") / "kernel.bin"

#: Adapters the fast flash runs on. The WiCAN flashes from its own firmware.
FAST_FLASH_ADAPTERS = frozenset({"j2534", "obdx"})

#: Hardware generations proven on the bench (2026-11-15: an NC2, LFDJEA).
ALLOWED_GENERATIONS = frozenset({"NC2"})

#: Chip classes (the kernel's "variant" byte) proven on the bench. Class 0
#: was only modelled; the 80 MHz classes were never run.
ALLOWED_CHIP_CLASSES = frozenset({1})


@dataclass(frozen=True)
class LoadedKernel:
    """A kernel image NC Flash may upload."""

    data: bytes
    #: Where it came from ("security module" or a file path), for the log.
    source: str
    #: Its pin, or None for an unpinned bench kernel.
    pin: Optional[KernelPin]


def find_pin(kernel: bytes) -> Optional[KernelPin]:
    """The pin matching ``kernel``'s size and SHA-256, or None."""
    digest = hashlib.sha256(kernel).hexdigest()
    for pin in FAST_FLASH_KERNELS:
        if pin.size == len(kernel) and pin.sha256.lower() == digest:
            return pin
    return None


def pin_line(kernel: bytes, version: int) -> str:
    """The line to add to :data:`FAST_FLASH_KERNELS` for ``kernel``."""
    return (
        f'KernelPin(size=0x{len(kernel):X}, sha256="'
        f'{hashlib.sha256(kernel).hexdigest()}", version={version}),'
    )


#: CRCs of unpinned kernels already warned about (one warning per kernel).
_warned_unpinned: set[int] = set()


def _unpinned_allowed() -> bool:
    return os.environ.get(UNPINNED_ENV, "").strip() == "1"


def _from_secure_module() -> Optional[bytes]:
    try:
        from ._secure import get_fast_flash_kernel
    except ImportError:
        from ._secure_stub import get_fast_flash_kernel
    try:
        return bytes(get_fast_flash_kernel())
    except Exception as e:  # noqa: BLE001 - stub or no kernel bundled
        logger.debug("No Dynamic Flash program in the security module: %s", e)
        return None


def _from_file() -> Tuple[Optional[bytes], str]:
    from src.utils.paths import get_app_root

    path = get_app_root() / KERNEL_FILE
    try:
        return path.read_bytes(), str(path)
    except OSError:
        return None, str(path)


def _accept(data: bytes, source: str) -> Optional[LoadedKernel]:
    try:
        validate_kernel(data)
    except FlashError as e:
        logger.warning("Dynamic Flash program from %s is not usable: %s", source, e)
        return None
    pin = find_pin(data)
    if pin is not None:
        return LoadedKernel(data, source, pin)
    if _unpinned_allowed():
        crc = zlib.crc32(data)
        if crc not in _warned_unpinned:  # the window checks on every refresh
            _warned_unpinned.add(crc)
            logger.warning(
                "Using a Dynamic Flash program that is not pinned (%s, CRC %08X) "
                "because %s=1. Bench use only.",
                source,
                crc,
                UNPINNED_ENV,
            )
        return LoadedKernel(data, source, None)
    logger.debug(
        "Dynamic Flash program from %s ignored: size 0x%X / CRC %08X is not pinned",
        source,
        len(data),
        zlib.crc32(data),
    )
    return None


def load_kernel() -> Optional[LoadedKernel]:
    """The kernel NC Flash may use, or None (then the normal flash is used).

    A release build (frozen) takes it from the security module only."""
    data = _from_secure_module()
    if data is not None:
        kernel = _accept(data, "security module")
        if kernel is not None:
            return kernel
    if getattr(sys, "frozen", False):
        return None
    data, path = _from_file()
    if data is not None:
        return _accept(data, path)
    return None


def unavailable_reason(
    adapter_kind: Optional[str], rom_data: Optional[bytes], kernel
) -> Optional[str]:
    """Why the fast flash cannot be used for this flash, or None if it can.

    Checked before anything is sent to the ECU. ``kernel`` is a
    :class:`LoadedKernel` or None.
    """
    if adapter_kind not in FAST_FLASH_ADAPTERS:
        return "the block-compare Dynamic Flash runs on the Tactrix (J2534) and OBDX Pro adapters only"
    if kernel is None:
        return "no Dynamic Flash program is installed"
    if not rom_data:
        return "no ROM to flash"
    try:
        generation = rom_hardware_generation(rom_data)
    except (ROMValidationError, ValueError) as e:
        return f"the ROM's generation could not be read ({e})"
    if generation not in ALLOWED_GENERATIONS:
        return f"the block-compare Dynamic Flash has not been tested on an {generation} yet"
    return None


def kernel_policy(kernel: LoadedKernel, generation: str) -> KernelPolicy:
    """What the app accepts from the running kernel before the first erase.

    The kernel must report the CRC of its own image (v3), so a kernel that
    arrived damaged is never given an erase."""
    versions = APP_KERNEL_VERSIONS
    if kernel.pin is not None:
        versions = versions & frozenset({kernel.pin.version})
    return KernelPolicy(
        chip_classes=ALLOWED_CHIP_CLASSES,
        versions=versions,
        require_self_check=True,
        generation=generation,
    )


def main(argv: list[str]) -> int:
    """``python -m src.ecu.fast_flash_kernel KERNEL.bin VERSION``: print the pin."""
    if len(argv) != 2:
        print("usage: python -m src.ecu.fast_flash_kernel KERNEL.bin VERSION")
        return 2
    data = Path(argv[0]).read_bytes()
    validate_kernel(data)
    print(pin_line(data, int(argv[1])))
    return 0


if __name__ == "__main__":  # pragma: no cover - developer helper
    sys.exit(main(sys.argv[1:]))
