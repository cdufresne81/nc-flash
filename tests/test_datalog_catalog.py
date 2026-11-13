"""The shipped NC MX-5 channel catalog (examples/datalog_catalog_ncmx5.json)."""

import json
import logging

import pytest

from src.core.datalog_channels import (
    MAX_NAME_LEN,
    MODE_DID,
    MODE_OBD,
    RESERVED_NAMES,
    builtin_channels,
    catalog_channels,
    to_firmware_row,
)
from src.utils.paths import get_app_root
from tools.import_opconfig import EXCLUDED_NAMES, EXCLUDED_PREFIXES, render

CATALOG_PATH = get_app_root() / "examples" / "datalog_catalog_ncmx5.json"


@pytest.fixture(scope="module")
def raw_entries():
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))["channels"]


@pytest.fixture(scope="module")
def catalog():
    return {c.name: c for c in catalog_channels()}


def test_every_entry_loads_and_validates(raw_entries, catalog):
    assert len(raw_entries) == 77
    assert len(catalog) == len(raw_entries)  # none skipped as invalid
    for ch in catalog.values():
        ch.validate()
        assert not ch.builtin


def test_every_entry_goes_to_the_wican_or_says_why_not(raw_entries, catalog):
    for entry in raw_entries:
        ch = catalog[entry["name"]]
        if "host_only" in entry:
            assert entry["host_only"].strip(), entry["name"]
            continue
        row = to_firmware_row(ch)
        assert row["Name"] == ch.name


def test_names_unique_short_and_not_reserved(raw_entries):
    names = [e["name"] for e in raw_entries]
    assert len(names) == len(set(names))
    for name in names:
        assert name == name.strip()
        assert len(name) <= MAX_NAME_LEN
        assert name.lower() not in RESERVED_NAMES


def test_excluded_rows_are_absent(catalog):
    for name in catalog:
        assert not name.startswith(EXCLUDED_PREFIXES), name
        assert name not in EXCLUDED_NAMES, name


def test_file_is_in_the_tools_canonical_format(raw_entries):
    # A hand edit that breaks the sorted, one-entry-per-line layout shows here.
    assert CATALOG_PATH.read_text(encoding="utf-8") == render(raw_entries)
    names = [e["name"] for e in raw_entries]
    assert names == sorted(names, key=str.lower)


def test_descriptions_fixed(catalog):
    text = " ".join(c.description for c in catalog.values())
    for typo in ("Votlage", "Stearing", "Tigger"):
        assert typo not in text
    assert catalog["CPP_SW"].description == "Clutch Pedal Position Switch"
    assert catalog["BPA_SW"].description == "Brake Pressure Applied Switch"
    assert catalog["IVS_SW"].description == "Idle Validation Switch"
    assert catalog["PNP_SW"].description == "Park Neutral Position Switch"


# --- decoding: data bytes after the PID/DID echo --------------------------------


@pytest.mark.parametrize(
    "name, data, expected",
    [
        ("RPM", "1B58", 1750.0),  # ((A*256)+B)/4
        ("ECT", "5A", 50.0),  # A-40
        ("MAF", "01F4", 5.0),  # ((A*256)+B)/100
        ("LOAD", "00FF", 1.0),  # ((A*256)+B)/255
        ("STFT", "80", 0.0),  # A*100/128-100
        ("STFT", "90", 12.5),
        ("KNOCKR", "47", 25.0),  # 71 * 0.3521126761
        ("FUEL_PW", "0100", 2.048),  # 256 * 0.008 ms
        ("SPARKADV", "94", 10.0),
        ("EQ_RATIO_ACT", "80008000", 1.0),  # lambda from bytes A-B
        ("EQ_RATIO_ACT_mA", "80008000", 0.0),  # current from bytes C-D
        ("EQ_RATIO_ACT_mA", "80008100", 1.0),
        ("CAT11_DSD", "0FA0", 360.0),  # SAE: (256A+B)/10 - 40
        ("O2S12_V", "B480", 0.9),  # SAE: A/200, B is the trim
    ],
)
def test_known_replies_decode(catalog, name, data, expected):
    assert catalog[name].decode(bytes.fromhex(data)) == pytest.approx(
        expected, abs=1e-4
    )


def test_fan_switches_share_a_did_with_different_bits(catalog):
    fan1, fan2 = catalog["FAN1_SW"], catalog["FAN2_SW"]
    assert (fan1.mode, fan1.ident) == (fan2.mode, fan2.ident) == (MODE_DID, 0x1103)
    assert (fan1.bit, fan2.bit) == (2, 3)
    assert fan1.decode(bytes([0b0100])) == 1.0
    assert fan2.decode(bytes([0b0100])) == 0.0
    assert fan1.decode(bytes([0b1000])) == 0.0
    assert fan2.decode(bytes([0b1000])) == 1.0


def test_firmware_rows_for_word_and_bit_channels(catalog):
    rpm = to_firmware_row(catalog["RPM"])
    assert rpm["PID"] == "010C1"
    assert rpm["Expression"] == "((B3*256)+B4)*0.25"
    fan2 = to_firmware_row(catalog["FAN2_SW"])
    assert fan2["PID"] == "2211031"
    assert fan2["Expression"] == "(B4>>3)&1"
    ma = to_firmware_row(catalog["EQ_RATIO_ACT_mA"])
    assert ma["Expression"] == "(((B5*256)+B6)*0.00390625)-128"


# --- agreement with the built-ins ------------------------------------------------

#: Built-ins whose catalog entry has the same read but a different formula, on
#: purpose (the built-ins are not changed by the catalog).
KNOWN_FORMULA_DIFFERENCES = {
    "FUELSYS": "built-in shows A + B/256; the list logs the raw 16-bit word",
}
#: Built-ins the catalog has under another name (same request and bytes).
RENAMED = {"TPS": "TP1", "VCT_ACT": "VT_ACT"}


def test_shared_channels_read_the_same_bytes_as_the_builtins(catalog):
    samples = [bytes.fromhex(h) for h in ("12345678", "C8058000", "00FF7F01")]
    for b in builtin_channels():
        c = catalog[RENAMED.get(b.name, b.name)]
        read = ("mode", "ident", "size", "offset", "kind", "bit")
        assert [getattr(c, k) for k in read] == [getattr(b, k) for k in read], b.name
        if b.name in KNOWN_FORMULA_DIFFERENCES:
            continue
        for data in samples:
            assert c.decode(data) == pytest.approx(b.decode(data), rel=1e-6), b.name


# --- loader robustness -------------------------------------------------------------


def test_bad_entries_are_skipped_not_fatal(tmp_path, caplog):
    good = {"name": "OK", "mode": "0x01", "ident": "0x0C", "size": 2}
    path = tmp_path / "cat.json"
    path.write_text(
        json.dumps(
            {
                "channels": [
                    good,
                    {"name": "BADSIZE", "mode": "0x01", "ident": "0x0C", "size": 9},
                    {"name": "BADFORMULA", "mode": 1, "ident": 5, "formula": "x**2"},
                    {"name": "NOMODE", "ident": 5},
                    "not a dict",
                    dict(good),  # duplicate name
                ]
            }
        ),
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        channels = catalog_channels(path)
    assert [c.name for c in channels] == ["OK"]
    assert channels[0].mode == MODE_OBD and channels[0].ident == 0x0C
    assert len(caplog.records) == 5


def test_missing_or_broken_file_gives_an_empty_list(tmp_path):
    assert catalog_channels(tmp_path / "nope.json") == []
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert catalog_channels(broken) == []
