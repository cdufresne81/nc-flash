#!/usr/bin/env python3
"""Probe which WiCAN fast-read firmware is live.

Thin CLI around :meth:`WiCANTransport.version_ping`: sends the fast-read version
sentinel and reports the ``NCFRv<rev>`` build marker the firmware answers with
(without touching CAN). This removes the "did the OTA actually take?" ambiguity
before a read/flash. Old/stock firmware has no sentinel handling, so the marker
never appears and this reports OLD/UNKNOWN.

    python tools/wican_fw_ping.py [--host H] [--port P]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.ecu.wican_config import WiCANConfigError, WiCANConfigurator  # noqa: E402
from src.ecu.wican_transport import WiCANError, WiCANTransport  # noqa: E402


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="192.168.1.169")
    p.add_argument("--port", type=int, default=35000)
    p.add_argument("--http-port", type=int, default=80)
    p.add_argument("--window-ms", type=int, default=3000)
    args = p.parse_args(argv)

    print(f"[PING] {args.host}:{args.port} — probing fast-read firmware version")
    try:
        cfg = WiCANConfigurator(args.host, http_port=args.http_port)
        with cfg.slcan_session():
            transport = WiCANTransport(args.host, args.port)
            transport.open()
            try:
                marker = transport.version_ping(window_ms=args.window_ms)
            finally:
                transport.close()
    except WiCANError as exc:
        print(f"[PING] link error: {exc}")
        return 2
    except WiCANConfigError as exc:
        print(f"[PING] config error: {exc}")
        return 2

    if marker:
        print(f"[RESULT] fast-read firmware live: {marker.decode('ascii', 'replace')}")
        return 0
    print("[RESULT] OLD/UNKNOWN firmware — version ping not answered")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
