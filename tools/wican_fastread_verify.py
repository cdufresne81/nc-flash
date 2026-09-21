#!/usr/bin/env python3
"""Fast-read an arbitrary ROM range via the firmware and byte-compare an oracle.

Unlike a raw socket capture, ``transport.fast_read`` returns exactly ``length``
bytes of pure ROM (the firmware suspends CAN forwarding for the duration), so
this is a clean way to test whether a *specific* region reads correctly —
e.g. the response-pending region at 0xD8400 — in isolation from a full read.

    python tools/wican_fastread_verify.py --start 0xD8400 --len 0x8000
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_REPO_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "tools"))

from src.ecu.protocol import UDSConnection  # noqa: E402
from src.ecu.constants import WICAN_DEDICATED_SLCAN_PORT  # noqa: E402
from src.ecu.wican_config import WiCANDatalogClient  # noqa: E402
from src.ecu.wican_transport import WiCANError, WiCANTransport  # noqa: E402
from wican_bench_read import _authenticate_for_raw_reads  # noqa: E402


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="192.168.1.169")
    p.add_argument("--port", type=int, default=WICAN_DEDICATED_SLCAN_PORT)
    p.add_argument("--start", type=lambda x: int(x, 0), default=0xD8400)
    p.add_argument("--len", dest="length", type=lambda x: int(x, 0), default=0x8000)
    p.add_argument("--reference", type=Path, default=Path("wican_stmin0_full.bin"))
    p.add_argument("--timeout-ms", type=int, default=60000)
    args = p.parse_args(argv)

    print(
        f"[VERIFY] fast-read 0x{args.start:06X}..0x{args.start + args.length:06X} "
        f"({args.length} bytes)"
    )
    # Reserve the CAN bus for the whole run. On the coexistence port the
    # datalogger is the sole TWAI consumer and EATS the ECU's UDS replies, so the
    # security-access auth below would time out and look exactly like a bricked
    # ECU. Soft-degrading: a device with no /datalog endpoint just carries on.
    try:
        with WiCANDatalogClient(args.host).reserved():
            transport = WiCANTransport(args.host, args.port)
            transport.open()
            try:
                uds = UDSConnection(transport)
                _authenticate_for_raw_reads(uds)
                print("[VERIFY] authenticated; reading ...")
                t0 = time.monotonic()
                data = transport.fast_read(
                    args.start, args.length, timeout_ms=args.timeout_ms
                )
                dt = time.monotonic() - t0
            finally:
                transport.close()
    except WiCANError as exc:
        print(f"[VERIFY] FAILED: {exc}")
        return 2

    nblk = (args.length + 0x3FF) // 0x400
    print(
        f"[VERIFY] read {len(data)} bytes in {dt:.1f}s "
        f"({nblk} blocks, {dt / nblk * 1000:.0f} ms/block)"
    )

    if not args.reference.exists():
        print(f"[VERIFY] no reference {args.reference}; skipping compare")
        return 0
    ref = args.reference.read_bytes()[args.start : args.start + args.length]
    if data == ref:
        print(
            "[VERIFY] RESULT: BYTE-IDENTICAL to oracle OK -- fix works for this region"
        )
        return 0
    diffs = [i for i in range(min(len(data), len(ref))) if data[i] != ref[i]]
    first = diffs[0] if diffs else -1
    print(
        f"[VERIFY] RESULT: MISMATCH -- {len(diffs)}/{len(ref)} bytes differ; "
        f"first diff at +{first} (abs 0x{args.start + first:06X})"
    )
    if first >= 0:
        print(f"         read[+{first}]={data[first:first+16].hex()}")
        print(f"         ref [+{first}]={ref[first:first+16].hex()}")
        # Does the read match the oracle shifted by a block/frame? (desync test)
        ref_full = args.reference.read_bytes()
        for shift in (0x400, -0x400, 8, -8, 1025):
            seg_ref = ref_full[args.start + shift : args.start + shift + len(data)]
            if len(seg_ref) == len(data) and data == seg_ref:
                print(f"         >>> read EQUALS oracle shifted by {shift:+d} bytes")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
