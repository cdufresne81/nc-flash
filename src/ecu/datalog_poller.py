"""Host Datalogger engine: NC Flash polls the ECU itself (Tactrix, OBDX).

Read-only by construction: the only services sent are OBD mode 0x01,
ReadDataByIdentifier (0x22) and ReadMemoryByAddress (0x23). Nothing here
opens a session, unlocks the ECU or writes to it.

One :meth:`DatalogPoller.sweep` polls every channel due on that sweep (a
channel with ``every = N`` is due on every Nth sweep) and returns one value per
channel; a channel not due, or whose read failed, repeats its last value or is
empty. One request is in flight at a time and the next goes out as soon as the
reply lands, so the ECU's reply time sets the pace (bench, Oct 2026: ~1.2 ms
per mode 0x01 request, ~8 ms per mode 0x22/0x23 request).

Mode 0x01 PIDs are packed up to six per request (SAE J1979 allows it and the NC
ECU answers them in one reply). If a packed reply can't be split, packing is
turned off for the rest of the run and those PIDs go one per request.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from src.core.datalog_channels import (
    MAX_EVERY,
    MODE_DID,
    MODE_MEM,
    MODE_OBD,
    Channel,
)

from .constants import SID_OBD_CURRENT_DATA, SID_READ_DATA_BY_IDENTIFIER
from .exceptions import NegativeResponseError, UDSError, UDSTimeoutError

logger = logging.getLogger(__name__)

#: Per-read timeout and whole-request budget (ms). The ECU answers in 1-8 ms;
#: the budget still rides out a few "response pending" replies.
REQUEST_TIMEOUT_MS = 100
REQUEST_BUDGET_MS = 250
#: No reply to anything for this long means the ECU (or the cable) is gone.
ECU_LOST_AFTER_S = 3.0
MAX_PIDS_PER_REQUEST = 6

NRC_SERVICE_NOT_SUPPORTED = 0x11
NRC_SUBFUNCTION_NOT_SUPPORTED = 0x12
NRC_CONDITIONS_NOT_CORRECT = 0x22
NRC_REQUEST_OUT_OF_RANGE = 0x31
_UNSUPPORTED_NRCS = {
    NRC_SERVICE_NOT_SUPPORTED,
    NRC_SUBFUNCTION_NOT_SUPPORTED,
    NRC_REQUEST_OUT_OF_RANGE,
}
_QUIET_NRCS = _UNSUPPORTED_NRCS | {NRC_CONDITIONS_NOT_CORRECT}

#: Data bytes per mode 0x01 PID (SAE J1979), needed to split a packed reply.
#: A PID missing here is never packed.
OBD_PID_LENGTHS = {
    0x00: 4, 0x01: 4, 0x02: 2, 0x03: 2, 0x04: 1, 0x05: 1, 0x06: 1, 0x07: 1,
    0x08: 1, 0x09: 1, 0x0A: 1, 0x0B: 1, 0x0C: 2, 0x0D: 1, 0x0E: 1, 0x0F: 1,
    0x10: 2, 0x11: 1, 0x12: 1, 0x13: 1, 0x14: 2, 0x15: 2, 0x16: 2, 0x17: 2,
    0x18: 2, 0x19: 2, 0x1A: 2, 0x1B: 2, 0x1C: 1, 0x1D: 1, 0x1E: 1, 0x1F: 2,
    0x20: 4, 0x21: 2, 0x22: 2, 0x23: 2, 0x24: 4, 0x25: 4, 0x26: 4, 0x27: 4,
    0x28: 4, 0x29: 4, 0x2A: 4, 0x2B: 4, 0x2C: 1, 0x2D: 1, 0x2E: 1, 0x2F: 1,
    0x30: 1, 0x31: 2, 0x32: 2, 0x33: 1, 0x34: 4, 0x35: 4, 0x36: 4, 0x37: 4,
    0x38: 4, 0x39: 4, 0x3A: 4, 0x3B: 4, 0x3C: 2, 0x3D: 2, 0x3E: 2, 0x3F: 2,
    0x40: 4, 0x41: 4, 0x42: 2, 0x43: 2, 0x44: 2, 0x45: 1, 0x46: 1, 0x47: 1,
    0x48: 1, 0x49: 1, 0x4A: 1, 0x4B: 1, 0x4C: 1, 0x4D: 2, 0x4E: 2, 0x4F: 4,
    0x50: 4, 0x51: 1, 0x52: 1, 0x53: 2, 0x54: 2, 0x55: 2, 0x56: 2, 0x57: 2,
    0x58: 2, 0x59: 2, 0x5A: 1, 0x5B: 1, 0x5C: 1, 0x5D: 2, 0x5E: 2, 0x5F: 1,
}  # fmt: skip


class DatalogEcuLost(UDSError):
    """The ECU stopped answering every request."""


class DatalogNothingToPoll(UDSError):
    """Every enabled channel was refused by the ECU (or none is enabled)."""


@dataclass
class SweepResult:
    values: list  # one Optional[float] per channel, in channel order
    requests: int = 0
    failed: int = 0


@dataclass
class _Request:
    """One wire request and the channels its reply feeds."""

    mode: int
    key: tuple  # (mode, ident[, read size]) or ("packed", pids...)
    pids: tuple = ()  # mode 0x01 PIDs (several when packed)
    channels: list = field(default_factory=list)


class DatalogPoller:
    """Polls *channels* (enabled ones only) over a :class:`UDSConnection`."""

    def __init__(
        self,
        uds,
        channels: list[Channel],
        *,
        pack: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._uds = uds
        self.channels = [c for c in channels if c.enabled]
        for ch in self.channels:
            ch.validate()
        self._index = {id(c): i for i, c in enumerate(self.channels)}
        self._pack = pack
        self._clock = clock
        self._sweep_no = 0
        self._last: list[Optional[float]] = [None] * len(self.channels)
        self._last_ok = clock()
        #: channel name -> why it was dropped (shown to the user)
        self.unsupported: dict[str, str] = {}
        #: channel name -> failed reads (timeouts, refusals) so far
        self.failures: dict[str, int] = {}
        if not self.channels:
            raise DatalogNothingToPoll("No channel is selected.")

    @property
    def header(self) -> str:
        """The CSV header: ``Time`` then one column per channel."""
        return ",".join(["Time"] + [c.name for c in self.channels])

    @property
    def packing(self) -> bool:
        return self._pack

    # --- one sweep ------------------------------------------------------------

    def sweep(self) -> SweepResult:
        """Poll every channel due now; raise on a lost ECU or a dead adapter.

        Adapter/driver errors (anything but a UDS timeout, refusal or bad
        reply) propagate: the caller stops the log and says why.
        """
        if not any(c.name not in self.unsupported for c in self.channels):
            raise DatalogNothingToPoll(
                "The ECU refused every selected channel: "
                + "; ".join(f"{n}: {r}" for n, r in self.unsupported.items())
            )
        # Skip sweeps where nothing is due (every channel polled less often
        # than every sweep): a row is only worth writing when it has news.
        due: list[Channel] = []
        for _ in range(MAX_EVERY):
            k = self._sweep_no
            self._sweep_no += 1
            due = [
                c
                for c in self.channels
                if c.name not in self.unsupported and k % c.every == 0
            ]
            if due:
                break
        values = list(self._last)
        for ch in due:
            values[self._index[id(ch)]] = None  # stays empty if this read fails
        result = SweepResult(values=values)
        for req in self._plan(due):
            result.requests += 1
            if not self._run(req, values):
                result.failed += 1
        for i, v in enumerate(values):
            self._last[i] = v
        if result.requests and result.failed == result.requests:
            if self._clock() - self._last_ok >= ECU_LOST_AFTER_S:
                raise DatalogEcuLost(
                    f"The ECU has not answered for {ECU_LOST_AFTER_S:.0f} s "
                    "(ignition off, or the cable unplugged?)"
                )
        return result

    def _plan(self, due: list[Channel]) -> list[_Request]:
        by_key: dict[tuple, _Request] = {}
        order: list[_Request] = []
        for ch in due:
            if ch.mode == MODE_MEM:
                key = (MODE_MEM, ch.ident, ch.read_size)
            else:
                key = (ch.mode, ch.ident)
            req = by_key.get(key)
            if req is None:
                req = _Request(mode=ch.mode, key=key)
                if ch.mode == MODE_OBD:
                    req.pids = (ch.ident,)
                by_key[key] = req
                order.append(req)
            req.channels.append(ch)
        if not self._pack:
            return order
        packable = [
            r for r in order if r.mode == MODE_OBD and r.pids[0] in OBD_PID_LENGTHS
        ]
        if len(packable) < 2:
            return order
        rest = [r for r in order if r not in packable]
        packed = []
        for i in range(0, len(packable), MAX_PIDS_PER_REQUEST):
            group = packable[i : i + MAX_PIDS_PER_REQUEST]
            if len(group) == 1:
                packed.append(group[0])
                continue
            pids = tuple(r.pids[0] for r in group)
            packed.append(
                _Request(
                    mode=MODE_OBD,
                    key=("packed",) + pids,
                    pids=pids,
                    channels=[c for r in group for c in r.channels],
                )
            )
        return packed + rest

    # --- one request ----------------------------------------------------------

    def _run(self, req: _Request, values: list) -> bool:
        """Send *req*, decode its reply into *values*; False if it failed."""
        try:
            if req.mode == MODE_OBD and len(req.pids) > 1:
                per_pid = self._read_packed(req.pids)
                if per_pid is None:
                    return self._unpack_fallback(req, values)
                for ch in req.channels:
                    self._store(ch, per_pid[ch.ident], values)
            else:
                data = self._read_one(req)
                for ch in req.channels:
                    self._store(ch, data, values)
            self._last_ok = self._clock()
            return True
        except NegativeResponseError as exc:
            self._last_ok = self._clock()  # it answered: the link is alive
            if exc.service_id is not None and exc.service_id != req.mode:
                # A late refusal of an earlier request, not an answer to this
                # one: never drop a channel (or packing) over it.
                self._count_failure(req)
                return False
            if req.mode == MODE_OBD and len(req.pids) > 1:
                # One of the PIDs is refused: find out which one by asking alone.
                return self._unpack_fallback(req, values)
            if exc.nrc in _UNSUPPORTED_NRCS or (
                req.mode == MODE_MEM and exc.nrc == NRC_CONDITIONS_NOT_CORRECT
            ):
                why = _unsupported_reason(req.mode, exc.nrc)
                for ch in req.channels:
                    if ch.name not in self.unsupported:
                        self.unsupported[ch.name] = why
                        logger.warning("Datalog channel %s dropped: %s", ch.name, why)
            else:
                self._count_failure(req)
            return False
        except UDSTimeoutError:
            self._count_failure(req)
            # A late reply may still arrive; on a link that buffers replies,
            # drop it now so it can't be read as the next request's answer.
            self._uds.flush()
            return False
        except UDSError as exc:
            logger.debug("Datalog %s: bad reply (%s)", req.key, exc)
            self._count_failure(req)
            return False

    def _unpack_fallback(self, req: _Request, values: list) -> bool:
        logger.info(
            "Datalog: the ECU did not answer packed OBD requests as expected; "
            "asking for one PID at a time from now on"
        )
        self._pack = False
        ok = False
        for pid in req.pids:
            single = _Request(
                mode=MODE_OBD,
                key=(MODE_OBD, pid),
                pids=(pid,),
                channels=[c for c in req.channels if c.ident == pid],
            )
            ok = self._run(single, values) or ok
        return ok

    def _read_one(self, req: _Request) -> bytes:
        uds = self._uds
        if req.mode == MODE_OBD:
            pid = req.pids[0]
            payload = uds.send_request(
                SID_OBD_CURRENT_DATA,
                bytes([pid]),
                timeout=REQUEST_TIMEOUT_MS,
                pending_max=REQUEST_BUDGET_MS,
                quiet_nrcs=_QUIET_NRCS,
                accept=lambda p: p[:1] == bytes([pid]),
            )
            return payload[1:]
        if req.mode == MODE_DID:
            echo = req.key[1].to_bytes(2, "big")
            payload = uds.send_request(
                SID_READ_DATA_BY_IDENTIFIER,
                echo,
                timeout=REQUEST_TIMEOUT_MS,
                pending_max=REQUEST_BUDGET_MS,
                quiet_nrcs=_QUIET_NRCS,
                accept=lambda p: p[:2] == echo,
            )
            return payload[2:]
        # Mode 0x23 replies carry no echo; the length check is the only guard
        # against a late reply to an earlier read.
        _, address, size = req.key
        data = uds.send_request(
            0x23,
            address.to_bytes(4, "big") + size.to_bytes(2, "big"),
            timeout=REQUEST_TIMEOUT_MS,
            pending_max=REQUEST_BUDGET_MS,
            quiet_nrcs=_QUIET_NRCS,
            accept=lambda p: len(p) == size,
        )
        return data

    def _read_packed(self, pids: tuple) -> Optional[dict]:
        """One request for several PIDs; {pid: data} or None if unsplittable."""
        payload = self._uds.send_request(
            SID_OBD_CURRENT_DATA,
            bytes(pids),
            timeout=REQUEST_TIMEOUT_MS,
            pending_max=REQUEST_BUDGET_MS,
            quiet_nrcs=_QUIET_NRCS,
            accept=lambda p: p[:1] in [bytes([x]) for x in pids],
        )
        out: dict[int, bytes] = {}
        i = 0
        while i < len(payload):
            pid = payload[i]
            length = OBD_PID_LENGTHS.get(pid)
            if pid not in pids or length is None or i + 1 + length > len(payload):
                return None
            out[pid] = payload[i + 1 : i + 1 + length]
            i += 1 + length
        if set(out) != set(pids):
            return None
        return out

    def _store(self, ch: Channel, data: bytes, values: list) -> None:
        try:
            values[self._index[id(ch)]] = ch.decode(data)
        except ValueError as exc:  # ChannelError: reply shorter than the channel
            logger.debug("Datalog %s: %s", ch.name, exc)
            self.failures[ch.name] = self.failures.get(ch.name, 0) + 1

    def _count_failure(self, req: _Request) -> None:
        for ch in req.channels:
            self.failures[ch.name] = self.failures.get(ch.name, 0) + 1


def _unsupported_reason(mode: int, nrc: int) -> str:
    if mode == MODE_MEM and nrc == NRC_CONDITIONS_NOT_CORRECT:
        return "the ECU refuses memory reads (mode 0x23 needs the patched ROM)"
    if nrc == NRC_REQUEST_OUT_OF_RANGE:
        return "the ECU doesn't know this identifier"
    return f"the ECU doesn't support this request (NRC 0x{nrc:02X})"


def format_row(elapsed_s: float, values: list) -> str:
    """One CSV row: the time in seconds, then each value (empty when None)."""
    cells = [f"{elapsed_s:.3f}"]
    for v in values:
        if v is None:
            cells.append("")
        elif float(v).is_integer() and abs(v) < 1e15:
            cells.append(str(int(v)))  # raw 32-bit reads stay exact
        else:
            cells.append(f"{v:.6g}" if abs(v) < 1e6 else f"{v:.10g}")
    return ",".join(cells)
