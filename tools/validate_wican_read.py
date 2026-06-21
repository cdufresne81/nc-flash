#!/usr/bin/env python3
"""
Validate a WiCAN-read ROM dump for self-consistency (no J2534 reference needed).

When we have no same-ECU J2534 dump to byte-compare against, the strongest
correctness signal available is the ROM's OWN internal structure:

  * size is exactly ROM_SIZE (1 MB),
  * the generation byte / ROM-ID / cal-ID decode to sane values,
  * EVERY Mazda checksum-table entry already matches its content — i.e.
    ``correct_rom_checksums`` finds ZERO corrections. A read with even one
    flipped/dropped byte inside a checksummed region would almost certainly
    break this, so 0 corrections is a powerful coherence proof.

A second, independent WiCAN read that is byte-identical to this one (run this
with --compare other.bin) upgrades that to a determinism proof. True
byte-perfect proof still requires a J2534 dump of THIS ECU.

Usage:
    python tools/validate_wican_read.py wican_read_live.bin
    python tools/validate_wican_read.py read_a.bin --compare read_b.bin
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.ecu.constants import ROM_SIZE  # noqa: E402
from src.ecu.checksum import correct_rom_checksums  # noqa: E402
from src.ecu.rom_utils import (  # noqa: E402
    detect_vehicle_generation,
    get_cal_id,
    get_rom_id,
)


def validate(path: Path) -> bool:
    data = path.read_bytes()
    ok = True
    print(f"\n[VALIDATE] {path}  ({len(data)} bytes)")

    if len(data) == ROM_SIZE:
        print(f"  size            : OK ({len(data)} == {ROM_SIZE})")
    else:
        print(f"  size            : FAIL ({len(data)} != {ROM_SIZE})")
        ok = False

    try:
        gen = detect_vehicle_generation(data)
        print(f"  generation      : OK ({gen})")
    except Exception as exc:
        print(f"  generation      : FAIL ({exc})")
        ok = False

    try:
        print(f"  ROM ID          : OK ({get_rom_id(data)})")
    except Exception as exc:
        # 0xFFC4C holds romdrop's patch-engine revision marker, which is
        # legitimately empty on a stock ROM never patched by romdrop — so an
        # empty value here is informational, not a read failure.
        print(f"  ROM ID          : empty/none ({exc}) — normal for a stock ROM")

    try:
        cal = get_cal_id(data)
        print(f"  cal ID          : OK ({cal.decode('ascii', 'replace')})")
    except Exception as exc:
        print(f"  cal ID          : FAIL ({exc})")
        ok = False

    # Self-consistency: 0 corrections == every checksum already matches content.
    corrections = correct_rom_checksums(bytearray(data))
    if not corrections:
        print("  checksum table  : OK (0 corrections — internally self-consistent)")
    else:
        print(
            f"  checksum table  : SUSPECT ({len(corrections)} mismatch(es) — "
            "either a non-stock/edited ROM or a corrupt read)"
        )
        for start, end, off, old, new in corrections[:8]:
            print(
                f"      [0x{start:06X}-0x{end:06X}] @0x{off:06X}: "
                f"0x{old:08X} -> 0x{new:08X}"
            )
        ok = False

    # VIN presence (best-effort; the VIN may not live in the main ROM region).
    vin = b"JM1NC2FF0A0207980"
    if vin in data:
        print(f"  VIN in image    : present ({vin.decode()})")
    else:
        print("  VIN in image    : not found in 0x000000-0x0FFFFF (informational)")

    return ok


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("rom", type=Path, help="ROM dump to validate")
    p.add_argument(
        "--compare",
        type=Path,
        help="A second dump to byte-compare against (determinism check)",
    )
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    args = p.parse_args(argv)

    ok = validate(args.rom)

    if args.compare:
        a = args.rom.read_bytes()
        b = args.compare.read_bytes()
        if a == b:
            print(
                f"\n[COMPARE] {args.rom} == {args.compare}: byte-identical — the "
                "WiCAN read path is deterministic/stable. ✅"
            )
        else:
            first = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), -1)
            n = sum(1 for x, y in zip(a, b) if x != y)
            print(
                f"\n[COMPARE] {args.rom} != {args.compare}: {n} byte(s) differ, "
                f"first at 0x{first:06X}. ❌"
            )
            ok = False

    print("\n[RESULT]", "PASS ✅" if ok else "FAIL ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
