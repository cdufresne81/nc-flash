#!/usr/bin/env python3
"""
Fast-flash bench tool (EXPERIMENTAL). BENCH ECU ONLY, never the car.

Uploads the fast-flash kernel through a J2534 cable and, by default, only
COMPARES the ECU with a ROM file: nothing is erased or written.

  COMPARE (default, changes nothing in flash):
    python tools/fast_flash_bench.py --kernel kernel.bin --rom image.bin

  FLASH (erases and rewrites the blocks that differ):
    python tools/fast_flash_bench.py --kernel kernel.bin --rom image.bin \\
        --flash --i-am-on-the-bench-ecu

  Before --flash: take a full ROM read of the bench ECU and keep it. If a
  fast flash is cut short the ECU waits in its boot loader; recover it with a
  normal full flash from NC Flash.

  The kernel is built in the nc-flash-re project (tools/fast_flash/kernel).
  Needs the private security module, the ignition ON and the engine off.
  See docs/internal/FAST_FLASH.md.
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
from src.ecu.flash_manager import FlashManager, enforce_rpm_gate  # noqa: E402
from src.ecu.flash_prep import prepare_flash_image  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--kernel", required=True, help="fast-flash kernel image (kernel.bin)"
    )
    ap.add_argument("--rom", required=True, help="1 MB ROM to compare with / flash")
    ap.add_argument("--dll", default=DEFAULT_J2534_DLL, help="J2534 DLL")
    ap.add_argument(
        "--flash", action="store_true", help="erase and rewrite the blocks that differ"
    )
    ap.add_argument(
        "--i-am-on-the-bench-ecu", action="store_true", help="required with --flash"
    )
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO, format="%(message)s"
    )

    if a.flash and not a.i_am_on_the_bench_ecu:
        print("--flash needs --i-am-on-the-bench-ecu. This tool is not for the car.")
        return 2

    kernel = Path(a.kernel).read_bytes()
    ff.validate_kernel(kernel)
    image, _sbl, generation, corrections = prepare_flash_image(
        Path(a.rom).read_bytes(), ROM_FLASH_START_MIN
    )
    print(
        f"ROM: {a.rom} ({generation}), {len(corrections)} checksum(s) corrected in memory"
    )
    print(f"Kernel: {a.kernel} ({len(kernel)} bytes)")
    print(
        "Mode: "
        + ("FLASH" if a.flash else "compare only (nothing is erased or written)")
    )

    fm = FlashManager(a.dll)
    t0 = time.monotonic()
    try:
        fm._connect()
        enforce_rpm_gate(fm._uds)
        fm._authenticate()
        fm._uds.check_flash_counter()
        progress = lambda msg, pct: print(f"  [{pct:5.1f}%] {msg}")  # noqa: E731
        if a.flash:
            result = ff.fast_flash(fm._uds, kernel, image, progress)
        else:
            result = ff.compare_only(fm._uds, kernel, image, progress)
        k = result.kernel
        print(
            f"\nKernel v{k.version}, chip class {k.chip_class}, flash routines {'ready' if k.flash_ready else 'NOT READY'}"
        )
        print("Block  Start     Size      ECU vs ROM file")
        for b in result.before:
            print(
                f"  {b.block:2d}   0x{b.start:06X}  0x{b.size:05X}   {'same' if b.same else 'DIFFERENT'}"
            )
        if a.flash:
            print(
                f"\nRewritten blocks: {result.rewritten or 'none'}; {result.bytes_sent} bytes sent; verified by CRC"
            )
        print(f"Elapsed: {time.monotonic() - t0:.1f} s")
        ff.reset_ecu(fm._uds)
        print(
            "Reset requested. If the ECU does not answer afterwards, cycle the ignition and note it."
        )
        return 0
    finally:
        fm._cleanup()


if __name__ == "__main__":
    sys.exit(main())
