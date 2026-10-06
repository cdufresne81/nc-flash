"""Tests for the patch definition overlay (src/core/definition_overlay.py)."""

import json
from pathlib import Path

import pytest

from src.core.definition_overlay import overlay, parse_fragment, stale_tables
from src.core.definition_parser import load_definition
from src.core.exceptions import DefinitionParseError

REPO = Path(__file__).resolve().parent.parent
LF9VEB_XML = REPO / "examples" / "metadata" / "lf9veb.xml"
FIXTURE = (
    REPO
    / "tests"
    / "fixtures"
    / "ncflash_patch"
    / "lf9veb_ncflash_features_1_0_0_fixture.ncpatch.json"
)


def _defs() -> str:
    return json.loads(FIXTURE.read_bytes())["definitions"]


def test_fragment_parses_the_embedded_autoblip_tables():
    scalings, tables = parse_fragment(_defs(), "ncflash-features 1.0.0")
    assert "autoblip_enable" in scalings
    names = {t.name for t in tables}
    assert "AutoBlip - Enable" in names and len(tables) == 16


def test_overlay_adds_tables_without_touching_the_base():
    base = load_definition(str(LF9VEB_XML))
    n = len(base.tables)
    merged, added, skipped = overlay(base, _defs(), "ncflash-features 1.0.0")
    assert len(base.tables) == n  # base unchanged
    assert len(merged.tables) == n + 16 and len(added) == 16 and skipped == []
    enable = merged.get_table_by_name("AutoBlip - Enable")
    assert enable.address_int == 0xFE600
    # renamed scaling: the scaling editor can never write it into lf9veb.xml
    assert enable.scaling == "ncflash-features 1.0.0:autoblip_enable"
    assert merged.get_scaling(enable.scaling).storagetype == "uint8"
    curve = merged.get_table_by_name("AutoBlip - Synthetic Pedal vs RPM Error")
    assert all(c.scaling.startswith("ncflash-features 1.0.0:") for c in curve.children)
    assert merged.xml_path == base.xml_path


def test_user_definition_wins_on_name_or_address(tmp_path):
    base = load_definition(str(LF9VEB_XML))
    once, _, _ = overlay(base, _defs(), "ncflash-features 1.0.0")
    # the owner's installed lf9veb.xml already defines the v3.4 tables
    twice, added, skipped = overlay(once, _defs(), "ncflash-features 1.0.0")
    assert added == [] and len(skipped) == 16
    assert len(twice.tables) == len(once.tables)


def test_address_clash_is_skipped():
    base = load_definition(str(LF9VEB_XML))
    frag = (
        '<scaling name="s" storagetype="uint8" toexpr="x" frexpr="x"/>'
        f'<table name="Clash" type="1D" address="{base.tables[0].address}" elements="1" scaling="s"/>'
    )
    _, added, skipped = overlay(base, frag, "t")
    assert added == [] and "already has a table at" in skipped[0]


def test_bad_xml_raises():
    with pytest.raises(DefinitionParseError):
        parse_fragment("<table", "t")


def test_tables_are_grouped_by_feature():
    _, tables = parse_fragment(_defs(), "ncflash-features 1.0.0")
    assert {t.category for t in tables} == {"NC Flash Features - AutoBlip"}


def _footprint():
    from src.ecu import ncflash_patch as N

    raw = FIXTURE.read_bytes()
    return N.load_patch(raw, __import__("hashlib").sha256(raw).hexdigest()).footprint


def test_stale_v1_tables_inside_the_pack_area_are_flagged():
    base = load_definition(str(LF9VEB_XML))
    tag = "ncflash-features 1.0.0"
    has_v1 = base.get_table_by_name("Auto-Blip - Enable") is not None
    stale = stale_tables(base, _footprint(), _defs(), tag)
    if has_v1:  # the retired v1 block (removed from the bundled file separately)
        assert any(s.startswith("Auto-Blip - Enable at 0xFCAC0") for s in stale)
    merged, _, _ = overlay(base, _defs(), tag)
    # the pack's own tables are never flagged
    assert stale_tables(merged, _footprint(), _defs(), tag) == stale
    assert not any(s.startswith("AutoBlip - ") for s in stale)
