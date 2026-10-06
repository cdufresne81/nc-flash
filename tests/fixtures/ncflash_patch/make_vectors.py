"""Cross-check vectors: the nc-flash-re reference applier's verdicts, recorded.

For one v2 patch file, apply nc-flash-re tools/ncflash_apply.py (the reference
applier NC Flash ports) to a fresh Romdrop LF9VEB file (examples/lf9veb.bin XOR
examples/lf9veb.patch) and to mutations of it, and record the verdict and
output sha256 of each case. tests/test_ncflash_patch_vectors.py replays every
case through src/ecu/ncflash_patch.py and requires the same verdict and output.

    python tests/fixtures/ncflash_patch/make_vectors.py <patch.ncpatch.json> \
        <nc-flash-re tools/ncflash_apply.py> <out vectors.json>

Mutations are data, so the test rebuilds every input without the reference
applier: base r0 | factory | installed, then flip (xor 0x55 one byte), set (hex
bytes) or setf (big-endian floats) at an address.
"""

import hashlib
import importlib.util
import json
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))

from src.ecu.checksum import correct_rom_checksums  # noqa: E402
from src.ecu.rom_utils import patch_rom  # noqa: E402


def r0() -> bytes:
    ex = REPO / "examples"
    res = patch_rom(
        (ex / "lf9veb.bin").read_bytes(), (ex / "lf9veb.patch").read_bytes()
    )
    buf = bytearray(res.patched_rom)
    correct_rom_checksums(buf)
    return bytes(buf)


def build(case: dict, installed: bytes | None) -> bytes:
    base = {
        "r0": r0,
        "factory": lambda: (REPO / "examples" / "lf9veb.bin").read_bytes(),
        "installed": lambda: installed,
    }[case.get("base", "r0")]()
    d = bytearray(base)
    for m in case.get("mutations", []):
        a = int(m["addr"], 16)
        if m["op"] == "flip":
            d[a] ^= 0x55
        elif m["op"] == "set":
            v = bytes.fromhex(m["hex"])
            d[a : a + len(v)] = v
        elif m["op"] == "setf":
            struct.pack_into(f">{len(m['values'])}f", d, a, *m["values"])
        elif m["op"] == "truncate":
            del d[a:]
    return bytes(d)


def cases(pt: dict) -> list:
    rng = pt["ranges"]

    def first(kind, i=0):
        return [r for r in rng if r["kind"] == kind][i]["addr"]

    out = [
        {"name": "fresh romdrop"},
        {"name": "factory rom", "base": "factory"},
        {"name": "already installed", "base": "installed"},
        {"name": "short file", "mutations": [{"op": "truncate", "addr": "0x0FFFFC"}]},
        {
            "name": "romdrop code byte",
            "mutations": [{"op": "flip", "addr": first("romdrop")}],
        },
        {
            "name": "romdrop data byte",
            "mutations": [{"op": "flip", "addr": first("romdrop_data", 3)}],
        },
        {
            "name": "code area not blank",
            "mutations": [{"op": "set", "addr": first("code"), "hex": "00"}],
        },
        {
            "name": "rom id",
            "mutations": [{"op": "set", "addr": pt["rom_id_addr"], "hex": "4c4647"}],
        },
        {
            "name": "checksum record shape",
            "mutations": [{"op": "set", "addr": "0x0FF654", "hex": "00001000"}],
        },
        {"name": "stale sum", "mutations": [{"op": "flip", "addr": "0x0FF65B"}]},
    ]
    # every check's addresses, mutated three ways (xor, zero, 0xFF), plus 22-everywhere
    for i, c in enumerate(pt["checks"]):
        for key in ("addr", "a", "b", "ratios", "bands"):
            if key in c and isinstance(c[key], str):
                for op, hx in (
                    ("flip", None),
                    ("set", "00000000"),
                    ("set", "ffffffff"),
                ):
                    m = {"op": op, "addr": c[key]} | ({"hex": hx} if hx else {})
                    out.append(
                        {
                            "name": f"check {i} {c['type']} {key} {op} {hx or ''}".strip(),
                            "mutations": [m],
                        }
                    )
        if c["type"] == "table_all_eq":
            n = c["nx"] * c["ny"]
            out.append(
                {
                    "name": f"check {i} all {c['value']}",
                    "mutations": [
                        {"op": "setf", "addr": c["addr"], "values": [c["value"]] * n}
                    ],
                }
            )
    # one byte in every 4 KB block (code pin, data pin, tables)
    for blk in range(0, 0x100000, 0x1000):
        out.append(
            {
                "name": f"block 0x{blk:06X}",
                "mutations": [{"op": "flip", "addr": f"0x{blk + 0x7F3:06X}"}],
            }
        )
    return out


def main(patch_path, ref_path, out_path):
    spec = importlib.util.spec_from_file_location("ncflash_apply_ref", ref_path)
    ref = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ref)
    raw = Path(patch_path).read_bytes()
    pin = hashlib.sha256(raw).hexdigest()
    pt = json.loads(raw)
    consents = [c["id"] for c in pt.get("consent", [])]
    _, installed = ref.apply_bytes(raw, pin, r0(), set(consents))
    vectors = []
    for case in cases(pt):
        d = build(case, installed)
        for cons in ([], consents):
            rep, out = ref.apply_bytes(raw, pin, d, set(cons))
            vectors.append(
                case
                | {
                    "consent": cons,
                    "verdict": rep.verdict(),
                    "output_sha256": hashlib.sha256(out).hexdigest() if out else None,
                }
            )
    doc = {
        "patch_file": Path(patch_path).name,
        "patch_sha256": pin,
        "reference_applier_sha256": hashlib.sha256(
            Path(ref_path).read_bytes()
        ).hexdigest(),
        "vectors": vectors,
    }
    Path(out_path).write_text(
        json.dumps(doc, indent=1) + "\n", encoding="utf-8", newline="\n"
    )
    verdicts = {}
    for v in vectors:
        verdicts[v["verdict"]] = verdicts.get(v["verdict"], 0) + 1
    print(out_path, len(vectors), "vectors", verdicts)


if __name__ == "__main__":
    main(*sys.argv[1:4])
