"""The WiCAN fast-flash package (src/ecu/fast_flash_package.py).

* Byte-identical to the firmware project's reference packer
  (nc-flash-wican-fw ``tools/fast_flash_package.py``) for the same inputs. The
  firmware tool is found through ``NCFF_FW_PACKER`` or the usual checkout paths;
  the comparison is skipped when it is not on this machine.
* Self-consistent the way the firmware checks it: every stream decodes to its
  image range, every CRC matches, every range obeys the hard rules, and the
  commit page chains to the compare CRCs.
"""

import importlib.util
import json
import lzma
import os
import random
import zlib
from pathlib import Path

import pytest

from src.ecu import fast_flash_package as ffp
from src.ecu.exceptions import FlashError

KERNEL_LEN = 0x1800


def _kernel(seed=7) -> bytes:
    rng = random.Random(seed)
    k = bytearray(rng.randbytes(KERNEL_LEN))
    for i in range(len(k) - 7):
        if k[i : i + 7] == b"SBL_END":
            k[i] ^= 0xFF
    k[KERNEL_LEN - 0x40 : KERNEL_LEN - 0x38] = b"*SBL_END"
    return bytes(k)


def _image(seed=11) -> bytes:
    rng = random.Random(seed)
    img = bytearray(0x100000)
    for chunk in range(0, 0x100000, 0x800):
        r = rng.random()
        if r < 0.3:
            img[chunk : chunk + 0x800] = rng.randbytes(0x800)
        elif r < 0.6:
            pat = rng.randbytes(rng.choice((2, 4, 8)))
            img[chunk : chunk + 0x800] = (pat * (0x800 // len(pat) + 1))[:0x800]
        else:
            img[chunk : chunk + 0x800] = b"\xff" * 0x800
    img[0xFFB00:0xFFB08] = b"\x00\x01\xff\xfe_WDS"
    img[0xFFF80:0x100000] = rng.randbytes(120) + b"\x00\x00\x40\x00\x00\x00\x20\x00"
    return bytes(img)


def _firmware_packer():
    candidates = [os.environ.get("NCFF_FW_PACKER", "")]
    home = Path.home()
    candidates += [
        str(home / ".claude/worktrees/wican-fw-fastflash/tools/fast_flash_package.py"),
        str(home / "Projets/nc-flash-wican-fw/tools/fast_flash_package.py"),
        str(
            Path(__file__).resolve().parents[2]
            / "nc-flash-wican-fw/tools/fast_flash_package.py"
        ),
    ]
    for c in candidates:
        if c and Path(c).is_file():
            spec = importlib.util.spec_from_file_location("fw_fast_flash_package", c)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    return None


@pytest.fixture(scope="module")
def built():
    image, kernel = _image(), _kernel()
    pkg, man = ffp.build(image, kernel, cal_id="TEST", generation="NC2")
    return image, kernel, pkg, man


class TestByteIdenticalToFirmwareTool:
    def test_same_bytes_as_the_firmware_reference_packer(self, built):
        fw = _firmware_packer()
        if fw is None:
            pytest.skip("firmware tools/fast_flash_package.py not on this machine")
        image, kernel, pkg, man = built
        fw_pkg, fw_man = fw.build(image, kernel, cal_id="TEST", generation="NC2")
        assert pkg == fw_pkg
        assert ffp.manifest_bytes(man) == fw.manifest_bytes(fw_man)

    @pytest.mark.parametrize(
        "kw", [{"boot_policy": "allow"}, {"rewrite_all": True}, {"cal_id": "LF9VEB"}]
    )
    def test_options_match_too(self, kw):
        fw = _firmware_packer()
        if fw is None:
            pytest.skip("firmware tools/fast_flash_package.py not on this machine")
        image, kernel = _image(3), _kernel(4)
        pkg, man = ffp.build(image, kernel, **kw)
        fw_pkg, fw_man = fw.build(image, kernel, **kw)
        assert pkg == fw_pkg
        assert ffp.manifest_bytes(man) == fw.manifest_bytes(fw_man)


class TestSelfConsistent:
    def test_streams_decode_to_the_image(self, built):
        image, _, pkg, man = built
        filt = [
            {"id": lzma.FILTER_LZMA1, "lc": 0, "lp": 2, "pb": 2, "dict_size": 1 << 20}
        ]
        n = 0
        for b in man["blocks"]:
            for r in b["ranges"]:
                s = pkg[r["off"] : r["off"] + r["len"]]
                assert zlib.crc32(s) == r["crc32"]
                assert s[0] == 0  # a range-coder stream starts with 0
                d = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=filt)
                out = d.decompress(s, max_length=r["size"])
                assert out == image[r["addr"] : r["addr"] + r["size"]]
                n += 1
        assert n == 15  # blocks 2..14 + two ranges in block 15

    def test_crcs_and_rules(self, built):
        image, kernel, pkg, man = built
        assert (
            man["package_len"] == len(pkg) and zlib.crc32(pkg) == man["package_crc32"]
        )
        k = man["kernel"]
        assert pkg[k["off"] : k["off"] + k["len"]] == kernel and k["version"] == 3
        assert sorted(b["block"] for b in man["blocks"]) == list(range(2, 16))
        for b in man["blocks"]:
            for a, n, c in b["cmp"] + b.get("cmp_pre", []):
                assert zlib.crc32(image[a : a + n]) == c
            for r in b["ranges"]:
                a, z = r["addr"], r["addr"] + r["size"]
                assert a >= 0x2000 and z <= 0xFFF80 and a % 128 == 0 and z % 128 == 0
                assert not (a < 0xFFB80 and z > 0xFFB00)  # never the counter page
        c = man["commit"]
        page = pkg[c["off"] : c["off"] + 128]
        assert c["addr"] == 0xFFF80 and page == image[0xFFF80:]
        assert man["boot"] == {"crc32": zlib.crc32(image[:0x2000]), "policy": "refuse"}
        # the firmware's chain rule: block 15's second "after" CRC = its "before"
        # CRC continued over the commit page
        b15 = man["blocks"][-1]
        assert zlib.crc32(page, b15["cmp_pre"][1][2]) == b15["cmp"][1][2]
        assert len(ffp.manifest_bytes(man)) <= ffp.MANIFEST_MAX
        assert json.loads(ffp.manifest_bytes(man)) == man

    def test_refusals(self):
        image, kernel = _image(), _kernel()
        blank = bytearray(image)
        blank[0xFFFFC:] = b"\xff" * 4
        with pytest.raises(FlashError):
            ffp.build(bytes(blank), kernel)
        with pytest.raises(FlashError):
            ffp.build(image[:-1], kernel)
        with pytest.raises(FlashError):
            ffp.build(image, kernel, boot_policy="maybe")
        with pytest.raises(FlashError):
            ffp.build(image, kernel, kernel_version=2)
        with pytest.raises(FlashError):
            ffp.build(image, kernel[:-0x400])  # marker gone from the last block


class TestNamesAndRomPrep:
    def test_staged_name_is_firmware_safe(self):
        n = ffp.staged_name("SW-LF9VEB/000 ü..", when=0)
        assert n.startswith("ff_SW-LF9VEB-000") and n.endswith(".ffp")
        assert all(ch.isalnum() or ch in "-_." for ch in n) and ".." not in n
        assert len(ffp.staged_name("X" * 300)) <= 96

    def test_package_rom_uses_the_prepared_image(self, sample_rom_path):
        from src.ecu.constants import ROM_FLASH_START_MIN
        from src.ecu.flash_prep import prepare_flash_image
        from src.ecu.rom_utils import rom_hardware_generation

        rom = sample_rom_path.read_bytes()
        p = ffp.package_rom(rom, _kernel(), 3, name="t.ffp")
        corrected = prepare_flash_image(rom, ROM_FLASH_START_MIN)[0]
        assert p.name == "t.ffp" and p.manifest_name == "t.json"
        assert p.manifest["generation"] == rom_hardware_generation(corrected)
        assert p.manifest_bytes == ffp.manifest_bytes(p.manifest)
        c = p.manifest["commit"]
        assert p.data[c["off"] : c["off"] + 128] == corrected[0xFFF80:]
        for b in p.manifest["blocks"]:
            for a, n, crc in b["cmp"]:
                assert zlib.crc32(corrected[a : a + n]) == crc
