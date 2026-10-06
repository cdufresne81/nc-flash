"""The NC Flash patches this release ships, sha256-pinned.

The files live in ``patches/`` at the app root (bundled by NCFlash.spec):
``patches/index.json`` plus ``patches/ncflash-features/<version>/<romid>.ncpatch.json``
(one feature pack per ROM ID), copied byte for byte
from nc-flash-re ``roms/ncflash_patches/`` (no JSON re-serialisation). The install folder can be
user-writable, so the pins live here, in code: a file whose sha256 is not in
PINS is never loaded. Adding or updating a patch means a new NC Flash release
that updates both the file and its pin (tests/test_bundled_patches.py checks
they agree).
"""

from functools import lru_cache

from ..utils.paths import get_app_root
from .ncflash_patch import Catalog, load_catalog

# Path relative to patches/ (forward slashes) -> sha256 of the exact bytes.
PINS = {
    "index.json": "2e79638fa32ea4c88afd0a6fa88ecd8b78016f479f46cc1eab3601ac50041c4e",
}


def patches_dir():
    return get_app_root() / "patches"


@lru_cache(maxsize=1)
def bundled_catalog() -> Catalog:
    """The pinned bundled patches (loaded once per run)."""
    return load_catalog(patches_dir(), PINS)


def patches_for(cal_id: str) -> list:
    """Bundled patches for one ROM ID (cal ID, e.g. 'LF9VEB')."""
    return [p for p in bundled_catalog().patches if p.rom_id == cal_id]
