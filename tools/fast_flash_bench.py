#!/usr/bin/env python3
"""
Fast-flash bench tool (EXPERIMENTAL). BENCH ECU ONLY, never the car.

Uploads the fast-flash kernel and, by default, only COMPARES the ECU with a
ROM file: nothing is erased or written. It drives the same code as the app
(ECU session, FlashManager.fast_flash), over the adapter the app would use:
the Tactrix through NC Flash's built-in OpenPort driver by default.

  COMPARE (default, changes nothing in flash):
    python tools/fast_flash_bench.py --kernel kernel.bin --rom image.bin

  FLASH (erases and rewrites the blocks that differ):
    python tools/fast_flash_bench.py --kernel kernel.bin --rom image.bin \\
        --flash --i-am-on-the-bench-ecu

  Adapters: Tactrix (default; --dll to pick another J2534 driver),
  --obdx [PORT] (empty = auto-detect), --wican HOST (compare only; it also needs
  the shared bench lock ~/.claude/wican-bench.lock, which this tool does not take).

  Before --flash: take a full ROM read of the bench ECU and keep it. If a
  fast flash is cut short the ECU waits (kernel still running, or its boot
  loader after a key cycle). Run the tool again to finish it (a running kernel
  is found and every block is rewritten), or recover it with a normal full
  flash from NC Flash.

  The kernel is built in the nc-flash-re project (tools/fast_flash/kernel). A
  kernel that is not pinned in src/ecu/fast_flash_kernel.py is accepted here
  with a warning (bench use). Needs the private security module, the ignition
  ON and the engine off. See docs/internal/FAST_FLASH.md.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

# Make the repo's `src` package importable when run as `python tools/...`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ecu import fast_flash as ff  # noqa: E402
from src.ecu.constants import DEFAULT_J2534_DLL, ROM_FLASH_START_MIN  # noqa: E402
from src.ecu.exceptions import ROMValidationError  # noqa: E402
from src.ecu.fast_flash_kernel import LoadedKernel, find_pin  # noqa: E402
from src.ecu.flash_manager import FlashManager, enforce_rpm_gate  # noqa: E402
from src.ecu.flash_prep import prepare_flash_image  # noqa: E402
from src.ecu.recovery import EcuMode, probe_ecu_mode  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--kernel", required=True, help="fast-flash kernel image (kernel.bin)"
    )
    ap.add_argument("--rom", required=True, help="1 MB ROM to compare with / flash")
    ap.add_argument(
        "--all",
        action="store_true",
        help="with --flash: rewrite every block, changed or not",
    )
    ap.add_argument(
        "--dll",
        default=DEFAULT_J2534_DLL,
        help="J2534 driver (default: NC Flash's automatic choice, as in the app)",
    )
    ap.add_argument(
        "--flash", action="store_true", help="erase and rewrite the blocks that differ"
    )
    ap.add_argument(
        "--i-am-on-the-bench-ecu", action="store_true", help="required with --flash"
    )
    adapter = ap.add_mutually_exclusive_group()
    adapter.add_argument(
        "--wican", metavar="HOST", help="use a WiCAN PRO at HOST instead of J2534"
    )
    adapter.add_argument(
        "--obdx",
        metavar="PORT",
        nargs="?",
        const="",
        help="use an OBDX Pro (PORT such as COM7; empty = auto-detect)",
    )
    ap.add_argument(
        "--chunk",
        type=int,
        default=None,
        help="uncompressed writes: bytes per request (128..1024; default 1024, "
        "256 over WiCAN)",
    )
    ap.add_argument(
        "--no-compress",
        action="store_true",
        help="send blocks uncompressed even if the kernel decodes LZMA (v2)",
    )
    ap.add_argument(
        "--car",
        choices=("NC1", "NC2"),
        help="which car the ECU is from, when its generation can't be read "
        "(an ECU in its boot loader); without it the flash stops there",
    )
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO, format="%(message)s"
    )

    if a.flash and a.wican:
        # The app never writes the ECU from the PC over the WiCAN (its flash
        # runs from the adapter firmware); neither does this tool.
        print("--flash is not available over --wican: compare only.")
        return 2
    if a.flash and not a.i_am_on_the_bench_ecu:
        print("--flash needs --i-am-on-the-bench-ecu. This tool is not for the car.")
        return 2

    data = Path(a.kernel).read_bytes()
    ff.validate_kernel(data)
    pin = find_pin(data)
    kernel = LoadedKernel(data, a.kernel, pin)
    rom = Path(a.rom).read_bytes()
    _image, _sbl, generation, corrections = prepare_flash_image(
        rom, ROM_FLASH_START_MIN
    )
    print(
        f"ROM: {a.rom} ({generation}), {len(corrections)} checksum(s) corrected in memory"
    )
    print(
        f"Kernel: {a.kernel} ({len(data)} bytes, "
        + (f"pinned v{pin.version})" if pin else "NOT pinned: bench use only)")
    )
    print(
        "Mode: "
        + ("FLASH" if a.flash else "compare only (nothing is erased or written)")
    )
    chunk = a.chunk or (256 if a.wican else ff.DEFAULT_CHUNK)
    return _run(a, kernel, rom, chunk)


def _adapter_config(a) -> dict:
    if a.wican:
        return {"kind": "wican", "host": a.wican}
    if a.obdx is not None:
        return {"kind": "obdx", "port": a.obdx}
    return {"kind": "j2534", "dll_path": a.dll}


def _run(a, kernel: LoadedKernel, rom: bytes, chunk: int) -> int:
    from src.ecu.session import ECUSession

    session = ECUSession(adapter_config=_adapter_config(a))
    lost: list[str] = []
    session.connection_lost.connect(lost.append)
    session.connect_ecu()
    if not session.is_connected:
        print(f"Could not connect: {'; '.join(lost) or 'no answer'}")
        return 1
    t0 = time.monotonic()
    try:
        probe = probe_ecu_mode(session.uds)
        print(f"ECU: {probe.mode.value} ({'; '.join(probe.evidence)})")
        resume = probe.mode == EcuMode.FAST_FLASH_KERNEL
        if resume:
            print("A fast-flash kernel is already running on the ECU: resuming with it")
        else:
            enforce_rpm_gate(session.uds)
        device, channel_id, filter_id, uds = session.acquire()
        fm = FlashManager(a.dll)
        if session.adapter_kind == "j2534":
            fm.use_session(device, channel_id, filter_id, uds)
        else:
            fm.use_uds(uds)
        if a.car:
            fm.set_unverified_generation_prompt(lambda *_args: a.car)

        def progress(p) -> None:
            print(f"  [{p.percent:5.1f}%] {p.state.value}: {p.message}")

        wait = bool(a.wican)  # only the WiCAN's link goes silent and comes back
        if a.flash:
            try:
                result = fm.fast_flash(
                    rom,
                    None,
                    progress,
                    kernel=kernel,
                    resume=resume,
                    rewrite_all=a.all,
                    compress=not a.no_compress,
                    chunk=chunk,
                    wait_out_outages=wait,
                )
            except ROMValidationError as e:
                print(f"\n{e}")
                print(f"Elapsed: {time.monotonic() - t0:.1f} s")
                return 0
        else:
            result = fm.fast_compare(
                rom, progress, kernel=kernel, resume=resume, wait_out_outages=wait
            )
        k = result.kernel
        print(
            f"\nKernel v{k.version}, chip class {k.chip_class}, flash routines "
            f"{'ready' if k.flash_ready else 'NOT READY'}"
        )
        print("Block  Start     Size      ECU vs ROM file")
        for b in result.before:
            print(
                f"  {b.block:2d}   0x{b.start:06X}  0x{b.size:05X}   "
                f"{'same' if b.same else 'DIFFERENT'}"
            )
        if not result.boot_block_same:
            print(
                "NOTE: the ROM file's boot block (0x0000-0x1FFF) differs from the "
                "ECU's. It is never written."
            )
        if result.interrupted_before:
            print(
                "NOTE: the ECU has no entry pointer: an earlier flash was cut short. "
                "A flash rewrites every block."
            )
        if a.flash:
            print(
                f"\nRewritten blocks: {result.rewritten or 'none'}; "
                f"{result.bytes_sent} bytes sent; verified by CRC"
            )
        print(f"Elapsed: {time.monotonic() - t0:.1f} s")
        print(
            "Reset sent. The ECU restarts by itself within a few seconds (bench "
            "2026-11-15); if it does not answer afterwards, cycle the ignition "
            "and note it."
        )
        return 0
    finally:
        session.release(connection_dead=True)
        session.cleanup()


if __name__ == "__main__":
    sys.exit(main())
