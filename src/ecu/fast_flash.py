"""
Fast flash (EXPERIMENTAL, bench only): rewrite only the flash blocks that differ.

The normal flash uploads Mazda's RAM flash program, which erases and rewrites
everything from a start block to the end of the ROM. This module uploads a
different RAM program (the "fast-flash kernel", built in the nc-flash-re
project) that can report a CRC of any ROM range, erase one block and program
one range. The tool then:

    1. asks the ECU for the CRC of every application block and compares it
       with the image to flash (no dependence on the local archive file);
    2. erases the LAST block first: the ECU's boot loader then no longer starts
       the application, so a flash cut short at any point leaves the ECU
       waiting for a new flash instead of running a half-written image;
    3. erases and programs each other changed block;
    4. programs the last block except its final page;
    5. asks for the CRCs again and compares everything written so far;
    6. only then programs the final page (the "commit": it holds the start
       address and the pointer the boot loader tests) and verifies it. If
       that check fails, the last block is erased again at once.

An ECU whose entry pointer reads erased had a flash cut short: every
application block is then rewritten, whatever the CRCs say.

Nothing below 0x2000 (the boot loader) is ever erased or programmed; the kernel
refuses those addresses itself, and refuses blocks other than the last one
until the last one has been erased in the same session.

This module is NOT wired into the UI. It is driven by
``tools/fast_flash_bench.py`` and has only been exercised against a simulated
ECU. See ``docs/internal/FAST_FLASH.md``.
"""

import logging
import zlib
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from .constants import (
    BLOCK_SIZE,
    FLASH_COUNTER_OFFSET,
    ROM_FLASH_START_MIN,
    ROM_SIZE,
    SBL_UPLOAD_ADDR,
    SID_ECU_RESET,
    SID_REQUEST_DOWNLOAD,
    SID_TRANSFER_DATA,
    SID_TRANSFER_EXIT,
)
from .exceptions import FlashAbortedError, FlashError, NegativeResponseError

logger = logging.getLogger(__name__)

#: Start address of each SH7058 erase block, plus the end of the ROM.
BLOCK_STARTS: Tuple[int, ...] = (
    0x00000,
    0x01000,
    0x02000,
    0x03000,
    0x04000,
    0x05000,
    0x06000,
    0x07000,
    0x08000,
    0x20000,
    0x40000,
    0x60000,
    0x80000,
    0xA0000,
    0xC0000,
    0xE0000,
    0x100000,
)
FIRST_APP_BLOCK = 2  # blocks 0 and 1 hold the boot loader
LAST_BLOCK = 15
#: The page the kernel stamps itself after erasing the last block. It is never
#: programmed from the image and is left out of every comparison.
COUNTER_PAGE_SIZE = 128
#: The last page of the ROM. Programming it makes the application bootable
#: again; the kernel accepts it only as a range of its own.
COMMIT_PAGE = 0xFFF80
COMMIT_PAGE_SIZE = 128
ENTRY_POINTER = 0xFFFFC

SID_ROUTINE = 0x31
ROUTINE_IDENTIFY = 0x00
ROUTINE_CRC = 0x01
ROUTINE_ERASE = 0x02

#: The boot loader computes the kernel length as (announced size - this).
LOADER_SIZE_BASE = 0x000FE000
#: Largest kernel the boot loader accepts at SBL_UPLOAD_ADDR.
KERNEL_MAX_SIZE = 0x3000
KERNEL_MARKER = b"*SBL_END"
KERNEL_MAGIC = b"NF"
SUPPORTED_KERNEL_VERSIONS = (1,)

#: The manual allows 20 s for one block erase; the kernel is silent meanwhile.
TIMEOUT_ERASE_MS = 30_000
TIMEOUT_CRC_MS = 10_000
TIMEOUT_PROGRAM_MS = 10_000

ProgressFn = Callable[[str, float], None]
AbortFn = Callable[[], bool]


@dataclass
class KernelInfo:
    """What the kernel reports about itself."""

    version: int
    chip_class: int
    flash_ready: bool


@dataclass
class BlockStatus:
    """One application block compared between the ECU and an image."""

    block: int
    start: int
    size: int
    same: bool


@dataclass
class FastFlashResult:
    """Outcome of a compare or a flash."""

    kernel: KernelInfo
    before: List[BlockStatus]
    boot_block_same: bool = True
    interrupted_before: bool = False
    rewritten: List[int] = field(default_factory=list)
    bytes_sent: int = 0

    @property
    def changed(self) -> List[int]:
        return [b.block for b in self.before if not b.same]


def block_range(block: int) -> Tuple[int, int]:
    """Return ``(start, size)`` of an erase block."""
    if not 0 <= block <= LAST_BLOCK:
        raise ValueError(f"no such flash block: {block}")
    return BLOCK_STARTS[block], BLOCK_STARTS[block + 1] - BLOCK_STARTS[block]


def compare_ranges(block: int, with_commit: bool = True) -> List[Tuple[int, int]]:
    """Ranges of a block that are compared.

    The last block is compared without the counter page and, when
    ``with_commit`` is false, without the commit page (the state just before
    the commit step).
    """
    start, size = block_range(block)
    if block != LAST_BLOCK:
        return [(start, size)]
    end = start + size if with_commit else COMMIT_PAGE
    after = FLASH_COUNTER_OFFSET + COUNTER_PAGE_SIZE
    return [(start, FLASH_COUNTER_OFFSET - start), (after, end - after)]


def validate_kernel(kernel: bytes) -> None:
    """Check a kernel image against what the boot loader will accept.

    The boot loader takes 0x400-byte blocks only, starts the program only if
    the marker arrives in the LAST block, and refuses it if the marker shows up
    in an earlier one.
    """
    if not kernel or len(kernel) % BLOCK_SIZE or len(kernel) > KERNEL_MAX_SIZE:
        raise FlashError(
            f"Fast-flash kernel size 0x{len(kernel):X} is not a multiple of "
            f"0x{BLOCK_SIZE:X} up to 0x{KERNEL_MAX_SIZE:X}"
        )
    last = kernel[-BLOCK_SIZE:]
    if KERNEL_MARKER not in last or KERNEL_MARKER[1:] in kernel[:-BLOCK_SIZE]:
        raise FlashError(
            "Fast-flash kernel must carry its end marker in the last block only"
        )


def upload_kernel(uds, kernel: bytes, abort: Optional[AbortFn] = None) -> None:
    """Upload the kernel through the boot loader and wait until it has started.

    Call this on a session that is already in programming mode, unlocked, with
    the flash counter checked (the same state the normal flash reaches before
    its own upload). The boot loader answers the last block with "response
    pending" and jumps; the kernel then sends the positive answer itself, so
    a normal return from the last block means the kernel is running.
    """
    validate_kernel(kernel)
    uds.request_download(SBL_UPLOAD_ADDR, LOADER_SIZE_BASE + len(kernel))
    uds.transfer_data(kernel, block_size=BLOCK_SIZE, abort_check=abort)


def identify(uds) -> KernelInfo:
    """Ask the kernel who it is; refuse anything that is not a known fast-flash kernel."""
    r = uds.send_request(SID_ROUTINE, bytes([ROUTINE_IDENTIFY]))
    if len(r) != 6 or r[0] != ROUTINE_IDENTIFY or r[1:3] != KERNEL_MAGIC:
        raise FlashError(f"Unexpected answer to kernel identify: {r.hex()}")
    info = KernelInfo(version=r[3], chip_class=r[4], flash_ready=r[5] == 1)
    if info.version not in SUPPORTED_KERNEL_VERSIONS:
        raise FlashError(f"Unsupported fast-flash kernel version {info.version}")
    return info


def ecu_crc(uds, address: int, size: int) -> int:
    """CRC-32 (zlib) of ``size`` bytes of ECU ROM at ``address``."""
    if size <= 0 or address < 0 or address + size > ROM_SIZE:
        raise FlashError(f"CRC range out of the ROM: 0x{address:06X}+0x{size:X}")
    req = bytes([ROUTINE_CRC]) + address.to_bytes(3, "big") + size.to_bytes(3, "big")
    r = uds.send_request(SID_ROUTINE, req, timeout=TIMEOUT_CRC_MS)
    if len(r) != 5 or r[0] != ROUTINE_CRC:
        raise FlashError(f"Unexpected answer to kernel CRC: {r.hex()}")
    return int.from_bytes(r[1:5], "big")


def compare_blocks(uds, image: bytes, with_commit: bool = True) -> List[BlockStatus]:
    """Compare every application block of the ECU with ``image`` by CRC."""
    if len(image) != ROM_SIZE:
        raise FlashError(f"Image must be {ROM_SIZE} bytes, got {len(image)}")
    out = []
    for block in range(FIRST_APP_BLOCK, LAST_BLOCK + 1):
        start, size = block_range(block)
        same = all(
            ecu_crc(uds, a, n) == zlib.crc32(image[a : a + n])
            for a, n in compare_ranges(block, with_commit)
        )
        out.append(BlockStatus(block, start, size, same))
    return out


def erase_block(uds, block: int) -> None:
    """Erase one application block. The kernel refuses blocks 0 and 1."""
    if not FIRST_APP_BLOCK <= block <= LAST_BLOCK:
        raise FlashError(f"Refusing to erase block {block}: not an application block")
    r = uds.send_request(
        SID_ROUTINE,
        bytes([ROUTINE_ERASE, block]),
        timeout=TIMEOUT_ERASE_MS,
        pending_max=TIMEOUT_ERASE_MS,
    )
    if r != bytes([ROUTINE_ERASE, block]):
        raise FlashError(f"Unexpected answer to erase of block {block}: {r.hex()}")


def _is_blank(data: bytes) -> bool:
    return data.count(0xFF) == len(data)


def _send_range(uds, image: bytes, address: int, size: int, abort) -> int:
    """Program ``image[address : address + size]`` as one kernel range."""
    if address < ROM_FLASH_START_MIN or address + size > ROM_SIZE:
        raise FlashError(f"Refusing to program 0x{address:06X}+0x{size:X}")
    uds.send_request(
        SID_REQUEST_DOWNLOAD,
        address.to_bytes(4, "big") + size.to_bytes(4, "big"),
        timeout=TIMEOUT_PROGRAM_MS,
    )
    for off in range(0, size, BLOCK_SIZE):
        if abort and abort():
            raise FlashAbortedError("Fast flash aborted by user")
        n = min(BLOCK_SIZE, size - off)
        uds.send_request(
            SID_TRANSFER_DATA,
            image[address + off : address + off + n],
            timeout=TIMEOUT_PROGRAM_MS,
        )
    uds.send_request(SID_TRANSFER_EXIT, timeout=TIMEOUT_PROGRAM_MS)
    return size


def program_block(uds, image: bytes, block: int, abort: Optional[AbortFn]) -> int:
    """Program one freshly erased block from ``image``. Returns the bytes sent.

    The commit page is never sent here. Pieces of up to 0x400 bytes that are
    all 0xFF are not sent: the block was just erased, and the CRC comparison
    covers them.
    """
    if not FIRST_APP_BLOCK <= block <= LAST_BLOCK:
        raise FlashError(f"Refusing to program block {block}")
    start, size = block_range(block)
    end = min(start + size, COMMIT_PAGE)
    sent = 0
    a = start
    while a < end:
        piece = min(BLOCK_SIZE, end - a)
        if _is_blank(image[a : a + piece]):
            a += piece
            continue
        n = 0
        while a + n < end:
            piece = min(BLOCK_SIZE, end - a - n)
            if _is_blank(image[a + n : a + n + piece]):
                break
            n += piece
        sent += _send_range(uds, image, a, n, abort)
        a += n
    return sent


def commit(uds, image: bytes) -> None:
    """Program the last page and verify it; undo it if the check fails.

    Until this page is programmed the boot loader does not start the
    application. If it does not read back right, block 15 is erased again so
    the ECU stays in its boot loader.
    """
    page = image[COMMIT_PAGE : COMMIT_PAGE + COMMIT_PAGE_SIZE]
    try:
        _send_range(uds, image, COMMIT_PAGE, COMMIT_PAGE_SIZE, None)
        ok = ecu_crc(uds, COMMIT_PAGE, COMMIT_PAGE_SIZE) == zlib.crc32(page)
    except Exception:
        ok = False
    if not ok:
        try:
            erase_block(uds, LAST_BLOCK)
        except Exception as e:  # noqa: BLE001
            logger.error("Could not erase block 15 after a failed commit: %s", e)
        raise FlashError(
            "Fast flash: the final page did not verify. The ECU must be "
            "flashed again before use."
        )


def reset_ecu(uds) -> None:
    """Ask the kernel to end the session. It answers, then stops; the ECU's
    supervisor is expected to reset it (same as after a normal flash)."""
    try:
        uds.send_request(SID_ECU_RESET, bytes([0x01]))
    except Exception as e:  # noqa: BLE001 - the flash is already complete
        logger.warning("Reset request after fast flash failed (non-fatal): %s", e)


def _note(progress: Optional[ProgressFn], msg: str, pct: float) -> None:
    logger.info("Fast flash: %s", msg)
    if progress:
        progress(msg, pct)


def compare_only(
    uds, kernel: bytes, image: bytes, progress: Optional[ProgressFn] = None
) -> FastFlashResult:
    """Upload the kernel, identify it and compare the ECU with ``image``.

    Erases nothing and programs nothing. The caller ends the session with
    :func:`reset_ecu`.
    """
    _note(progress, "Uploading the fast-flash kernel...", 5.0)
    upload_kernel(uds, kernel)
    info = identify(uds)
    _note(
        progress,
        f"Kernel v{info.version} running (chip class {info.chip_class}, "
        f"flash {'ready' if info.flash_ready else 'NOT ready'})",
        15.0,
    )
    before = compare_blocks(uds, image)
    boot_same = ecu_crc(uds, 0, ROM_FLASH_START_MIN) == zlib.crc32(
        image[:ROM_FLASH_START_MIN]
    )
    if not boot_same:
        logger.warning(
            "Fast flash: the ROM file's boot block (0x0000-0x1FFF) differs from "
            "the ECU's. It is never written; the ECU keeps its own."
        )
    erased = zlib.crc32(b"\xff" * 4)
    interrupted = ecu_crc(uds, ENTRY_POINTER, 4) == erased
    return FastFlashResult(
        kernel=info,
        before=before,
        boot_block_same=boot_same,
        interrupted_before=interrupted,
    )


def fast_flash(
    uds,
    kernel: bytes,
    image: bytes,
    progress: Optional[ProgressFn] = None,
    abort: Optional[AbortFn] = None,
) -> FastFlashResult:
    """Rewrite the blocks of the ECU that differ from ``image``.

    ``image`` must be the checksum-corrected 1 MB ROM (use
    ``flash_prep.prepare_flash_image``). Raises :class:`FlashError` if the
    final comparison is not clean; the ECU then waits in its boot loader and
    must be flashed again.
    """
    if _is_blank(image[ENTRY_POINTER : ENTRY_POINTER + 4]):
        raise FlashError("This ROM has no entry pointer; it would never start")
    result = compare_only(uds, kernel, image, progress)
    changed = result.changed
    if result.interrupted_before:
        # An earlier flash was cut short: cells written just before the cut
        # may read right without being fully programmed. Rewrite everything.
        _note(progress, "Earlier flash was cut short: rewriting every block", 18.0)
        changed = list(range(FIRST_APP_BLOCK, LAST_BLOCK + 1))
    if not changed:
        _note(progress, "ECU already holds this image; nothing to do", 100.0)
        return result
    if not result.kernel.flash_ready:
        raise FlashError("The kernel could not prepare the flash routines; no change")
    if abort and abort():
        raise FlashAbortedError("Fast flash aborted before the first erase")

    others = [b for b in changed if b != LAST_BLOCK]
    steps = len(others) + 2
    _note(progress, f"Blocks to rewrite: {others + [LAST_BLOCK]}", 20.0)
    # From here to the end an abort would leave the ECU without an application,
    # so aborts are no longer honoured: finishing is the safe direction.
    try:
        erase_block(uds, LAST_BLOCK)
        for i, block in enumerate(others, start=1):
            _note(progress, f"Rewriting block {block}...", 20.0 + 70.0 * i / steps)
            erase_block(uds, block)
            result.bytes_sent += program_block(uds, image, block, None)
            result.rewritten.append(block)
        _note(
            progress,
            f"Rewriting block {LAST_BLOCK}...",
            20.0 + 70.0 * (steps - 1) / steps,
        )
        result.bytes_sent += program_block(uds, image, LAST_BLOCK, None)
        result.rewritten.append(LAST_BLOCK)
    except NegativeResponseError as e:
        raise FlashError(
            f"Fast flash stopped: the ECU refused a step ({e}). The ECU is "
            "waiting in its boot loader; flash it again."
        ) from e

    # Verify BEFORE the commit page: the ECU still waits in its boot loader,
    # so a bad block cannot be started.
    _note(progress, "Verifying every block...", 92.0)
    after = compare_blocks(uds, image, with_commit=False)
    bad = [b.block for b in after if not b.same]
    if bad:
        raise FlashError(
            f"Fast flash verification FAILED for blocks {bad}. The ECU is "
            "waiting in its boot loader and must be flashed again."
        )
    _note(progress, "Committing...", 97.0)
    commit(uds, image)
    _note(progress, "Verified", 100.0)
    return result
