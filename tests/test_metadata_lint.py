"""
Tests for the bundled ROM metadata and tools/metadata_lint.py.

The first group is the CI gate for examples/metadata: every definition must
parse, detect unambiguously and pass the lint (new issues only; see
tools/metadata_lint_baseline.txt). The rest pin the formatter's promise that
it only ever changes whitespace, and the lint rules that catch wrong cell sizes.
"""

import textwrap
from pathlib import Path

import pytest

from src.core.definition_parser import load_definition
from src.core.rom_detector import RomDetector
from tools import metadata_lint as lint

METADATA_DIR = Path(__file__).resolve().parent.parent / "examples" / "metadata"
BUNDLED = sorted(METADATA_DIR.glob("*.xml"))


def _xml(body: str) -> bytes:
    return textwrap.dedent(body).strip().encode("utf-8")


def _write(tmp_path, body: str, name="t.xml") -> Path:
    path = tmp_path / name
    path.write_bytes(_xml(body))
    return path


ROMID = """
    <romid>
      <xmlid>TEST01</xmlid>
      <internalidaddress>b8046</internalidaddress>
      <internalidstring>TEST01</internalidstring>
    </romid>
"""


# ------------------------------------------------------- bundled metadata


class TestBundledMetadata:
    def test_bundled_set_is_present(self):
        # 102 NC calibrations from speeps' set (his Mazda6 l3r3ee left out)
        # + our lf9veb + 2 TCM definitions.
        assert len(BUNDLED) >= 105

    def test_lint_finds_no_new_issues(self):
        new = lint.new_issues(lint.check(BUNDLED), lint.load_baseline())
        assert [str(i) for i in new] == []

    @pytest.mark.parametrize("path", BUNDLED, ids=lambda p: p.name)
    def test_every_definition_parses(self, path):
        definition = load_definition(str(path))
        assert definition.tables

    def test_each_rom_id_matches_exactly_one_definition(self, tmp_path):
        """A ROM carrying any bundled ID must detect to that file alone.

        Catches duplicate IDs and prefix clashes (detection compares only
        len(internalidstring) bytes, so 'ABC' would also match 'ABCD')."""
        detector = RomDetector(str(METADATA_DIR))
        infos = detector.rom_definitions
        assert len(infos) == len(BUNDLED)
        rom = tmp_path / "rom.bin"
        for info in infos:
            data = bytearray(b"\xff" * 0x100000)
            addr = info.internal_id_address_int
            data[addr : addr + len(info.internalidstring)] = (
                info.internalidstring.encode("ascii")
            )
            rom.write_bytes(bytes(data))
            matches = [
                other.xml_path.name
                for other in infos
                if data[
                    other.internal_id_address_int : other.internal_id_address_int
                    + len(other.internalidstring)
                ]
                == other.internalidstring.encode("ascii")
            ]
            assert matches == [info.xml_path.name]
            assert detector.detect_rom_id(str(rom))[1] == str(info.xml_path)

    def test_kr_accumulator_exit_delay_is_one_byte_everywhere(self):
        """#101: as a 4-byte float, saving it zeroed the knock-retard rate."""
        found = 0
        for path in BUNDLED:
            definition = load_definition(str(path))
            table = definition.get_table_by_name("KR Accumulator - Exit Delay")
            if table is None:
                continue
            found += 1
            scaling = definition.get_scaling(table.scaling)
            assert scaling.storagetype == "uint8", path.name
        assert found >= 103


# ------------------------------------------------------------- formatter


MESSY = """
    <?xml version="1.0" encoding="UTF-8" standalone="yes"?>
    <roms
    ><rom
    ><romid
    ><xmlid
    >TEST01</xmlid
    ><internalidaddress
    >b8046</internalidaddress
    ><internalidstring
    >TEST01</internalidstring
    ><market
    ></market
    ></romid
    ><scaling name="s1" units="&quot;a&amp;b&lt;" toexpr="x*2" frexpr="x/2" format="%0.2f" storagetype="uint16" endian="big"
    ></scaling
    ><!-- a comment
         over two lines -->
    <table name="T &amp; U" type="2D" address="100" scaling="s1"
    ><table type="Y Axis" address="200" elements="3" scaling="s1"
    ></table
    ></table
    ></rom
    ></roms
    >
"""


class TestFormatter:
    def test_output_is_readable_and_content_identical(self):
        formatted = lint.format_xml(_xml(MESSY))
        assert formatted == textwrap.dedent("""\
            <?xml version="1.0" encoding="UTF-8" standalone="yes"?>
            <roms>
              <rom>
                <romid>
                  <xmlid>TEST01</xmlid>
                  <internalidaddress>b8046</internalidaddress>
                  <internalidstring>TEST01</internalidstring>
                  <market/>
                </romid>
                <scaling name="s1" units="&quot;a&amp;b&lt;" toexpr="x*2" frexpr="x/2" format="%0.2f" storagetype="uint16" endian="big"/>
                <!-- a comment
                 over two lines -->
                <table name="T &amp; U" type="2D" address="100" scaling="s1">
                  <table type="Y Axis" address="200" elements="3" scaling="s1"/>
                </table>
              </rom>
            </roms>
            """)
        assert lint.same_content(_xml(MESSY), formatted.encode("utf-8"))

    def test_is_idempotent(self):
        once = lint.format_xml(_xml(MESSY))
        assert lint.format_xml(once.encode("utf-8")) == once

    def test_same_content_sees_attribute_and_text_changes(self):
        a = _xml(MESSY)
        assert not lint.same_content(a, a.replace(b'"uint16"', b'"uint8"'))
        assert not lint.same_content(a, a.replace(b">TEST01<", b">TEST02<", 1))
        assert not lint.same_content(a, a.replace(b"a comment", b"a remark"))

    def test_refuses_mixed_content(self):
        with pytest.raises(lint.FormatError):
            lint.format_xml(b"<roms><rom>text<romid/></rom></roms>")

    def test_format_file_writes_and_reports_change(self, tmp_path):
        path = _write(tmp_path, MESSY)
        assert lint.format_file(path) is True
        assert lint.format_file(path) is False
        assert lint.check([path]) == []


# ------------------------------------------------------------------ lint


def _issues(tmp_path, tables: str, scalings: str) -> list:
    path = _write(
        tmp_path,
        f"<roms><rom>{ROMID}{scalings}{tables}</rom></roms>",
    )
    lint.format_file(path)
    return [i.message for i in lint.check([path])]


class TestLintRules:
    def test_clean_definition_has_no_issues(self, tmp_path):
        assert (
            _issues(
                tmp_path,
                '<table name="A" type="1D" address="bbbd0" elements="1" scaling="f"/>',
                '<scaling name="f" storagetype="float"/>',
            )
            == []
        )

    def test_misaligned_float_is_reported(self, tmp_path):
        issues = _issues(
            tmp_path,
            '<table name="A" type="1D" address="bbbd1" elements="1" scaling="f"/>',
            '<scaling name="f" storagetype="float"/>',
        )
        assert len(issues) == 1 and "not 4-byte aligned" in issues[0]

    def test_misaligned_axis_is_reported(self, tmp_path):
        issues = _issues(
            tmp_path,
            '<table name="A" type="2D" address="100" elements="2" scaling="b">'
            '<table type="Y Axis" address="201" elements="2" scaling="w"/></table>',
            '<scaling name="b" storagetype="uint8"/>'
            '<scaling name="w" storagetype="uint16"/>',
        )
        assert len(issues) == 1 and "'A' 'Y Axis' at 0x201" in issues[0]

    def test_unknown_scaling_and_storagetype(self, tmp_path):
        issues = _issues(
            tmp_path,
            '<table name="A" type="1D" address="100" scaling="nope"/>',
            '<scaling name="s" storagetype="uint12"/>',
        )
        assert any("unknown scaling 'nope'" in i for i in issues)
        assert any("unknown storagetype" in i for i in issues)

    def test_identical_duplicate_scaling_is_fine_differing_is_not(self, tmp_path):
        same = _issues(
            tmp_path,
            "",
            '<scaling name="s" storagetype="uint8"/><scaling name="s" storagetype="uint8"/>',
        )
        assert same == []
        differ = _issues(
            tmp_path,
            "",
            '<scaling name="s" storagetype="uint8"/><scaling name="s" storagetype="int8"/>',
        )
        assert differ == ["scaling 's' is defined twice, differently"]

    def test_table_past_end_of_rom(self, tmp_path):
        issues = _issues(
            tmp_path,
            '<table name="A" type="1D" address="ffffe" elements="2" scaling="w"/>',
            '<scaling name="w" storagetype="uint16"/>',
        )
        assert any("past 1 MB" in i for i in issues)

    def test_unformatted_file_is_reported(self, tmp_path):
        path = _write(tmp_path, MESSY)
        assert any("canonical layout" in i.message for i in lint.check([path]))

    def test_duplicate_rom_id_across_files(self, tmp_path):
        body = f"<roms><rom>{ROMID}</rom></roms>"
        a = _write(tmp_path, body, "a.xml")
        b = _write(tmp_path, body, "b.xml")
        lint.format_file(a)
        lint.format_file(b)
        assert any("also claimed by b.xml" in i.message for i in lint.check([a, b]))

    def test_elements_must_match_the_axes(self, tmp_path):
        # speeps' Mazda6 l3r3ee had 112 cells on 14 x 5 axes, so the table ran
        # into its neighbour.
        issues = _issues(
            tmp_path,
            '<table name="A" type="3D" address="100" elements="112" scaling="f">'
            '<table type="X Axis" address="400" elements="14" scaling="f"/>'
            '<table type="Y Axis" address="500" elements="5" scaling="f"/></table>',
            '<scaling name="f" storagetype="float"/>',
        )
        assert 'has elements="112" but its axes make 70 cells' in issues[0]

    def test_overlapping_tables_are_reported_aliases_are_not(self, tmp_path):
        scaling = '<scaling name="f" storagetype="float"/>'
        overlap = _issues(
            tmp_path,
            '<table name="A" type="1D" address="100" elements="2" scaling="f"/>'
            '<table name="B" type="1D" address="104" elements="1" scaling="f"/>',
            scaling,
        )
        assert overlap == ["'A' (0x100-0x107) overlaps 'B' (0x104-0x107)"]
        alias = _issues(
            tmp_path,
            '<table name="A" type="1D" address="100" elements="1" scaling="f"/>'
            '<table name="B" type="1D" address="100" elements="1" scaling="f"/>',
            scaling,
        )
        assert alias == []

    def test_duplicate_table_name_names_both_addresses(self, tmp_path):
        issues = _issues(
            tmp_path,
            '<table name="A" type="1D" address="100" elements="1" scaling="f"/>'
            '<table name="A" type="1D" address="200" elements="1" scaling="f"/>',
            '<scaling name="f" storagetype="float"/>',
        )
        assert issues == ["table 'A' at 0x200 has the same name as the one at 0x100"]

    def test_baseline_covers_a_line_only_as_often_as_listed(self, tmp_path):
        path = tmp_path / "t.xml"
        issues = [lint.Issue(path, "x"), lint.Issue(path, "x")]
        baseline = tmp_path / "baseline.txt"
        baseline.write_text("# comment\nt.xml: x\n", encoding="utf-8")
        new = lint.new_issues(issues, lint.load_baseline(baseline))
        assert [str(i) for i in new] == ["t.xml: x"]

    def test_single_quoted_declaration_is_fine(self, tmp_path):
        # The app's scaling editor saves through lxml, which writes
        # <?xml version='1.0' ...?>; that alone must not fail the layout check.
        path = _write(tmp_path, MESSY)
        lint.format_file(path)
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace(
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>",
            ),
            encoding="utf-8",
        )
        assert lint.check([path]) == []
