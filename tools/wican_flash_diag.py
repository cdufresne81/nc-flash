#!/usr/bin/env python3
"""WiCAN PRO flash DIAGNOSTIC — instrument the OUTBOUND multi-frame TransferData.

Why this exists
---------------
A real FULL FLASH over the WiCAN (SLCAN-over-WiFi) failed: auth + security + the
single-frame ``RequestDownload`` all succeeded, but the very first ``TransferData``
(SID 0x36) block of the SBL timed out after 60 s ("Timed out waiting for response
to SID 0x36"). READ / RAM / DTC all work over the same link.

The asymmetry: a READ has the ECU *send* multi-frame data to us (the host has an
N_Cr fast-fail + idempotent retry to recover a dropped frame). A WRITE has the
host *send* a 1024-byte block to the ECU as an ISO-TP First Frame + ~146
Consecutive Frames. If one CF is dropped (or the ECU's RX overflows), the ECU's
reassembly never completes, it silently drops the block, and never answers — so
``send_message()`` returns fine and the receive loop times out.

The GUI log hides the ONE datum that decides the fix: the ECU's ISO-TP **Flow
Control** (block size + STmin) for the outbound transfer, and exactly which
Consecutive Frame the link loses. This tool captures both by wrapping the
ISO-TP frame I/O and driving the real flash preamble.

Safety
------
DEFAULT (diagnostic) mode drives: connect -> auth -> check flash counter ->
RequestDownload -> ONE-or-N TransferData SBL block(s), then STOPS. This is the
SBL phase (upload to RAM at 0x8000) — it is *pre-erase*: the program-data
transfer (which erases/writes flash) is NEVER reached, so the ECU's flash is not
modified. This is exactly where the real flash already failed, so it is no more
destructive than what already happened (and that was Tactrix-recoverable).

``--commit`` is RETIRED: the host-driven Option-A flash it drove was removed
(audit D4) in favour of the SD-staged, firmware-driven flash (``WiCANSdFlasher``).
This tool is now read-only bench instrumentation (probe / SBL send / frame tap).

Usage
-----
  # Diagnostic (safe, pre-erase) — capture the ECU's Flow Control + drop point:
  python tools/wican_flash_diag.py --rom <rom.bin> --auto-config

  # Send the full 6-block SBL (still pre-erase) for a fuller picture:
  python tools/wican_flash_diag.py --rom <rom.bin> --auto-config --sbl-blocks 6

(``--commit`` is retired — see Safety above. To actually flash over WiCAN, use
the app's SD-staged path or the wican_sd_* bench tools.)
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.ecu.constants import (  # noqa: E402
    BLOCK_SIZE,
    CAN_REQUEST_ID,
    CAN_RESPONSE_ID,
    ROM_FLASH_START_MIN,
    SID_TRANSFER_DATA,
    TIMEOUT_TRANSFER,
)
from src.ecu.exceptions import ECUError, SecureModuleNotAvailable  # noqa: E402
from src.ecu.flash_manager import FlashManager  # noqa: E402
from src.ecu.protocol import UDSConnection  # noqa: E402
from src.ecu.rom_utils import detect_vehicle_generation  # noqa: E402
from src.ecu.wican_config import WiCANConfigError, WiCANConfigurator  # noqa: E402
from src.ecu.wican_transport import WiCANError, WiCANTransport  # noqa: E402

try:
    from src.ecu._secure import get_sbl_data

    _SECURE = True
except ImportError:  # pragma: no cover - depends on private module presence
    _SECURE = False

logger = logging.getLogger("wican_flash_diag")


# --- ISO-TP frame-level instrumentation ------------------------------------


def _decode_pci(data: bytes) -> str:
    """Human-readable ISO-TP PCI summary for one CAN frame's payload."""
    if not data:
        return "EMPTY"
    t = (data[0] >> 4) & 0xF
    if t == 0x0:
        return f"SF   len={data[0] & 0xF}"
    if t == 0x1:
        ln = ((data[0] & 0xF) << 8) | (data[1] if len(data) > 1 else 0)
        return f"FF   len={ln}"
    if t == 0x2:
        return f"CF   seq={data[0] & 0xF}"
    if t == 0x3:
        status = {0: "CTS", 1: "WAIT", 2: "OVFL"}.get(
            data[0] & 0xF, f"0x{data[0]&0xF:X}"
        )
        bs = data[1] if len(data) > 1 else 0
        stmin = data[2] if len(data) > 2 else 0
        return f"FC   {status} BS={bs} STmin=0x{stmin:02X}"
    return f"?0x{data[0]:02X}"


class FrameTap:
    """Wrap an ``IsoTpSession``'s frame callables to record every CAN frame.

    Captures the prize datum — the ECU's Flow Control (BS/STmin) for an outbound
    multi-frame send — and counts Consecutive Frames so a 146-frame burst is
    summarized rather than dumped. Flow Control + First/Single frames are always
    printed; CFs only at the burst edges.
    """

    def __init__(self, session, verbose: bool = False):
        self._verbose = verbose
        self.last_fc: tuple[int, int, int] | None = None  # (status, bs, stmin)
        self.cf_sent = 0
        self.cf_recv = 0
        self.fc_seen = 0
        self._orig_send = session._send_frame
        self._orig_recv = session._recv_frame
        session._send_frame = self._send
        session._recv_frame = self._recv

    def _stamp(self) -> str:
        return time.strftime("%H:%M:%S")

    def _send(self, can_id: int, data: bytes) -> None:
        t = (data[0] >> 4) & 0xF if data else -1
        if t == 0x2:  # Consecutive Frame — count, print only edges
            self.cf_sent += 1
            if self._verbose or self.cf_sent <= 2:
                print(
                    f"    TX 0x{can_id:03X} {_decode_pci(data)}  (cf #{self.cf_sent})"
                )
        else:
            print(f"    TX 0x{can_id:03X} {_decode_pci(data)}")
        self._orig_send(can_id, data)

    def _recv(self, timeout_ms: int):
        frame = self._orig_recv(timeout_ms)
        if frame is None:
            return None
        can_id, data = frame
        t = (data[0] >> 4) & 0xF if data else -1
        if t == 0x3:  # Flow Control — THE PRIZE
            self.fc_seen += 1
            status = data[0] & 0xF
            bs = data[1] if len(data) > 1 else 0
            stmin = data[2] if len(data) > 2 else 0
            self.last_fc = (status, bs, stmin)
            print(f"    RX 0x{can_id:03X} {_decode_pci(data)}   <-- ECU flow control")
        elif t == 0x2:
            self.cf_recv += 1
            if self._verbose:
                print(f"    RX 0x{can_id:03X} {_decode_pci(data)}")
        else:
            print(f"    RX 0x{can_id:03X} {_decode_pci(data)}")
        return frame

    def block_report(self) -> None:
        print(
            f"    >> block summary: FC seen={self.fc_seen}, CFs sent={self.cf_sent}"
            + (
                f", ECU asked BS={self.last_fc[1]} STmin=0x{self.last_fc[2]:02X}"
                if self.last_fc
                else ", NO FLOW CONTROL RECEIVED"
            )
        )
        # reset per-block counters but keep last_fc
        self.cf_sent = 0
        self.cf_recv = 0
        self.fc_seen = 0


# --- diagnostic / commit drivers -------------------------------------------


def run_diag(transport, rom: bytes, sbl_blocks: int, resp_timeout_ms: int) -> int:
    """Drive the failing preamble with frame instrumentation; stop pre-erase."""
    if not _SECURE:
        print("[DIAG] FAILED — needs the private _secure module (auth + SBL).")
        return 4

    uds = UDSConnection(transport)
    tap = FrameTap(transport._session, verbose=False)

    fm = FlashManager()
    fm.use_uds(uds)

    print("\n[DIAG] Connect (borrowed session, Tester Present) ...")
    fm._connect(None)

    print("[DIAG] Authenticate (programming session + security access) ...")
    fm._authenticate(None)

    print("[DIAG] Check flash counter ...")
    uds.check_flash_counter()

    generation = detect_vehicle_generation(rom)
    sbl = get_sbl_data(ROM_FLASH_START_MIN, generation)
    print(f"[DIAG] Generation={generation}  SBL={len(sbl)} bytes")

    print("[DIAG] RequestDownload (addr=0x8000) ...")
    uds.request_download()

    total_blocks = (len(sbl) + BLOCK_SIZE - 1) // BLOCK_SIZE
    n = min(sbl_blocks, total_blocks) if sbl_blocks > 0 else total_blocks
    print(
        f"\n[DIAG] Sending {n}/{total_blocks} SBL TransferData block(s) of "
        f"{BLOCK_SIZE} bytes, response wait {resp_timeout_ms} ms.\n"
        f"       (This is the exact step that failed. STOPPING before program "
        f"transfer — flash is NOT erased.)\n"
    )

    for i in range(n):
        chunk = sbl[i * BLOCK_SIZE : (i + 1) * BLOCK_SIZE]
        print(f"  -- TransferData block {i + 1}/{n} ({len(chunk)} bytes) --")
        t0 = time.monotonic()
        try:
            uds.send_request(
                SID_TRANSFER_DATA,
                chunk,
                timeout=TIMEOUT_TRANSFER,
                pending_max=resp_timeout_ms,
            )
            dt = (time.monotonic() - t0) * 1000
            print(f"  ++ block {i + 1} ACKed in {dt:.0f} ms")
            tap.block_report()
        except Exception as exc:  # noqa: BLE001 - diagnostic surfaces everything
            dt = (time.monotonic() - t0) * 1000
            print(
                f"  !! block {i + 1} FAILED after {dt:.0f} ms: "
                f"{type(exc).__name__}: {exc}"
            )
            tap.block_report()
            _verdict(tap)
            return 1

    print("\n[DIAG] All requested SBL blocks ACKed (outbound multi-frame works!).")
    _verdict(tap)
    return 0


def _verdict(tap: FrameTap) -> None:
    print("\n" + "=" * 70)
    print("DIAGNOSIS")
    print("=" * 70)
    if tap.last_fc is None:
        print(
            "The ECU sent NO Flow Control after our First Frame. The outbound\n"
            "multi-frame send never even started — the FF itself didn't reach the\n"
            "ECU, or its FC was lost. (Different from the dropped-CF hypothesis.)"
        )
        return
    status, bs, stmin = tap.last_fc
    print(
        f"ECU Flow Control: status={status} BS={bs} STmin=0x{stmin:02X}\n"
        f"  - BS={bs}: "
        + (
            "send all CFs without further FC"
            if bs == 0
            else f"wait for a new FC every {bs} CFs"
        )
        + "\n"
        f"  - STmin=0x{stmin:02X}: minimum separation the ECU wants between CFs.\n"
    )


def run_commit(transport, rom: bytes, archive_path: str | None) -> int:
    """RETIRED. The host-driven Option-A flash this instrumented no longer exists.

    ``WiCANFlasher`` lost its ``flash_rom``/``dynamic_flash`` (audit D4): the
    production flash is the SD-staged, firmware-driven path (``WiCANSdFlasher``),
    which does the per-block TransferData ON THE ESP32 over CAN — there is no
    host-side per-block send left for this frame tap to instrument. Use the
    read-only diagnostics here (probe / SBL send), or drive a real SD flash from
    the NC Flash UI / the SD bench tools.
    """
    print(
        "\n[COMMIT] Retired: the host-driven Option-A WiCAN flash was removed "
        "(D4). Use the SD-staged flash (WiCANSdFlasher) via the NC Flash UI; the "
        "read-only probe/SBL diagnostics in this tool still work."
    )
    return 2


# --- CLI -------------------------------------------------------------------


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="WiCAN flash diagnostic — instrument the outbound TransferData.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--host", default="192.168.1.169")
    p.add_argument("--port", type=int, default=35000)
    p.add_argument("--tx-id", type=lambda x: int(x, 0), default=CAN_REQUEST_ID)
    p.add_argument("--rx-id", type=lambda x: int(x, 0), default=CAN_RESPONSE_ID)
    p.add_argument(
        "--rom", type=Path, required=True, help="ROM .bin (source of the SBL blocks)"
    )
    p.add_argument(
        "--sbl-blocks",
        type=int,
        default=1,
        help="How many SBL blocks to send in diag mode (0=all 6). Default 1.",
    )
    p.add_argument(
        "--response-timeout-ms",
        type=int,
        default=10000,
        help="Per-block wait for the ECU's 0x36 response in diag mode (default 10s).",
    )
    p.add_argument(
        "--commit",
        action="store_true",
        help="RETIRED (audit D4): prints a retirement notice and exits 2. "
        "Flash over WiCAN via the app's SD-staged path instead.",
    )
    p.add_argument("--yes", action="store_true", help="No-op (was: confirm --commit).")
    p.add_argument(
        "--tx-stmin",
        type=int,
        default=None,
        help="Outbound CF pacing floor (ms) for our TransferData burst. Omit to use "
        "the WiCAN default (DEFAULT_TX_STMIN). 0 = no pacing (reproduce the failure).",
    )
    p.add_argument("--auto-config", action="store_true", help="HTTP-switch to slcan.")
    p.add_argument("--http-port", type=int, default=80)
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def _dispatch(args, transport) -> int:
    rom = args.rom.read_bytes()
    print(f"[ROM] {args.rom}  ({len(rom)} bytes)")
    if args.commit:
        # run_commit unconditionally prints the retirement notice and returns 2
        # — no --yes gating needed for something that never writes.
        return run_commit(transport, rom, archive_path=None)
    return run_diag(transport, rom, args.sbl_blocks, args.response_timeout_ms)


def _open_and_run(args) -> int:
    tx_kw = {} if args.tx_stmin is None else {"tx_stmin": args.tx_stmin}
    transport = WiCANTransport(
        args.host, args.port, tx_id=args.tx_id, rx_id=args.rx_id, **tx_kw
    )
    print(
        "[CFG] outbound CF pacing floor: "
        + (
            f"{args.tx_stmin} ms (override)"
            if args.tx_stmin is not None
            else "WiCAN default"
        )
    )
    try:
        print(f"\n[LINK] Opening {transport.description} ...")
        transport.open()
        print("[LINK] Channel up.")
    except WiCANError as exc:
        print(f"[LINK] FAILED to open: {exc}")
        return 2
    try:
        return _dispatch(args, transport)
    except SecureModuleNotAvailable:
        print("\n[ERROR] Private _secure module not available (auth/SBL).")
        return 4
    except ECUError as exc:
        print(f"\n[ERROR] {type(exc).__name__}: {exc}")
        return 1
    finally:
        try:
            transport.close()
            print("[LINK] Closed.")
        except Exception:
            pass


def main(argv) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    print("=" * 70)
    print("WiCAN PRO flash DIAGNOSTIC")
    print(
        f"  target: {args.host}:{args.port}  mode: "
        f"{'COMMIT (retired — will refuse)' if args.commit else 'diag (pre-erase)'}"
    )
    print("=" * 70)

    if args.auto_config:
        configurator = WiCANConfigurator(args.host, http_port=args.http_port)
        try:
            print(f"\n[AUTO-CONFIG] Switching {args.host} -> slcan ...")
            with configurator.slcan_session() as previous:
                print(f"[AUTO-CONFIG] Device in slcan (was {previous!r}).")
                rc = _open_and_run(args)
            if previous != "slcan":
                print(f"[AUTO-CONFIG] Restored to {previous!r}.")
            return rc
        except WiCANConfigError as exc:
            print(f"[AUTO-CONFIG] FAILED: {exc}")
            return 6
    return _open_and_run(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
