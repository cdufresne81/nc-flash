"""Quick raw-CAN sniff over WiCAN SLCAN: is the ECU broadcasting anything?

Opens the channel through the real WiCANTransport (so it is primed exactly as
NC Flash opens it), then reads the raw socket for a few seconds and tallies
EVERY CAN id seen — not just the ECU reply id. If the ECU application is
running, the powertrain bus carries periodic broadcast frames; total silence
means the ECU is not running its app (bootloader/unpowered).
"""

import argparse
import select
import time
from collections import Counter

from src.ecu.wican_transport import WiCANTransport


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.1.169")
    ap.add_argument("--port", type=int, default=35000)
    ap.add_argument("--seconds", type=float, default=3.0)
    args = ap.parse_args()

    t = WiCANTransport(host=args.host, port=args.port)
    t.open()
    sock = t._sock
    stream = t._stream

    seen = Counter()
    sample = {}
    deadline = time.monotonic() + args.seconds
    total = 0
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        readable, _, _ = select.select([sock], [], [], remaining)
        if not readable:
            continue
        chunk = sock.recv(4096)
        if chunk == b"":
            print("socket closed by peer")
            break
        for can_id, data in stream.feed(chunk):
            seen[can_id] += 1
            total += 1
            sample.setdefault(can_id, data.hex())

    t.close()

    print(
        f"\n=== sniff done: {total} frames over {args.seconds:.1f}s, "
        f"{len(seen)} distinct ids ==="
    )
    for can_id, n in sorted(seen.items()):
        print(f"  0x{can_id:03X}  x{n:<5}  sample={sample[can_id]}")
    if total == 0:
        print("  (BUS SILENT — no CAN frames at all)")


if __name__ == "__main__":
    main()
