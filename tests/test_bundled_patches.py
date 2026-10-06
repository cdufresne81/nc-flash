"""The bundled patches (patches/) agree with the pins in src/ecu/bundled_patches.py.

A release that adds or updates a patch file without its pin (or the reverse)
fails here, before users get a patch that NC Flash refuses to load.
"""

import hashlib
import json
from pathlib import Path

from src.ecu import bundled_patches as B
from src.ecu import ncflash_patch as N

ROOT = Path(__file__).resolve().parent.parent / "patches"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_every_bundled_file_is_pinned_and_every_pin_has_a_file():
    files = {p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*") if p.is_file()}
    assert files == set(B.PINS)
    for rel, pin in B.PINS.items():
        assert _sha(ROOT / rel) == pin, rel


def test_index_lists_every_patch_file_with_its_pin():
    idx = json.loads((ROOT / "index.json").read_bytes())
    assert idx["format"] == "ncflash-patch-index/1"
    listed = {e["file"]: e["sha256"] for e in idx["patches"]}
    assert listed == {k: v for k, v in B.PINS.items() if k != "index.json"}


def test_catalog_loads_without_problems():
    cat = N.load_catalog(ROOT, B.PINS)
    assert cat.problems == []
    for p in cat.patches:
        assert p.data["patch"]["id"] and p.rom_id
