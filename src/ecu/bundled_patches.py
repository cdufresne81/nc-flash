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
# NC Flash features 1.0.0 (= AutoBlip 3.4, channel test): nc-flash-re a61ec85,
# roms/ncflash_patches/ (not yet reviewed for release).
PINS = {
    "index.json": "c8c41c4449cbbfb18c2bcc2e2d4f551ab463a1fde5925e8296fd5a6e20180292",
    "ncflash-features/1.0.0/lf9veb.ncpatch.json": (
        "18deb93a66c54576830d822f50332f50d296cd2cfdfb83e2beba5048857e6c14"
    ),
    "ncflash-features/1.0.0/lfg2ep.ncpatch.json": (
        "9e01a1e3ce48290467678af3b3aa1ec534a3329c08fad38fc3407c08b5851fda"
    ),
    "ncflash-features/1.0.0/lfh9ef.ncpatch.json": (
        "a099b06800dfb7310afafff1129671f97326e5feee4fa437d8d1db409e4c0f08"
    ),
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
