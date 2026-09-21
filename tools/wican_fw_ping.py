#!/usr/bin/env python3
"""Probe which WiCAN fast-read firmware is live.

Thin CLI around :meth:`WiCANTransport.version_ping`: sends the fast-read version
sentinel and reports the ``NCFRv<rev>`` build marker the firmware answers with
(without touching CAN). This removes the "did the OTA actually take?" ambiguity
before a read/flash. Old/stock firmware has no sentinel handling, so the marker
never appears and this reports OLD/UNKNOWN.

Talks to the adapter only: opens the socket WITHOUT the SLCAN channel bring-up
(no ``C``/``S6``/``O``, no prime frame), so it puts nothing on the CAN bus and
cannot disturb a running datalog trip. Needs no bus reservation.

    python tools/wican_fw_ping.py [--host H] [--port P]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.ecu.constants import WICAN_DEDICATED_SLCAN_PORT  # noqa: E402
from src.ecu.wican_transport import WiCANError, WiCANTransport  # noqa: E402


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="192.168.1.169")
    p.add_argument("--port", type=int, default=WICAN_DEDICATED_SLCAN_PORT)
    p.add_argument("--window-ms", type=int, default=3000)
    args = p.parse_args(argv)

    print(f"[PING] {args.host}:{args.port} — probing fast-read firmware version")
    try:
        transport = WiCANTransport(args.host, args.port)
        # Socket only — a full open() would disable the shared CAN peripheral
        # and inject a TesterPresent, which is exactly what this must not do.
        transport.open_socket_only()
        try:
            marker = transport.version_ping(window_ms=args.window_ms)
        finally:
            transport.close()
    except WiCANError as exc:
        print(f"[PING] link error: {exc}")
        return 2

    if marker:
        print(f"[RESULT] fast-read firmware live: {marker.decode('ascii', 'replace')}")
        return 0
    print("[RESULT] OLD/UNKNOWN firmware — version ping not answered")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
