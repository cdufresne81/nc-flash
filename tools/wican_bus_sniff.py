"""Quick raw-CAN sniff over WiCAN SLCAN: is the ECU broadcasting anything?

Opens the channel through the real WiCANTransport (so it is primed exactly as
NC Flash opens it), then reads the raw socket for a few seconds and tallies
EVERY CAN id seen — not just the ECU reply id. If the ECU application is
running, the powertrain bus carries periodic broadcast frames; total silence
means the ECU is not running its app (bootloader/unpowered).

RESERVES the bus for the sniff window, which is counter-intuitive for a listener
but required: on the coexistence port frames only reach us via the firmware's
RX-forward, which runs when the host HAS reserved the bus and no flash is
active. An unreserved sniff sees silence on a perfectly healthy chattering bus
and reports a false "ECU not running". Reserving parks the datalogger's own
queries; the ECU's periodic broadcasts keep flowing, and those are what we count.

Not passive at the adapter level either: opening the channel primes it with one
TesterPresent frame.
"""

import argparse
import select
import sys
import time
from collections import Counter
from pathlib import Path

# Make the repo's `src` package importable when run as `python tools/...`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.ecu.wican_transport import WiCANTransport  # noqa: E402
from src.ecu.constants import WICAN_DEDICATED_SLCAN_PORT  # noqa: E402
from src.ecu.wican_config import WiCANDatalogClient  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.1.169")
    ap.add_argument(
        "--port",
        type=int,
        default=WICAN_DEDICATED_SLCAN_PORT,
        help="SLCAN TCP port (the firmware's fixed coexistence port).",
    )
    ap.add_argument("--seconds", type=float, default=3.0)
    args = ap.parse_args()

    # Reserve the bus, or the RX-forward that carries frames to this port never
    # runs and a healthy chattering bus reads as silent (see module docstring).
    # Soft-degrading: a device with no /datalog endpoint just carries on.
    with WiCANDatalogClient(args.host).reserved():
        _sniff(args)


def _sniff(args):
    t = WiCANTransport(host=args.host, port=args.port)
    t.open()
    try:
        _tally(t, args)
    finally:
        t.close()


def _tally(t, args):
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
