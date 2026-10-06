"""Build the LF9VEB v2 test fixture from the nc-flash-re v1 AutoBlip patch.

TEST FIXTURE ONLY, never shipped: the real v2 files come from nc-flash-re's
emitter (tools/ncflash_patch.py there), which derives the checks from the full
ROM profile. This one keeps v1's ranges byte for byte (so applying it to a
fresh Romdrop LF9VEB must give v1's reference image) and adds:

- code pin over 0x000000-0x0B7FFF and 0x0FC300-0x0FC847 (OEM code + Romdrop's
  code block), data pin over 0x0B8000-0x0FBFFF at level warn, both excluding
  every table / axis of examples/metadata/lf9veb.xml and every patch range;
- a few checks of each kind on real LF9VEB addresses (gear ratios 0x0C14FC,
  TP Delta 0x0F629C 15x29) and the release post checks.

    python tests/fixtures/ncflash_patch/make_fixture.py <v1 lf9veb_autoblip_v3_4.json> <nc-flash-re xml/lf9veb.xml>

The embedded ``definitions`` are the "DBW - AutoBlip" tables and their scalings
from the nc-flash-re master definition.
"""

import hashlib
import json

from lxml import etree
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))

from src.ecu.checksum import correct_rom_checksums  # noqa: E402
from src.ecu.ncflash_patch import pin_hashes  # noqa: E402
from src.ecu.rom_utils import patch_rom  # noqa: E402
from tools import metadata_lint as lint  # noqa: E402

TYRE_TEXT = (
    "The gear and speed readings switch to the ECU's own tyre value (the OEM path): "
    "throttle feel per gear, the DSC / torque gear input and the tyre byte on CAN can "
    "change, and the NC Flash / Romdrop tyre-size setting stops having any effect."
)


def r0():
    ex = REPO / "examples"
    res = patch_rom(
        (ex / "lf9veb.bin").read_bytes(), (ex / "lf9veb.patch").read_bytes()
    )
    buf = bytearray(res.patched_rom)
    correct_rom_checksums(buf)
    return bytes(buf)


def table_spans():
    path = REPO / "examples" / "metadata" / "lf9veb.xml"
    rom = lint._rom_element(lint._parse(path.read_bytes()))
    scalings = {sc.get("name"): sc for sc in rom.iter("scaling")}
    out = []
    for t in rom.iter("table"):
        ext = lint._check_table(path, t, scalings, "", [])
        if ext:
            out.append((ext[0], ext[1] - 1))
    return out


def pin(rom, regions, exclude, level):
    sha, blocks = pin_hashes(rom, regions, exclude, 4096)
    return {
        "regions": [[f"0x{a:06X}", f"0x{b:06X}"] for a, b in regions],
        "exclude": [[f"0x{a:06X}", f"0x{b:06X}"] for a, b in sorted(set(exclude))],
        "sha256": sha,
        "block_size": 4096,
        "blocks": {f"0x{k:06X}": v for k, v in sorted(blocks.items())},
        "level": level,
    }


def autoblip_definitions(xml_path):
    root = etree.parse(str(xml_path)).getroot()
    rom = root if root.tag == "rom" else root.find("rom")
    tables = [
        t for t in rom.findall("table") if t.get("category", "").endswith("AutoBlip")
    ]
    names = {x.get("scaling") for t in tables for x in [t] + t.findall("table")}
    scalings = [s for s in rom.findall("scaling") if s.get("name") in names]
    parts = []
    for el in scalings + tables:
        el = etree.fromstring(etree.tostring(el))
        el.tail = None
        parts.append(etree.tostring(el, encoding="unicode"))
    return "\n".join(parts)


def main(v1_path, xml_path):
    v1_raw = Path(v1_path).read_bytes()
    v1 = json.loads(v1_raw)
    base = r0()
    assert hashlib.sha256(base).hexdigest() == v1["r0_sha256"], "R0 mismatch"
    ranges = v1["ranges"]
    spans = [
        (int(r["addr"], 16), int(r["addr"], 16) + len(r["old"]) // 2 - 1)
        for r in ranges
    ]
    exclude = table_spans() + spans
    code = [r for r in ranges if r["kind"] == "code"]
    sig = hashlib.sha256(b"".join(bytes.fromhex(r["new"]) for r in code)).hexdigest()
    tyre = [r["addr"] for r in ranges if r["kind"] == "tyre_restore"]
    d = {
        "format": "ncflash-patch/2",
        "patch": {
            "id": "autoblip",
            "name": "AutoBlip",
            "version": "3.4",
            "channel": "test",
            "source_sha256": v1["autoblip"]["source_sha256"],
            "notes": "TEST FIXTURE derived from ncflash-autoblip-patch/1 "
            + hashlib.sha256(v1_raw).hexdigest()[:16],
        },
        "rom_id": "LF9VEB",
        "rom_id_addr": "0x0B8046",
        "requires": {"romdrop_rev": v1["romdrop"]["rev"]},
        "reference": {
            "factory_sha256": v1["factory_sha256"],
            "base_sha256": v1["r0_sha256"],
            "result_sha256": v1["reference_sha256"],
        },
        "ranges": ranges,
        "code_pin": pin(base, [(0, 0xB7FFF), (0xFC300, 0xFC847)], exclude, "refuse"),
        "data_pin": pin(base, [(0xB8000, 0xFBFFF)], exclude, "warn"),
        "checks": [
            {
                "type": "table_finite",
                "level": "refuse",
                "name": "TP Delta - Fault Threshold",
                "addr": "0x0F629C",
                "nx": 15,
                "ny": 29,
            },
            {
                "type": "table_all_eq",
                "level": "warn",
                "name": "TP Delta - Fault Threshold "
                "(set every cell to 22 before enabling AutoBlip)",
                "addr": "0x0F629C",
                "nx": 15,
                "ny": 29,
                "value": 22.0,
            },
            {
                "type": "gear",
                "level": "warn",
                "name": "Gear detection",
                "ratios": "0x0C14FC",
                "bands": "0x0C1524",
                "five_speed_ratio": 1.10,
                "margin_warn": 0.05,
                "from_gear": 2,
            },
            {
                "type": "f_desc",
                "level": "refuse",
                "name": "Gear ratios 1st-6th",
                "addr": "0x0C14FC",
                "n": 6,
            },
        ],
        "consent": [
            {"id": "tyre_restore", "ranges": tyre, "level": "warn", "text": TYRE_TEXT}
        ],
        "post": [
            {
                "type": "u8_eq",
                "addr": "0x0FE600",
                "value": 0,
                "name": "AutoBlip - Enable ships 0",
            },
            {
                "type": "f_le",
                "addr": "0x0FE604",
                "x": 25.0,
                "name": "Max Synthetic Pedal <= 25 %",
            },
        ],
        "checksums": {
            "table": "0x0FF650",
            "records": 35,
            "algorithm": "ncflash-sum32-v1",
        },
        "installed_signature": {"kind": "code", "sha256": sig},
        "definitions": autoblip_definitions(xml_path),
        "next_steps": [
            "Flash this file, then drive with AutoBlip off (Enable = 0).",
            "Log the confirmed gear in every gear (5th at several speeds).",
            "Set every cell of TP Delta - Fault Threshold to 22.",
            "Then set AutoBlip - Enable = 1 and flash again.",
        ],
        "footprint": {
            "rom": [
                [f"0x{a:06X}", f"0x{b:06X}"]
                for (a, b), r in zip(spans, ranges)
                if r["kind"] in ("code", "cal", "settings", "erase", "hook")
            ]
        },
    }
    out = HERE / "lf9veb_autoblip_3_4_fixture.ncpatch.json"
    out.write_text(json.dumps(d, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(out, hashlib.sha256(out.read_bytes()).hexdigest())


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
