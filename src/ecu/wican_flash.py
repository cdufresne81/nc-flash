"""Pre-flight WiCAN flash gate (link-quality + battery), composed by the SD flasher.

The host-driven, block-by-block WiFi flash ("Option A") this module used to
implement was RETIRED (audit D4). It was production-unreachable — the UI always
routes the SD-staged, firmware-driven flash (:class:`WiCANSdFlasher`) — and it was
the known brick-on-interrupt path: an interrupted host-driven programming session
needs a reflash, not a power cycle (memory ``project_wican_write_bricks_on_interrupt``).

What remains is the reusable safety GATE the SD flash composes:

  1. **Pre-flight link-quality gate** — refuse to start if the link drops frames
     or is congested (see :mod:`src.ecu.link_quality`). The write path has no
     mid-stream resend, so a clean link is a hard precondition.
  2. **Battery/voltage guard** — refuse below :data:`BATTERY_VOLTAGE_WARNING`
     (the historical brick cause), matching the J2534 UI guard.

J2534 flashing is never gated by this. Headless module: standard library +
sibling core modules only (no PySide6).
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from .constants import BATTERY_VOLTAGE_WARNING
from .exceptions import FlashError
from .link_quality import (
    DEFAULT_MAX_P95_MS,
    DEFAULT_PINGS,
    LinkQualityResult,
    check_link_quality,
)
from .protocol import UDSConnection

logger = logging.getLogger(__name__)

#: MASTER GATE for the WiCAN write path at the UI seam.
#: **ON (Option B Phase 6).** The brick-prone host-driven, block-by-block WiFi
#: write (the retired Option A) was the reason this was OFF (an interrupted
#: programming session needs a reflash, not a power cycle — see memory
#: ``project_wican_write_bricks_on_interrupt``); it has been superseded by the
#: SD-staged, firmware-driven local-CAN flash (:class:`WiCANSdFlasher`, "Option
#: B"), proven byte-perfect on the live MX-5 NC ECU (2026-06-23).
#: ``_build_flash_driver`` routes WiCAN writes to that SD flasher; this gate now
#: enables it, behind the ``version_ping`` firmware rev-gate (NCFRv5+), the
#: link/battery pre-flight gate, and the SD-image CRC32 digest gate. J2534
#: flashing is never gated by this.
WICAN_WRITE_ENABLED = True


class WiCANFlasher:
    """Pre-flight gate for a WiCAN flash — link-quality + battery guard.

    Composed by :class:`~src.ecu.wican_sd_flash.WiCANSdFlasher` (which owns the
    actual SD-staged, firmware-driven flash). The caller owns the transport
    lifecycle (open/close, ``slcan`` auto-config) — this class only reads the
    link/battery over it and never closes it.
    """

    def __init__(
        self,
        transport,
        *,
        link_pings: int = DEFAULT_PINGS,
        max_loss_pct: float = 0.0,
        max_p95_ms: float = DEFAULT_MAX_P95_MS,
        min_voltage: float = BATTERY_VOLTAGE_WARNING,
    ):
        """
        Args:
            transport: An already-open ``WiCANTransport`` (or any ``EcuTransport``).
            link_pings: Tester Present round-trips for the pre-flight gate.
            max_loss_pct: Maximum tolerated pre-flight packet loss (0 = clean only).
            max_p95_ms: Maximum tolerated pre-flight p95 latency (ms).
            min_voltage: Refuse to flash below this battery voltage.
        """
        self._transport = transport
        self._link_pings = link_pings
        self._max_loss_pct = max_loss_pct
        self._max_p95_ms = max_p95_ms
        self._min_voltage = min_voltage

    def preflight(
        self, progress_cb: Optional[Callable[[int, int], None]] = None
    ) -> LinkQualityResult:
        """Run ONLY the pre-flight link-quality gate (no flash).

        Lets a UI show link status / enable the flash button before committing.
        Does NOT check battery (that is read by :meth:`_gate`). Returns the
        :class:`LinkQualityResult`; ``result.ok is False`` means do not flash.
        """
        uds = UDSConnection(self._transport)
        return check_link_quality(
            uds,
            pings=self._link_pings,
            max_loss_pct=self._max_loss_pct,
            max_p95_ms=self._max_p95_ms,
            progress_cb=progress_cb,
        )

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
