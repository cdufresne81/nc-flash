#!/usr/bin/env python3
"""Turn the community NC MX-5 OBD parameter list into the Datalogger catalog.

Source: ``opconfig_ncmx5_obd_pids.xlsx``, a community list of Tactrix-style
(logcfg) parameters for the Mazda NC MX-5 ECU, shared by the user's contact.
The spreadsheet is first dumped to TSV (one row per parameter, the header row
``;select  paramname  ;description  mode  paramid  isfloat  databits
offsetbits  scalingrpn  ;scaling  ;units  ...``). This tool reads that TSV and
writes ``examples/datalog_catalog_ncmx5.json``, which
:func:`src.core.datalog_channels.catalog_channels` loads.

How a row becomes a channel:

* ``databits`` / ``offsetbits`` place the raw value in the reply data (the bytes
  after the echoed mode + PID/DID). A 1-bit field is byte ``offsetbits // 8``,
  bit ``offsetbits % 8`` counted from the least significant bit. That reading
  matches the list's own monitor-status rows (PID 0x41: bit 8 = misfire test
  available, the LSB of byte B, as in SAE J1979) and the built-in HIDET_SW /
  IMTV_SW bits, which were checked on the bench ECU.
* The infix ``;scaling`` column (A, B, C, D = data bytes) gives the bytes and
  the formula; the RPN ``scalingrpn`` column is evaluated too, and any row where
  the two disagree is printed so it gets a human look.
* ``CORRECTIONS`` / ``DESCRIPTIONS`` / ``UNITS`` below are deliberate fixes to
  the source, each with its reason.

Usage::

    python tools/import_opconfig.py path/to/pids.tsv [--out FILE]

The output is deterministic (sorted by name, fixed formatting), so a rerun on
the same TSV gives a byte-identical file.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.core.datalog_channels import (  # noqa: E402
    Channel,
    ChannelError,
    compile_formula,
    to_firmware_row,
)

DEFAULT_OUT = _REPO_ROOT / "examples" / "datalog_catalog_ncmx5.json"

SOURCE = "opconfig_ncmx5_obd_pids.xlsx, community list shared by the user's contact"

EXCLUDED_PREFIXES = ("ECU_", "PID_", "MS_DRIVE_")

#: Not engine datalogging channels: trouble-code counters, supported-PID
#: bitmaps, immobilizer state, configuration values and the EVAP leak-test
#: (LDP) results.
EXCLUDED_NAMES = {
    "DTC_CLR_DIST", "DTC_CLR_WARMUPS", "DTC_CNT", "DTC_CNTD", "DTC_FREEZE",
    "MIL_DIST", "MS_DTC_CLEARED", "O2S_PRESENT", "OBD_STANDARDS",
    "PIDS_01_T0_20", "PIDS_21_to_40", "PIDS_41_to_60", "PATSOUT", "SECSTAT",
    "SECTIME", "ENGINE_STATUS", "TIRESIZE", "LDP_EVAPCP", "LDP_IDL_current",
    "LDP_MON_current", "LDP_REF_current", "LDP_SL_DV", "LDP_VSL_DV",
    "LDP_VSL_FV", "LDP_VSL_SV",
}  # fmt: skip

#: Formula fixes, applied after conversion: name -> (formula, reason).
CORRECTIONS = {
    "CAT11_DSD": (
        "(x*0.1)-40",
        "SAE J1979 PID 0x3C is (256A+B)/10 - 40; the list adds 40",
    ),
    "FLI": ("x*0.39215684", "SAE J1979 PID 0x2F is A*100/255 %; the list has raw A"),
    "TPCT": (
        "x*0.000076293945",
        "the list scales it as % and reads 672 % on the bench ECU; with the "
        "5/65536 V scale of the other sensor voltages (BARO_V, ECT_V) the same "
        "raw value is 0.50 V, a normal closed-throttle voltage. Not car-checked",
    ),
    "EGRP_DSD": (
        "x*0.39215684",
        "SAE J1979 PID 0x2C is A*100/255 %; the list has raw A",
    ),
}

#: Description fixes: spelling, swapped texts (described by the NAME; which
#: bit really carries which switch still needs a car test) and empty ones
#: whose meaning is certain.
DESCRIPTIONS = {
    "BPA_SW": "Brake Pressure Applied Switch",  # list: "Clutch Pedal Position Switch"
    "CPP_SW": "Clutch Pedal Position Switch",  # list: "Brake Pressure Applied Switch"
    "IVS_SW": "Idle Validation Switch",  # list: "Park Neutral Position Switch"
    "PNP_SW": "Park Neutral Position Switch",  # list: "Idle Validation Switch"
    "O2S12_A_V": "Rear O2 Sensor Voltage",  # list: "Votlage"
    "PSP_SW": "Power Steering Position Switch",  # list: "Stearing"
    "SCCS_V": "Cruise Control Switch Voltage (+OP2 Trigger)",  # list: "Tigger"
    "EQ_RATIO_ACT": "Air Fuel Equivalence Ratio (lambda) Actual",
    "EQ_RATIO_ACT_mA": "Front O2 Sensor Current",
    "FAN1_SW": "Cooling Fan Relay 1 Control",
    "FAN2_SW": "Cooling Fan Relay 2 Control",
    "FAN3_SW": "Cooling Fan Relay 3 Control",
}

#: Unit fixes by name (empty in the list, meaning certain).
UNITS = {"IDLE": "rpm", "TPCT": "V"}

#: Unit spellings normalized to the built-in channels' plain-ASCII style.
UNIT_ALIASES = {
    "off / on": "on/off",
    "g/sec": "g/s",
    "secs": "s",
    "(angle)": "deg",
    "(retard)°": "deg",
    "°": "deg",
}

#: Constants the list stores as float32 round-off, replaced by the exact value.
CONSTANTS = {
    "0.0099999998": "0.01",
    "0.00099999993": "0.001",
    "0.099999994": "0.1",
    "0.0079999994": "0.008",
    "0.0049999999": "0.005",
}

_BYTE_PATTERNS = (
    # (infix text, offset, size)
    ("((A*256)+B)", 0, 2),
    ("((C*256)+D)", 2, 2),
)


def _num(text: str) -> int:
    return int(float(text)) if text.strip() else 0


def _rpn(expr: str, x: float) -> float:
    stack: list[float] = []
    for tok in [t.strip() for t in expr.split(",") if t.strip()]:
        if tok == "x":
            stack.append(x)
        elif tok in "+-*/":
            b, a = stack.pop(), stack.pop()
            stack.append({"+": a + b, "-": a - b, "*": a * b, "/": a / b}[tok])
        else:
            stack.append(float(tok))
    return stack[-1]


def _clean_constants(formula: str) -> str:
    return re.sub(r"\d+\.\d+", lambda m: CONSTANTS.get(m.group(0), m.group(0)), formula)


def _infix_to_channel(infix: str, databits: int, offsetbits: int):
    """(offset, size, bit, formula) from the ``;scaling`` column."""
    if databits == 1:
        if infix:
            raise ValueError(f"bit field with a formula {infix!r}")
        return offsetbits // 8, 1, offsetbits % 8, "x"
    if offsetbits % 8 or databits % 8:
        raise ValueError(f"not byte aligned ({databits} bits at {offsetbits})")
    if not infix:
        return offsetbits // 8, databits // 8, None, "x"
    text = infix.replace(" ", "")
    for pattern, offset, size in _BYTE_PATTERNS:
        if pattern in text:
            formula = text.replace(pattern, "x")
            break
    else:
        letters = set(re.findall(r"[A-D]", text))
        if len(letters) != 1:
            raise ValueError(f"can't map {infix!r} to one raw value")
        letter = letters.pop()
        offset, size = "ABCD".index(letter), 1
        formula = text.replace(letter, "x")
    if re.search(r"[A-Z]", formula):
        raise ValueError(f"can't map {infix!r} to one raw value")
    return offsetbits // 8 + offset, size, None, formula


def _rpn_mismatch(row: dict, ch: Channel) -> str:
    """A note when the RPN column disagrees with the converted channel."""
    rpn = row["scalingrpn"].strip()
    if not rpn or ch.bit is not None:
        return ""
    databits, offsetbits = _num(row["databits"]), _num(row["offsetbits"])
    width = databits // 8
    for data in (bytes([0x12, 0x34, 0x56, 0x78]), bytes([0xC8, 0x05, 0x80, 0x00])):
        full = int.from_bytes(data[offsetbits // 8 :][:width], "big")
        want = _rpn(rpn, full)
        got = ch.decode(data + bytes(8))
        if got is None or abs(want - got) > max(1e-6, abs(want) * 0.001):
            return f"RPN {rpn!r} gives {want:g}, channel gives {got} for {data.hex()}"
    return ""


def convert(rows: list[dict]) -> tuple[list[dict], list[str], list[str]]:
    """(catalog entries, dropped notes, warnings)."""
    entries: dict[str, dict] = {}
    dropped: list[str] = []
    warnings: list[str] = []
    for row in rows:
        name = row["paramname"].strip()
        if not name or name.startswith(EXCLUDED_PREFIXES) or name in EXCLUDED_NAMES:
            continue
        try:
            offset, size, bit, formula = _infix_to_channel(
                row[";scaling"].strip(), _num(row["databits"]), _num(row["offsetbits"])
            )
        except ValueError as exc:
            dropped.append(f"{name}: {exc}")
            continue
        formula = _clean_constants(formula)
        unit = row[";units"].strip()
        ch = Channel(
            name=name,
            mode=int(row["mode"], 16),
            ident=int(row["paramid"], 16),
            size=size,
            offset=offset,
            bit=bit,
            formula=formula,
            unit=UNITS.get(name, UNIT_ALIASES.get(unit, unit)),
            description=DESCRIPTIONS.get(name, " ".join(row[";description"].split())),
        )
        note = _rpn_mismatch(row, ch)
        if note:
            warnings.append(f"{name}: {note} (kept the ;scaling column)")
        if name in CORRECTIONS:
            ch.formula, reason = CORRECTIONS[name]
            warnings.append(f"{name}: formula corrected to {ch.formula!r}: {reason}")
        try:
            ch.validate()
            compile_formula(ch.formula)
        except ChannelError as exc:
            dropped.append(f"{name}: {exc}")
            continue
        width = {0x01: 2, 0x22: 4, 0x23: 8}[ch.mode]
        entry = {
            "name": ch.name,
            "mode": f"0x{ch.mode:02X}",
            "ident": f"0x{ch.ident:0{width}X}",
            "size": ch.size,
            "offset": ch.offset,
            "kind": ch.kind,
            "bit": ch.bit,
            "formula": ch.formula,
            "unit": ch.unit,
            "description": ch.description,
        }
        try:
            to_firmware_row(ch)
        except ChannelError as exc:
            entry["host_only"] = str(exc)
        if name in entries:
            dropped.append(f"{name}: duplicate name, kept the first row")
            continue
        entries[name] = entry
    return [entries[n] for n in sorted(entries, key=str.lower)], dropped, warnings


def render(entries: list[dict]) -> str:
    lines = [
        "{",
        f'  "source": {json.dumps(SOURCE)},',
        '  "format": 1,',
        '  "channels": [',
    ]
    body = [
        "    " + json.dumps(e, ensure_ascii=True, separators=(", ", ": "))
        for e in entries
    ]
    lines.append(",\n".join(body))
    lines += ["  ]", "}", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("tsv", type=Path, help="the spreadsheet dumped to TSV")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    with args.tsv.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    entries, dropped, warnings = convert(rows)
    args.out.write_text(render(entries), encoding="utf-8", newline="\n")
    print(f"{len(rows)} rows read, {len(entries)} channels written to {args.out}")
    for line in dropped:
        print(f"DROPPED  {line}")
    for line in warnings:
        print(f"NOTE     {line}")
    host_only = [e["name"] for e in entries if "host_only" in e]
    print(f"host-only: {', '.join(host_only) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
