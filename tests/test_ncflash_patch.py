"""Tests for the patch format v2 applier (src/ecu/ncflash_patch.py).

The LF9VEB fixture keeps the nc-flash-re v1 AutoBlip 3.4 ranges byte for byte,
so applying it to a fresh Romdrop LF9VEB file (examples/lf9veb.bin XOR
examples/lf9veb.patch) must give v1's reference image 09036104…
"""

import copy
import hashlib
import json
import struct
from functools import lru_cache
from pathlib import Path

import pytest

from src.ecu import ncflash_patch as N
from src.ecu.checksum import correct_rom_checksums
from src.ecu.rom_utils import patch_rom

REPO = Path(__file__).resolve().parent.parent
FIXTURE = (
    REPO
    / "tests"
    / "fixtures"
    / "ncflash_patch"
    / "lf9veb_ncflash_features_1_0_0_fixture.ncpatch.json"
)
R0_SHA = "68b6ec4ed663273f30b0ce522a9885e11826db96c44f7d7c3e0be3820201c7c5"
R1_SHA = "09036104c2cde71169e699ba4b6e7d785afc6b8d062d85a3b2d9f68f5f1caeec"
TYRE = {"tyre_restore"}


@lru_cache(maxsize=None)
def _r0() -> bytes:
    ex = REPO / "examples"
    res = patch_rom(
        (ex / "lf9veb.bin").read_bytes(), (ex / "lf9veb.patch").read_bytes()
    )
    buf = bytearray(res.patched_rom)
    correct_rom_checksums(buf)
    assert hashlib.sha256(buf).hexdigest() == R0_SHA
    return bytes(buf)


def _raw() -> dict:
    return json.loads(FIXTURE.read_bytes())


def _patch(d: dict) -> N.Patch:
    raw = json.dumps(d).encode()
    return N.load_patch(raw, hashlib.sha256(raw).hexdigest())


@lru_cache(maxsize=None)
def _fixture() -> N.Patch:
    raw = FIXTURE.read_bytes()
    return N.load_patch(raw, hashlib.sha256(raw).hexdigest())


def _flip(rom: bytes, addr: int, value: int | None = None) -> bytes:
    b = bytearray(rom)
    b[addr] = (b[addr] ^ 0x55) if value is None else value
    return bytes(b)


def _setf(rom: bytes, addr: int, *values: float) -> bytes:
    b = bytearray(rom)
    struct.pack_into(f">{len(values)}f", b, addr, *values)
    return bytes(b)


def _msgs(res, level):
    return [f.message for f in res.report.rows if f.level == level]


# ------------------------------------------------------------ golden chain


class TestGolden:
    def test_fresh_romdrop_file_gives_the_reference_image(self):
        res = N.apply_patch(_r0(), _fixture(), TYRE)
        assert res.verdict == "WARN"  # consent accepted, TP Delta advice
        assert hashlib.sha256(res.output).hexdigest() == R1_SHA
        assert _fixture().data["reference"]["result_sha256"] == R1_SHA

    def test_tyre_restore_without_consent_is_refused(self):
        res = N.apply_patch(_r0(), _fixture())
        assert res.verdict == "REFUSED" and res.output is None
        assert any("tyre_restore needs consent" in m for m in _msgs(res, N.REFUSE))

    def test_output_is_detected_installed_and_refused_again(self):
        out = N.apply_patch(_r0(), _fixture(), TYRE).output
        assert N.detect(out, _fixture()).state == "installed"
        res = N.apply_patch(out, _fixture(), TYRE)
        assert res.verdict == "REFUSED"
        assert any("already installed" in m for m in _msgs(res, N.REFUSE))

    def test_output_checksums_valid_and_only_ranges_changed(self):
        p = _fixture()
        out = N.apply_patch(_r0(), p, TYRE).output
        assert N.ck_stale(out, p.ck) == []
        allowed = N.ck_sum_words(p.ck)
        for r in p.ranges:
            allowed.update(range(r.addr, r.end + 1))
        assert all(k in allowed for k in N.diff_addrs(out, _r0()))
        assert out[0xFE600] == 0


# ------------------------------------------------------------ detection


class TestDetect:
    def test_fresh_romdrop_is_available(self):
        st = N.detect(_r0(), _fixture())
        assert st.state == "available" and "21053000" in st.detail

    def test_factory_rom_needs_romdrop_first(self):
        factory = (REPO / "examples" / "lf9veb.bin").read_bytes()
        st = N.detect(factory, _fixture())
        assert st.state == "unavailable" and "install Romdrop first" in st.detail

    def test_other_rom_id(self):
        rom = bytearray(_r0())
        rom[0xB8046:0xB804C] = b"LFG2EP"
        st = N.detect(bytes(rom), _fixture())
        assert st.state == "unavailable" and "LF9VEB only" in st.detail

    def test_previous_version_is_recognised_and_refused(self):
        d = _raw()
        code = next(r for r in d["ranges"] if r["kind"] == "code")
        a = int(code["addr"], 16)
        rom = bytearray(_r0())
        rom[a : a + 4] = b"\x12\x34\x56\x78"
        held = bytes(rom[a : a + len(code["old"]) // 2])
        d["previous"] = [
            {"version": "3.3", "signature": hashlib.sha256(held).hexdigest()}
        ]
        p = _patch(d)
        assert N.detect(bytes(rom), p).state == "previous"
        res = N.apply_patch(bytes(rom), p, TYRE)
        assert any("upgrade from 3.3 not supported" in m for m in _msgs(res, N.REFUSE))

    def test_romdrop_patch_file_is_recognised(self):
        ex = REPO / "examples"
        assert N.looks_like_romdrop_patch((ex / "lf9veb.patch").read_bytes())
        assert not N.looks_like_romdrop_patch((ex / "lf9veb.bin").read_bytes())
        assert not N.looks_like_romdrop_patch(_r0())


# ------------------------------------------------------------ refusals


class TestRefusals:
    def test_wrong_size(self):
        res = N.apply_patch(_r0()[:-1], _fixture(), TYRE)
        assert (
            res.verdict == "REFUSED" and "not a 1 MB ROM" in res.report.rows[-1].message
        )

    def test_romdrop_code_byte_changed(self):
        r = next(x for x in _fixture().ranges if x.kind == "romdrop")
        res = N.apply_patch(_flip(_r0(), r.addr), _fixture(), TYRE)
        assert res.verdict == "REFUSED"
        assert any(
            "Romdrop code differs" in m and f"0x{r.addr:06X}" in m
            for m in _msgs(res, N.REFUSE)
        )

    def test_romdrop_data_byte_changed_warns_and_keeps_input(self):
        r = next(x for x in _fixture().ranges if x.kind == "romdrop_data")
        rom = _flip(_r0(), r.addr)
        res = N.apply_patch(rom, _fixture(), TYRE)
        assert res.verdict == "WARN"
        assert any("Romdrop data" in m for m in _msgs(res, N.WARN))
        assert res.output[r.addr] == rom[r.addr]

    def test_code_area_not_blank(self):
        r = next(x for x in _fixture().ranges if x.kind == "code")
        res = N.apply_patch(_flip(_r0(), r.addr + 0x100, 0x00), _fixture(), TYRE)
        assert res.verdict == "REFUSED" and res.output is None

    def test_oem_code_byte_changed_names_its_block(self):
        res = N.apply_patch(_flip(_r0(), 0x010123), _fixture(), TYRE)
        assert res.verdict == "REFUSED"
        assert any(
            "code pin" in m and "0x010000-0x010FFF" in m for m in _msgs(res, N.REFUSE)
        )

    def test_data_byte_outside_tables_only_warns(self):
        # 0xB8040 is in the data pin region and in no table of lf9veb.xml
        p = _fixture()
        assert any(a <= 0xB8040 <= b for a, b in p.data_pin.regions)
        assert not any(a <= 0xB8040 <= b for a, b in p.data_pin.exclude)
        res = N.apply_patch(_flip(_r0(), 0xB8040), p, TYRE)
        assert res.verdict == "WARN"
        assert any(
            "data pin" in m and "0x0B8000-0x0B8FFF" in m for m in _msgs(res, N.WARN)
        )

    def test_broken_checksum_table_is_refused(self):
        rom = bytearray(_r0())
        struct.pack_into(">I", rom, 0xFF650 + 4, 0x1000)  # end % 4 != 3
        res = N.apply_patch(bytes(rom), _fixture(), TYRE)
        assert any("checksum table" in m for m in _msgs(res, N.REFUSE))

    def test_missing_sentinel_is_refused(self):
        rom = bytearray(_r0())
        struct.pack_into(">I", rom, 0xFF650 + 35 * 12, 0x2000)
        res = N.apply_patch(bytes(rom), _fixture(), TYRE)
        assert any("no sentinel" in m for m in _msgs(res, N.REFUSE))

    def test_non_finite_table_cell_is_refused(self):
        res = N.apply_patch(_setf(_r0(), 0xF629C, float("nan")), _fixture(), TYRE)
        assert any("non-finite" in m for m in _msgs(res, N.REFUSE))

    def test_post_check_failure_is_refused(self):
        d = _raw()
        cal = next(r for r in d["ranges"] if r["kind"] == "cal")
        off = (0xFE600 - int(cal["addr"], 16)) * 2
        cal["new"] = cal["new"][:off] + "01" + cal["new"][off + 2 :]
        res = N.apply_patch(_r0(), _patch(d), TYRE)
        assert res.verdict == "REFUSED" and res.output is None
        assert any("Enable ships 0" in m for m in _msgs(res, N.REFUSE))

    def test_a_second_pack_is_refused(self):
        d = _raw()
        d["patch"]["channel"] = "release"  # another pack file, same ROM ID
        res = N.apply_patch(_r0(), _fixture(), TYRE, installed=[_patch(d)])
        assert any("a ROM holds one NC Flash pack" in m for m in _msgs(res, N.REFUSE))

    def test_the_same_pack_listed_as_installed_is_not_a_second_pack(self):
        res = N.apply_patch(_r0(), _fixture(), TYRE, installed=[_fixture()])
        assert res.verdict == "WARN"

    def test_empty_dependency_lists_are_accepted(self):
        d = _raw()
        assert d["requires"]["patches"] == [] and d["conflicts"] == []
        assert d["upgrade_from"] == []
        assert _patch(d).summary == "NC Flash features 1.0.0 (AutoBlip 3.4)"

    def test_non_empty_upgrade_from_is_refused(self):
        d = _raw()
        d["upgrade_from"] = [{"version": "0.9.0"}]
        with pytest.raises(N.PatchFormatError, match="not supported"):
            _patch(d)

    def test_output_check_sees_the_written_image(self):
        d = _raw()
        d["checks"].append(
            {
                "type": "u8_eq",
                "on": "output",
                "level": "refuse",
                "name": "enable in output",
                "addr": "0x0FE600",
                "value": 1,
            }
        )
        res = N.apply_patch(_r0(), _patch(d), TYRE)
        assert any("enable in output" in m for m in _msgs(res, N.REFUSE))


class TestFeatures:
    def test_feature_states(self):
        p = _fixture()
        out = N.apply_patch(_r0(), p, TYRE).output
        (st,) = N.feature_states(out, p)
        assert (st.id, st.enabled, st.missing) == ("autoblip", False, [])
        on = bytearray(out)
        on[0xFE600] = 1
        assert N.feature_states(bytes(on), p)[0].enabled is True

    def test_missing_required_feature(self):
        d = _raw()
        d["patch"]["features"].append(
            {
                "id": "antilag",
                "name": "Antilag",
                "version": "0.1",
                "enable": {"addr": "0x0FE601", "type": "uint8"},
                "requires": ["autoblip"],
            }
        )
        p = _patch(d)
        rom = bytearray(N.apply_patch(_r0(), _fixture(), TYRE).output)
        rom[0xFE601] = 1
        states = {s.id: s for s in N.feature_states(bytes(rom), p)}
        assert states["antilag"].missing == ["autoblip"]


class TestTuneEdits:
    def test_stale_sums_warn_and_are_fixed(self):
        rom = _setf(_r0(), 0xF629C, *([22.0] * (15 * 29)))  # also clears the advice
        p = _fixture()
        assert N.ck_stale(rom, p.ck)
        res = N.apply_patch(rom, p, TYRE)
        assert res.verdict == "WARN"
        assert any("stale" in m for m in _msgs(res, N.WARN))
        assert not any("TP Delta" in m for m in _msgs(res, N.WARN))
        assert N.ck_stale(res.output, p.ck) == []


# ------------------------------------------------------------ patch file


class TestLoad:
    def test_pin_mismatch_is_refused(self):
        with pytest.raises(N.PatchFormatError, match="not the pinned"):
            N.load_patch(FIXTURE.read_bytes(), "0" * 64)

    def test_reference_block_is_optional(self):
        d = _raw()
        d.pop("reference")
        res = N.apply_patch(_r0(), _patch(d), TYRE)
        assert (
            res.verdict == "WARN"
            and "reference result None" in res.report.rows[0].message
        )

    def test_no_pin_is_refused(self):
        with pytest.raises(N.PatchFormatError, match="no sha256 pin"):
            N.load_patch(FIXTURE.read_bytes(), None)

    @pytest.mark.parametrize(
        "mutate, msg",
        [
            (lambda d: d.update(format="ncflash-patch/3"), "is not one of"),
            (lambda d: d["ranges"][0].update(new="00"), "lengths differ"),
            (lambda d: d["ranges"].append(copy.deepcopy(d["ranges"][0])), "overlap"),
            (lambda d: d["ranges"][0].update(kind="mystery"), "unknown kind"),
            (lambda d: d["checks"].append({"type": "mystery"}), "unknown type"),
            (lambda d: d["checks"][0].update(on="later"), "unknown image"),
            (lambda d: d["checksums"].update(algorithm="crc"), "algorithm"),
            (
                lambda d: d["consent"][0].update(ranges=["0x0FD800"]),
                "not a range of that kind",
            ),
            (lambda d: d["requires"].update(patches=[{"id": "x"}]), "not supported"),
            (lambda d: d.update(installed_signature="abc"), "structure"),
            (lambda d: d["code_pin"].pop("sha256"), "positive block_size and a sha256"),
            (lambda d: d["consent"][0].pop("text"), "level / text missing"),
            (lambda d: d["consent"][0].update(level="loud"), "level / text missing"),
            (
                lambda d: d["consent"][0].update(ranges=d["consent"][0]["ranges"][:1]),
                "not listed in a consent entry",
            ),
            (
                lambda d: d["code_pin"]["regions"].append(["0x0B7000", "0x0B7001"]),
                "share a block",
            ),
        ],
    )
    def test_malformed(self, mutate, msg):
        d = _raw()
        mutate(d)
        with pytest.raises(N.PatchFormatError, match=msg):
            _patch(d)


# ------------------------------------------------------------ check semantics


def _rom(**floats) -> bytes:
    b = bytearray(b"\xff" * N.ROM_SIZE)
    for k, v in floats.items():
        struct.pack_into(">f", b, int(k[1:], 16), v)
    return bytes(b)


def _f32(x):
    return struct.unpack(">f", struct.pack(">f", x))[0]


class TestChecks:
    def test_bytes_eq(self):
        rom = bytearray(b"\x00" * N.ROM_SIZE)
        chk = {"type": "bytes_eq", "name": "pair", "a": "0x100", "b": "0x200", "len": 4}
        assert N.run_check(bytes(rom), chk) == []
        rom[0x201] = 1
        assert N.run_check(bytes(rom), chk)[0][0] == "refuse"

    def test_fsub_is_bit_exact_float32(self):
        b, c = _f32(1.1), _f32(0.3)
        a = _f32(b - c)
        chk = {"type": "fsub", "name": "band", "a": "0x0", "b": "0x4", "c": "0x8"}
        assert N.run_check(_rom(x0=a, x4=b, x8=c), chk) == []
        assert N.run_check(_rom(x0=a + 1e-6, x4=b, x8=c), chk)

    def test_f_gt_and_finite(self):
        chk = {"type": "f_gt", "name": "flat shift", "addr": "0x0", "x": 50.0}
        assert N.run_check(_rom(x0=50.5), chk) == []
        assert N.run_check(_rom(x0=50.0), chk)
        assert N.run_check(_rom(x0=float("inf")), chk)

    def test_setting_le_cal(self):
        rom = bytearray(_rom(x0=6200.0))
        struct.pack_into(">H", rom, 0x10, 6500)
        chk = {
            "type": "setting_le_cal",
            "name": "fuel cut",
            "margin": 300,
            "setting": {"addr": "0x0", "type": "float"},
            "cal": {"addr": "0x10", "type": "uint16"},
        }
        assert N.run_check(bytes(rom), chk) == []
        struct.pack_into(">f", rom, 0, 6201.0)
        assert N.run_check(bytes(rom), chk)

    def test_table_all_eq_and_advice(self):
        rom = _setf(b"\x00" * N.ROM_SIZE, 0, 22.0, 22.0, 22.0, 5.6)
        chk = {
            "type": "table_all_eq",
            "name": "TP Delta",
            "addr": "0x0",
            "nx": 2,
            "ny": 2,
            "value": 22.0,
            "level": "warn",
            "advice": "set every cell to 22",
        }
        ((lv, msg),) = N.run_check(rom, chk)
        assert lv == "warn" and msg.endswith(". set every cell to 22")

    def test_unevaluable_check_refuses(self):
        chk = {"type": "f_gt", "name": "broken", "addr": "0x0"}  # no x
        assert N.run_check(_rom(x0=1.0), chk)[0][0] == "refuse"

    def test_word_checks(self):
        rom = bytearray(b"\x00" * N.ROM_SIZE)
        struct.pack_into(">I", rom, 0x100, 0x000FD820)
        only = {
            "type": "word_only_at",
            "name": "hook",
            "value": "0x000FD820",
            "addrs": ["0x100"],
        }
        none = {
            "type": "no_word_in",
            "name": "free",
            "lo": "0x0FD800",
            "hi": "0x0FE5FF",
        }
        assert N.run_check(bytes(rom), only) == []
        assert N.run_check(bytes(rom), none)
        none["except_words"] = ["0x100"]
        assert N.run_check(bytes(rom), none) == []

    def test_pcrel_loader_only(self):
        rom = bytearray(b"\x00" * N.ROM_SIZE)
        rom[0x1000], rom[0x1001] = 0xD1, 0x02  # mov.l @(2*4, PC): loads 0x1000+4+8
        chk = {
            "type": "pcrel_loader_only",
            "name": "lit",
            "lit": "0x100C",
            "site": "0x1000",
        }
        assert N.run_check(bytes(rom), chk) == []

    RATIOS = (3.815, 2.260, 1.640, 1.177, 1.000, 0.832)
    BANDS = (0.9, 1.1) * 6

    def _gear(self, ratios=RATIOS, bands=BANDS):
        rom = _setf(_setf(b"\x00" * N.ROM_SIZE, 0x100, *ratios), 0x200, *bands)
        chk = {
            "type": "gear",
            "level": "refuse",
            "name": "Gear",
            "ratios": "0x100",
            "bands": "0x200",
            "five_speed_ratio": 1.10,
            "margin_warn": 0.05,
            "from_gear": 2,
        }
        return N.run_check(rom, chk)

    def test_gear_ok(self):
        assert self._gear() == []

    @pytest.mark.parametrize("i, v", [(5, 1.05), (0, float("nan")), (5, 0.0)])
    def test_gear_refuses(self, i, v):
        r = list(self.RATIOS)
        r[i] = v
        assert self._gear(r)[0][0] == "refuse"

    def test_gear_five_speed_with_an_empty_band_6_is_silent(self):
        # a factory 5MT calibration: 6th = 5th and band 6 empty (lo = hi = 1.0)
        r = list(self.RATIOS)
        r[5] = r[4]
        bands = list(self.BANDS)
        bands[10], bands[11] = 1.0, 1.0
        assert self._gear(r, bands) == []

    def test_gear_five_speed_warns(self):
        r = list(self.RATIOS)
        r[5] = 0.95
        out = self._gear(r)
        assert out and all(lv == "warn" for lv, _ in out)
        assert any("5-speed" in m for _, m in out)

    def test_gear_thin_margin_warns(self):
        bands = list(self.BANDS)
        bands[6], bands[7] = 0.98, 1.02  # 4th: +/-2 % band
        out = self._gear(bands=bands)
        assert any("gear 4 reads as gear" in m for _, m in out)

    def test_gear_margins_definition(self):
        nom, (eu, gu), (ed, _gd) = N.gear_margins(self.RATIOS, [(0.9, 1.1)] * 6, [2])[2]
        assert nom == 2 and gu == 0
        assert eu == pytest.approx(0.1, abs=0.0005) and ed == pytest.approx(
            0.1, abs=0.0005
        )


# ------------------------------------------------------------ files


class TestFiles:
    def test_write_new_file_refuses_existing(self, tmp_path):
        p = tmp_path / "out.bin"
        N.write_new_file(p, b"abc")
        assert p.read_bytes() == b"abc"
        with pytest.raises(FileExistsError):
            N.write_new_file(p, b"def")
        assert p.read_bytes() == b"abc"
        assert list(tmp_path.iterdir()) == [p]

    def test_apply_file_leaves_the_input_alone(self, tmp_path):
        src = tmp_path / "in.bin"
        src.write_bytes(_r0())
        res = N.apply_file(src, _fixture(), TYRE)
        assert hashlib.sha256(res.output).hexdigest() == R1_SHA
        assert src.read_bytes() == _r0()


class TestCatalog:
    def _make(self, tmp_path, pins_override=None):
        root = tmp_path / "patches"
        (root / "ncflash-features" / "1.0.0").mkdir(parents=True)
        rel = "ncflash-features/1.0.0/lf9veb.ncpatch.json"
        raw = FIXTURE.read_bytes()
        (root / rel).write_bytes(raw)
        sha = hashlib.sha256(raw).hexdigest()
        idx = json.dumps(
            {
                "format": "ncflash-patch-index/1",
                "patches": [
                    {
                        "id": "ncflash-features",
                        "version": "1.0.0",
                        "channel": "test",
                        "rom_id": "LF9VEB",
                        "file": rel,
                        "sha256": sha,
                        "notes": "",
                    }
                ],
            }
        ).encode()
        (root / "index.json").write_bytes(idx)
        pins = {"index.json": hashlib.sha256(idx).hexdigest(), rel: sha}
        pins.update(pins_override or {})
        return root, pins

    def test_pinned_catalog_loads(self, tmp_path):
        root, pins = self._make(tmp_path)
        cat = N.load_catalog(root, pins)
        assert [p.rom_id for p in cat.patches] == ["LF9VEB"] and cat.problems == []

    def test_unpinned_file_is_left_out(self, tmp_path):
        root, pins = self._make(
            tmp_path, {"ncflash-features/1.0.0/lf9veb.ncpatch.json": "0" * 64}
        )
        cat = N.load_catalog(root, pins)
        assert cat.patches == [] and cat.problems

    def test_altered_index_is_rejected(self, tmp_path):
        root, pins = self._make(tmp_path, {"index.json": "0" * 64})
        assert N.load_catalog(root, pins).patches == []


class TestChecksumArithmetic:
    def test_matches_the_flash_path_on_every_record(self):
        import os

        from src.ecu.checksum import mazda_checksum

        p = _fixture()
        images = [_r0(), N.apply_patch(_r0(), p, TYRE).output, os.urandom(N.ROM_SIZE)]
        for d in images:
            for i in range(p.ck.records):
                a, b = struct.unpack_from(">II", _r0(), p.ck.table + 12 * i)
                assert N.ck_calc(d, a, b) == mazda_checksum(d, a, b + 1)
        assert N.ck_calc(b"\xff" * 16, 0, 15) == mazda_checksum(b"\xff" * 16, 0, 16)


# ------------------------------------------------- nc-flash-re review 2026-10-07


def _with_vector_pin(d: dict) -> dict:
    """The fixture with the review's code pin: 0xFF650-0xFFFFF (vectors) minus the
    carve-out 0xFF800-0xFFC4F, re-hashed on R0 (what the re-emitted packs carry)."""
    pin = d["code_pin"]
    regions = [tuple(N.num(x) for x in r) for r in pin["regions"]]
    regions = [r for r in regions if r[0] < 0xFF000] + [(0xFF650, 0xFFFFF)]
    exclude = [tuple(N.num(x) for x in r) for r in pin["exclude"]]
    exclude += [(0xFF800, 0xFFC4F)]
    exclude += [(0xFF650 + 12 * i + 8, 0xFF650 + 12 * i + 11) for i in range(35)]
    sha, blocks = N.pin_hashes(_r0(), regions, exclude, 4096)
    pin.update(
        regions=[[f"0x{a:06X}", f"0x{b:06X}"] for a, b in regions],
        exclude=[[f"0x{a:06X}", f"0x{b:06X}"] for a, b in exclude],
        sha256=sha,
        blocks={f"0x{k:06X}": v for k, v in blocks.items()},
    )
    return d


def _fixed(rom: bytes) -> bytes:
    p = _fixture()
    return N.ck_fix(rom, p.ck)


class TestReviewFixes:
    VEC = 0xFFC50 + 4 * 192  # vector 192, the CMT1 1 ms tick

    def _apply(self, rom, d=None):
        p = _patch(_with_vector_pin(d or _raw()))
        return N.apply_patch(_fixed(rom), p, TYRE)

    def test_r0_still_applies(self):
        res = self._apply(_r0())
        assert hashlib.sha256(res.output).hexdigest() == R1_SHA

    @pytest.mark.parametrize(
        "label, mutate",
        [
            (
                "vector zeroed",
                lambda b: struct.pack_into(">I", b, 0xFFC50 + 4 * 192, 0),
            ),
            (
                "vector redirected",
                lambda b: b.__setitem__(
                    slice(0xFFC50 + 4 * 192, 0xFFC50 + 4 * 193),
                    bytes(b[0xFFC50 + 4 * 193 : 0xFFC50 + 4 * 194]),
                ),
            ),
            ("entry pointer changed", lambda b: b.__setitem__(0xFFFFB, b[0xFFFFB] ^ 2)),
        ],
    )
    def test_vector_table_refuses(self, label, mutate):
        rom = bytearray(_r0())
        mutate(rom)
        res = self._apply(bytes(rom))
        assert res.verdict == "REFUSED" and res.output is None, label
        assert any("code pin" in m and "0x0FF000" in m for m in _msgs(res, N.REFUSE))

    def test_flash_counter_and_carve_out_stay_out_of_the_code_pin(self):
        rom = bytearray(_r0())
        rom[0xFFB05] ^= 0x5A  # SBL flash counter: excluded from the data pin too
        rom[0xFF900] ^= 0x01  # carve-out: data pin (non-routine WARN), not code pin
        res = self._apply(bytes(rom))
        assert res.output is not None
        assert not any("code pin" in m for m in _msgs(res, N.REFUSE))

    def test_wrapper_code_range_refuses(self):
        d = _raw()
        # the re-emitted packs mark Romdrop's wrapper code ranges kind romdrop
        for r in d["ranges"]:
            a = int(r["addr"], 16)
            if r["kind"] == "romdrop_data" and 0xFB9D0 <= a <= 0xFC2FF:
                r["kind"] = "romdrop"
        rom = bytearray(_r0())
        rom[0xFB9D3] ^= 0x01
        res = N.apply_patch(_fixed(bytes(rom)), _patch(d), TYRE)
        assert res.verdict == "REFUSED"
        assert any(m.startswith("romdrop 0x0FB9D0-") for m in _msgs(res, N.REFUSE))

    def test_range_note_is_shown_on_refuse(self):
        d = _raw()
        erase = next(r for r in d["ranges"] if r["kind"] == "erase")
        erase["note"] = "retired AutoBlip v1 tables: set them back to blank"
        rom = bytearray(_r0())
        rom[int(erase["addr"], 16) + 0x10] = 0
        res = N.apply_patch(_fixed(bytes(rom)), _patch(d), TYRE)
        assert any(
            m.endswith(". retired AutoBlip v1 tables: set them back to blank")
            for m in _msgs(res, N.REFUSE)
        )


class TestWarnClasses:
    def test_consent_routine_tp_delta_non_routine(self):
        res = N.apply_patch(_r0(), _fixture(), TYRE)
        cls = {m.split(" ")[0]: c for lv, m, c in res.report.findings() if lv == N.WARN}
        assert cls["tyre_restore"] == N.ROUTINE
        assert cls["TP"] == N.NON_ROUTINE
        assert res.report.non_routine() == [
            m for lv, m, c in res.report.findings() if c == N.NON_ROUTINE
        ]

    def test_routine_flag_on_a_check(self):
        d = _raw()
        for c in d["checks"]:
            if c["type"] == "table_all_eq":
                c["routine"] = True
        res = N.apply_patch(_r0(), _patch(d), TYRE)
        assert res.report.non_routine() == []

    def test_stale_sums_routine_romdrop_data_and_data_pin_not(self):
        r = next(x for x in _fixture().ranges if x.kind == "romdrop_data")
        rom = bytearray(_r0())
        rom[r.addr] ^= 0x55  # a Romdrop data byte, sums left stale
        rom[0xB8040] ^= 0x01  # a data-pin byte outside every table
        res = N.apply_patch(bytes(rom), _fixture(), TYRE)
        rows = [(m, c) for lv, m, c in res.report.findings() if lv == N.WARN]
        assert any(m.startswith("input checksum") and c == N.ROUTINE for m, c in rows)
        assert any(m.startswith("Romdrop data") and c == N.NON_ROUTINE for m, c in rows)
        assert any(m.startswith("data pin") and c == N.NON_ROUTINE for m, c in rows)
        assert all(c is None for lv, m, c in res.report.findings() if lv != N.WARN)


class TestParseValidation:
    """An unevaluable check, range or table REFUSES the file at parse time."""

    @pytest.mark.parametrize(
        "check",
        [
            {"type": "bytes_eq", "a": "0x0C8FFC", "b": "0x0F8348", "len": 0},
            {"type": "bytes_eq", "a": "0x1000000", "b": "0x1000010", "len": 4},
            {"type": "bytes_is", "addr": "0x0B8046", "hex": ""},
            {"type": "f_finite", "addr": -4},
            {"type": "f_finite", "addr": True},
            {"type": "f_desc", "addr": "0x0C14FC", "n": 0},
            {"type": "table_finite", "addr": "0x0F629C", "nx": 0, "ny": 29},
            {
                "type": "gear",
                "ratios": "0x0C14FC",
                "bands": "0x0FFFF0",
                "five_speed_ratio": 1.1,
                "margin_warn": 0.05,
                "from_gear": 2,
            },
            {"type": "f_gt", "addr": "0x0F3E9C", "x": "50"},
            {"type": "u8_eq", "addr": "0x0FE600", "value": 256},
            {"type": "word_only_at", "value": "0x000FD820", "addrs": ["0x0230CD"]},
            {"type": "f_finite", "addr": "0x0F3E9C", "routine": "yes"},
            {"type": "f_finite", "addr": "0x0F3E9C", "name": ""},
        ],
        ids=lambda c: f"{c['type']}-{sorted(c.items())[1:][:2]}",
    )
    def test_malformed_check(self, check):
        d = _raw()
        d["checks"].append(dict({"name": "malformed"}, **check))
        with pytest.raises(N.PatchFormatError):
            _patch(d)

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda d: d["ranges"][0].update(addr="-1"),
            lambda d: d["ranges"][0].update(note=5),
            lambda d: d["checksums"].update(records=0),
            lambda d: d["checksums"].update(table="0x0FFFF0"),
            lambda d: d.update(rom_id_addr="0x0FFFFE"),
        ],
    )
    def test_malformed_file(self, mutate):
        d = _raw()
        mutate(d)
        with pytest.raises(N.PatchFormatError):
            _patch(d)
