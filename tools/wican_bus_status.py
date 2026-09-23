"""Probe CAN data-link health to tell 'ECU unpowered' from 'ECU app not running'.

Any powered CAN node ACKs every valid frame at the data-link layer, even a
bootloader that ignores the application request. So:
  * If our transmits are ACKed -> a node has power (app OR bootloader).
  * If nothing ACKs -> TWAI controller accumulates TX errors / goes
    error-passive / bus-off -> the ECU is unpowered (ignition off).

Strategy: open the channel, fire several CAN frames, then read the SLCAN
status-flags byte (`F` command) which exposes error-warning / error-passive /
bus-off bits. Also report tx error behaviour heuristically.
"""

import argparse
import select
import sys
import time
from pathlib import Path

# Make the repo's `src` package importable when run as `python tools/...`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.ecu.wican_transport import WiCANTransport  # noqa: E402
from src.ecu.constants import WICAN_DEDICATED_SLCAN_PORT  # noqa: E402
from src.ecu.wican_config import WiCANDatalogClient  # noqa: E402

# SLCAN F-command status bits (CANable/LAWICEL convention).
BITS = {
    0: "RX-FIFO-full",
    1: "TX-FIFO-full",
    2: "ERROR-WARNING",
    3: "DATA-OVERRUN",
    5: "ERROR-PASSIVE",
    6: "ARBITRATION-LOST",
    7: "BUS-ERROR/BUS-OFF",
}
# Bits that mean no node is ACKing our frames (warn / error-passive / bus-off).
NO_ACK_BITS = (2, 5, 7)


def _read_for(sock, seconds):
    out = b""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        r, _, _ = select.select([sock], [], [], deadline - time.monotonic())
        if not r:
            break
        b = sock.recv(4096)
        if not b:
            break
        out += b
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.1.169")
    ap.add_argument(
        "--port",
        type=int,
        default=WICAN_DEDICATED_SLCAN_PORT,
        help="SLCAN TCP port (the firmware's fixed coexistence port).",
    )
    args = ap.parse_args()

    # Reserve the bus so the frames below are OURS. The signal this tool reads
    # is whether a node ACKs frames WE transmit, and the ECU node ACKs those
    # whether or not the datalogger is polling — but parking the poller stops its
    # traffic from muddying the status-flag read. Soft-degrading.
    with WiCANDatalogClient(args.host).reserved():
        _probe_bus(args)


def _probe_bus(args):
    t = WiCANTransport(host=args.host, port=args.port)
    t.open()  # primes the channel (sends a TesterPresent)
    try:
        _read_status(t)
    finally:
        # Close on the error path too, or a failed probe leaves the adapter's
        # only CAN listener holding a connection until the OS reaps it.
        t.close()


def _read_status(t):
    sock = t._sock

    # Fire a few raw TesterPresent frames to force TX attempts that need an ACK.
    for _ in range(5):
        try:
            t.send_message(bytes([0x3E, 0x80]), timeout_ms=300)
        except Exception as exc:  # noqa: BLE001
            print(f"send_message raised: {type(exc).__name__}: {exc}")
        time.sleep(0.05)

    # Drain anything pending, then query SLCAN status flags.
    _read_for(sock, 0.3)
    t._send_raw(b"F\r")
    resp = _read_for(sock, 0.6)
    print(f"raw F-response: {resp!r}")

    # Parse Fxx if present.
    txt = resp.decode("ascii", "replace")
    idx = txt.find("F")
    if idx >= 0 and len(txt) >= idx + 3:
        hexpart = txt[idx + 1 : idx + 3]
        try:
            flags = int(hexpart, 16)
            active = [name for bit, name in BITS.items() if flags & (1 << bit)]
            print(f"status flags = 0x{flags:02X} -> {active or ['(none set)']}")
            if any(flags & (1 << b) for b in NO_ACK_BITS):
                print(
                    ">>> ERROR/PASSIVE/BUS-OFF set: NO node is ACKing -> "
                    "ECU appears UNPOWERED (ignition OFF)."
                )
            else:
                print(
                    ">>> No error bits: frames are being ACKed -> "
                    "a node HAS POWER (app or bootloader)."
                )
        except ValueError:
            print("could not parse status hex")
    else:
        print("no parseable F-status returned (firmware may not implement 'F')")


if __name__ == "__main__":
    main()
