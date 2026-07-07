"""One-shot validation: Auto-Blip definitions parse and read correct values
from an autoblip-patched ROM. Run from repo root:
    python tools/validate_autoblip_defs.py <path-to-autoblip-rom.bin>
"""

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.definition_parser import DefinitionParser  # noqa: E402

if len(sys.argv) < 2:
    print(__doc__.strip())
    sys.exit(2)

rom_path = sys.argv[1]
rom = open(rom_path, "rb").read()

defn = DefinitionParser("examples/metadata/lf9veb.xml").parse()
blip_tables = [t for t in defn.tables if t.category == "Auto-Blip"]
print(
    f"Parsed {len(defn.scalings)} scalings, {len(defn.tables)} tables; "
    f"{len(blip_tables)} in Auto-Blip category"
)

expected = {
    "Auto-Blip - Enable": ("uint8", 0xFCAC0, 0),
    "Auto-Blip - APP Threshold": ("float", 0xFCAC4, 5.0),
    "Auto-Blip - Min VSS": ("float", 0xFCAC8, 20.0),
    "Auto-Blip - Min RPM": ("float", 0xFCACC, 1500.0),
    "Auto-Blip - Max RPM Target": ("float", 0xFCAD0, 7200.0),
    "Auto-Blip - Decay Factor": ("float", 0xFCAD4, 0.92),
    "Auto-Blip - Max Duration": ("uint8", 0xFCAD8, 60),
}

failures = 0
for t in blip_tables:
    addr = int(t.address, 16)
    scaling = defn.scalings.get(t.scaling)
    if t.name in expected:
        kind, exp_addr, exp_val = expected[t.name]
        if kind == "float":
            val = struct.unpack_from(">f", rom, addr)[0]
            ok = abs(val - exp_val) < 1e-5
        else:
            val = rom[addr]
            ok = val == exp_val
        ok = ok and addr == exp_addr and scaling is not None
        print(
            f'  {"PASS" if ok else "FAIL"}: {t.name} @0x{addr:06X} = {val} '
            f"(expect {exp_val}, scaling {t.scaling!r} "
            f'{"resolved" if scaling else "MISSING"})'
        )
        if not ok:
            failures += 1
    elif t.name == "Auto-Blip - RPM Delta to TP Offset":
        data = struct.unpack_from(">8f", rom, addr)
        axis_addr = int(t.axes[0].address, 16) if getattr(t, "axes", None) else None
        ok = scaling is not None and addr == 0xFCAFC
        print(f'  {"PASS" if ok else "FAIL"}: {t.name} @0x{addr:06X} data={list(data)}')
        if axis_addr is not None:
            axis = struct.unpack_from(">8f", rom, axis_addr)
            print(f"        axis @0x{axis_addr:06X} = {list(axis)}")
        if not ok:
            failures += 1

print("ALL OK" if failures == 0 else f"{failures} FAILURES")
sys.exit(0 if failures == 0 else 1)
