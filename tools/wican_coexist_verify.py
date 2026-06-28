#!/usr/bin/env python3
"""Read-only firmware-level verification of the no-reboot coexistence build (#36).

Run AFTER OTA-ing the coexistence firmware, BEFORE any ECU flash. Touches the
adapter only (and, on the optional ping, the ECU's UDS layer via a no-CAN version
sentinel) — it never writes the ECU, so it is safe to run against the live bench.

Checks, in order (each independent, all non-destructive):
  1. Dedicated SLCAN port 35001 answers a version ping with NCFRv>=6
     -> proves 36.A: the always-on port + DEV_SLCAN_PORT dispatch are live, and the
        host can detect the coexist build WITHOUT a protocol-switch reboot.
  2. GET /datalog -> POST pause -> GET (parked) -> POST resume -> GET (running)
     -> proves 36.C: the REST coordination endpoint drives DATALOG_PARK_BIT and the
        firmware reports state truthfully. No ECU contact at all.

Usage:
    python tools/wican_coexist_verify.py [--host 192.168.1.169]
Exit code 0 iff every check passes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.ecu.constants import (
    COEXIST_MIN_FW_REV,
    WICAN_DEDICATED_SLCAN_PORT,
)  # noqa: E402
from src.ecu.wican_config import WiCANDatalogClient  # noqa: E402
from src.ecu.wican_sd_flash import _parse_fw_rev  # noqa: E402
from src.ecu.wican_transport import WiCANTransport  # noqa: E402


def _ok(label: str, passed: bool, detail: str = "") -> bool:
    mark = "PASS" if passed else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""), flush=True)
    return passed


def check_dedicated_port(host: str) -> bool:
    print(
        f"[1] Dedicated SLCAN port {WICAN_DEDICATED_SLCAN_PORT} version ping",
        flush=True,
    )
    t = WiCANTransport(host, WICAN_DEDICATED_SLCAN_PORT)
    try:
        t.open()
    except Exception as exc:  # noqa: BLE001
        return _ok(
            f"connect :{WICAN_DEDICATED_SLCAN_PORT}",
            False,
            f"{type(exc).__name__}: {exc} (is the coexist firmware flashed?)",
        )
    try:
        marker = t.version_ping(window_ms=3000)
        rev = _parse_fw_rev(marker)
        return _ok(
            "version ping",
            rev is not None and rev >= COEXIST_MIN_FW_REV,
            f"marker={marker!r} rev={rev} (need >= {COEXIST_MIN_FW_REV})",
        )
    finally:
        try:
            t.close()
        except Exception:
            pass


def check_datalog(host: str) -> bool:
    print("[2] REST /datalog pause/resume coordination", flush=True)
    c = WiCANDatalogClient(host)
    s0 = c.get_state()
    if not _ok("GET /datalog", s0 is not None, f"state={s0}"):
        return False
    paused = c.pause()
    ok_pause = _ok(
        "POST pause",
        bool(paused) and paused.get("datalog_parked") is True,
        f"-> {paused}",
    )
    s1 = c.get_state()
    ok_state = _ok(
        "GET shows parked", bool(s1) and s1.get("datalog_parked") is True, f"-> {s1}"
    )
    resumed = c.resume()
    ok_resume = _ok(
        "POST resume",
        bool(resumed) and resumed.get("datalog_parked") is False,
        f"-> {resumed}",
    )
    s2 = c.get_state()
    ok_back = _ok(
        "GET shows running", bool(s2) and s2.get("datalog_parked") is False, f"-> {s2}"
    )
    # Leave the datalogger RUNNING regardless (resume already attempted above).
    c.clear_stopped()
    return ok_pause and ok_state and ok_resume and ok_back


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="192.168.1.169")
    args = ap.parse_args(argv)

    print(
        f"=== #36 coexistence verify against {args.host} (READ-ONLY, no ECU write) ==="
    )
    results = [check_dedicated_port(args.host), check_datalog(args.host)]
    passed = all(results)
    print()
    print(
        "=== RESULT:",
        (
            "ALL PASS — firmware-level coexistence verified"
            if passed
            else "FAILURES — do NOT proceed to a flash"
        ),
        "===",
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
