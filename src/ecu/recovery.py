"""Detect an ECU sitting in its boot loader's recovery server ("bricked").

An NC ECU whose application header is invalid (a failed or wrong flash) does not
go dead: the boot loader stays in a small recovery server that still answers
UDS. It refuses the normal diagnostic services, and NC Flash's ordinary full
flash recovers it (bench-proven 2026-11-09 on a spare ECU over WiCAN): the
boot loader hands out the fixed seed ``55 AA 55``, and the normal key algorithm
computes the right key for it.

This module only *recognises* that state so the UI can explain it and steer the
user to a full flash. It adds no flash capability.

What the recovery server looks like on the wire (bench, 2026-11-09):

* Tester Present is answered (the session is alive);
* OBD Mode 0x01 and the VIN block (0x21) are refused with NRC 0x11 (service not
  supported), ROM-ID / DTC reads (0x22) with NRC 0x31;
* after entering the programming session, SecurityAccess returns the fixed seed
  ``55 AA 55``. The running application returns a different seed every time.

The decision uses a combination, never one NRC: all three refusals must match
(NRC 0x11 for 0x01 and 0x21, NRC 0x31 for 0x22, each echoing the refused
service), and only then is the seed asked for. A healthy
application answers OBD Mode 0x01, so it never gets past the first request: no
programming session, no seed request. No key is ever sent by the probe.

Headless: no PySide6.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .constants import (
    BOOT_LOADER_SEED,
    DIAG_SESSION_PROGRAMMING,
    NRC_CONDITIONS_NOT_CORRECT,
    NRC_REQUEST_OUT_OF_RANGE,
    NRC_SERVICE_NOT_SUPPORTED,
    OBD_PID_ENGINE_RPM,
    SECURITY_REQUEST_SEED,
    SID_DIAGNOSTIC_SESSION,
    SID_OBD_CURRENT_DATA,
    SID_SECURITY_ACCESS,
    TIMEOUT_PROBE,
)
from .exceptions import NegativeResponseError

logger = logging.getLogger(__name__)

#: SID of the VIN-block read (KWP ReadDataByLocalIdentifier, romdrop's 0x21 0x00).
SID_READ_VIN_BLOCK = 0x21

#: SID of the ROM-ID read (ReadDataByIdentifier 0x22 0xE6 0x11).
SID_READ_ROM_ID = 0x22

#: NRCs the probe expects and handles; logged at DEBUG, not WARNING.
_QUIET = {
    NRC_SERVICE_NOT_SUPPORTED,
    NRC_CONDITIONS_NOT_CORRECT,
    NRC_REQUEST_OUT_OF_RANGE,
}


class EcuMode(Enum):
    """What the probe concluded about the ECU."""

    #: The application answered a normal service: a running ECU.
    NORMAL = "normal"
    #: Refusal pattern AND the fixed boot-loader seed: recovery server.
    BOOT_LOADER = "boot_loader"
    #: Refusal pattern, but no seed was obtained (refused or no reply). Normal diagnostics are unavailable; cause not proven.
    SUSPECT = "suspect"
    #: Inconclusive (no reply, another NRC, a link error): say nothing.
    UNKNOWN = "unknown"


@dataclass
class RecoveryProbeResult:
    mode: EcuMode
    seed: Optional[bytes] = None
    #: One short line per step, for the Activity Log / a field report.
    evidence: list[str] = field(default_factory=list)

    @property
    def needs_recovery(self) -> bool:
        """True when normal diagnostics are unavailable (confirmed or not)."""
        return self.mode in (EcuMode.BOOT_LOADER, EcuMode.SUSPECT)


def is_boot_loader_seed(seed: Optional[bytes]) -> bool:
    """True for the boot loader's fixed SecurityAccess seed ``55 AA 55``."""
    return seed is not None and bytes(seed) == BOOT_LOADER_SEED


def _refused_as_unsupported(exc: Exception, sid: int) -> bool:
    """NRC 0x11 echoing ``sid``: the service does not exist in this server."""
    return (
        isinstance(exc, NegativeResponseError)
        and exc.nrc == NRC_SERVICE_NOT_SUPPORTED
        and exc.service_id == sid
    )


def _refused_out_of_range(exc: Exception, sid: int) -> bool:
    """NRC 0x31 echoing ``sid`` (how the boot loader refuses the ROM-ID read)."""
    return (
        isinstance(exc, NegativeResponseError)
        and exc.nrc == NRC_REQUEST_OUT_OF_RANGE
        and exc.service_id == sid
    )


def _request(uds, sid: int, data: bytes) -> bytes:
    return uds.send_request(
        sid,
        data,
        timeout=TIMEOUT_PROBE,
        pending_max=TIMEOUT_PROBE,
        quiet_nrcs=_QUIET,
    )


def probe_ecu_mode(uds) -> RecoveryProbeResult:
    """Classify the ECU behind ``uds``. Never raises; never sends a key.

    Steps, each one stopping the probe unless it matches the recovery pattern:

    1. OBD Mode 0x01 PID 0x0C. Answered -> NORMAL. NRC 0x11 -> go on.
    2. VIN block (0x21 0x00). Answered -> NORMAL. NRC 0x11 -> go on.
    3. ROM-ID read (0x22 0xE6 0x11). Answered -> NORMAL. NRC 0x31 -> go on.
    4. Programming session (0x10 0x85) + seed request (0x27 0x01) -- the exact
       opening of the normal flash, which the boot loader is proven to accept.
       Seed ``55 AA 55`` -> BOOT_LOADER; no seed obtained -> SUSPECT; any
       other seed (the running application's) -> UNKNOWN.

    Any other outcome of steps 1-3 (no reply, another NRC, a corrupt reply)
    is UNKNOWN: it proves nothing, so nothing more is sent.
    """
    result = RecoveryProbeResult(EcuMode.UNKNOWN)
    if uds is None:
        result.evidence.append("no open session")
        return result

    for sid, data, name, refused in (
        (
            SID_OBD_CURRENT_DATA,
            bytes([OBD_PID_ENGINE_RPM]),
            "OBD Mode 0x01",
            _refused_as_unsupported,
        ),
        (SID_READ_VIN_BLOCK, bytes([0x00]), "VIN read 0x21", _refused_as_unsupported),
        (
            SID_READ_ROM_ID,
            bytes([0xE6, 0x11]),
            "ROM-ID read 0x22",
            _refused_out_of_range,
        ),
    ):
        try:
            _request(uds, sid, data)
        except Exception as exc:
            if refused(exc, sid):
                result.evidence.append(f"{name}: refused (NRC 0x{exc.nrc:02X})")
                continue
            result.evidence.append(f"{name}: {exc}")
            logger.debug("Recovery probe inconclusive at %s: %s", name, exc)
            return result
        result.mode = EcuMode.NORMAL
        result.evidence.append(f"{name}: answered")
        return result

    # Both normal services are missing. Ask for the seed the way the flash does.
    result.mode = EcuMode.SUSPECT
    try:
        _request(uds, SID_DIAGNOSTIC_SESSION, bytes([DIAG_SESSION_PROGRAMMING]))
        result.evidence.append("programming session: accepted")
        resp = _request(uds, SID_SECURITY_ACCESS, bytes([SECURITY_REQUEST_SEED]))
    except Exception as exc:
        result.evidence.append(f"seed request: {exc}")
        logger.debug("Recovery probe: seed not obtained: %s", exc)
        return result
    seed = bytes(resp[1:]) if len(resp) >= 2 else None
    result.seed = seed
    if is_boot_loader_seed(seed):
        result.mode = EcuMode.BOOT_LOADER
        result.evidence.append("seed 55aa55: boot-loader recovery server")
    else:
        # A varying seed is the running application's: not a recovery case.
        result.mode = EcuMode.UNKNOWN
        result.evidence.append(
            f"seed {seed.hex() if seed else '(none)'}: not the boot-loader seed"
        )
    return result
