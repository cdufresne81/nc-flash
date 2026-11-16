"""
Fast flash (EXPERIMENTAL): rewrite only the flash blocks that differ.

The normal flash uploads Mazda's RAM flash program, which erases and rewrites
everything from a start block to the end of the ROM. This module uploads a
different RAM program (the "fast-flash kernel", built in the nc-flash-re
project) that can report a CRC of any ROM range, erase one block and program
one range. The tool then:

    1. checks the kernel image it sent by its CRC in ECU RAM (kernel v3);
    2. asks the ECU for the CRC of every application block and compares it
       with the image to flash (no dependence on the local archive file);
    3. erases the LAST block first: the ECU's boot loader then no longer starts
       the application, so a flash cut short at any point leaves the ECU
       waiting for a new flash instead of running a half-written image;
    4. erases and programs each other changed block;
    5. programs the last block except its final page;
    6. asks for the CRCs again and compares everything written so far;
    7. only then programs the final page (the "commit": it holds the start
       address and the pointer the boot loader tests) and verifies it. If
       that check fails, the last block is erased again at once.

An ECU whose entry pointer reads erased had a flash cut short: every
application block is then rewritten, whatever the CRCs say.

Nothing below 0x2000 (the boot loader) is ever erased or programmed; the kernel
refuses those addresses itself, and refuses every range (v3) and every block
other than the last one until the last one has been erased in the same session.

Driven by :meth:`FlashManager.fast_flash` (the app, opt-in) and by
``tools/fast_flash_bench.py``. See ``docs/internal/FAST_FLASH.md``.
"""

import contextvars
import logging
import lzma
import time
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, FrozenSet, Iterator, List, Optional, Tuple

from .constants import (
    BLOCK_SIZE,
    BOOT_SIGNATURE_LEN,
    BOOT_SIGNATURE_NC1,
    BOOT_SIGNATURE_NC2,
    FLASH_COUNTER_OFFSET,
    ROM_FLASH_START_MIN,
    ROM_SIZE,
    SBL_UPLOAD_ADDR,
    SID_ECU_RESET,
    SID_REQUEST_DOWNLOAD,
    SID_TRANSFER_DATA,
    SID_TRANSFER_EXIT,
)
from .exceptions import (
    ECUError,
    ECUGenerationMismatchError,
    FastFlashUnavailable,
    FlashAbortedError,
    FlashError,
    J2534Error,
    NegativeResponseError,
)

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
SUPPORTED_KERNEL_VERSIONS = (1, 2, 3)
#: First kernel version that reports the CRC of its own image in RAM.
KERNEL_CRC_VERSION = 3
ROUTINE_KERNEL_CRC = 0x03
NRC_SEQUENCE = 0x24
#: First kernel version that decodes LZMA-compressed ranges.
LZ_KERNEL_VERSION = 2
UPLOAD_RESTARTS = 2  # kernel upload: restarts after a refused resend

#: The manual allows 20 s for one block erase; the kernel is silent meanwhile.
TIMEOUT_ERASE_MS = 30_000
#: Total wait for one CRC answer (a block's CRC takes well under a second; a
#: lost answer is retried after this instead of the 60 s default budget).
TIMEOUT_CRC_MS = 10_000
TIMEOUT_PROGRAM_MS = 10_000
#: Identify: a short request the kernel answers at once.
TIMEOUT_IDENTIFY_MS = 2_000
#: ECUReset: the kernel answers 51 at once, then halts.
TIMEOUT_RESET_MS = 3_000
#: Wait for one write answer: 8 pages at the manual's 200 ms maximum, plus margin.
TIMEOUT_WRITE_MS = 4_000
NRC_PROGRAMMING_FAILURE = 0x72
#: Bytes per write request (a multiple of 128, at most 0x400). A lossy link
#: (WiCAN over WiFi) uses less: fewer CAN frames per request to lose.
DEFAULT_CHUNK = BLOCK_SIZE
#: How often one write request is tried again after the ECU gave no answer.
WRITE_RETRIES = 4
#: Pause before a retry: the kernel drops a half-received request after ~1 s.
LINK_RETRY_WAIT_S = 1.5
#: How long a silent link is waited out before giving up, and the pause between
#: tries meanwhile, when the caller asks for it (``wait_out_outages``). Seen on
#: the bench: WiCAN silent ~1-2 min, then fine again. A wired adapter (J2534,
#: OBDX) does not wait: a silent link there is a real fault.
LINK_OUTAGE_S = 300.0
LINK_OUTAGE_WAIT_S = 10.0
#: Kernel v2: LZMA-compressed ranges. Settings the kernel decodes (lc is 0).
LZ_LP = 2
LZ_PB = 2
#: Stream bytes per piece: 0x36, the sequence number and this fill 1024 bytes.
LZ_PIECE = 1022
#: Wait for one piece's answer. A piece can expand to many pages; the kernel
#: sends "response pending" every ~4 s, so this must be longer than that.
TIMEOUT_LZ_PIECE_MS = 6_000

ProgressFn = Callable[[str, float], None]
AbortFn = Callable[[], bool]
#: Told when the flash moves to the next stage: "upload", "compare",
#: "program", "verify", "commit".
StageFn = Callable[[str], None]

#: Outage wait (seconds) for the requests of the running fast flash; 0 = none.
#: A context variable, so a flash on one thread never changes another's.
_outage_wait_s: contextvars.ContextVar[float] = contextvars.ContextVar(
    "fast_flash_outage_wait_s", default=0.0
)


@contextmanager
def _outage_policy(wait_out_outages: bool) -> Iterator[None]:
    token = _outage_wait_s.set(LINK_OUTAGE_S if wait_out_outages else 0.0)
    try:
        yield
    finally:
        _outage_wait_s.reset(token)


@dataclass
class KernelInfo:
    """What the kernel reports about itself."""

    version: int
    chip_class: int
    flash_ready: bool


@dataclass(frozen=True)
class KernelPolicy:
    """What a caller accepts before the first erase. Anything else ends the
    fast flash with :class:`FastFlashUnavailable` and nothing changed.

    ``None`` fields are not checked (the bench tool's compare-only mode).
    """

    #: Chip classes (identify's "variant" byte) the kernel is proven on.
    chip_classes: Optional[FrozenSet[int]] = None
    #: Kernel versions accepted (the pinned kernel's own version).
    versions: Optional[FrozenSet[int]] = None
    #: Refuse a kernel too old to report the CRC of its own image.
    require_self_check: bool = False
    #: The ECU's hardware generation, checked by the CRC of its boot code.
    generation: Optional[str] = None

    def refusal(self, info: KernelInfo, self_checked: bool) -> Optional[str]:
        """Why this kernel may not flash, or None."""
        if self.versions is not None and info.version not in self.versions:
            return f"kernel version {info.version} is not the one NC Flash expects"
        if self.require_self_check and not self_checked:
            return f"kernel version {info.version} cannot check its own image"
        if self.chip_classes is not None and info.chip_class not in self.chip_classes:
            return (
                f"this ECU's processor (chip class {info.chip_class}) has not been "
                "tested with the block-compare Dynamic Flash yet"
            )
        return None


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
    #: The kernel answered the final ECUReset (``51``). False: the ECU may not
    #: have restarted; the user must cycle the ignition before starting.
    restart_confirmed: bool = True

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
            f"Dynamic Flash program size 0x{len(kernel):X} is not a multiple of "
            f"0x{BLOCK_SIZE:X} up to 0x{KERNEL_MAX_SIZE:X}"
        )
    last = kernel[-BLOCK_SIZE:]
    if KERNEL_MARKER not in last or KERNEL_MARKER[1:] in kernel[:-BLOCK_SIZE]:
        raise FlashError(
            "Dynamic Flash program must carry its end marker in the last block only"
        )


def _kreq(uds, sid: int, data: bytes = b"", **kw) -> bytes:
    """A request to the running kernel. A negative response that names another
    service belongs to another tester on the bus (a logger): it is ignored."""
    return uds.send_request(sid, data, match_nrc_sid=True, **kw)


def _flush(uds) -> None:
    """Drop late answers so one cannot be read as the answer to the next request."""
    try:
        uds.flush()
    except Exception:  # noqa: BLE001 - best effort
        pass


#: Per-read wait while dropping late answers (ms). Not 0: see _drain.
DRAIN_READ_MS = 5
#: Most reads one drain makes (it never blocks longer than this many waits).
DRAIN_MAX_READS = 16


def _drain(uds) -> None:
    """Read and drop any answer already waiting, so a late answer to an
    earlier request is never taken for the next CRC (its answer carries no
    address). Local to this module: the J2534 transport's own flush stays a
    no-op for every other caller."""
    transport = getattr(uds, "_transport", None)
    receive = getattr(transport, "receive_message", None)
    if receive is None:
        return
    for _ in range(DRAIN_MAX_READS):
        started = time.perf_counter()
        try:
            # Never 0: some J2534 DLLs (the OpenPort clone driver) treat a
            # zero timeout as "wait forever" (bench, 2026-11-15).
            late = receive(DRAIN_READ_MS)
        except Exception:  # noqa: BLE001 - best effort; the request reports faults
            return
        if late:
            logger.debug(
                "Dropped a late answer before a CRC request: %s", bytes(late).hex()
            )
            continue
        # None that came back at once is a frame without a UDS payload (a J2534
        # TX / start-of-message indication): something may queue behind it, so
        # keep reading. None after the full wait: nothing is waiting.
        if time.perf_counter() - started >= DRAIN_READ_MS / 1000.0 * 0.8:
            return


def _adapter_fault_types() -> tuple:
    types: list = [J2534Error]
    try:  # the OBDX transport needs pyserial; without it there is no OBDX fault
        from .obdx import ObdxError

        types.append(ObdxError)
    except ImportError:  # pragma: no cover - pyserial is a dependency
        pass
    return tuple(types)


def is_adapter_fault(e: BaseException) -> bool:
    """The adapter itself failed (a dead J2534 bridge, a closed port, an OBDX
    that refused or did not acknowledge a write), as opposed to an ECU that
    did not answer. Resending then is not safe (the OBDX does not say whether
    the ECU saw part of the message) and cannot help: such faults are fatal."""
    faults = _adapter_fault_types()
    seen: Optional[BaseException] = e
    hops = 0
    while seen is not None and hops < 8:
        if isinstance(seen, faults):
            return True
        seen = seen.__cause__ or seen.__context__
        hops += 1
    return False


def _is_link_error(e: BaseException) -> bool:
    """No answer or a lost frame (as opposed to a refusal from the ECU or an
    adapter fault): the request may be sent again."""
    return (
        isinstance(e, ECUError)
        and not isinstance(e, (NegativeResponseError, FlashError))
        and not is_adapter_fault(e)
    )


def _retry(what: str, fn: Callable[[], object], uds, tries: int = WRITE_RETRIES):
    """Run an idempotent request, trying again after a link error.

    Late answers are flushed before every retry. After ``tries`` quick retries
    it gives up, unless the caller asked to wait out link outages (WiCAN: its
    link can go silent for a minute or two and come back by itself while the
    kernel keeps waiting, bench 2026-11-15); it then keeps trying, more
    slowly, for up to :data:`LINK_OUTAGE_S`. ``tries <= 1`` (a probe) never
    waits out an outage. Adapter faults are never retried.
    """
    deadline = None
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - filtered below
            if not _is_link_error(e):
                raise
            attempt += 1
            _flush(uds)
            if attempt <= tries:
                logger.warning("%s: no answer (%s), trying again", what, e)
                time.sleep(LINK_RETRY_WAIT_S)
                continue
            budget = _outage_wait_s.get()
            if tries <= 1 or budget <= 0:
                raise
            now = time.monotonic()
            if deadline is None:
                deadline = now + budget
            if now >= deadline:
                raise
            logger.warning(
                "%s: link silent, waiting for it to come back (%.0f s left)",
                what,
                deadline - now,
            )
            time.sleep(LINK_OUTAGE_WAIT_S)


def upload_kernel(uds, kernel: bytes, abort: Optional[AbortFn] = None) -> None:
    """Upload the kernel through the boot loader and wait until it has started.

    Call this on a session that is already in programming mode, unlocked, with
    the flash counter checked (the same state the normal flash reaches before
    its own upload). The boot loader answers the last block with "response
    pending" and jumps; the kernel then sends the positive answer itself, so
    a normal return from the last block means the kernel is running.
    """
    validate_kernel(kernel)
    for restart in range(UPLOAD_RESTARTS + 1):
        try:
            uds.request_download(SBL_UPLOAD_ADDR, LOADER_SIZE_BASE + len(kernel))
        except NegativeResponseError:
            if restart == 0:
                raise
            break  # the boot loader will not start over in this session
        if _upload_blocks(uds, kernel, abort):
            return
        # A block's answer was lost and the boot loader refused the resend: it
        # had taken the block. If that was the last one the kernel is running.
        # (Only the boot loader or this kernel can be listening here.)
        if running_kernel(uds) is not None:
            return
        logger.warning("Kernel upload out of step, starting it again (%d)", restart + 1)
    raise FlashError(
        "The kernel upload lost its place and could not be restarted. Nothing "
        "was erased or written; cycle the ignition and try again."
    )


def _upload_blocks(uds, kernel: bytes, abort: Optional[AbortFn]) -> bool:
    """Send the kernel blocks. False = a resent block was refused (out of step).

    Nothing is erased before the kernel runs. A block without an answer is
    sent again; the boot loader ignores a block it did not receive completely.
    If it HAD received it (only the answer was lost) it refuses the resend,
    and the upload must start over from RequestDownload.
    """
    for off in range(0, len(kernel), BLOCK_SIZE):
        if abort and abort():
            raise FlashAbortedError("Kernel upload aborted by user")
        resent = False
        for attempt in range(WRITE_RETRIES + 1):
            try:
                uds.send_request(
                    SID_TRANSFER_DATA,
                    kernel[off : off + BLOCK_SIZE],
                    timeout=TIMEOUT_PROGRAM_MS,
                )
                break
            except NegativeResponseError:
                if resent:
                    return False
                raise
            except Exception as e:  # noqa: BLE001 - filtered below
                if not _is_link_error(e) or attempt == WRITE_RETRIES:
                    raise
                logger.warning("Kernel block 0x%X: no answer, sending again", off)
                resent = True
                time.sleep(LINK_RETRY_WAIT_S)
                _flush(uds)
    return True


def parse_identify(r: bytes) -> Optional[KernelInfo]:
    """The kernel's identity from an identify answer, or None if the answer is
    not a fast-flash kernel's. The version is NOT checked here."""
    if len(r) != 6 or r[0] != ROUTINE_IDENTIFY or r[1:3] != KERNEL_MAGIC:
        return None
    return KernelInfo(version=r[3], chip_class=r[4], flash_ready=r[5] == 1)


def probe_kernel(uds) -> Optional[KernelInfo]:
    """Send ONE identify and return the kernel's identity, or None.

    Never sent to an ECU that may be running its application or the Mazda
    loader: callers first see the kernel's refusal pattern (see
    ``recovery.probe_ecu_mode``). No retry, no outage wait.
    """
    try:
        r = _kreq(
            uds,
            SID_ROUTINE,
            bytes([ROUTINE_IDENTIFY]),
            timeout=TIMEOUT_IDENTIFY_MS,
            pending_max=TIMEOUT_IDENTIFY_MS,
        )
    except Exception as e:  # noqa: BLE001 - anything else means "not our kernel"
        logger.debug("Kernel identify: no kernel (%s)", e)
        return None
    return parse_identify(r)


def running_kernel(uds) -> Optional[KernelInfo]:
    """The kernel's identity if a fast-flash kernel is already running, else None.

    Sends identify unconditionally: only call it where nothing but the boot
    loader or the kernel can be listening (the upload), or after the kernel's
    refusal pattern was seen.
    """
    try:
        return identify(uds, tries=1)
    except Exception:  # noqa: BLE001 - anything else means "not our kernel"
        return None


def identify(uds, tries: int = WRITE_RETRIES) -> KernelInfo:
    """Ask the kernel who it is; refuse anything that is not a known fast-flash kernel."""
    r = _retry(
        "identify",
        lambda: _kreq(
            uds,
            SID_ROUTINE,
            bytes([ROUTINE_IDENTIFY]),
            timeout=TIMEOUT_IDENTIFY_MS,
            pending_max=TIMEOUT_IDENTIFY_MS,
        ),
        uds,
        tries,
    )
    info = parse_identify(r)
    if info is None:
        raise FlashError(f"Unexpected answer to kernel identify: {r.hex()}")
    if info.version not in SUPPORTED_KERNEL_VERSIONS:
        raise FlashError(f"Unsupported Dynamic Flash program version {info.version}")
    return info


def verify_kernel_image(uds, kernel: bytes, info: KernelInfo) -> bool:
    """Compare the kernel as it sits in ECU RAM with the file that was sent.

    Every guard that protects the boot block lives in that RAM image, so a
    kernel that arrived damaged must never be given an erase. Returns False if
    the kernel is too old to report its CRC (nothing was checked); raises
    :class:`FlashError` on a mismatch.
    """
    if info.version < KERNEL_CRC_VERSION:
        return False
    req = bytes([ROUTINE_KERNEL_CRC]) + len(kernel).to_bytes(2, "big")
    _drain(uds)
    r = _retry(
        "kernel CRC",
        lambda: _kreq(
            uds, SID_ROUTINE, req, timeout=TIMEOUT_CRC_MS, pending_max=TIMEOUT_CRC_MS
        ),
        uds,
    )
    if len(r) != 5 or r[0] != ROUTINE_KERNEL_CRC:
        raise FlashError(f"Unexpected answer to kernel self-check: {r.hex()}")
    if int.from_bytes(r[1:5], "big") != zlib.crc32(kernel):
        raise FlashError(
            "The Dynamic Flash program in the ECU does not match the file that was "
            "sent. Nothing was erased or written; cycle the ignition and try again."
        )
    return True


def _close_range(uds) -> None:
    """TransferExit. If its answer is lost and the resend is refused as out of
    sequence, the first one had closed the range."""
    resent = False
    for attempt in range(WRITE_RETRIES + 1):
        try:
            _kreq(
                uds,
                SID_TRANSFER_EXIT,
                timeout=TIMEOUT_PROGRAM_MS,
                pending_max=TIMEOUT_PROGRAM_MS,
            )
            return
        except NegativeResponseError as e:
            if resent and e.nrc == NRC_SEQUENCE:
                return
            raise
        except Exception as e:  # noqa: BLE001 - filtered below
            if not _is_link_error(e) or attempt == WRITE_RETRIES:
                raise
            logger.warning("TransferExit: no answer (%s), sending again", e)
            resent = True
            time.sleep(LINK_RETRY_WAIT_S)
            _flush(uds)


def ecu_crc(uds, address: int, size: int) -> int:
    """CRC-32 (zlib) of ``size`` bytes of ECU ROM at ``address``."""
    if size <= 0 or address < 0 or address + size > ROM_SIZE:
        raise FlashError(f"CRC range out of the ROM: 0x{address:06X}+0x{size:X}")
    req = bytes([ROUTINE_CRC]) + address.to_bytes(3, "big") + size.to_bytes(3, "big")
    _drain(uds)
    r = _retry(
        f"CRC 0x{address:06X}",
        lambda: _kreq(
            uds, SID_ROUTINE, req, timeout=TIMEOUT_CRC_MS, pending_max=TIMEOUT_CRC_MS
        ),
        uds,
    )
    if len(r) != 5 or r[0] != ROUTINE_CRC:
        raise FlashError(f"Unexpected answer to kernel CRC: {r.hex()}")
    return int.from_bytes(r[1:5], "big")


def ecu_boot_generation(uds) -> Optional[str]:
    """The ECU's hardware generation ("NC1"/"NC2") from the CRC of its boot
    code signature (the same bytes the normal flash reads), or None if it is
    neither. Works while the kernel runs, when memory reads are refused."""
    crc = ecu_crc(uds, 0, BOOT_SIGNATURE_LEN)
    for generation, signature in (
        ("NC1", BOOT_SIGNATURE_NC1),
        ("NC2", BOOT_SIGNATURE_NC2),
    ):
        if crc == zlib.crc32(signature):
            return generation
    return None


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
    # Erasing again is harmless, so a request without an answer is repeated.
    r = _retry(
        f"erase block {block}",
        lambda: _kreq(
            uds,
            SID_ROUTINE,
            bytes([ROUTINE_ERASE, block]),
            timeout=TIMEOUT_ERASE_MS,
            pending_max=TIMEOUT_ERASE_MS,
        ),
        uds,
    )
    if r != bytes([ROUTINE_ERASE, block]):
        raise FlashError(f"Unexpected answer to erase of block {block}: {r.hex()}")


def _is_blank(data: bytes) -> bool:
    return data.count(0xFF) == len(data)


def _open_range(uds, address: int, size: int) -> None:
    _retry(
        f"open range 0x{address:06X}",
        lambda: _kreq(
            uds,
            SID_REQUEST_DOWNLOAD,
            address.to_bytes(4, "big") + size.to_bytes(4, "big"),
            timeout=TIMEOUT_PROGRAM_MS,
            pending_max=TIMEOUT_PROGRAM_MS,
        ),
        uds,
    )


def _send_range(
    uds, image: bytes, address: int, size: int, abort, chunk: int = DEFAULT_CHUNK
) -> int:
    """Program ``image[address : address + size]`` as one kernel range.

    A write request that gets no answer is recovered without ever writing a
    chunk at the wrong address: the range is re-opened at that chunk (the
    kernel's write pointer is set by the range, never by the data) and the
    chunk is sent again. If the kernel had in fact programmed it, it refuses
    the second write (page not blank); the chunk is then checked by CRC and
    the range re-opened after it.
    """
    if address < ROM_FLASH_START_MIN or address + size > ROM_SIZE:
        raise FlashError(f"Refusing to program 0x{address:06X}+0x{size:X}")
    if chunk % 128 or not 128 <= chunk <= BLOCK_SIZE:
        raise FlashError(f"Bad write chunk size {chunk}")
    _open_range(uds, address, size)
    off = 0
    is_open = True
    while off < size:
        if abort and abort():
            raise FlashAbortedError("Dynamic Flash aborted by user")
        a = address + off
        n = min(chunk, size - off)
        data = image[a : a + n]
        retried = False
        for attempt in range(WRITE_RETRIES + 1):
            try:
                _kreq(
                    uds,
                    SID_TRANSFER_DATA,
                    data,
                    timeout=TIMEOUT_WRITE_MS,
                    pending_max=TIMEOUT_WRITE_MS,
                )
                break
            except NegativeResponseError as e:
                if not retried or e.nrc != NRC_PROGRAMMING_FAILURE:
                    raise
                if ecu_crc(uds, a, n) != zlib.crc32(data):
                    raise
                logger.warning("Write at 0x%06X had landed; continuing", a)
                if off + n < size:
                    _open_range(uds, a + n, size - off - n)
                else:
                    is_open = False  # the kernel closed the range on the refusal
                break
            except Exception as e:  # noqa: BLE001 - filtered below
                if not _is_link_error(e) or attempt == WRITE_RETRIES:
                    raise
                logger.warning("Write at 0x%06X: no answer (%s), recovering", a, e)
                time.sleep(LINK_RETRY_WAIT_S)
                _flush(uds)
                _open_range(uds, a, size - off)
                retried = True
        off += n
    if is_open:
        _close_range(uds)
    return size


def program_block(
    uds,
    image: bytes,
    block: int,
    abort: Optional[AbortFn],
    chunk: int = DEFAULT_CHUNK,
) -> int:
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
        sent += _send_range(uds, image, a, n, abort, chunk)
        a += n
    return sent


def lzma_stream(data: bytes) -> bytes:
    """Raw LZMA1 stream in the form kernel v2 decodes (lc 0, lp 2, pb 2, no header)."""
    filters = [
        {
            "id": lzma.FILTER_LZMA1,
            "preset": 9 | lzma.PRESET_EXTREME,
            "lc": 0,
            "lp": LZ_LP,
            "pb": LZ_PB,
            "dict_size": 1 << 20,
        }
    ]
    return lzma.compress(data, format=lzma.FORMAT_RAW, filters=filters)


def _lz_ranges(block: int) -> List[Tuple[int, int]]:
    """Ranges sent compressed: the whole block; block 15 around the counter
    page (the kernel keeps its own counter there) and without the commit page."""
    start, size = block_range(block)
    if block != LAST_BLOCK:
        return [(start, size)]
    after = FLASH_COUNTER_OFFSET + COUNTER_PAGE_SIZE
    return [(start, FLASH_COUNTER_OFFSET - start), (after, COMMIT_PAGE - after)]


def _send_lz_range(uds, image: bytes, address: int, size: int) -> int:
    """Program one range from an LZMA stream (kernel v2). Returns the bytes sent.

    Each piece carries a sequence number. A piece whose answer is lost is sent
    again with the same number: the kernel answers it again without using it
    twice, or uses it if it never arrived. A refusal raises; the caller erases
    the block again and resends it whole.
    """
    stream = lzma_stream(image[address : address + size])
    _retry(
        f"open compressed range 0x{address:06X}",
        lambda: _kreq(
            uds,
            SID_REQUEST_DOWNLOAD,
            bytes([0x10, LZ_LP, LZ_PB])
            + address.to_bytes(4, "big")
            + size.to_bytes(4, "big"),
            timeout=TIMEOUT_PROGRAM_MS,
            pending_max=TIMEOUT_PROGRAM_MS,
        ),
        uds,
    )
    seq = 0
    for off in range(0, len(stream), LZ_PIECE):
        seq = (seq + 1) & 0xFF
        req = bytes([seq]) + stream[off : off + LZ_PIECE]
        r = _retry(
            f"compressed piece {seq} at 0x{address:06X}",
            lambda req=req: _kreq(
                uds,
                SID_TRANSFER_DATA,
                req,
                timeout=TIMEOUT_LZ_PIECE_MS,
                pending_max=TIMEOUT_ERASE_MS,
            ),
            uds,
        )
        if r != bytes([seq]):
            raise FlashError(f"Unexpected answer to compressed piece {seq}: {r.hex()}")
    _close_range(uds)
    return len(stream)


def program_block_lz(uds, image: bytes, block: int) -> int:
    """Program one freshly erased block from compressed ranges (kernel v2).

    If the kernel refuses a piece (corrupted data, a piece out of order), the
    block is erased again and sent again once.
    """
    if not FIRST_APP_BLOCK <= block <= LAST_BLOCK:
        raise FlashError(f"Refusing to program block {block}")
    for attempt in range(2):
        try:
            return sum(_send_lz_range(uds, image, a, n) for a, n in _lz_ranges(block))
        except NegativeResponseError as e:
            if attempt:
                raise
            logger.warning(
                "Block %d: compressed transfer refused (%s); erasing it again", block, e
            )
            erase_block(uds, block)
    return 0  # not reached


def commit(uds, image: bytes) -> None:
    """Program the last page and verify it; undo it if the check fails.

    Until this page is programmed the boot loader does not start the
    application. If it does not read back right, block 15 is erased again so
    the ECU stays in its boot loader.
    """
    page = image[COMMIT_PAGE : COMMIT_PAGE + COMMIT_PAGE_SIZE]
    refused = False
    try:
        _send_range(uds, image, COMMIT_PAGE, COMMIT_PAGE_SIZE, None)
    except NegativeResponseError as e:
        refused = True
        logger.error("The ECU refused the final page: %s", e)
    except Exception as e:  # noqa: BLE001 - filtered below
        if not _is_link_error(e):
            raise
        # Only an answer was lost: the page may well be programmed. The CRC
        # below decides; a good flash is never erased over a lost answer.
        logger.warning("Final page: no answer (%s); checking it by CRC", e)
    try:
        ok = ecu_crc(uds, COMMIT_PAGE, COMMIT_PAGE_SIZE) == zlib.crc32(page)
    except Exception as e:  # noqa: BLE001 - filtered below
        if not _is_link_error(e):
            raise
        if refused:
            raise FlashError(
                "Dynamic Flash: the ECU refused the final step and the link was "
                "lost while checking it, so NC Flash cannot tell whether the ECU "
                "is complete. Every other block was verified. Reconnect and "
                "flash again before starting the car."
            ) from e
        raise FlashError(
            "Dynamic Flash: the link was lost while checking the final page. "
            "Every other block was verified. Reconnect and flash again to "
            "confirm; the ECU may already be complete."
        ) from e
    if not ok:
        try:
            erase_block(uds, LAST_BLOCK)
        except Exception as e:  # noqa: BLE001
            logger.error("Could not erase block 15 after a failed commit: %s", e)
        raise FlashError(
            "Dynamic Flash: the final page did not verify. The ECU must be "
            "flashed again before use."
        )


def reset_kernel(uds) -> None:
    """Ask the kernel to end the session (``11 01``); raises if it does not
    answer. It answers, then stops; the ECU restarts by itself (bench
    2026-11-15: its application within a few seconds, or the boot loader's
    recovery server when the application is incomplete)."""
    _kreq(
        uds,
        SID_ECU_RESET,
        bytes([0x01]),
        timeout=TIMEOUT_RESET_MS,
        pending_max=TIMEOUT_RESET_MS,
    )


def reset_ecu(uds) -> bool:
    """:func:`reset_kernel`, never raising. Returns True once the kernel
    answered. If it did not (the request or the answer was lost), the request
    is sent once more: a kernel that already stopped stays silent, one that
    never got it stops now. False: the ECU may still be running the kernel and
    will not restart until the ignition is cycled."""
    for attempt in range(2):
        try:
            reset_kernel(uds)
            return True
        except Exception as e:  # noqa: BLE001 - the flash is already decided
            logger.warning(
                "Dynamic Flash: no answer to the ECU restart request (%s)%s",
                e,
                "; sending it again" if attempt == 0 else "",
            )
            if is_adapter_fault(e) or isinstance(e, NegativeResponseError):
                return False
            _flush(uds)
    return False


def _note(progress: Optional[ProgressFn], msg: str, pct: float) -> None:
    logger.info("Dynamic Flash: %s", msg)
    if progress:
        progress(msg, pct)


def _stage(on_stage: Optional[StageFn], name: str) -> None:
    if on_stage:
        on_stage(name)


def _refuse(reason: str, resume: bool = False) -> None:
    """Report that the block compare cannot run here. The caller stops the
    kernel. In a resumed session an earlier one may already have erased
    blocks, so "nothing was changed" is not said then."""
    logger.warning("Dynamic Flash block compare not available: %s", reason)
    if resume:
        raise FastFlashUnavailable(
            f"Dynamic Flash can't finish the interrupted flash on this ECU: "
            f"{reason}. NC Flash told the ECU to restart; if it then shows "
            "recovery mode, do a full flash."
        )
    raise FastFlashUnavailable(
        f"Dynamic Flash can't compare blocks on this ECU: {reason}. Nothing was changed."
    )


def compare_only(
    uds,
    kernel: bytes,
    image: bytes,
    progress: Optional[ProgressFn] = None,
    resume: bool = False,
    *,
    policy: Optional[KernelPolicy] = None,
    on_stage: Optional[StageFn] = None,
    wait_out_outages: bool = False,
) -> FastFlashResult:
    """Upload the kernel, identify it and compare the ECU with ``image``.

    Erases nothing and programs nothing. The caller ends the session with
    :func:`reset_ecu`. ``resume`` reuses a kernel already running: only pass it
    once the kernel was recognised by its refusal pattern (never sent to a
    running application). ``policy`` refuses (kernel reset,
    :class:`FastFlashUnavailable`) a kernel or ECU the caller does not accept.
    """
    with _outage_policy(wait_out_outages):
        return _compare(uds, kernel, image, progress, resume, policy, on_stage)


def _compare(uds, kernel, image, progress, resume, policy, on_stage):
    info = running_kernel(uds) if resume else None
    if info is None:
        if resume:
            raise FlashError(
                "The ECU no longer answers as the Dynamic Flash program. Nothing was "
                "changed; reconnect and try again."
            )
        _stage(on_stage, "upload")
        _note(progress, "Uploading the Dynamic Flash program...", 5.0)
        upload_kernel(uds, kernel)
    else:
        _note(progress, "Flash program already running: resuming", 5.0)
    # The kernel runs from here. Nothing below erases: a failure stops it
    # (if the link still answers), so the ECU restarts instead of waiting. In a
    # resume a mere link error leaves it running: an earlier session may have
    # erased blocks, and a second resume can still finish the flash.
    try:
        return _check_and_compare(
            uds, kernel, image, progress, resume, policy, on_stage, info
        )
    except BaseException as e:
        if not (resume and _is_link_error(e)):
            reset_ecu(uds)
        raise


def _check_and_compare(uds, kernel, image, progress, resume, policy, on_stage, info):
    if info is None:
        try:
            info = identify(uds)
        except FlashError as e:
            _refuse(f"the kernel did not identify itself ({e})")
    _note(
        progress,
        f"Kernel v{info.version} running (chip class {info.chip_class}, "
        f"flash {'ready' if info.flash_ready else 'NOT ready'})",
        15.0,
    )
    try:
        checked = verify_kernel_image(uds, kernel, info)
    except FlashError:
        if resume:
            raise FlashError(
                "The flash program running in the ECU is not NC Flash's current "
                "Dynamic Flash program, so the interrupted flash was not "
                "finished. NC Flash told the ECU to restart; if it then shows "
                "recovery mode, do a full flash."
            ) from None
        raise
    if not checked:
        logger.warning(
            "Dynamic Flash: kernel v%d cannot report its own CRC; not checked",
            info.version,
        )
    if policy is not None:
        reason = policy.refusal(info, checked)
        if reason:
            _refuse(reason, resume)
        if policy.generation is not None:
            ecu_generation = ecu_boot_generation(uds)
            if ecu_generation is None:
                _refuse("the ECU's boot code is not one NC Flash knows", resume)
            if ecu_generation != policy.generation:
                raise ECUGenerationMismatchError(policy.generation, ecu_generation)
    _stage(on_stage, "compare")
    _note(progress, "Comparing the ECU with the ROM...", 16.0)
    before = compare_blocks(uds, image)
    boot_same = ecu_crc(uds, 0, ROM_FLASH_START_MIN) == zlib.crc32(
        image[:ROM_FLASH_START_MIN]
    )
    if not boot_same:
        logger.warning(
            "Dynamic Flash: the ROM file's boot block (0x0000-0x1FFF) differs from "
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
    chunk: int = DEFAULT_CHUNK,
    resume: bool = False,
    compress: bool = True,
    rewrite_all: bool = False,
    *,
    policy: Optional[KernelPolicy] = None,
    on_stage: Optional[StageFn] = None,
    wait_out_outages: bool = False,
) -> FastFlashResult:
    """Rewrite the blocks of the ECU that differ from ``image``.

    With ``compress`` and a kernel that supports it (v2), blocks travel
    LZMA-compressed; otherwise in plain 0x36 writes of ``chunk`` bytes.
    ``rewrite_all`` rewrites every block even when its CRC already matches.

    ``image`` must be the checksum-corrected 1 MB ROM (use
    ``flash_prep.prepare_flash_image``). Before the first erase, anything the
    caller does not accept (``policy``, a kernel whose flash routines are not
    ready) stops the kernel and raises :class:`FastFlashUnavailable`: nothing
    changed. After it, any failure raises :class:`FlashError`; the ECU then
    waits (kernel or boot loader) and must be flashed again. Returns with
    ``rewritten`` empty when the ECU already holds ``image`` (kernel still
    running: the caller resets it).
    """
    if _is_blank(image[ENTRY_POINTER : ENTRY_POINTER + 4]):
        raise FlashError("This ROM has no entry pointer; it would never start")
    with _outage_policy(wait_out_outages):
        result = _compare(uds, kernel, image, progress, resume, policy, on_stage)
        return _flash_changed(
            uds, image, result, progress, abort, chunk, compress, rewrite_all, on_stage
        )


def _flash_changed(
    uds, image, result, progress, abort, chunk, compress, rewrite_all, on_stage
) -> FastFlashResult:
    changed = result.changed
    if result.interrupted_before:
        # An earlier flash was cut short: cells written just before the cut
        # may read right without being fully programmed. Rewrite everything.
        _note(progress, "Earlier flash was cut short: rewriting every block", 18.0)
        changed = list(range(FIRST_APP_BLOCK, LAST_BLOCK + 1))
    elif rewrite_all:
        # Full rewrite on request: every block, whether it differs or not.
        changed = list(range(FIRST_APP_BLOCK, LAST_BLOCK + 1))
    if not changed:
        _note(progress, "ECU already holds this image; nothing to do", 100.0)
        return result
    if not result.kernel.flash_ready or (abort and abort()):
        # Nothing erased yet: stop the kernel so the ECU restarts.
        reset_ecu(uds)
        if not result.kernel.flash_ready:
            _refuse("the kernel could not prepare the flash routines")
        raise FlashAbortedError("Dynamic Flash aborted before the first erase")

    lz = compress and result.kernel.version >= LZ_KERNEL_VERSION

    def write(block: int) -> int:
        if lz:
            return program_block_lz(uds, image, block)
        return program_block(uds, image, block, None, chunk)

    others = [b for b in changed if b != LAST_BLOCK]
    steps = len(others) + 2
    _note(progress, f"Blocks to rewrite: {others + [LAST_BLOCK]}", 20.0)
    # From here to the end an abort would leave the ECU without an application,
    # so aborts are no longer honoured: finishing is the safe direction.
    _stage(on_stage, "program")
    try:
        erase_block(uds, LAST_BLOCK)
        for i, block in enumerate(others, start=1):
            _note(progress, f"Rewriting block {block}...", 20.0 + 70.0 * i / steps)
            erase_block(uds, block)
            result.bytes_sent += write(block)
            result.rewritten.append(block)
        _note(
            progress,
            f"Rewriting block {LAST_BLOCK}...",
            20.0 + 70.0 * (steps - 1) / steps,
        )
        result.bytes_sent += write(LAST_BLOCK)
        result.rewritten.append(LAST_BLOCK)

        # Verify BEFORE the commit page: the ECU still waits in its boot
        # loader, so a bad block cannot be started.
        _stage(on_stage, "verify")
        _note(progress, "Verifying every block...", 92.0)
        after = compare_blocks(uds, image, with_commit=False)
        bad = [b.block for b in after if not b.same]
        if bad:
            raise FlashError(
                f"Dynamic Flash verification FAILED for blocks {bad}. The ECU is "
                "waiting in its boot loader and must be flashed again."
            )
        _stage(on_stage, "commit")
        _note(progress, "Committing...", 97.0)
        commit(uds, image)
    except FlashError:
        raise  # already says what state the ECU is in
    except NegativeResponseError as e:
        raise FlashError(
            f"Dynamic Flash stopped: the ECU refused a step ({e}). The ECU is "
            "waiting in its boot loader; flash it again."
        ) from e
    except Exception as e:  # noqa: BLE001 - reworded, never swallowed
        raise FlashError(
            f"Dynamic Flash stopped part-way ({e}). The ECU is waiting in its "
            "boot loader; flash it again."
        ) from e
    _note(progress, "Verified", 100.0)
    return result
