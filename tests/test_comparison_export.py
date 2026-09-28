"""
Tests for src/core/comparison_export.py

Unit tests build diff entries by hand (same dict shape CompareWindow's
_compute_diffs produces) so the formatting logic is exercised without Qt or
a real ROM. The integration test drives the real pipeline — real XML
definition + real ROM binary + CompareWindow's own _compute_diffs — against
committed golden fixture files, so a regression in either the diff engine
or the export formatting shows up as a diff against a file on disk.
"""

import csv
import io

import numpy as np
import pytest

from src.core.comparison_export import (
    ComparisonRow,
    build_comparison_rows,
    export_comparison_csv,
    export_comparison_markdown,
    rows_for_entry,
)
from src.core.rom_definition import RomDefinition, RomID, Scaling, Table, TableType

FIXTURES_DIR = (
    __import__("pathlib").Path(__file__).parent / "fixtures" / "comparison_export"
)


# ---------------------------------------------------------------------------
# Helpers (mirrors tests/test_compare_window.py's conventions)
# ---------------------------------------------------------------------------


def _make_romid(xmlid="test_rom"):
    return RomID(
        xmlid=xmlid,
        internalidaddress="0x0",
        internalidstring="TEST",
        ecuid="",
        make="",
        model="",
        flashmethod="",
        memmodel="",
        checksummodule="",
    )


def _make_scaling(name="s", fmt="%0.2f"):
    return Scaling(
        name=name,
        units="",
        toexpr="x",
        frexpr="x",
        format=fmt,
        min=0.0,
        max=100.0,
        inc=1.0,
        storagetype="float",
        endian="big",
    )


def _make_table(
    name="T1", scaling="s", table_type=TableType.ONE_D, elements=1, category="Cat"
):
    return Table(
        name=name,
        address="0x100",
        type=table_type,
        elements=elements,
        scaling=scaling,
        category=category,
    )


def _make_definition(scaling_fmt="%0.2f", xmlid="rom"):
    scaling = _make_scaling(fmt=scaling_fmt)
    return RomDefinition(romid=_make_romid(xmlid), scalings={"s": scaling}, tables=[])


def _base_entry(**overrides):
    entry = {
        "table_a": _make_table(),
        "table_b": _make_table(),
        "name": "T1",
        "category": "Cat",
        "data_a": {"values": np.array([1.0])},
        "data_b": {"values": np.array([2.0])},
        "changed_cells": {(0, 0)},
        "changed_axes": {},
        "change_count": 1,
        "shape_mismatch": False,
        "a_only": False,
        "b_only": False,
    }
    entry.update(overrides)
    return entry


def _parse_csv(text: str):
    return list(csv.reader(io.StringIO(text)))


# ---------------------------------------------------------------------------
# Unit tests: rows_for_entry — cell changes
# ---------------------------------------------------------------------------


class TestRowsForEntryCell:
    def test_one_d_cell_change(self):
        definition = _make_definition()
        entry = _base_entry(
            data_a={"values": np.array([1.0])},
            data_b={"values": np.array([2.5])},
        )
        rows = rows_for_entry(entry, definition, definition)
        assert rows == [
            ComparisonRow(
                category="Cat",
                table="T1",
                change_type="cell",
                row=0,
                col=None,
                x_axis="",
                y_axis="",
                value_a="1.00",
                value_b="2.50",
                delta="1.50",
            )
        ]

    def test_two_d_cell_change_reports_row_col_and_axes(self):
        definition = _make_definition()
        table = _make_table(table_type=TableType.THREE_D)
        values_a = np.array([[1.0, 2.0], [3.0, 4.0]])
        values_b = np.array([[1.0, 2.0], [3.0, 40.0]])
        entry = _base_entry(
            table_a=table,
            table_b=table,
            data_a={
                "values": values_a,
                "x_axis": np.array([10.0, 20.0]),
                "y_axis": np.array([100.0, 200.0]),
            },
            data_b={
                "values": values_b,
                "x_axis": np.array([10.0, 20.0]),
                "y_axis": np.array([100.0, 200.0]),
            },
            changed_cells={(1, 1)},
        )
        rows = rows_for_entry(entry, definition, definition)
        assert len(rows) == 1
        row = rows[0]
        assert row.row == 1 and row.col == 1
        assert row.x_axis == "20.00"
        assert row.y_axis == "200.00"
        assert row.value_a == "4.00"
        assert row.value_b == "40.00"
        assert row.delta == "36.00"

    def test_multiple_changed_cells_sorted_by_position(self):
        definition = _make_definition()
        values_a = np.array([1.0, 2.0, 3.0])
        values_b = np.array([9.0, 2.0, 9.0])
        entry = _base_entry(
            data_a={"values": values_a},
            data_b={"values": values_b},
            changed_cells={(2, 0), (0, 0)},
        )
        rows = rows_for_entry(entry, definition, definition)
        assert [r.row for r in rows] == [0, 2]

    def test_cross_definition_uses_each_sides_own_format(self):
        """Side A and side B may have different scalings (cross-def compare);
        each value must be rendered with its own side's format, and the
        delta with side B's (the 'new' value's) format."""
        def_a = _make_definition(scaling_fmt="%0.0f")
        def_b = _make_definition(scaling_fmt="%0.3f")
        entry = _base_entry(
            data_a={"values": np.array([1.0])},
            data_b={"values": np.array([2.5])},
        )
        rows = rows_for_entry(entry, def_a, def_b)
        row = rows[0]
        assert row.value_a == "1"  # def_a's integer format
        assert row.value_b == "2.500"  # def_b's 3-decimal format
        assert row.delta == "1.500"  # delta rendered with def_b's format

    def test_x_axis_blank_when_values_stayed_1d_despite_an_x_axis(self):
        """RomReader only reshapes a THREE_D table's values into a 2D grid
        when len(values) == x_len * y_len; on a mismatched/stale definition
        it leaves 'values' 1D while still populating 'x_axis'. _compute_diffs
        then reports every changed cell as (i, 0) — col is a placeholder,
        not a real X-axis index — so the X Axis column must stay blank
        rather than repeat x_axis[0] for every row."""
        definition = _make_definition()
        table = _make_table(table_type=TableType.THREE_D)
        entry = _base_entry(
            table_a=table,
            table_b=table,
            data_a={
                "values": np.array([1.0, 2.0, 3.0]),  # reshape failed: still 1D
                "x_axis": np.array([100.0, 200.0]),  # populated despite that
                "y_axis": np.array([5.0, 6.0, 7.0]),
            },
            data_b={
                "values": np.array([1.0, 99.0, 3.0]),
                "x_axis": np.array([100.0, 200.0]),
                "y_axis": np.array([5.0, 6.0, 7.0]),
            },
            changed_cells={(1, 0)},
        )
        rows = rows_for_entry(entry, definition, definition)
        assert len(rows) == 1
        assert rows[0].x_axis == ""
        assert rows[0].y_axis == "6.00"  # the real per-row axis is unaffected


# ---------------------------------------------------------------------------
# Unit tests: rows_for_entry — axis breakpoint changes
# ---------------------------------------------------------------------------


class TestRowsForEntryAxis:
    def test_y_axis_only_change(self):
        definition = _make_definition()
        table = _make_table(table_type=TableType.TWO_D)
        values = np.array([1.0, 2.0])
        entry = _base_entry(
            table_a=table,
            table_b=table,
            data_a={"values": values.copy(), "y_axis": np.array([10.0, 20.0])},
            data_b={"values": values.copy(), "y_axis": np.array([10.0, 25.0])},
            changed_cells=set(),
            changed_axes={"y_axis": {1}},
        )
        rows = rows_for_entry(entry, definition, definition)
        assert len(rows) == 1
        row = rows[0]
        assert row.change_type == "y_axis"
        assert row.row == 1
        assert row.col is None
        assert row.value_a == "20.00"
        assert row.value_b == "25.00"
        assert row.delta == "5.00"
        # Per-cell axis columns are not populated for an axis-change row —
        # the row itself *is* the axis change, not a data cell.
        assert row.x_axis == "" and row.y_axis == ""

    def test_x_axis_only_change(self):
        definition = _make_definition()
        table = _make_table(table_type=TableType.THREE_D)
        values = np.array([[1.0, 2.0]])
        entry = _base_entry(
            table_a=table,
            table_b=table,
            data_a={
                "values": values.copy(),
                "x_axis": np.array([10.0, 20.0]),
                "y_axis": np.array([1.0]),
            },
            data_b={
                "values": values.copy(),
                "x_axis": np.array([10.0, 99.0]),
                "y_axis": np.array([1.0]),
            },
            changed_cells=set(),
            changed_axes={"x_axis": {1}},
        )
        rows = rows_for_entry(entry, definition, definition)
        assert len(rows) == 1
        assert rows[0].change_type == "x_axis"
        assert rows[0].row == 1
        assert rows[0].value_a == "20.00"
        assert rows[0].value_b == "99.00"

    def test_both_cell_and_axis_changes_reported(self):
        definition = _make_definition()
        table = _make_table(table_type=TableType.TWO_D)
        entry = _base_entry(
            table_a=table,
            table_b=table,
            data_a={"values": np.array([1.0, 2.0]), "y_axis": np.array([10.0, 20.0])},
            data_b={"values": np.array([1.0, 99.0]), "y_axis": np.array([10.0, 25.0])},
            changed_cells={(1, 0)},
            changed_axes={"y_axis": {1}},
        )
        rows = rows_for_entry(entry, definition, definition)
        types = sorted(r.change_type for r in rows)
        assert types == ["cell", "y_axis"]


# ---------------------------------------------------------------------------
# Unit tests: shape mismatch and one-sided tables
# ---------------------------------------------------------------------------


class TestRowsForEntryShapeMismatchAndOneSided:
    def test_shape_mismatch_emits_single_summary_row(self):
        definition = _make_definition()
        entry = _base_entry(
            data_a={"values": np.array([[1.0, 2.0]])},
            data_b={"values": np.array([[1.0], [2.0]])},
            changed_cells={(0, 0), (0, 1), (1, 0)},
            shape_mismatch=True,
        )
        rows = rows_for_entry(entry, definition, definition)
        assert len(rows) == 1
        row = rows[0]
        assert row.change_type == "shape_mismatch"
        assert row.row is None and row.col is None
        assert "(1, 2)" in row.value_a
        assert "(2, 1)" in row.value_b
        assert row.note != ""

    def test_shape_mismatch_still_reports_a_concurrent_axis_change(self):
        """A table can have BOTH a shape mismatch and a changed axis
        breakpoint (_compute_diffs detects axis changes independently of
        the shape check) — the axis change must not be silently dropped."""
        definition = _make_definition()
        table = _make_table(table_type=TableType.TWO_D)
        entry = _base_entry(
            table_a=table,
            table_b=table,
            data_a={
                "values": np.array([[1.0, 2.0]]),
                "y_axis": np.array([10.0, 20.0]),
            },
            data_b={
                "values": np.array([[1.0], [2.0]]),
                "y_axis": np.array([10.0, 25.0]),
            },
            changed_cells={(0, 0), (0, 1), (1, 0)},
            changed_axes={"y_axis": {1}},
            shape_mismatch=True,
        )
        rows = rows_for_entry(entry, definition, definition)
        types = [r.change_type for r in rows]
        assert types == ["shape_mismatch", "y_axis"]
        axis_row = rows[1]
        assert axis_row.value_a == "20.00" and axis_row.value_b == "25.00"

    def test_a_only_reports_every_element_on_side_a(self):
        definition = _make_definition()
        table = _make_table()
        entry = _base_entry(
            table_a=table,
            table_b=None,
            data_a={"values": np.array([10.0, 20.0])},
            data_b=None,
            changed_cells={(0, 0), (1, 0)},
            a_only=True,
            b_only=False,
        )
        rows = rows_for_entry(entry, definition, None)
        assert len(rows) == 2
        assert all(r.change_type == "only_in_a" for r in rows)
        assert all(r.value_b == "" for r in rows)
        assert {r.value_a for r in rows} == {"10.00", "20.00"}

    def test_b_only_reports_every_element_on_side_b(self):
        definition = _make_definition()
        table = _make_table()
        entry = _base_entry(
            table_a=None,
            table_b=table,
            data_a=None,
            data_b={"values": np.array([5.0])},
            changed_cells={(0, 0)},
            a_only=False,
            b_only=True,
        )
        rows = rows_for_entry(entry, None, definition)
        assert len(rows) == 1
        assert rows[0].change_type == "only_in_b"
        assert rows[0].value_a == ""
        assert rows[0].value_b == "5.00"


# ---------------------------------------------------------------------------
# Unit tests: build_comparison_rows (ordering across entries)
# ---------------------------------------------------------------------------


class TestBuildComparisonRows:
    def test_sorts_by_category_then_name_regardless_of_input_order(self):
        definition = _make_definition()
        entry_z = _base_entry(name="Zebra", category="B")
        entry_a = _base_entry(name="Apple", category="A")
        entry_b = _base_entry(name="Banana", category="A")
        rows = build_comparison_rows(
            [entry_z, entry_b, entry_a], definition, definition
        )
        assert [r.table for r in rows] == ["Apple", "Banana", "Zebra"]

    def test_empty_entries_returns_empty_list(self):
        assert build_comparison_rows([], None, None) == []


# ---------------------------------------------------------------------------
# Unit tests: CSV rendering
# ---------------------------------------------------------------------------


class TestExportComparisonCsv:
    def test_header_uses_rom_names(self):
        text = export_comparison_csv([], None, None, "Stock", "Custom Tune")
        parsed = _parse_csv(text)
        assert parsed[0] == [
            "Category",
            "Table",
            "Change Type",
            "Row",
            "Col",
            "X Axis",
            "Y Axis",
            "Stock",
            "Custom Tune",
            "Delta",
            "Note",
        ]

    def test_empty_entries_is_header_only(self):
        text = export_comparison_csv([], None, None, "A", "B")
        parsed = _parse_csv(text)
        assert len(parsed) == 1

    def test_row_values_round_trip_through_csv_parser(self):
        definition = _make_definition()
        entry = _base_entry(
            data_a={"values": np.array([1.0])},
            data_b={"values": np.array([2.5])},
        )
        text = export_comparison_csv([entry], definition, definition, "A", "B")
        parsed = _parse_csv(text)
        assert parsed[1] == [
            "Cat",
            "T1",
            "cell",
            "0",
            "",
            "",
            "",
            "1.00",
            "2.50",
            "1.50",
            "",
        ]

    def test_rom_name_with_comma_is_quoted_and_round_trips(self):
        """A ROM/tune name containing a comma must not corrupt the CSV grid —
        csv.writer's quoting handles it, and csv.reader must parse it back
        as a single header field."""
        text = export_comparison_csv([], None, None, "Stock, v2", "B")
        parsed = _parse_csv(text)
        assert parsed[0][7] == "Stock, v2"


# ---------------------------------------------------------------------------
# Unit tests: Markdown rendering
# ---------------------------------------------------------------------------


class TestExportComparisonMarkdown:
    def test_empty_entries_reports_zero_changes(self):
        text = export_comparison_markdown([], None, None, "A", "B")
        assert "Tables changed: 0" in text
        assert "Total changes: 0" in text

    def test_includes_per_table_section_and_summary(self):
        definition = _make_definition()
        entry = _base_entry(
            data_a={"values": np.array([1.0])},
            data_b={"values": np.array([2.5])},
        )
        text = export_comparison_markdown([entry], definition, definition, "A", "B")
        assert "# ROM Comparison — A vs B" in text
        assert "Tables changed: 1" in text
        assert "## Cat / T1" in text
        assert "| cell | 0 |  |  |  | 1.00 | 2.50 | 1.50 |  |" in text

    def test_pipe_characters_in_table_name_are_escaped(self):
        """A table/category name containing '|' must not break the Markdown
        table grid."""
        definition = _make_definition()
        entry = _base_entry(name="Left | Right", category="Cat | X")
        text = export_comparison_markdown(
            [entry], definition, definition, "Stock", "Modified"
        )
        assert "## Cat \\| X / Left \\| Right" in text
        # The unescaped raw name must not appear anywhere in the output —
        # only its escaped form (checked above) is allowed.
        assert "Left | Right" not in text

    def test_change_counts_reflect_reported_rows_not_raw_change_count(self):
        """A shape-mismatch entry's change_count (set by _compute_diffs) is
        the union of both tables' cell-index sets — 3 for a (1,2) vs (2,1)
        mismatch — but the report only ever prints ONE summary row for it.
        Both the per-table '_N change(s)_' line and the top-level 'Total
        changes' must count what was actually printed, not that raw
        detection metric."""
        definition = _make_definition()
        entry = _base_entry(
            data_a={"values": np.array([[1.0, 2.0]])},
            data_b={"values": np.array([[1.0], [2.0]])},
            changed_cells={(0, 0), (0, 1), (1, 0)},
            shape_mismatch=True,
            change_count=3,  # what _compute_diffs would set — deliberately wrong here
        )
        text = export_comparison_markdown(
            [entry], definition, definition, "Stock", "Modified"
        )
        assert "Total changes: 1" in text
        assert "_1 change(s)_" in text
        assert "_3 change(s)_" not in text


# ---------------------------------------------------------------------------
# Integration test: real definition + real ROM + real CompareWindow diffing
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestComparisonExportIntegration:
    """Mutates two real tables in a copy of the bundled example ROM, runs
    them through CompareWindow._compute_diffs() (the actual production diff
    path), exports, and compares byte-for-byte against committed golden
    fixture files.
    """

    @staticmethod
    def _build_modified_tables(sample_xml_path, sample_rom_path):
        from src.core.definition_parser import load_definition
        from src.core.rom_reader import RomReader
        from src.ui.compare_window import CompareWindow

        definition = load_definition(str(sample_xml_path))
        reader_a = RomReader(str(sample_rom_path), definition)
        reader_b = RomReader(str(sample_rom_path), definition)

        table1 = next(t for t in definition.tables if t.name == "TP Closed - Max")
        table2 = next(
            t
            for t in definition.tables
            if t.name == "AFS Scaling - Barometric Multiplier"
        )

        # TP Closed - Max is a single-element (ONE_D) table: stock 16.0 -> 17.5
        reader_b.write_table_data(table1, np.array([17.5]))

        # AFS Scaling - Barometric Multiplier is a TWO_D table (y_axis +
        # 1D values): change only the row at y_axis=90.0 (index 2)
        data2 = reader_b.read_table_data(table2)
        new_values = data2["values"].copy()
        new_values[2] = 1.75
        reader_b.write_table_data(table2, new_values)

        win = CompareWindow.__new__(CompareWindow)
        win._reader_a = reader_a
        win._reader_b = reader_b
        win._definition_a = definition
        win._definition_b = definition
        win._cross_def = False
        win._modified_tables = []
        win._compute_diffs()

        return win._modified_tables, definition

    def test_exactly_the_two_mutated_tables_are_detected(
        self, sample_xml_path, sample_rom_path
    ):
        modified, _ = self._build_modified_tables(sample_xml_path, sample_rom_path)
        names = sorted(e["name"] for e in modified)
        assert names == [
            "AFS Scaling - Barometric Multiplier",
            "TP Closed - Max",
        ]

    def test_csv_export_matches_golden_fixture(self, sample_xml_path, sample_rom_path):
        modified, definition = self._build_modified_tables(
            sample_xml_path, sample_rom_path
        )
        text = export_comparison_csv(
            modified, definition, definition, "Stock", "Modified"
        )
        expected = (FIXTURES_DIR / "expected_comparison.csv").read_text(
            encoding="utf-8"
        )
        assert text == expected

    def test_markdown_export_matches_golden_fixture(
        self, sample_xml_path, sample_rom_path
    ):
        modified, definition = self._build_modified_tables(
            sample_xml_path, sample_rom_path
        )
        text = export_comparison_markdown(
            modified, definition, definition, "Stock", "Modified"
        )
        expected = (FIXTURES_DIR / "expected_comparison.md").read_text(encoding="utf-8")
        assert text == expected

    def test_original_rom_bytes_on_disk_are_untouched(
        self, sample_xml_path, sample_rom_path
    ):
        """write_table_data() mutates the RomReader's in-memory buffer only —
        the bundled example ROM on disk must never be modified by this test."""
        original_bytes = sample_rom_path.read_bytes()
        self._build_modified_tables(sample_xml_path, sample_rom_path)
        assert sample_rom_path.read_bytes() == original_bytes
