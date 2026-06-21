#!/usr/bin/env python3
"""
WiCAN PRO bench — ECU diagnostic functions (RAM scan, read/clear DTC).

Hardware-confirm vehicle for **Part A** of the WiCAN ECU-functions goal
(`.claude/plans/wican-ecu-functions-goal.md`). It drives the SAME
``WiCANTransport`` + ``FlashManager`` seam NC Flash itself uses — over SLCAN
/ TCP + Python ISO-TP — so a green run here is direct evidence the UI path
(goal 3) will work. Nothing here is WiCAN-only logic: it borrows a WiCAN
``UDSConnection`` into ``FlashManager`` exactly as the J2534 path does.

  MODES (pick one):
    --scan-ram    Authenticate, then dump 48 KB of ECU RAM (0xFFFF0000),
                  save it, and print a sanity summary. Needs the private
                  _secure module (security access), like a ROM read.
    --read-dtc    Read stored DTCs and pretty-print them. No auth needed.
    --clear-dtc   Read -> clear -> re-read DTCs to prove the clear took.
                  *** MUTATES ECU STATE *** — requires --yes to proceed.

  PREREQUISITES on the WiCAN PRO (web UI): protocol "slcan", bitrate 500K
  (S6), "monitoring" enabled, ignition ON. Or pass --auto-config to flip the
  device to slcan over HTTP and restore your previous protocol on exit.

  USAGE:
    python tools/wican_bench_ecu.py --host 192.168.1.169 --port 35000 --read-dtc
    python tools/wican_bench_ecu.py --host 192.168.1.169 --port 35000 --scan-ram \\
        --out wican_ram.bin
    python tools/wican_bench_ecu.py --host 192.168.1.169 --port 35000 --clear-dtc --yes

  READ RAM and READ DTC are non-destructive (idempotent reads). CLEAR DTC
  erases stored trouble codes — a standard, benign diagnostic write, but a
  write: confirm with the vehicle owner first. This tool NEVER flashes.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

# Make the repo's `src` package importable when run as `python tools/...`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.ecu.constants import (  # noqa: E402
    CAN_REQUEST_ID,
    CAN_RESPONSE_ID,
)
from src.ecu.exceptions import (  # noqa: E402
    ECUError,
    SecureModuleNotAvailable,
)
from src.ecu.flash_manager import FlashManager  # noqa: E402
from src.ecu.protocol import UDSConnection  # noqa: E402
from src.ecu.wican_config import (  # noqa: E402
    WiCANConfigError,
    WiCANConfigurator,
)
from src.ecu.wican_transport import (  # noqa: E402
    WiCANError,
    WiCANTransport,
)

logger = logging.getLogger("wican_bench_ecu")


# --- pure, hardware-free helpers (unit-tested) -----------------------------


def summarize_ram(ram: bytes) -> dict:
    """Reduce a RAM dump to a sanity summary (pure; no I/O).

    RAM is volatile, so there is no byte-perfect oracle to compare against.
    Instead we sanity-check that the dump looks like *real* memory and not an
    all-zero / all-0xFF "the read silently failed" pattern: count non-zero
    bytes, distinct byte values, and flag the degenerate uniform cases.
    """
    total = len(ram)
    nonzero = sum(1 for b in ram if b != 0)
    distinct = len(set(ram))
    return {
        "bytes": total,
        "nonzero": nonzero,
        "nonzero_pct": (100.0 * nonzero / total) if total else 0.0,
        "distinct_values": distinct,
        # Plausible memory has a spread of values and is neither all-0x00 nor
        # all-0xFF. A single distinct value almost always means a failed read.
        "looks_plausible": distinct > 1 and 0 < nonzero < total,
    }


def format_dtc_lines(dtcs: list) -> list[str]:
    """Format a list of DTC objects into de-duplicated display lines (pure).

    ``FlashManager.read_dtcs`` returns the raw list (with possible repeats);
    this de-dupes by code, preserving first-seen order, and renders each as
    ``CODE  status=0xNN  description``.
    """
    seen: set[int] = set()
    lines: list[str] = []
    for d in dtcs:
        if d.code in seen:
            continue
        seen.add(d.code)
        lines.append(f"  {d.formatted}  status=0x{d.status:02X}  {d.description}")
    return lines


def clear_succeeded(before: list, after: list) -> bool:
    """True iff the clear left strictly fewer (ideally zero) unique DTCs (pure).

    A successful clear empties the stored codes; we accept "strictly fewer
    unique codes than before" as success too, since a hard fault can re-set
    immediately (engine running) yet the clear still worked.
    """
    before_codes = {d.code for d in before}
    after_codes = {d.code for d in after}
    return len(after_codes) == 0 or len(after_codes) < len(before_codes)


# --- mode implementations (drive the real FlashManager/WiCAN seam) ---------


def _progress_printer():
    """Return a FlashManager progress callback that updates one terminal line."""
    last_len = 0

    def cb(progress) -> None:
        nonlocal last_len
        line = f"  [{progress.percent:5.1f}%] {progress.message}"
        pad = max(0, last_len - len(line))
        sys.stdout.write("\r" + line + " " * pad)
        sys.stdout.flush()
        last_len = len(line)

    return cb


def run_scan_ram(uds: UDSConnection, out_path: Path) -> int:
    """Authenticate and dump ECU RAM over WiCAN; save + sanity-check it."""
    fm = FlashManager()
    # scan_ram() authenticates over self._uds, so borrow the WiCAN connection
    # in first (this also makes _connect() a no-op Tester Present, never J2534).
    fm.use_uds(uds)

    print("\n[RAM] Authenticating + scanning 48 KB RAM (0xFFFF0000)...")
    try:
        ram = fm.scan_ram(progress_cb=_progress_printer())
    except SecureModuleNotAvailable:
        sys.stdout.write("\n")
        print(
            "[RAM] FAILED — RAM scan needs the private _secure module (security "
            "access), the same one the app uses to read/flash. Install it, or use "
            "--read-dtc which needs no auth."
        )
        return 4
    sys.stdout.write("\n")

    summary = summarize_ram(bytes(ram))
    out_path.write_bytes(bytes(ram))
    print(
        f"[RAM] {summary['bytes']} bytes  non-zero={summary['nonzero']} "
        f"({summary['nonzero_pct']:.1f}%)  distinct values={summary['distinct_values']}"
    )
    if summary["looks_plausible"]:
        print(
            f"[RAM] RESULT: OK — dump looks like real memory. Saved to {out_path}. ✅"
        )
        return 0
    print(
        "[RAM] RESULT: SUSPECT — dump is uniform (all-zero / all-0xFF / single "
        "value), which usually means the read did not really land. ⚠️"
    )
    return 5


def run_read_dtc(uds: UDSConnection) -> int:
    """Read and print stored DTCs over WiCAN (no auth needed)."""
    fm = FlashManager()
    print("\n[DTC] Tester Present + reading stored DTCs...")
    uds.tester_present()
    dtcs = fm.read_dtcs(uds=uds)
    lines = format_dtc_lines(dtcs)
    if not lines:
        print("[DTC] RESULT: OK — no stored DTCs (clean ECU or already cleared). ✅")
        return 0
    print(f"[DTC] RESULT: OK — {len(lines)} unique DTC(s): ✅")
    for line in lines:
        print(line)
    return 0


def run_clear_dtc(uds: UDSConnection, confirmed: bool) -> int:
    """Read -> clear -> re-read DTCs over WiCAN to prove the clear took.

    Clearing DTCs mutates ECU state, so it is gated behind an explicit --yes.
    """
    if not confirmed:
        print(
            "\n[CLEAR] REFUSED — clearing DTCs mutates ECU state. Re-run with --yes "
            "once the vehicle owner has confirmed. (READ DTC / RAM are safe; clear "
            "is a benign but real write.)"
        )
        return 2

    fm = FlashManager()
    print("\n[CLEAR] Tester Present + reading DTCs before clear...")
    uds.tester_present()
    before = fm.read_dtcs(uds=uds)
    before_lines = format_dtc_lines(before)
    print(f"[CLEAR] before: {len(before_lines)} unique DTC(s)")
    for line in before_lines:
        print(line)

    print("[CLEAR] Clearing DTCs...")
    fm.clear_dtcs(uds=uds)

    print("[CLEAR] Re-reading DTCs after clear...")
    after = fm.read_dtcs(uds=uds)
    after_lines = format_dtc_lines(after)
    print(f"[CLEAR] after: {len(after_lines)} unique DTC(s)")
    for line in after_lines:
        print(line)

    if clear_succeeded(before, after):
        print("[CLEAR] RESULT: OK — clear took (DTCs gone or reduced). ✅")
        return 0
    print(
        "[CLEAR] RESULT: SUSPECT — DTC set did not shrink. A hard fault may be "
        "re-setting codes immediately (engine running), or the clear was rejected. ⚠️"
    )
    return 5


# --- CLI plumbing ----------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="WiCAN PRO bench — ECU diagnostic functions (RAM/DTC) over SLCAN.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--host", default="192.168.1.169", help="WiCAN IP")
    p.add_argument(
        "--port", type=int, default=35000, help="SLCAN TCP port (PRO often 35000)"
    )
    p.add_argument(
        "--tx-id",
        type=lambda x: int(x, 0),
        default=CAN_REQUEST_ID,
        help="Tester CAN ID (default 0x7E0)",
    )
    p.add_argument(
        "--rx-id",
        type=lambda x: int(x, 0),
        default=CAN_RESPONSE_ID,
        help="ECU CAN ID (default 0x7E8)",
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--scan-ram", action="store_true", help="Dump 48 KB ECU RAM")
    mode.add_argument("--read-dtc", action="store_true", help="Read stored DTCs")
    mode.add_argument(
        "--clear-dtc",
        action="store_true",
        help="Clear DTCs (mutates state; needs --yes)",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the state-mutating --clear-dtc (required for it)",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path(f"wican_ram_{time.strftime('%Y%m%d_%H%M%S')}.bin"),
        help="Where to save the RAM dump (--scan-ram)",
    )
    p.add_argument(
        "--connect-timeout-ms", type=int, default=5000, help="TCP connect timeout"
    )
    p.add_argument(
        "--auto-config",
        action="store_true",
        help=(
            "Flip the WiCAN HTTP-config protocol to 'slcan' before opening the link "
            "and restore your previous protocol afterwards. OFF by default."
        ),
    )
    p.add_argument(
        "--http-port",
        type=int,
        default=80,
        help="WiCAN web/HTTP-config port for --auto-config (default 80)",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    # UTF-8 console so status glyphs can't raise UnicodeEncodeError on cp1252.
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

    mode = "scan-ram" if args.scan_ram else "read-dtc" if args.read_dtc else "clear-dtc"
    print("=" * 70)
    print("WiCAN PRO bench — ECU functions")
    print(
        f"  target: {args.host}:{args.port}  tx=0x{args.tx_id:03X} rx=0x{args.rx_id:03X}"
    )
    print(f"  mode:   {mode}")
    print("  REMINDER: 'monitoring' + S6/500K + right port in the WiCAN web UI,")
    print("            ignition ON.")
    print("=" * 70)

    if args.auto_config:
        configurator = WiCANConfigurator(args.host, http_port=args.http_port)
        try:
            print(f"\n[AUTO-CONFIG] Switching {args.host} HTTP protocol -> slcan ...")
            with configurator.slcan_session() as previous_protocol:
                print(
                    f"[AUTO-CONFIG] Device in slcan mode (was {previous_protocol!r})."
                )
                rc = _run(args)
            if previous_protocol != "slcan":
                print(f"[AUTO-CONFIG] Device restored to {previous_protocol!r}.")
            return rc
        except WiCANConfigError as exc:
            print(f"[AUTO-CONFIG] FAILED: {exc}")
            return 6

    return _run(args)


def _run(args: argparse.Namespace) -> int:
    """Open the WiCAN link and dispatch the selected mode."""
    transport = WiCANTransport(
        args.host,
        args.port,
        tx_id=args.tx_id,
        rx_id=args.rx_id,
        connect_timeout_ms=args.connect_timeout_ms,
    )

    try:
        print(f"\n[LINK] Opening {transport.description} ...")
        transport.open()
        print("[LINK] Channel up (C/S6/O acked).")
    except WiCANError as exc:
        print(f"[LINK] FAILED to open: {exc}")
        print(
            "       Check: WiCAN reachable, correct --port, 'monitoring' enabled, "
            "bitrate S6/500K."
        )
        return 2

    try:
        uds = UDSConnection(transport)
        if args.scan_ram:
            return run_scan_ram(uds, args.out)
        if args.read_dtc:
            return run_read_dtc(uds)
        return run_clear_dtc(uds, args.yes)
    except KeyboardInterrupt:
        print("\nAborted by user.")
        return 130
    except ECUError as exc:
        print(f"\n[ERROR] {type(exc).__name__}: {exc}")
        return 1
    finally:
        try:
            transport.close()
            print("[LINK] Closed.")
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
