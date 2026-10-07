"""The bundled NC Flash feature pack 1.0.0 (patches/ncflash-features/1.0.0/).

The files come byte for byte from nc-flash-re a61ec85 (channel test). LF9VEB
is checked end to end from examples/ (factory ROM + Romdrop XOR); LFG2EP and
LFH9EF get load, pin and signature checks (no base ROM ships with NC Flash).

The cars already running AutoBlip 3.4 are checked only when their images are
available: set NCFLASH_CAR_IMAGES to a folder holding them (they are owner and
friends' tunes, not committed here).
"""

import hashlib
import json
import os
from pathlib import Path

import pytest

from src.core.definition_overlay import parse_fragment
from src.ecu import ncflash_patch as N
from src.ecu.bundled_patches import PINS

from test_ncflash_patch import R0_SHA, R1_SHA, REPO, _r0

ROOT = REPO / "patches"
PACK = "ncflash-features/1.0.0/{}.ncpatch.json"
IDS = ["lf9veb", "lfg2ep", "lfh9ef"]


def _pack(rid: str) -> N.Patch:
    rel = PACK.format(rid)
    return N.load_patch((ROOT / rel).read_bytes(), PINS[rel])


@pytest.mark.parametrize("rid", IDS)
def test_pack_loads_pinned(rid):
    p = _pack(rid)
    assert p.rom_id == rid.upper() and p.id == "ncflash-features"
    assert p.version == "1.0.0" and p.channel == "test"
    assert p.summary == "NC Flash features 1.0.0 (AutoBlip 3.4)"
    assert p.romdrop_rev == "21053000"


@pytest.mark.parametrize("rid", IDS)
def test_signature_is_the_code_new_bytes(rid):
    p = _pack(rid)
    code = b"".join(r.new for r in p.ranges_of("code"))
    assert hashlib.sha256(code).hexdigest() == p.signature


@pytest.mark.parametrize("rid", IDS)
def test_pack_contents(rid):
    p = _pack(rid)
    # the boot block is never pinned (flashers don't write it; car files differ)
    assert min(lo for lo, _ in p.code_pin.regions) == 0x2000
    assert p.data_pin.level == "warn"
    # every feature ships disabled, and the post checks hold that
    (feat,) = p.features
    assert feat["enable"]["addr"] == "0x0FE600"
    assert any(c["type"] == "u8_eq" and c["value"] == 0 for c in p.data["post"])
    # the tyre restore needs consent; its definitions are grouped per feature
    assert [c["id"] for c in p.consents] == ["tyre_restore"]
    _, tables = parse_fragment(p.definitions, "t")
    assert {t.category for t in tables} == {"NC Flash Features - AutoBlip"}
    assert any(t.name == "AutoBlip - Enable" for t in tables)
    # footprint.rom covers every written block, footprint.hooks every hook literal
    for r in p.ranges_of("code", "cal", "settings", "erase"):
        assert any(lo <= r.addr and r.end <= hi for lo, hi in p.footprint)
    hooks = {N.num(a) for a in p.data["footprint"]["hooks"]}
    assert hooks == {r.addr for r in p.ranges_of("hook")}


def test_lf9veb_golden_chain():
    p = _pack("lf9veb")
    assert p.data["reference"]["base_sha256"] == R0_SHA
    assert p.data["reference"]["result_sha256"] == R1_SHA
    assert N.detect(_r0(), p).state == "available"
    res = N.apply_patch(_r0(), p, {"tyre_restore"})
    assert res.verdict == "WARN"
    assert hashlib.sha256(res.output).hexdigest() == R1_SHA
    st = N.detect(res.output, p)
    assert st.state == "installed" and st.detail == "NC Flash features 1.0.0 installed"
    assert [(s.id, s.enabled) for s in N.feature_states(res.output, p)] == [
        ("autoblip", False)
    ]
    without = N.apply_patch(_r0(), p)
    assert without.verdict == "REFUSED" and without.output is None


def test_lf9veb_factory_rom_needs_romdrop():
    factory = (REPO / "examples" / "lf9veb.bin").read_bytes()
    st = N.detect(factory, _pack("lf9veb"))
    assert st.state == "unavailable" and "install Romdrop first" in st.detail


def test_index_matches_the_files():
    idx = json.loads((ROOT / "index.json").read_bytes())
    assert {e["rom_id"] for e in idx["patches"]} == {r.upper() for r in IDS}
    for e in idx["patches"]:
        assert (
            hashlib.sha256((ROOT / e["file"]).read_bytes()).hexdigest() == e["sha256"]
        )


CARS = {
    # owner's car, v3.4 image a09154c6 (AutoBlip enabled)
    "lf9veb": ("lf9veb_car_autoblip_v3_4.bin", "a09154c61a8b1251", True),
    "lfg2ep": (
        "lfg2ep_njamesformic_autoblip_v3_4_enable0.bin",
        "fe07fcb12bf04742",
        False,
    ),
    "lfh9ef": (
        "lfh9ef_aleksander_autoblip_v3_4_enable0.bin",
        "7346efc526089eca",
        False,
    ),
}


@pytest.mark.parametrize("rid", IDS)
def test_cars_on_v3_4_show_the_pack_installed(rid):
    folder = os.environ.get("NCFLASH_CAR_IMAGES")
    name, sha16, enabled = CARS[rid]
    if not folder or not (Path(folder) / name).is_file():
        pytest.skip("car images not available (set NCFLASH_CAR_IMAGES)")
    d = (Path(folder) / name).read_bytes()
    assert hashlib.sha256(d).hexdigest().startswith(sha16)
    p = _pack(rid)
    st = N.detect(d, p)
    assert st.state == "installed" and st.detail == "NC Flash features 1.0.0 installed"
    assert N.feature_states(d, p)[0].enabled is enabled
