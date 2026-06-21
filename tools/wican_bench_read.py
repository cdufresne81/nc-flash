#!/usr/bin/env python3
"""
WiCAN PRO bench READ tool — standalone GO/NO-GO + throughput harness.

Non-destructive. Drives the new WiCANTransport (SLCAN over TCP + Python ISO-TP)
to:

  1. SMOKE TEST  — open the link and fire a burst of Tester Present round-trips,
                   reporting packet loss + latency. This is the cheap answer to
                   wican-fw #476 ("does SLCAN-over-TCP pass traffic at all?") and
                   mirrors the planned pre-flight link-quality gate. Needs no
                   security key / private _secure module.

  2. FULL READ   — read the entire 1 MB ROM, measure real throughput, save it,
                   and (optionally) byte-compare against a known-good J2534 dump
                   to prove the ISO-TP engine is byte-perfect (the build gate,
                   step 5). This phase authenticates (seed/key) and therefore
                   requires the private _secure module the app already uses.

  PREREQUISITES on the WiCAN PRO (from wican-fw #476 — do these in the web UI):
    * Protocol = "slcan", CAN bitrate = 500K (S6), wifi = AP.
    * ENABLE "monitoring" — the socket passes NO frames until it is on.
    * Note the SLCAN TCP port (general socket 3333, but the PRO is often 23) and
      pass it with --port. Read it off the device; do not assume.

  USAGE:
    # Fast link check only (seconds, no security module needed):
    python tools/wican_bench_read.py --host 192.168.0.10 --port 3333 --smoke-only

    # Full read + save, then prove byte-perfect against a J2534 dump:
    python tools/wican_bench_read.py --host 192.168.0.10 --port 3333 \\
        --out wican_read.bin --reference j2534_dump.bin

  READ-SPEED SWEEP (Phase 0 instrumentation — non-destructive, idempotent):
    # Does the ECU honour read sizes > 0x400? (probe 0x400/0x800/0xFFE)
    python tools/wican_bench_read.py --host 192.168.0.10 --port 35000 --probe

    # Time 64 blocks under different pacing to find the per-block floor:
    #   STmin sweep (shot A):   --rx-stmin 2     (try 3, 2, 1, 0xF5)
    #   BS-paced bursting (B):  --rx-stmin 0 --rx-block-size 15
    #   bigger blocks:          --block-size 0xFFE   (after --probe confirms it)
    python tools/wican_bench_read.py --host 192.168.0.10 --port 35000 \\
        --bench-blocks 64 --rx-stmin 2 --block-size 0x400

This script is non-destructive (it only READS the ECU). It never flashes.
"""

from __future__ import annotations

import argparse
import logging
import statistics
import sys
import time
from pathlib import Path

# Make the repo's `src` package importable when run as `python tools/...`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.ecu.constants import (  # noqa: E402
    BLOCK_SIZE,
    CAN_REQUEST_ID,
    CAN_RESPONSE_ID,
    ROM_SIZE,
)
from src.ecu.exceptions import (  # noqa: E402
    ECUError,
    NegativeResponseError,
    SecureModuleNotAvailable,
)
from src.ecu.flash_manager import FlashManager  # noqa: E402
from src.ecu.protocol import UDSConnection  # noqa: E402
from src.ecu.wican_config import (  # noqa: E402
    WiCANConfigError,
    WiCANConfigurator,
)
from src.ecu.wican_transport import (  # noqa: E402
    DEFAULT_N_CR_MS,
    DEFAULT_RX_BLOCK_SIZE,
    DEFAULT_RX_STMIN,
    WiCANError,
    WiCANTransport,
)

logger = logging.getLogger("wican_bench_read")


def _fmt_rate(num_bytes: int, seconds: float) -> str:
    """Human-readable throughput, in both KB/s and kbit/s (to compare to #204)."""
    if seconds <= 0:
        return "n/a"
    kbps = num_bytes / seconds / 1024.0
    kbit = num_bytes * 8 / seconds / 1000.0
    return f"{kbps:.1f} KB/s ({kbit:.0f} kbit/s)"


def smoke_test(uds: UDSConnection, pings: int) -> bool:
    """Fire `pings` Tester Present round-trips; report loss + latency.

    Returns True if the link looks usable (at least some replies and 0% loss is
    ideal). This is the #476 GO/NO-GO and the pre-flight link-quality gate in one.
    """
    print(f"\n[SMOKE] {pings} Tester Present round-trips over the link...")
    latencies_ms: list[float] = []
    failures = 0
    for i in range(pings):
        t0 = time.monotonic()
        try:
            uds.tester_present()
            latencies_ms.append((time.monotonic() - t0) * 1000.0)
        except ECUError as exc:
            failures += 1
            logger.debug("Tester Present #%d failed: %s", i + 1, exc)

    replied = len(latencies_ms)
    loss_pct = 100.0 * failures / pings if pings else 100.0
    print(f"[SMOKE] replies: {replied}/{pings}  loss: {loss_pct:.1f}%")

    if replied:
        ordered = sorted(latencies_ms)
        p95 = ordered[min(len(ordered) - 1, int(0.95 * (len(ordered) - 1)))]
        print(
            f"[SMOKE] latency ms  min={ordered[0]:.1f}  "
            f"avg={statistics.fmean(latencies_ms):.1f}  "
            f"p95={p95:.1f}  max={ordered[-1]:.1f}"
        )

    if failures == 0 and replied == pings:
        print("[SMOKE] RESULT: GO — link passes traffic cleanly. ✅")
        return True
    if replied == 0:
        print(
            "[SMOKE] RESULT: NO-GO — no replies at all. Check: 'monitoring' enabled "
            "in the WiCAN web UI, correct --port, bitrate S6/500K, ignition ON. ❌"
        )
        return False
    print(
        "[SMOKE] RESULT: MARGINAL — some loss. Usable for reads/diagnostics, but "
        "improve the link (move closer / AP mode) before trusting a flash. ⚠️"
    )
    return True


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


def full_read(
    uds: UDSConnection, out_path: Path, read_block_size: int = BLOCK_SIZE
) -> bytearray:
    """Read the full 1 MB ROM via FlashManager, timing the whole operation."""
    fm = FlashManager()
    fm.use_uds(uds)  # transport-agnostic borrow — no J2534 device involved

    print(
        f"\n[READ] Reading {ROM_SIZE} bytes ({ROM_SIZE // 1024} KB) "
        f"in 0x{read_block_size:X}-byte blocks ..."
    )
    t0 = time.monotonic()
    rom = fm.read_rom(progress_cb=_progress_printer(), read_block_size=read_block_size)
    elapsed = time.monotonic() - t0
    sys.stdout.write("\n")

    print(f"[READ] DONE in {elapsed:.1f}s — {_fmt_rate(len(rom), elapsed)}")
    if elapsed > 0:
        # A full 1 MB read at this rate; flash writes are SLOWER (per-block
        # erase/program on the ECU), so treat this as a floor for flash time.
        print(
            f"[READ] (full 1 MB read ~= {ROM_SIZE / (len(rom) / elapsed):.0f}s at "
            "this rate; a flash WRITE will be slower)"
        )

    out_path.write_bytes(bytes(rom))
    print(f"[READ] Saved to {out_path}")
    return rom


def fast_read_full(
    transport, out_path: Path, start: int = 0, length: int = ROM_SIZE
) -> bytearray:
    """Autonomous in-firmware ROM read (custom WiCAN firmware) + timing.

    Requires the ECU to already be authenticated (the caller does seed/key over
    the normal SLCAN path first). Sends one fast-read command and streams the
    requested ``[start, start+length)`` range back; the firmware does the
    per-block CAN round-trips locally. ``start``/``length`` default to the whole
    1 MB but can target a sub-range (e.g. just the response-pending region) using
    the same proven command path that engages the firmware fast-read.
    """
    if not hasattr(transport, "fast_read"):
        raise WiCANError("transport does not support fast_read")

    print(f"\n[FASTREAD] firmware autonomous read of {length} bytes "
          f"from 0x{start:06X} ...")
    last = [0.0]

    def cb(done, total):
        now = time.monotonic()
        if now - last[0] >= 0.5 or done >= total:
            last[0] = now
            pct = 100.0 * done / total if total else 0.0
            sys.stdout.write(f"\r  [{pct:5.1f}%] {done}/{total} bytes")
            sys.stdout.flush()

    t0 = time.monotonic()
    data = transport.fast_read(start, length, progress_cb=cb)
    elapsed = time.monotonic() - t0
    sys.stdout.write("\n")
    print(f"[FASTREAD] DONE in {elapsed:.1f}s — {_fmt_rate(len(data), elapsed)}")
    out_path.write_bytes(bytes(data))
    print(f"[FASTREAD] Saved to {out_path}")
    return bytearray(data)


def diff_reference(rom: bytes, reference_path: Path, start: int = 0) -> bool:
    """Byte-compare the read against a known-good J2534 dump (the build gate).

    ``start`` selects the matching slice of the reference when ``rom`` is a
    sub-range read (e.g. just the response-pending region) rather than the full
    ROM.
    """
    ref = reference_path.read_bytes()[start : start + len(rom)]
    print(f"\n[GATE] Comparing read vs reference {reference_path} "
          f"(slice 0x{start:06X}..0x{start + len(rom):06X}) ...")
    if len(ref) != len(rom):
        print(
            f"[GATE] RESULT: FAIL — size mismatch: read={len(rom)} "
            f"reference={len(ref)} ❌"
        )
        return False

    first_diff = -1
    diff_count = 0
    for i, (a, b) in enumerate(zip(rom, ref)):
        if a != b:
            diff_count += 1
            if first_diff < 0:
                first_diff = i

    if diff_count == 0:
        print(
            "[GATE] RESULT: PASS — byte-for-byte identical. The ISO-TP engine is "
            "byte-perfect; the WiCAN read path is trustworthy. ✅"
        )
        return True
    print(
        f"[GATE] RESULT: FAIL — {diff_count} byte(s) differ, first at "
        f"0x{first_diff:06X}. Do NOT trust the WiCAN path for flashing yet. ❌"
    )
    return False


# --- Phase 0 instrumentation: read-size probe + per-block timing harness ---


def _authenticate_for_raw_reads(uds: UDSConnection) -> None:
    """Bring the ECU to an authenticated programming session (no full read).

    Reuses FlashManager's tested connect + security-access sequence so the
    probe / per-block harness can issue raw ReadMemoryByAddress calls. Requires
    the private _secure module (raises SecureModuleNotAvailable if absent),
    exactly like a real read.
    """
    fm = FlashManager()
    fm.use_uds(uds)  # borrowed: _connect only Tester-Presents, never opens J2534
    fm._connect()
    fm._authenticate()


def probe_read_sizes(
    uds: UDSConnection,
    sizes: list[int],
    start_addr: int,
    timeout_ms: int,
) -> list[dict]:
    """Probe whether the ECU honours ReadMemoryByAddress sizes above 0x400.

    A bigger read amortises per-block round-trip + ECU-service latency ~linearly,
    so this answers the top open question for the read-speed work: is 0x400 a
    real ECU limit or just romdrop's convention? Reads are idempotent, so trying
    a larger size can never change ECU state. Prints one line per size and
    returns the structured results.
    """
    print(
        f"\n[PROBE] ReadMemoryByAddress size probe from 0x{start_addr:06X} "
        "(ISO-TP max payload is 0xFFF)..."
    )
    results: list[dict] = []
    for size in sizes:
        t0 = time.monotonic()
        ok = False
        try:
            data = uds.read_memory_by_address(
                start_addr, size, timeout=timeout_ms, pending_max=timeout_ms
            )
            dt = (time.monotonic() - t0) * 1000.0
            if len(data) == size:
                ok = True
                status = f"OK    ({len(data)} bytes in {dt:.0f} ms)"
            else:
                status = f"SHORT ({len(data)}/{size} bytes in {dt:.0f} ms)"
        except NegativeResponseError as exc:
            dt = (time.monotonic() - t0) * 1000.0
            status = f"NRC 0x{exc.nrc:02X} ({exc.description}) in {dt:.0f} ms"
        except ECUError as exc:
            dt = (time.monotonic() - t0) * 1000.0
            status = f"FAIL  ({type(exc).__name__}: {exc}) in {dt:.0f} ms"
        print(f"  size 0x{size:04X} ({size:>4} B): {status}")
        results.append({"size": size, "ok": ok, "status": status})

    accepted = [r["size"] for r in results if r["ok"]]
    if accepted:
        best = max(accepted)
        print(
            f"[PROBE] Largest accepted size: 0x{best:X} ({best} B) — "
            f"{ROM_SIZE // best} requests for a full 1 MB read vs "
            f"{ROM_SIZE // BLOCK_SIZE} at 0x{BLOCK_SIZE:X}."
        )
    else:
        print("[PROBE] No probed size returned data — keep BLOCK_SIZE 0x400.")
    return results


def summarize_block_times(times_ms: list[float], block_size: int) -> dict:
    """Reduce per-block read times (ms) to a reportable summary.

    Pure (no I/O) so it is unit-testable. ``extrapolated_1mb_s`` projects the
    full-ROM time from the average clean-block time, holding pacing constant.
    """
    if not times_ms:
        return {
            "n": 0,
            "min": 0.0,
            "avg": 0.0,
            "p95": 0.0,
            "max": 0.0,
            "extrapolated_1mb_s": 0.0,
        }
    ordered = sorted(times_ms)
    p95 = ordered[min(len(ordered) - 1, int(0.95 * (len(ordered) - 1)))]
    avg = statistics.fmean(times_ms)
    blocks_per_mb = ROM_SIZE / block_size if block_size else 0
    return {
        "n": len(times_ms),
        "min": ordered[0],
        "avg": avg,
        "p95": p95,
        "max": ordered[-1],
        "extrapolated_1mb_s": avg / 1000.0 * blocks_per_mb,
    }


def bench_blocks(
    uds: UDSConnection,
    n_blocks: int,
    block_size: int,
    start_addr: int,
    timeout_ms: int,
) -> dict:
    """Time ``n_blocks`` raw block reads and report the per-block distribution.

    This is the measurement that turns every read-speed lever (STmin, FC block
    size, read size, TCP tuning) into a number: run it with different transport
    pacing and compare the per-block average and the extrapolated 1 MB time.
    Drops/garbled blocks are counted (and the transport flushed) but excluded
    from the clean-block timing stats.
    """
    print(
        f"\n[BENCH] Timing {n_blocks} reads of 0x{block_size:X} ({block_size} B) "
        f"from 0x{start_addr:06X}..."
    )
    times_ms: list[float] = []
    errors = 0
    offset = start_addr
    t_all = time.monotonic()
    for i in range(n_blocks):
        t0 = time.monotonic()
        try:
            data = uds.read_memory_by_address(
                offset, block_size, timeout=timeout_ms, pending_max=timeout_ms
            )
            dt = (time.monotonic() - t0) * 1000.0
            if len(data) == block_size:
                times_ms.append(dt)
            else:
                errors += 1
                logger.warning("block %d short: %d/%d bytes", i, len(data), block_size)
        except ECUError as exc:
            errors += 1
            logger.warning("block %d failed: %s", i, exc)
            try:
                uds.flush()
            except Exception:  # pragma: no cover - best-effort
                pass
        offset += block_size
    total_s = time.monotonic() - t_all

    stats = summarize_block_times(times_ms, block_size)
    print(f"[BENCH] clean blocks: {stats['n']}/{n_blocks}  drops/errors: {errors}")
    if stats["n"]:
        clean_s = sum(times_ms) / 1000.0
        print(
            f"[BENCH] per-block ms  min={stats['min']:.0f}  avg={stats['avg']:.0f}  "
            f"p95={stats['p95']:.0f}  max={stats['max']:.0f}"
        )
        print(
            f"[BENCH] {stats['n']} clean blocks in {clean_s:.1f}s "
            f"({_fmt_rate(stats['n'] * block_size, clean_s)})"
        )
        print(
            "[BENCH] extrapolated full 1 MB at this pacing: "
            f"~{stats['extrapolated_1mb_s']:.0f}s"
        )
    print(
        f"[BENCH] wall time for {n_blocks} blocks (incl. drops/retries): {total_s:.1f}s"
    )
    return stats


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="WiCAN PRO bench READ — link smoke test + 1MB ROM read (non-destructive).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--host", default="192.168.0.10", help="WiCAN IP (PRO AP default 192.168.0.10)"
    )
    p.add_argument(
        "--port",
        type=int,
        default=3333,
        help="SLCAN TCP port — read it off the device (PRO is often 23, general 3333)",
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
    p.add_argument(
        "--pings",
        type=int,
        default=25,
        help="Tester Present round-trips in the smoke phase",
    )
    p.add_argument(
        "--smoke-only",
        action="store_true",
        help="Run only the link smoke test, then exit",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path(f"wican_read_{time.strftime('%Y%m%d_%H%M%S')}.bin"),
        help="Where to save the read ROM",
    )
    p.add_argument(
        "--reference", type=Path, help="Known-good J2534 dump to byte-compare against"
    )
    # --- Read-speed sweep knobs (Phase 0/1 of the read-speed work) ---
    p.add_argument(
        "--rx-stmin",
        type=lambda x: int(x, 0),
        default=DEFAULT_RX_STMIN,
        help=(
            "ISO-TP STmin we advertise when receiving (ECU inter-frame pacing). "
            f"0x00-0x7F = ms, 0xF1-0xF9 = 100-900 us. Default {DEFAULT_RX_STMIN}. "
            "Lower = faster but the gateway may drop frames — sweep it."
        ),
    )
    p.add_argument(
        "--rx-block-size",
        type=lambda x: int(x, 0),
        default=DEFAULT_RX_BLOCK_SIZE,
        help=(
            "ISO-TP Flow-Control Block Size we advertise when receiving (0 = all "
            f"CFs in one burst). Default {DEFAULT_RX_BLOCK_SIZE}. Set ~8-15 to test "
            "BS-paced bursting (STmin=0 + small BS)."
        ),
    )
    p.add_argument(
        "--block-size",
        type=lambda x: int(x, 0),
        default=BLOCK_SIZE,
        help=(
            f"ReadMemoryByAddress bytes per request for --bench-blocks. Default "
            f"0x{BLOCK_SIZE:X}. Try 0x800/0xFFE once --probe confirms the ECU allows it."
        ),
    )
    p.add_argument(
        "--read-timeout-ms",
        type=int,
        default=4000,
        help="Per-read timeout AND response-pending budget for --probe / --bench-blocks",
    )
    p.add_argument(
        "--n-cr-ms",
        type=int,
        default=DEFAULT_N_CR_MS,
        help=(
            "ISO-TP N_Cr: max wait (ms) for the next Consecutive Frame before a "
            f"dropped block fails fast. Default {DEFAULT_N_CR_MS}. Tune down toward "
            "the clean-block inter-frame gap once measured."
        ),
    )
    p.add_argument(
        "--no-tcp-nodelay",
        action="store_true",
        help="Leave Nagle ON (default disables it). Use to A/B the TCP_NODELAY effect.",
    )
    p.add_argument(
        "--so-rcvbuf",
        type=int,
        default=None,
        help="Request this socket receive-buffer size in bytes (default: OS default)",
    )
    p.add_argument(
        "--probe",
        action="store_true",
        help="Probe whether the ECU honours read sizes > 0x400, then exit (after any --bench-blocks)",
    )
    p.add_argument(
        "--probe-sizes",
        type=lambda x: [int(s, 0) for s in x.split(",") if s.strip()],
        default=[0x400, 0x800, 0xFFE],
        help="Comma-separated sizes for --probe (default 0x400,0x800,0xFFE)",
    )
    p.add_argument(
        "--bench-blocks",
        type=int,
        default=0,
        help="Time this many raw block reads (per-block latency distribution) instead of a full read",
    )
    p.add_argument(
        "--bench-start",
        type=lambda x: int(x, 0),
        default=0,
        help="Start address for --probe / --bench-blocks (default 0x000000)",
    )
    p.add_argument(
        "--fast-read",
        action="store_true",
        help=(
            "Use the custom firmware's autonomous in-firmware read (one command, "
            "streamed 1 MB) instead of per-block reads. Times it and (with "
            "--reference) byte-compares. Requires the fast-read firmware."
        ),
    )
    p.add_argument(
        "--fast-read-start",
        type=lambda x: int(x, 0),
        default=0,
        help="Start address for --fast-read (default 0). Use to target a sub-range.",
    )
    p.add_argument(
        "--fast-read-len",
        type=lambda x: int(x, 0),
        default=ROM_SIZE,
        help=f"Byte count for --fast-read (default full 0x{ROM_SIZE:X}).",
    )
    p.add_argument(
        "--connect-timeout-ms", type=int, default=5000, help="TCP connect timeout"
    )
    p.add_argument(
        "--auto-config",
        action="store_true",
        help=(
            "Before opening the link, switch the WiCAN's HTTP-config protocol to "
            "'slcan' (required to pass CAN traffic), and restore your previous "
            "protocol afterwards. OFF by default."
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
    # Force UTF-8 on the console so the em-dashes / status glyphs in the output
    # can't raise UnicodeEncodeError mid-read on a legacy Windows code page.
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
    print("WiCAN PRO bench READ (non-destructive)")
    print(
        f"  target: {args.host}:{args.port}  tx=0x{args.tx_id:03X} rx=0x{args.rx_id:03X}"
    )
    print(
        f"  pacing: rx_stmin={args.rx_stmin} rx_block_size={args.rx_block_size} "
        f"read_size=0x{args.block_size:X} n_cr_ms={args.n_cr_ms} "
        f"tcp_nodelay={not args.no_tcp_nodelay} so_rcvbuf={args.so_rcvbuf}"
    )
    print("  REMINDER: enable 'monitoring' + set S6/500K + the right port in the")
    print("            WiCAN web UI, and keep the ignition ON.")
    print("=" * 70)

    # Opt-in: flip the device's HTTP-config protocol to 'slcan' before we open
    # the socket (the firmware passes NO CAN traffic otherwise) via a durable
    # session context manager that ALWAYS restores the user's previous protocol
    # on exit — even on a link-open failure, a read error, or Ctrl-C — and
    # survives a hard kill mid-session via its crash-recovery sidecar.
    if args.auto_config:
        configurator = WiCANConfigurator(args.host, http_port=args.http_port)
        try:
            print(
                f"\n[AUTO-CONFIG] Switching {args.host} HTTP protocol -> slcan "
                f"(was: querying)..."
            )
            if configurator.read_recovery() is not None:
                print(
                    "[AUTO-CONFIG] Found a stranded recovery file from a prior "
                    "interrupted run — the TRUE original protocol will be restored "
                    "on exit, not the current slcan value."
                )
            with configurator.slcan_session() as previous_protocol:
                print(
                    f"[AUTO-CONFIG] Device now in slcan mode "
                    f"(previous protocol: {previous_protocol!r})."
                )
                if previous_protocol != "slcan":
                    print(
                        f"[AUTO-CONFIG] Previous protocol {previous_protocol!r} "
                        f"will be restored on exit."
                    )
                rc = _run_link(args)
            # The context manager's finally has now restored + cleared recovery.
            if previous_protocol != "slcan":
                print(f"[AUTO-CONFIG] Device restored to {previous_protocol!r}.")
            return rc
        except WiCANConfigError as exc:
            print(f"[AUTO-CONFIG] FAILED: {exc}")
            print(
                "       Check the --http-port and that the WiCAN web UI is reachable, "
                "or drop --auto-config and set 'slcan' manually. If a recovery file "
                "was left behind, the next --auto-config run will restore it."
            )
            return 6

    return _run_link(args)


def _run_link(args: argparse.Namespace) -> int:
    """Open the transport and run the smoke test + optional full read.

    Returns the process exit code. The caller is responsible for any protocol
    auto-config restore (handled by ``slcan_session`` around this call).
    """
    transport = WiCANTransport(
        args.host,
        args.port,
        tx_id=args.tx_id,
        rx_id=args.rx_id,
        connect_timeout_ms=args.connect_timeout_ms,
        rx_block_size=args.rx_block_size,
        rx_stmin=args.rx_stmin,
        n_cr_ms=args.n_cr_ms,
        tcp_nodelay=not args.no_tcp_nodelay,
        so_rcvbuf=args.so_rcvbuf,
    )

    try:
        print(f"\n[LINK] Opening {transport.description} ...")
        transport.open()
        print("[LINK] Channel up (C/S6/O acked).")
    except WiCANError as exc:
        print(f"[LINK] FAILED to open: {exc}")
        print(
            "       Check: WiCAN reachable on the network, correct --port, "
            "'monitoring' enabled, bitrate S6/500K."
        )
        return 2

    try:
        uds = UDSConnection(transport)

        link_ok = smoke_test(uds, args.pings)
        if args.smoke_only:
            return 0 if link_ok else 3
        if not link_ok:
            print("\nLink smoke test was NO-GO — skipping the full read.")
            return 3

        # Read-speed instrumentation modes (Phase 0): probe the ECU's max read
        # size and/or time a slice of blocks under the configured pacing, then
        # exit without doing the full 1 MB read. Both need an authenticated
        # session for ReadMemoryByAddress.
        if args.probe or args.bench_blocks > 0:
            try:
                _authenticate_for_raw_reads(uds)
            except SecureModuleNotAvailable:
                print(
                    "\n[PROBE/BENCH] FAILED — security access needs the private "
                    "_secure module (the same one the app uses to flash). Install "
                    "it, or use --smoke-only to validate the link without auth."
                )
                return 4
            if args.bench_blocks > 0:
                bench_blocks(
                    uds,
                    args.bench_blocks,
                    args.block_size,
                    args.bench_start,
                    args.read_timeout_ms,
                )
            if args.probe:
                probe_read_sizes(
                    uds, args.probe_sizes, args.bench_start, args.read_timeout_ms
                )
            return 0

        if args.fast_read:
            try:
                _authenticate_for_raw_reads(uds)
            except SecureModuleNotAvailable:
                print(
                    "\n[FASTREAD] FAILED — security access needs the private "
                    "_secure module."
                )
                return 4
            try:
                rom = fast_read_full(
                    transport, args.out, args.fast_read_start, args.fast_read_len
                )
            except WiCANError as exc:
                print(f"\n[FASTREAD] FAILED: {exc}")
                print(
                    "       The firmware fast-read may not be flashed, or a block "
                    "failed (stream stopped short). Fall back to a normal read."
                )
                return 7
            if args.reference:
                return (
                    0
                    if diff_reference(
                        bytes(rom), args.reference, args.fast_read_start
                    )
                    else 5
                )
            print(
                "\n[GATE] No --reference. Re-run with --reference wican_stmin0_full.bin "
                "to prove the fast read is byte-identical."
            )
            return 0

        try:
            rom = full_read(uds, args.out, read_block_size=args.block_size)
        except SecureModuleNotAvailable:
            print(
                "\n[READ] FAILED — security access needs the private _secure module "
                "(the same one the app uses to flash). Install it, or run with "
                "--smoke-only to validate the link without authenticating."
            )
            return 4

        if args.reference:
            gate_ok = diff_reference(bytes(rom), args.reference)
            return 0 if gate_ok else 5
        else:
            print(
                "\n[GATE] No --reference supplied. Re-run with --reference <j2534_dump.bin> "
                "to prove the read is byte-perfect."
            )
        return 0
    except KeyboardInterrupt:
        print("\nAborted by user (read is non-destructive — ECU is fine).")
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
