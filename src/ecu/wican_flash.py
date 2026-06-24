"""Lossy-link-safe WRITE/flash orchestration over WiCAN (host-driven).

This is the host-driven WiCAN flash path (goal-2 Part B, "Option A" — the
design-of-record in ``WICAN_TRANSPORT.md`` §6). It wraps the existing,
transport-agnostic :class:`~src.ecu.flash_manager.FlashManager` with the extra
safeguards a wireless link needs, WITHOUT changing the J2534 path (which flashes
directly through ``FlashManager`` and stays byte-for-byte identical):

  1. **Pre-flight link-quality gate (flash only)** — refuse to start if the link
     drops frames or is congested (see :mod:`src.ecu.link_quality`). The write
     path has no mid-stream resend, so a clean link is a hard precondition.
  2. **Battery/voltage guard** — refuse below :data:`BATTERY_VOLTAGE_WARNING`
     (the historical brick cause), matching the J2534 UI guard.
  3. **Abort-and-restart-from-scratch** — on a mid-flash transport drop, NEVER
     resend a block. Abort and re-run the entire flash (re-auth, re-SBL,
     re-transfer) from the beginning, up to ``max_attempts`` times. Each attempt
     is a fresh :class:`FlashManager`, so there is no shared mid-flash state.
  4. **Optional read-back verify** — read the flashed region back and byte-compare
     to the checksum-corrected source (off by default, matching the trusted cable
     flow).

SAFETY / STATUS: this module is **BUILT but NOT hardware-validated** — no real
flash has been performed over WiCAN. The no-resend invariant is preserved (a
restart is a whole fresh flash, never a mid-stream block resend), but the path
MUST be bench-validated (user-gated, link-quality permitting) before production
use. The ECU MUST stay powered with the ignition ON for the whole operation.

Headless module: standard library + sibling core modules only (no PySide6).
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from .checksum import correct_rom_checksums
from .constants import (
    BATTERY_VOLTAGE_WARNING,
    FLASH_COUNTER_OFFSET,
    FLASH_COUNTER_SIZE,
    ROM_FLASH_START_MIN,
)
from .exceptions import (
    FlashError,
    TransferError,
    UDSTimeoutError,
)
from .flash_manager import FlashManager, ProgressCallback
from .link_quality import (
    DEFAULT_MAX_P95_MS,
    DEFAULT_PINGS,
    LinkQualityResult,
    check_link_quality,
)
from .protocol import UDSConnection
from .rom_utils import find_first_difference
from .wican_transport import WiCANError

logger = logging.getLogger(__name__)

#: Whole-flash attempts before giving up. Each is a full restart-from-scratch
#: (re-auth, re-SBL, re-transfer) — never a mid-stream resend.
DEFAULT_FLASH_ATTEMPTS = 3

#: Pause (s) between a dropped attempt and the restart. Also rides out the ECU's
#: security-access cooldown — rapid re-auth trips NRC 0x22 (WICAN_TRANSPORT.md §8b),
#: so leave a few seconds between programming-session attempts.
DEFAULT_RESTART_BACKOFF_S = 3.0

#: Transport-drop errors that justify an abort-and-restart. Anything else
#: (ROM validation, checksum, security denied, user abort) is non-recoverable by
#: retrying and propagates immediately.
_RESTARTABLE = (WiCANError, TransferError, UDSTimeoutError)

#: MASTER GATE for the WiCAN write path at the UI seam.
#: **ON (Option B Phase 6).** The brick-prone host-driven, block-by-block WiFi
#: write (this module's ``WiCANFlasher``) was the reason this was OFF (an
#: interrupted programming session needs a reflash, not a power cycle — see memory
#: ``project_wican_write_bricks_on_interrupt``); it has been **superseded** by the
#: SD-staged, firmware-driven local-CAN flash (``WiCANSdFlasher``, "Option B"),
#: which removes WiFi from the flash loop and was proven byte-perfect on the live
#: MX-5 NC ECU (2026-06-23). ``_build_flash_driver`` routes WiCAN writes to that
#: SD flasher; this gate now enables it. It stays behind the ``version_ping``
#: firmware rev-gate (NCFRv5+), the link/battery pre-flight gate, and the SD-image
#: CRC32 digest gate. J2534 flashing is never gated by this.
WICAN_WRITE_ENABLED = True


class WiCANFlasher:
    """Host-driven, lossy-link-safe flash over an already-open WiCAN transport.

    The caller owns the transport lifecycle (open/close, and any ``slcan``
    auto-config) — this class only drives the flash over it and never closes it.

    Example::

        transport = WiCANTransport(host, port); transport.open()
        flasher = WiCANFlasher(transport)
        flasher.flash_rom(rom_bytes, verify=True)   # raises FlashError on a bad link
        transport.close()
    """

    def __init__(
        self,
        transport,
        *,
        max_attempts: int = DEFAULT_FLASH_ATTEMPTS,
        link_pings: int = DEFAULT_PINGS,
        max_loss_pct: float = 0.0,
        max_p95_ms: float = DEFAULT_MAX_P95_MS,
        min_voltage: float = BATTERY_VOLTAGE_WARNING,
        restart_backoff_s: float = DEFAULT_RESTART_BACKOFF_S,
    ):
        """
        Args:
            transport: An already-open ``WiCANTransport`` (or any ``EcuTransport``).
            max_attempts: Whole-flash restart-from-scratch attempts on a drop.
            link_pings: Tester Present round-trips for the pre-flight gate.
            max_loss_pct: Maximum tolerated pre-flight packet loss (0 = clean only).
            max_p95_ms: Maximum tolerated pre-flight p95 latency (ms).
            min_voltage: Refuse to flash below this battery voltage.
            restart_backoff_s: Pause between a dropped attempt and the restart.
        """
        self._transport = transport
        self._max_attempts = max(1, max_attempts)
        self._link_pings = link_pings
        self._max_loss_pct = max_loss_pct
        self._max_p95_ms = max_p95_ms
        self._min_voltage = min_voltage
        self._restart_backoff_s = restart_backoff_s

    # --- public API ---------------------------------------------------------

    def preflight(
        self, progress_cb: Optional[Callable[[int, int], None]] = None
    ) -> LinkQualityResult:
        """Run ONLY the pre-flight link-quality gate (no flash).

        Lets a UI show link status / enable the flash button before committing.
        Does NOT check battery (that is read during :meth:`flash_rom`). Returns
        the :class:`LinkQualityResult`; ``result.ok is False`` means do not flash.
        """
        uds = UDSConnection(self._transport)
        return check_link_quality(
            uds,
            pings=self._link_pings,
            max_loss_pct=self._max_loss_pct,
            max_p95_ms=self._max_p95_ms,
            progress_cb=progress_cb,
        )

    def flash_rom(
        self,
        rom_data: bytes,
        *,
        progress_cb: Optional[ProgressCallback] = None,
        archive_path: Optional[str] = None,
        verify: bool = False,
    ) -> None:
        """Full flash over WiCAN with the lossy-link safeguards.

        Raises:
            FlashError: pre-flight gate failed, battery too low, the flash failed
                after every restart attempt, or read-back verify mismatched.
            ECUError subclasses: a non-recoverable ECU/validation error.
        """
        self._gate()
        self._run_with_restart(
            lambda: self._one_flash_rom(rom_data, progress_cb, archive_path),
            label="full flash",
        )
        if verify:
            self._verify(rom_data, progress_cb)

    def dynamic_flash(
        self,
        rom_data: bytes,
        archive_path: str,
        *,
        progress_cb: Optional[ProgressCallback] = None,
        verify: bool = False,
    ) -> None:
        """Differential (calibration) flash over WiCAN with the safeguards.

        Mirrors :meth:`flash_rom`; the changed-region math stays in
        ``FlashManager.dynamic_flash``.
        """
        self._gate()
        self._run_with_restart(
            lambda: self._one_dynamic_flash(rom_data, archive_path, progress_cb),
            label="dynamic flash",
        )
        if verify:
            self._verify(rom_data, progress_cb)

    # --- internals ----------------------------------------------------------

    def _gate(self) -> None:
        """Pre-flight link-quality gate + battery guard. Raises on failure."""
        lq = self.preflight()
        if not lq.ok:
            raise FlashError(
                f"Pre-flight link-quality gate FAILED: {lq.reason}. A flash needs a "
                "clean link — the write path cannot resend a dropped block. Improve "
                "the WiFi (move closer / AP mode) and retry. (Reads are unaffected.)"
            )

        uds = UDSConnection(self._transport)
        voltage = uds.read_battery_voltage()
        if voltage is not None and voltage < self._min_voltage:
            raise FlashError(
                f"Battery voltage {voltage:.1f} V is below the {self._min_voltage:.1f} V "
                "flash minimum — flashing now risks a brown-out brick. Connect a "
                "charger/maintainer and retry."
            )
        if voltage is None:
            logger.warning(
                "Battery voltage unavailable (PID unsupported) — proceeding "
                "without the voltage guard, as the J2534 path does."
            )

    def _run_with_restart(self, do_flash: Callable[[], None], *, label: str) -> None:
        """Run ``do_flash``; on a transport drop, restart the whole flash.

        NEVER resends a block mid-stream — each attempt is a fresh, complete
        flash. Non-restartable errors propagate immediately.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                do_flash()
                if attempt > 1:
                    logger.info("%s succeeded on attempt %d", label, attempt)
                return
            except _RESTARTABLE as exc:
                last_exc = exc
                if attempt >= self._max_attempts:
                    break
                logger.warning(
                    "%s attempt %d/%d dropped (%s); restarting from scratch "
                    "(no mid-stream resend)",
                    label,
                    attempt,
                    self._max_attempts,
                    exc,
                )
                self._recover_between_attempts()

        raise FlashError(
            f"{label} failed after {self._max_attempts} attempt(s); last error: "
            f"{last_exc}. Every attempt restarted from scratch — no block was ever "
            "resent mid-stream, so the ECU was never left in a shifted/partial "
            "state by this tool. Keep the ignition ON and retry on a better link."
        ) from last_exc

    def _recover_between_attempts(self) -> None:
        """Flush stale frames and pause before a restart (best-effort flush)."""
        try:
            self._transport.flush()
        except Exception as exc:  # pragma: no cover - best-effort
            logger.debug("flush before flash restart failed (ignored): %s", exc)
        if self._restart_backoff_s > 0:
            time.sleep(self._restart_backoff_s)

    def _fresh_fm(self) -> FlashManager:
        """A fresh FlashManager borrowing the WiCAN link.

        Each flash attempt gets its own FlashManager so there is no shared
        mid-flash state to carry over — the honest way to "restart from scratch".
        """
        fm = FlashManager()
        fm.use_uds(UDSConnection(self._transport))
        return fm

    def _one_flash_rom(self, rom_data, progress_cb, archive_path) -> None:
        """One full-flash attempt over a fresh FlashManager borrowing the link."""
        self._fresh_fm().flash_rom(
            rom_data, progress_cb=progress_cb, archive_path=archive_path
        )

    def _one_dynamic_flash(self, rom_data, archive_path, progress_cb) -> None:
        """One dynamic-flash attempt over a fresh FlashManager borrowing the link."""
        self._fresh_fm().dynamic_flash(rom_data, archive_path, progress_cb=progress_cb)

    def _verify(self, rom_data, progress_cb) -> None:
        """Read the flashable region back and byte-compare to the written image.

        Compares ``[ROM_FLASH_START_MIN:]`` (the full program/calibration region)
        against the checksum-corrected source — which is exactly what
        ``FlashManager`` writes — so it validates both a full flash and a dynamic
        flash (the unchanged head below the diff is already correct on the ECU).
        ``read_rom`` is whole-ROM only, so the head below ``start`` is transferred
        then discarded.
        """
        expected = bytearray(rom_data)
        correct_rom_checksums(expected)  # match what was actually written
        written = bytearray(self._fresh_fm().read_rom(progress_cb=progress_cb))

        # The ECU stamps its own flash-cycle counter during programming, so those
        # bytes never match the source. Mask the counter region on BOTH sides
        # (matching get_calibration_crc's clear_flash_counter) so the read-back
        # compares only what we actually wrote — otherwise a byte-perfect flash
        # would "fail" on the ECU-managed counter (hardware-confirmed 2026-06-23).
        ctr, clen = FLASH_COUNTER_OFFSET, FLASH_COUNTER_SIZE
        expected[ctr : ctr + clen] = b"\xff" * clen
        written[ctr : ctr + clen] = b"\xff" * clen

        start = ROM_FLASH_START_MIN
        off = find_first_difference(written[start:], expected[start:])
        if off < 0:
            logger.info("read-back verify: PASS (region 0x%06X..end)", start)
            return
        raise FlashError(
            f"Read-back verify FAILED: the written ROM differs from the source at "
            f"0x{start + off:06X}. Do NOT trust this flash — re-flash on a clean "
            "link and keep the ignition ON."
        )
