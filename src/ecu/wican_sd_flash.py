"""SD-staged, firmware-driven WiCAN flash orchestration (Option B).

This is the *safe* WiCAN WRITE path (``.claude/plans/wican-write-option-b-goal.md``)
that supersedes the host-driven :class:`~src.ecu.wican_flash.WiCANFlasher`
("Option A", which soft-bricks on a mid-flash WiFi drop and is disabled at the UI
seam). Instead of pushing ~150k ISO-TP frames over WiFi, it:

  1. **packages** the ROM host-side (checksum-correct, SBL, flash plan, digests —
     :mod:`src.ecu.wican_sd_package`),
  2. **uploads** the staged image to the WiCAN SD card over reliable TCP and
     verifies the device's CRC (:mod:`src.ecu.wican_sd_upload`) — the only WiFi
     step, fully checkable *before* the ECU is touched,
  3. **triggers** the firmware to drive ``RequestDownload → TransferData(SBL) →
     TransferData(program) → TransferExit → ECUReset`` locally over CAN at line
     rate (no WiFi in the flash loop — the brick driver is gone), confirming
     every block over CAN (positive ``0x76`` per block, ``0x77`` on exit), and
  4. optionally **read-back verifies** the flashed region byte-for-byte — but
     ONLY after a physical ignition cycle (see below), so this is an explicit,
     off-by-default step, not part of the inline flash result.

WRITE INTEGRITY is proven by step 3 itself, exactly like the trusted J2534
:class:`FlashManager.flash_rom` path (which also does NOT read back): the SD image
CRC32 is firmware-verified before any CAN traffic, then every program block gets a
positive ECU response and ``TransferExit`` is acknowledged. ``NCFWDONE`` therefore
means the exact staged bytes are committed — a write proof at least as strong as a
J2534 flash.

READ-BACK VERIFY cannot run inline: the firmware ends with ``ECUReset`` and the NC
ECU then sits in the **bootloader** (``ReadMemoryByAddress`` and OBD Mode 01 both
return NRC 0x11) until a **physical ignition cycle** (key OFF ~10 s, then ON) boots
the application — the standard, documented Mazda NC post-flash step, which the host
cannot perform. So ``verify`` defaults **OFF**; when explicitly enabled, the
operator must cycle the ignition first (bench: ``tools/wican_fastread_verify.py``).
Hardware-confirmed 2026-06-23, the post-cycle read-back equals
``correct_rom_checksums(source)`` exactly bar the ECU-stamped flash counter.

It keeps the proven safeguards of :class:`WiCANFlasher` by **composition** (link
pre-flight gate, battery guard, read-back compare) — no mixin, no duplication.

STATUS. Implemented + unit-tested end-to-end; step 3 drives the firmware
``fast_write`` (NCFRv5+) over CAN, **rev-gated** (refuses on a fast-read-only
build). Live flash proven byte-perfect on the MX-5 NC ECU (2026-06-23). Headless:
stdlib + sibling core modules.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from .checksum import correct_rom_checksums
from .constants import (
    BATTERY_VOLTAGE_WARNING,
    FLASH_COUNTER_OFFSET,
    FLASH_COUNTER_SIZE,
    ROM_FLASH_START_MIN,
    ROM_SIZE,
)
from .exceptions import FlashError
from .flash_manager import FlashManager, FlashProgress, FlashState, ProgressCallback
from .rom_utils import find_first_difference
from .wican_flash import WiCANFlasher
from .wican_sd_package import FlashPackage, build_flash_package
from .wican_sd_upload import WiCANSdUploader
from .wican_transport import WiCANError, _FAST_READ_VERSION_PREFIX

logger = logging.getLogger(__name__)

#: After the flash's ECUReset, the ECU reboots and drops the programming session.
#: Wait this long per attempt (up to this many attempts) for it to come back and
#: accept a fresh programming session before the read-back verify.
POST_FLASH_SETTLE_S = 4.0
POST_FLASH_AUTH_RETRIES = 4

#: Minimum firmware build (``NCFRv<rev>``) that implements the ``fast_write``
#: SD-flash command. Below this the device is fast-read-only and the trigger is
#: refused. Bump when the wire contract changes incompatibly.
FASTWRITE_MIN_FW_REV = 5

#: Default HTTP port for the SD-upload endpoint.
DEFAULT_HTTP_PORT = 80


def _parse_fw_rev(marker: Optional[bytes]) -> Optional[int]:
    """Parse the integer rev out of a ``b"NCFRv<rev>"`` version-ping marker."""
    if not marker:
        return None
    idx = marker.find(_FAST_READ_VERSION_PREFIX)
    if idx < 0:
        return None
    tail = marker[idx + len(_FAST_READ_VERSION_PREFIX) :]
    digits = bytearray()
    for b in tail:
        if 0x30 <= b <= 0x39:
            digits.append(b)
        else:
            break
    if not digits:
        return None
    return int(bytes(digits))


class WiCANSdFlasher:
    """SD-staged, firmware-driven flash over an already-open WiCAN transport.

    Same surface as :class:`WiCANFlasher` (``flash_rom`` / ``dynamic_flash`` /
    ``preflight``) so it is a one-line swap at the UI's ``_build_flash_driver``.
    The caller owns the transport lifecycle; this class never closes it.
    """

    def __init__(
        self,
        transport,
        *,
        http_port: int = DEFAULT_HTTP_PORT,
        uploader: Optional[WiCANSdUploader] = None,
        rom_id: Optional[str] = None,
        source_name: Optional[str] = None,
        min_voltage: float = BATTERY_VOLTAGE_WARNING,
        **flasher_kwargs,
    ):
        """
        Args:
            transport: An already-open ``WiCANTransport`` (or compatible).
            http_port: HTTP port of the SD-upload endpoint on the device.
            uploader: Inject a :class:`WiCANSdUploader` (tests); otherwise one is
                built from ``transport.host``.
            rom_id: Identity label for the manifest; defaults to the ROM's
                calibration ID.
            source_name: The ROM file's name as shown in NC Flash; when given, the
                staged SD filename is derived from it (sanitised to safe ASCII) so
                a timestamped cal-ID isn't the only clue to the file's content.
            min_voltage: Battery flash floor (passed to the safeguard flasher).
            flasher_kwargs: Forwarded to the composed :class:`WiCANFlasher`
                (e.g. ``max_attempts``, ``link_pings``, ``max_p95_ms``).
        """
        self._transport = transport
        self._http_port = http_port
        self._rom_id = rom_id
        self._source_name = source_name
        if uploader is None:
            host = getattr(transport, "host", None)
            if not host:
                raise WiCANError(
                    "WiCANSdFlasher needs the device host for the HTTP SD upload "
                    "(transport exposes no .host and no uploader was injected)"
                )
            uploader = WiCANSdUploader(host, http_port=http_port)
        self._uploader = uploader
        # Proven WiCAN safeguards reused by COMPOSITION (not a mixin): link
        # pre-flight gate, battery guard, and the read-back verify all operate on
        # the same transport.
        self._safeguards = WiCANFlasher(
            transport, min_voltage=min_voltage, **flasher_kwargs
        )

    # --- public API (mirrors WiCANFlasher) ----------------------------------

    def preflight(self, progress_cb=None):
        """Run ONLY the pre-flight link-quality gate (no flash)."""
        return self._safeguards.preflight(progress_cb)

    def flash_rom(
        self,
        rom_data: bytes,
        *,
        progress_cb: Optional[ProgressCallback] = None,
        archive_path: Optional[str] = None,
        verify: bool = False,
    ) -> None:
        """Full SD-staged flash.

        Completes when the firmware confirms the write (``NCFWDONE`` — every block
        positively acknowledged by the ECU over CAN), the same integrity bar as the
        J2534 path. ``verify`` (read-back compare) defaults **OFF**: it can only run
        after a physical ignition cycle boots the app (the NC ECU sits in the
        bootloader after the flash reset), which the host cannot trigger — so the
        caller must cycle the ignition before opting in. See the module docstring.
        """
        self._safeguards._gate()
        self._stage_and_flash(
            rom_data,
            "full",
            archive_data=None,
            archive_path=archive_path,
            progress_cb=progress_cb,
        )
        if verify:
            self._verify_readback(rom_data, progress_cb)

    def dynamic_flash(
        self,
        rom_data: bytes,
        archive_path: str,
        *,
        progress_cb: Optional[ProgressCallback] = None,
        verify: bool = False,
    ) -> None:
        """Differential SD-staged flash (only the changed region streams).

        ``verify`` defaults **OFF** for the same reason as :meth:`flash_rom`: the
        read-back can only run after a post-flash ignition cycle. The write is
        firmware-confirmed (``NCFWDONE``).
        """
        self._safeguards._gate()
        archive_data = Path(archive_path).read_bytes()
        self._stage_and_flash(
            rom_data,
            "dynamic",
            archive_data=archive_data,
            archive_path=archive_path,
            progress_cb=progress_cb,
        )
        if verify:
            self._verify_readback(rom_data, progress_cb)

    # --- internals ----------------------------------------------------------

    @staticmethod
    def _notify(progress_cb, state: FlashState, message: str, percent: float) -> None:
        if progress_cb:
            progress_cb(FlashProgress(state=state, percent=percent, message=message))

    def build_package(
        self, rom_data: bytes, flash_type: str, archive_data
    ) -> FlashPackage:
        """Host-side pre-compute (exposed for the orchestrator / tests)."""
        return build_flash_package(
            rom_data,
            flash_type=flash_type,
            archive_data=archive_data,
            rom_id=self._rom_id,
            source_name=self._source_name,
        )

    def _stage_and_flash(
        self, rom_data, flash_type, *, archive_data, archive_path, progress_cb
    ) -> None:
        # 1. Package host-side (all secrets/compute stay here).
        self._notify(
            progress_cb, FlashState.PREPARING_SBL, "Preparing SD flash package…", 1.0
        )
        pkg = self.build_package(rom_data, flash_type, archive_data)
        logger.info(
            "Staged flash package: %s (%d bytes, flash_start=0x%06X, %s)",
            pkg.staged_filename,
            pkg.manifest["image_len"],
            pkg.manifest["flash_start_index"],
            pkg.manifest["flash_type"],
        )

        # 2. Upload + verify (the only WiFi step; reliable TCP, checkable here).
        self._notify(
            progress_cb,
            FlashState.PREPARING_SBL,
            f"Uploading staged ROM to SD ({pkg.staged_filename})…",
            4.0,
        )
        self._uploader.upload_package(pkg)
        self._uploader.upload_manifest(pkg)
        self._notify(
            progress_cb,
            FlashState.PREPARING_SBL,
            "Staged ROM uploaded + verified",
            25.0,
        )

        # 3. Trigger the firmware flash (rev-gated, then host-auth, then fast_write).
        self._trigger_firmware_flash(pkg, progress_cb)

        # 4. Archive what is now on the ECU (only reached after a confirmed flash).
        if archive_path:
            try:
                Path(archive_path).write_bytes(pkg.corrected_rom)
            except Exception as exc:  # pragma: no cover - non-fatal
                logger.warning("Archive save failed (non-fatal): %s", exc)

    def _authenticate_ecu(self) -> None:
        """Bring the ECU to an authenticated programming session over CAN.

        The firmware fastwrite replays plain bytes and issues RequestDownload with
        no security of its own, so the host must establish the programming session
        + security access first (``compute_security_key`` stays host-side). Mirrors
        the J2534 flash's auth and the validated manual bench flash.
        """
        from .protocol import UDSConnection

        fm = FlashManager()
        fm.use_uds(UDSConnection(self._transport))
        # _connect() (borrowed mode) issues a Tester Present and advances the
        # state machine IDLE -> CONNECTING, so the following _authenticate()'s
        # CONNECTING -> AUTHENTICATING transition is valid. Calling _authenticate()
        # straight from IDLE works but logs a spurious "Invalid state transition
        # blocked: idle -> authenticating" error; this mirrors the read path.
        fm._connect()
        fm._authenticate()
        fm._uds.check_flash_counter()

    def _verify_readback(self, rom_data: bytes, progress_cb=None) -> None:
        """Explicit, post-ignition-cycle read-back: re-read the ECU and byte-compare.

        **Only valid after a physical ignition cycle** has booted the application —
        the NC ECU sits in the bootloader after the flash's ECUReset and refuses
        ``ReadMemoryByAddress`` (NRC 0x11) until then, which the host cannot trigger.
        Callers therefore opt in (``verify=True``) only once the operator has cycled
        the key; the inline flash never runs this. Re-authenticate, fast-read the
        whole ROM, and compare the flashed region (``[ROM_FLASH_START_MIN:]``) to the
        checksum-corrected source — EXCLUDING the ECU-managed flash counter (stamped
        by the ECU, never matches). Raises :class:`FlashError` on any other diff.
        """
        self._notify(progress_cb, FlashState.READING, "Re-reading ECU to verify…", 91.0)
        last: Optional[Exception] = None
        for _ in range(POST_FLASH_AUTH_RETRIES):
            try:
                self._authenticate_ecu()
                last = None
                break
            except Exception as exc:  # ECU not yet readable — settle and retry
                last = exc
                time.sleep(POST_FLASH_SETTLE_S)
        if last is not None:
            raise FlashError(
                "read-back verify: the ECU is not answering memory reads. Cycle the "
                "ignition (key OFF ~10 s, then ON) to boot the flashed calibration "
                f"out of the bootloader, then verify again. ({last})"
            )

        self._notify(
            progress_cb, FlashState.READING, "Reading ECU back to verify…", 92.0
        )
        written = bytearray(self._transport.fast_read(0, ROM_SIZE))
        expected = bytearray(rom_data)
        correct_rom_checksums(expected)
        ctr, clen = FLASH_COUNTER_OFFSET, FLASH_COUNTER_SIZE
        expected[ctr : ctr + clen] = b"\xff" * clen
        written[ctr : ctr + clen] = b"\xff" * clen
        off = find_first_difference(
            written[ROM_FLASH_START_MIN:], expected[ROM_FLASH_START_MIN:]
        )
        if off < 0:
            logger.info(
                "read-back verify PASS (region 0x%06X..end)", ROM_FLASH_START_MIN
            )
            self._notify(progress_cb, FlashState.COMPLETE, "Read-back verified", 100.0)
            return
        raise FlashError(
            "Read-back verify FAILED: the flashed ROM differs from the source at "
            f"0x{ROM_FLASH_START_MIN + off:06X}. Do NOT trust this flash — re-flash "
            "and keep the ignition ON."
        )

    def _firmware_rev(self) -> Optional[int]:
        """Best-effort firmware rev via the version-ping sentinel (no CAN touch)."""
        try:
            return _parse_fw_rev(self._transport.version_ping())
        except Exception as exc:
            logger.warning("version_ping failed during SD-flash rev-gate: %s", exc)
            return None

    def _trigger_firmware_flash(self, pkg: FlashPackage, progress_cb) -> None:
        """Rev-gate, authenticate, then drive the firmware ``fast_write`` SD-flash.

        Until a fastwrite-capable firmware (``NCFRv{FASTWRITE_MIN_FW_REV}+``)
        answers the version ping, refuse with a clear error — the staged upload
        already succeeded and is safe on the SD card, but NO ECU contact is made.
        The ECU is authenticated only AFTER the rev-gate passes, so an old-firmware
        device is never put into a programming session.
        """
        rev = self._firmware_rev()
        if rev is None or rev < FASTWRITE_MIN_FW_REV:
            have = f"NCFRv{rev}" if rev is not None else "unknown/fast-read-only"
            raise WiCANError(
                "WiCAN SD flash requires firmware "
                f"NCFRv{FASTWRITE_MIN_FW_REV}+ with the fastwrite command; device "
                f"reports {have}. The staged ROM was uploaded + verified on the SD "
                "card, but the flash was NOT triggered (no ECU contact). Update the "
                "WiCAN firmware and retry."
            )

        # Authenticate the ECU (programming session + security) — the firmware
        # fastwrite relies on this host-established session for RequestDownload.
        self._notify(
            progress_cb, FlashState.AUTHENTICATING, "Authenticating ECU…", 26.0
        )
        self._authenticate_ecu()

        # Drive the firmware flash over CAN, mapping its streamed block markers
        # into the existing FlashProgress band (35%→90%, matching the J2534 path).
        self._notify(
            progress_cb,
            FlashState.TRANSFERRING_PROGRAM,
            "Flashing from SD over CAN…",
            35.0,
        )

        def on_block(done: int, total: int) -> None:
            pct = 35.0 + (done / total) * 55.0 if total else 35.0
            self._notify(
                progress_cb,
                FlashState.TRANSFERRING_PROGRAM,
                f"Flashing: {done}/{total} blocks",
                pct,
            )

        # Raises WiCANError on FWERR / stall / socket close; returns on NCFWDONE.
        self._transport.fast_write(pkg.staged_filename, mode="L", progress_cb=on_block)
        self._notify(
            progress_cb,
            FlashState.FINALIZING,
            "Flash sequence complete; finalizing…",
            92.0,
        )
