"""Standard ROM conversion (src/core/standard_rom.py) on a small synthetic pack.

The pack has two revisions of one line (old "OLDAEA", standard "NEWAEB"), a
5-speed variant of each ("OLDBEA", "NEWBEB") and an ID with no factory variant
on the standard ("CARREA"). The new revision's tables sit 0x10 higher, as a
real revision moves its layout.
"""

import hashlib
import io
import json
import lzma
import struct
import tarfile

import pytest

from src.core.standard_rom import (
    BOOT_BLOCK_END,
    WHY_FEATURE,
    WHY_MISSING,
    WHY_STOCK,
    StandardPack,
    StandardPackError,
    convert,
    normalized_name,
    pair_key,
    run_release_checks,
)

SIZE = 0x3000
ID_ADDR = 0x2800
SHIFT = 0x10  # layout shift of the new revision

# name -> (address in the OLD layout, element count, storage)
TABLES = {
    "Fuel Map": (0x2900, 4, "float"),
    "Idle Target": (0x2920, 1, "float"),
    "Limit (pair with 0x2A00)": (0x2930, 2, "float"),
    "Limit (Data Integrity) (pair with 0x2930)": (0x2A00, 2, "float"),
    "AutoBlip - Enable": (0x2B00, 1, "uint8"),
}
FUEL_AXIS = 0x2910
OLD_ONLY = ("Old Only", 0x2940, 1, "float")
FEATURE_RANGE = (0x2B00, 0x2B0F)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _xml(rom_id: str, shift: int, with_old_only: bool) -> str:
    rows = []
    for name, (addr, count, storage) in TABLES.items():
        scaling = "f" if storage == "float" else "b"
        axis = ""
        if name == "Fuel Map":
            axis = (
                f'<table name="RPM" type="Y Axis" address="{FUEL_AXIS + shift:x}" '
                f'elements="{count}" scaling="f"/>'
            )
        kind = "2D" if axis else "1D"
        rows.append(
            f'<table level="1" type="{kind}" category="Test" name="{name}" '
            f'address="{addr + shift:x}" elements="{count}" scaling="{scaling}">'
            f"{axis}</table>"
        )
    if with_old_only:
        name, addr, count, _ = OLD_ONLY
        rows.append(
            f'<table level="1" type="1D" category="Test" name="{name}" '
            f'address="{addr:x}" elements="{count}" scaling="f"></table>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?><roms><rom><romid>'
        f"<xmlid>{rom_id}</xmlid><internalidaddress>{ID_ADDR:x}</internalidaddress>"
        f"<internalidstring>{rom_id}</internalidstring><ecuid>{rom_id}</ecuid>"
        "<make>Mazda</make><model>MX5</model><flashmethod>Romdrop</flashmethod>"
        "<memmodel>SH7058</memmodel><checksummodule>None</checksummodule></romid>"
        '<scaling name="f" units="" toexpr="x" frexpr="x" format="%0.2f" min="0" '
        'max="0" inc="1" storagetype="float" endian="big"/>'
        '<scaling name="b" units="" toexpr="x" frexpr="x" format="%d" min="0" '
        'max="1" inc="1" storagetype="uint8" endian="big"/>'
        + "".join(rows)
        + "</rom></roms>"
    )


def _image(rom_id: str, shift: int, idle: float, gear: float) -> bytes:
    """A factory image: same table values in both layouts, except Idle Target."""
    img = bytearray(b"\xff" * SIZE)
    img[:BOOT_BLOCK_END] = bytes([shift or 1]) * BOOT_BLOCK_END
    img[ID_ADDR : ID_ADDR + len(rom_id)] = rom_id.encode()
    struct.pack_into(">4f", img, 0x2900 + shift, 1.0, 2.0, 3.0, gear)
    struct.pack_into(">4f", img, FUEL_AXIS + shift, 500.0, 1000.0, 2000.0, 4000.0)
    struct.pack_into(">f", img, 0x2920 + shift, idle)
    struct.pack_into(">2f", img, 0x2930 + shift, 10.0, 20.0)
    struct.pack_into(">2f", img, 0x2A00 + shift, 10.0, 20.0)
    img[0x2B00 + shift] = 0
    if not shift:
        struct.pack_into(">f", img, OLD_ONLY[1], 7.0)
    return bytes(img)


@pytest.fixture
def pack_dir(tmp_path):
    # gear value 4.0 = 6-speed, 9.0 = 5-speed (the variant difference)
    images = {
        "OLDAEA": _image("OLDAEA", 0, idle=700.0, gear=4.0),
        "OLDBEA": _image("OLDBEA", 0, idle=700.0, gear=9.0),
        "NEWAEB": _image("NEWAEB", SHIFT, idle=750.0, gear=4.0),
        "NEWBEB": _image("NEWBEB", SHIFT, idle=750.0, gear=9.0),
        "CARREA": _image("CARREA", 0, idle=700.0, gear=5.0),
    }
    release = {"ids": {}}
    for rom_id, img in images.items():
        shift = SHIFT if rom_id.startswith("NEW") else 0
        folder = tmp_path / rom_id
        folder.mkdir()
        (folder / f"{rom_id.lower()}_ncf_1.0.0.bin").write_bytes(img)
        (folder / f"{rom_id.lower()}.xml").write_text(
            _xml(rom_id, shift, with_old_only=not shift), encoding="utf-8"
        )
        release["ids"][rom_id] = {
            "image_sha256": _sha(img),
            "rom_id_addr": f"0x{ID_ADDR:06X}",
            "no_transfer": [
                {
                    "feature": "autoblip",
                    "addr": f"0x{FEATURE_RANGE[0] + shift:06X}",
                    "end": f"0x{FEATURE_RANGE[1] + shift:06X}",
                }
            ],
            "code_pin": [],
            "checks": [
                {
                    "addr": f"0x{0x2930 + shift:06X}",
                    "pair": f"0x{0x2A00 + shift:06X}",
                    "len": 8,
                    "rule": "equal",
                    "level": "REFUSE",
                    "what": "integrity pair",
                }
            ],
        }
    (tmp_path / "manifest.json").write_text(json.dumps(release), encoding="utf-8")

    std, var = images["NEWAEB"], images["NEWBEB"]
    diff = [k for k in range(BOOT_BLOCK_END, SIZE) if std[k] != var[k]]
    runs, start = [], None
    for k in diff + [None]:
        if start is None:
            start = prev = k
        elif k is None or k != prev + 1:
            runs.append([f"0x{start:06X}", var[start : prev + 1].hex()])
            start = prev = k
        else:
            prev = k
    (tmp_path / "presets").mkdir()
    (tmp_path / "presets" / "newbeb_on_newaeb.json").write_text(
        json.dumps(
            {
                "format": "ncflash-standard-preset/1",
                "standard": "NEWAEB",
                "target": "NEWBEB",
                "standard_sha256": _sha(std),
                "runs": runs,
            }
        ),
        encoding="utf-8",
    )

    def entry(standard, method, target, moves, preset=None, gearbox="6MT"):
        e = {
            "engine_cc": "1998.0",
            "gearbox": gearbox,
            "market_code": "20",
            "standard": standard,
            "method": method,
            "target": target,
            "moves_revision": moves,
        }
        if preset:
            e["preset"] = preset
        return e

    preset = "presets/newbeb_on_newaeb.json"
    standard = {
        "format": "ncflash-standard-manifest/1",
        "ids": {
            "NEWAEB": entry("NEWAEB", "standard", "NEWAEB", False),
            "OLDAEA": entry("NEWAEB", "standard_image", "NEWAEB", True),
            "NEWBEB": entry("NEWAEB", "factory", "NEWBEB", False, preset, "5MT"),
            "OLDBEA": entry("NEWAEB", "factory", "NEWBEB", True, preset, "5MT"),
            "CARREA": entry("NEWAEB", "carried", None, True),
        },
    }
    (tmp_path / "standard_manifest.json").write_text(
        json.dumps(standard), encoding="utf-8"
    )
    return tmp_path


def _stock(pack_dir, rom_id) -> bytearray:
    path = pack_dir / rom_id / f"{rom_id.lower()}_ncf_1.0.0.bin"
    return bytearray(path.read_bytes())


class TestNames:
    def test_pair_suffix_is_not_part_of_the_name(self):
        assert normalized_name("Limit (pair with 0xF7C94)") == "Limit"
        assert normalized_name("Limit (pair with f456c; OEM flag 0x1)") == "Limit"
        assert normalized_name("Fuel Map") == "Fuel Map"

    def test_pair_key_joins_a_table_and_its_copy(self):
        a = pair_key("Limit (pair with 0x2A00)")
        b = pair_key("Limit (Data Integrity) (pair with 0x2930)")
        assert a == b == "Limit"


class TestIdentify:
    def test_standard_itself(self, pack_dir):
        ident = StandardPack(pack_dir).identify(bytes(_stock(pack_dir, "NEWAEB")))
        assert (ident.rom_id, ident.standard, ident.target) == (
            "NEWAEB",
            "NEWAEB",
            "NEWAEB",
        )
        assert not ident.moves_revision

    def test_old_variant_goes_to_the_variant_image(self, pack_dir):
        ident = StandardPack(pack_dir).identify(bytes(_stock(pack_dir, "OLDBEA")))
        assert (ident.standard, ident.target, ident.gearbox) == (
            "NEWAEB",
            "NEWBEB",
            "5MT",
        )
        assert ident.moves_revision and ident.convertible

    def test_carried_id_is_not_convertible(self, pack_dir):
        ident = StandardPack(pack_dir).identify(bytes(_stock(pack_dir, "CARREA")))
        assert ident.target is None and not ident.convertible
        assert "cannot be converted" in ident.summary()

    def test_unknown_rom(self, pack_dir):
        assert StandardPack(pack_dir).identify(b"\x00" * SIZE) is None


class TestBuildBase:
    def test_standard_plus_preset_is_the_variant_image(self, pack_dir):
        pack = StandardPack(pack_dir)
        ident = pack.identify(bytes(_stock(pack_dir, "OLDBEA")))
        base = pack.build_base(ident)
        want = bytes(_stock(pack_dir, "NEWBEB"))
        assert base[BOOT_BLOCK_END:] == want[BOOT_BLOCK_END:]
        # the boot block stays the standard's
        assert (
            base[:BOOT_BLOCK_END] == bytes(_stock(pack_dir, "NEWAEB"))[:BOOT_BLOCK_END]
        )

    def test_preset_into_the_boot_block_is_refused(self, pack_dir):
        path = pack_dir / "presets" / "newbeb_on_newaeb.json"
        preset = json.loads(path.read_text())
        preset["runs"].append(["0x000010", "00"])
        path.write_text(json.dumps(preset))
        pack = StandardPack(pack_dir)
        ident = pack.identify(bytes(_stock(pack_dir, "OLDBEA")))
        with pytest.raises(StandardPackError):
            pack.build_base(ident)

    def test_tampered_image_is_refused(self, pack_dir):
        path = pack_dir / "NEWAEB" / "newaeb_ncf_1.0.0.bin"
        data = bytearray(path.read_bytes())
        data[0x2F00] ^= 1
        path.write_bytes(data)
        pack = StandardPack(pack_dir)
        with pytest.raises(StandardPackError):
            pack.image("NEWAEB")

    def test_missing_pack_file(self, tmp_path):
        with pytest.raises(StandardPackError):
            StandardPack(tmp_path)


def _pack_file(folder, path):
    """The folder pack as one file, the way nc-flash-re writes it."""
    with lzma.open(path, "wb") as z, tarfile.open(fileobj=z, mode="w") as tar:
        for item in sorted(p for p in folder.rglob("*") if p.is_file()):
            if item == path:
                continue
            data = item.read_bytes()
            info = tarfile.TarInfo(item.relative_to(folder).as_posix())
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


class TestPackFile:
    def test_gives_the_same_result_as_the_folder(self, pack_dir, tmp_path_factory):
        packed = _pack_file(pack_dir, tmp_path_factory.mktemp("p") / "std.ncpack")
        src = _stock(pack_dir, "OLDBEA")
        struct.pack_into(">f", src, 0x2904, 2.5)
        from_folder, report_a = convert(StandardPack(pack_dir), bytes(src))
        from_file, report_b = convert(StandardPack(packed), bytes(src))
        assert report_a.ok and from_folder is not None
        assert from_file == from_folder
        assert report_b.to_dict() == report_a.to_dict()

    def test_reads_every_id(self, pack_dir, tmp_path_factory):
        packed = _pack_file(pack_dir, tmp_path_factory.mktemp("p") / "std.ncpack")
        pack = StandardPack(packed)
        for rom_id in pack.rom_ids:
            assert pack.image(rom_id) == bytes(_stock(pack_dir, rom_id))
            assert pack.definition(rom_id).tables

    def test_a_file_that_is_not_a_pack_is_refused(self, tmp_path):
        bad = tmp_path / "bad.ncpack"
        bad.write_bytes(b"not a pack" * 100)
        with pytest.raises(StandardPackError, match="could not be read"):
            StandardPack(bad)

    def test_a_cut_off_file_is_refused(self, pack_dir, tmp_path_factory):
        packed = _pack_file(pack_dir, tmp_path_factory.mktemp("p") / "std.ncpack")
        packed.write_bytes(packed.read_bytes()[:-200])
        with pytest.raises(StandardPackError):
            StandardPack(packed)

    def test_a_changed_image_is_refused(self, pack_dir, tmp_path_factory):
        image = pack_dir / "NEWAEB" / "newaeb_ncf_1.0.0.bin"
        data = bytearray(image.read_bytes())
        data[0x2A00] ^= 1
        image.write_bytes(data)
        packed = _pack_file(pack_dir, tmp_path_factory.mktemp("p") / "std.ncpack")
        with pytest.raises(StandardPackError, match="does not match"):
            StandardPack(packed).image("NEWAEB")

    def test_a_path_that_does_not_exist_is_refused(self, tmp_path):
        with pytest.raises(StandardPackError, match="not a pack file or folder"):
            StandardPack(tmp_path / "nothing.ncpack")


class TestConvert:
    def test_edit_arrives_at_the_new_address(self, pack_dir):
        src = _stock(pack_dir, "OLDAEA")
        struct.pack_into(">f", src, 0x2904, 2.5)  # Fuel Map cell 1
        struct.pack_into(">f", src, FUEL_AXIS, 600.0)  # its axis, cell 0
        before = bytes(src)
        image, report = convert(StandardPack(pack_dir), bytes(src))
        assert report.ok and image is not None
        assert struct.unpack_from(">f", image, 0x2904 + SHIFT)[0] == 2.5
        assert struct.unpack_from(">f", image, FUEL_AXIS + SHIFT)[0] == 600.0
        assert [t.table for t in report.moved] == ["Fuel Map"]
        assert bytes(src) == before  # the source is never modified
        # everything else is the standard image
        base = bytes(_stock(pack_dir, "NEWAEB"))
        changed = [k for k in range(SIZE) if image[k] != base[k]]
        assert set(changed) <= set(range(0x2900 + SHIFT, 0x2920 + SHIFT))

    def test_variant_keeps_its_own_values(self, pack_dir):
        src = _stock(pack_dir, "OLDBEA")
        struct.pack_into(">f", src, 0x2900, 1.5)
        image, report = convert(StandardPack(pack_dir), bytes(src))
        assert report.ok and report.identity.target == "NEWBEB"
        assert struct.unpack_from(">f", image, 0x2900 + SHIFT)[0] == 1.5
        assert struct.unpack_from(">f", image, 0x290C + SHIFT)[0] == 9.0  # 5-speed

    def test_table_mazda_changed_is_not_moved(self, pack_dir):
        src = _stock(pack_dir, "OLDAEA")
        struct.pack_into(">f", src, 0x2920, 900.0)  # Idle Target: 700 old, 750 new
        image, report = convert(StandardPack(pack_dir), bytes(src))
        assert report.ok
        assert struct.unpack_from(">f", image, 0x2920 + SHIFT)[0] == 750.0
        assert [(t.table, t.why) for t in report.not_moved] == [
            ("Idle Target", WHY_STOCK)
        ]

    def test_table_missing_in_the_new_definition(self, pack_dir):
        src = _stock(pack_dir, "OLDAEA")
        struct.pack_into(">f", src, OLD_ONLY[1], 8.0)
        _image, report = convert(StandardPack(pack_dir), bytes(src))
        assert [(t.table, t.why) for t in report.not_moved] == [
            ("Old Only", WHY_MISSING)
        ]

    def test_feature_settings_are_never_copied(self, pack_dir):
        src = _stock(pack_dir, "OLDAEA")
        src[0x2B00] = 1  # AutoBlip - Enable
        image, report = convert(StandardPack(pack_dir), bytes(src))
        assert report.ok and image[0x2B00 + SHIFT] == 0
        assert [t.why for t in report.not_moved] == [WHY_FEATURE]
        assert "feature settings" in report.summary()

    def test_integrity_pair_moves_together(self, pack_dir):
        src = _stock(pack_dir, "OLDAEA")
        struct.pack_into(">2f", src, 0x2930, 11.0, 22.0)
        struct.pack_into(">2f", src, 0x2A00, 11.0, 22.0)
        image, report = convert(StandardPack(pack_dir), bytes(src))
        assert report.ok and len(report.moved) == 2
        assert struct.unpack_from(">2f", image, 0x2930 + SHIFT) == (11.0, 22.0)
        assert struct.unpack_from(">2f", image, 0x2A00 + SHIFT) == (11.0, 22.0)

    def test_half_edited_pair_is_refused_and_explained(self, pack_dir):
        src = _stock(pack_dir, "OLDAEA")
        struct.pack_into(">2f", src, 0x2930, 11.0, 22.0)  # copy left alone
        image, report = convert(StandardPack(pack_dir), bytes(src))
        assert image is None and not report.ok
        assert any("integrity pair" in m for m in report.checks_refused)
        assert any('"Limit"' in m for m in report.source_already_fails)
        assert "Fix them in the source ROM first" in report.summary()

    def test_bytes_outside_tables_are_counted_not_copied(self, pack_dir):
        src = _stock(pack_dir, "OLDAEA")
        src[0x2F00] = 0x12  # no table covers this byte
        image, report = convert(StandardPack(pack_dir), bytes(src))
        assert report.ok and image[0x2F00] == 0xFF and image[0x2F00 + SHIFT] == 0xFF
        assert sum(report.bytes_not_copied.values()) == 1

    def test_boot_block_is_the_standards(self, pack_dir):
        src = _stock(pack_dir, "OLDAEA")
        src[0x10] ^= 0xFF
        image, report = convert(StandardPack(pack_dir), bytes(src))
        assert report.ok
        assert (
            image[:BOOT_BLOCK_END] == bytes(_stock(pack_dir, "NEWAEB"))[:BOOT_BLOCK_END]
        )

    def test_carried_id_is_refused(self, pack_dir):
        image, report = convert(
            StandardPack(pack_dir), bytes(_stock(pack_dir, "CARREA"))
        )
        assert image is None and "cannot be converted" in report.reason

    def test_unknown_rom_is_refused(self, pack_dir):
        image, report = convert(StandardPack(pack_dir), b"\x00" * SIZE)
        assert image is None and report.identity is None

    def test_wrong_size_is_refused(self, pack_dir):
        src = bytes(_stock(pack_dir, "OLDAEA")) + b"\x00"
        image, report = convert(StandardPack(pack_dir), src)
        assert image is None and "size" in report.reason

    def test_report_is_json_serialisable(self, pack_dir):
        src = _stock(pack_dir, "OLDAEA")
        struct.pack_into(">f", src, 0x2904, 2.5)
        _image, report = convert(StandardPack(pack_dir), bytes(src))
        data = json.loads(json.dumps(report.to_dict()))
        assert data["original_rom_id"] == "OLDAEA" and data["image"] == "NEWAEB"
        assert data["moved"][0]["table"] == "Fuel Map"


class TestReleaseChecks:
    IMG = bytes(range(256)) * 4

    def test_unknown_rule_refuses(self):
        entry = {
            "checks": [{"addr": "0x10", "rule": "magic", "level": "WARN", "what": "x"}]
        }
        refused, warned = run_release_checks(self.IMG, entry)
        assert len(refused) == 1 and not warned

    def test_unknown_level_refuses(self):
        check = {
            "addr": "0x10",
            "pair": "0x110",
            "len": 4,
            "rule": "equal",
            "level": "INFO",
            "what": "x",
        }
        refused, _ = run_release_checks(self.IMG, {"checks": [check]})
        assert len(refused) == 1

    def test_missing_check_list_refuses(self):
        refused, _ = run_release_checks(self.IMG, {})
        assert refused

    def test_equal_pair_and_warn_level(self):
        ok = {"addr": "0x10", "pair": "0x110", "len": 4, "rule": "equal"}
        bad = {"addr": "0x10", "pair": "0x111", "len": 4, "rule": "equal"}
        entry = {
            "checks": [
                dict(ok, level="REFUSE", what="a"),
                dict(bad, level="WARN", what="b"),
            ]
        }
        refused, warned = run_release_checks(self.IMG, entry)
        assert not refused and len(warned) == 1

    def test_code_pin_mismatch_refuses(self):
        entry = {
            "checks": [],
            "code_pin": [
                {"what": "blip", "addr": "0x0", "end": "0xF", "sha256": "0" * 64}
            ],
        }
        refused, _ = run_release_checks(self.IMG, entry)
        assert refused and "feature code changed" in refused[0]
