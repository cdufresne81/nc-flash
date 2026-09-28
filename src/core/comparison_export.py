"""
ROM Comparison Export

Formats the diff entries produced by CompareWindow._compute_diffs() into a
CSV or Markdown report. Pure data transformation — no Qt — so it can run
without a GUI and be unit-tested directly.

Entry schema (one dict per modified table, see compare_window.py):
    table_a, table_b: Table | None
    name, category: str
    data_a, data_b: dict | None  ('values', optional 'x_axis'/'y_axis')
    changed_cells: set[(row, col)]
    changed_axes: dict[str, set[int]]  ('x_axis'/'y_axis' -> changed indices)
    shape_mismatch, a_only, b_only: bool
"""

import csv
import io
from dataclasses import dataclass
from typing import Iterator, List, Optional

from .rom_definition import AxisType, RomDefinition, Table
from ..utils.formatting import (
    format_value as _format_value,
    get_axis_format as _get_axis_format,
    get_scaling_format as _get_scaling_format,
)

CSV_FIXED_COLUMNS = [
    "Category",
    "Table",
    "Change Type",
    "Row",
    "Col",
    "X Axis",
    "Y Axis",
    "Delta",
    "Note",
]


@dataclass(frozen=True)
class ComparisonRow:
    """One reported change: a data cell, an axis breakpoint, a one-sided
    table element, or a whole-table shape-mismatch summary. Value/axis
    fields are already rendered with the table's own scaling format."""

    category: str
    table: str
    change_type: str  # "cell" | "x_axis" | "y_axis" | "only_in_a" | "only_in_b" | "shape_mismatch"
    row: Optional[int]
    col: Optional[int]
    x_axis: str
    y_axis: str
    value_a: str
    value_b: str
    delta: str
    note: str = ""


def _value_str(
    table: Optional[Table], definition: Optional[RomDefinition], value
) -> str:
    if table is None or definition is None:
        return _format_value(value, ".2f")
    fmt = _get_scaling_format(definition, table.scaling)
    return _format_value(value, fmt)


def _axis_str(
    table: Optional[Table],
    definition: Optional[RomDefinition],
    axis_type: AxisType,
    array,
    idx: Optional[int],
) -> str:
    if array is None or idx is None or idx >= len(array):
        return ""
    fmt = _get_axis_format(definition, table, axis_type) if table else ".2f"
    return _format_value(array[idx], fmt)


def _delta_str(value_a: float, value_b: float, fmt: str) -> str:
    return _format_value(value_b - value_a, fmt)


def _rows_for_two_sided_entry(
    entry: dict,
    definition_a: RomDefinition,
    definition_b: RomDefinition,
) -> Iterator[ComparisonRow]:
    """Both table_a and table_b exist. Handles shape mismatch, cell
    changes, and axis-breakpoint changes."""
    category = entry["category"] or "Uncategorized"
    name = entry["name"]
    table_a, table_b = entry["table_a"], entry["table_b"]
    data_a, data_b = entry["data_a"], entry["data_b"]

    if entry["shape_mismatch"]:
        shape_a = getattr(data_a.get("values"), "shape", None)
        shape_b = getattr(data_b.get("values"), "shape", None)
        yield ComparisonRow(
            category=category,
            table=name,
            change_type="shape_mismatch",
            row=None,
            col=None,
            x_axis="",
            y_axis="",
            value_a=f"shape {shape_a}",
            value_b=f"shape {shape_b}",
            delta="",
            note="Shapes differ — cannot compare cell-by-cell.",
        )
        # Fall through to the axis-breakpoint loop below: _compute_diffs
        # detects axis-breakpoint changes independently of a shape
        # mismatch, so a table can have both.
    else:
        values_a = data_a.get("values")
        values_b = data_b.get("values")
        x_axis_a, y_axis_a = data_a.get("x_axis"), data_a.get("y_axis")

        for row, col in sorted(entry["changed_cells"]):
            if values_a.ndim == 1:
                va, vb = float(values_a[row]), float(values_b[row])
                out_row, out_col = row, None
                # col is a placeholder (0) for a 1D array, not a real X-axis
                # index — only a genuine 2D grid has a meaningful column.
                x_idx = None
            else:
                va, vb = float(values_a[row, col]), float(values_b[row, col])
                out_row, out_col = row, col
                x_idx = col

            value_a_str = _value_str(table_a, definition_a, va)
            value_b_str = _value_str(table_b, definition_b, vb)
            value_fmt = (
                _get_scaling_format(definition_b, table_b.scaling) if table_b else ".2f"
            )
            yield ComparisonRow(
                category=category,
                table=name,
                change_type="cell",
                row=out_row,
                col=out_col,
                x_axis=_axis_str(
                    table_a, definition_a, AxisType.X_AXIS, x_axis_a, x_idx
                ),
                y_axis=_axis_str(table_a, definition_a, AxisType.Y_AXIS, y_axis_a, row),
                value_a=value_a_str,
                value_b=value_b_str,
                delta=_delta_str(va, vb, value_fmt),
            )

    for axis_key, axis_type in (
        ("x_axis", AxisType.X_AXIS),
        ("y_axis", AxisType.Y_AXIS),
    ):
        changed_idx = entry["changed_axes"].get(axis_key)
        if not changed_idx:
            continue
        arr_a = data_a.get(axis_key)
        arr_b = data_b.get(axis_key)
        for idx in sorted(changed_idx):
            va, vb = float(arr_a[idx]), float(arr_b[idx])
            value_fmt = _get_axis_format(definition_b, table_b, axis_type)
            yield ComparisonRow(
                category=category,
                table=name,
                change_type=axis_key,
                row=idx,
                col=None,
                x_axis="",
                y_axis="",
                value_a=_axis_str(table_a, definition_a, axis_type, arr_a, idx),
                value_b=_axis_str(table_b, definition_b, axis_type, arr_b, idx),
                delta=_delta_str(va, vb, value_fmt),
            )


def _rows_for_one_sided_entry(
    entry: dict,
    definition_a: Optional[RomDefinition],
    definition_b: Optional[RomDefinition],
) -> Iterator[ComparisonRow]:
    """Only table_a or only table_b exists — every element is reported."""
    category = entry["category"] or "Uncategorized"
    name = entry["name"]
    is_a = entry["a_only"]
    table = entry["table_a"] if is_a else entry["table_b"]
    definition = definition_a if is_a else definition_b
    data = entry["data_a"] if is_a else entry["data_b"]
    values = data.get("values")
    x_axis, y_axis = data.get("x_axis"), data.get("y_axis")
    change_type = "only_in_a" if is_a else "only_in_b"

    for row, col in sorted(entry["changed_cells"]):
        if values.ndim == 1:
            v = float(values[row])
            out_row, out_col = row, None
            # col is a placeholder (0) for a 1D array, not a real X-axis
            # index — only a genuine 2D grid has a meaningful column.
            x_idx = None
        else:
            v = float(values[row, col])
            out_row, out_col = row, col
            x_idx = col

        value_str = _value_str(table, definition, v)
        yield ComparisonRow(
            category=category,
            table=name,
            change_type=change_type,
            row=out_row,
            col=out_col,
            x_axis=_axis_str(table, definition, AxisType.X_AXIS, x_axis, x_idx),
            y_axis=_axis_str(table, definition, AxisType.Y_AXIS, y_axis, row),
            value_a=value_str if is_a else "",
            value_b="" if is_a else value_str,
            delta="",
        )


def rows_for_entry(
    entry: dict,
    definition_a: Optional[RomDefinition],
    definition_b: Optional[RomDefinition],
) -> List[ComparisonRow]:
    """Flatten one CompareWindow diff entry into report rows."""
    if entry["a_only"] or entry["b_only"]:
        return list(_rows_for_one_sided_entry(entry, definition_a, definition_b))
    return list(_rows_for_two_sided_entry(entry, definition_a, definition_b))


def build_comparison_rows(
    entries: List[dict],
    definition_a: Optional[RomDefinition],
    definition_b: Optional[RomDefinition],
) -> List[ComparisonRow]:
    """Flatten every diff entry into a single, category/table-sorted row list."""
    ordered = sorted(entries, key=lambda e: (e["category"] or "", e["name"]))
    rows: List[ComparisonRow] = []
    for entry in ordered:
        rows.extend(rows_for_entry(entry, definition_a, definition_b))
    return rows


def export_comparison_csv(
    entries: List[dict],
    definition_a: Optional[RomDefinition],
    definition_b: Optional[RomDefinition],
    name_a: str,
    name_b: str,
) -> str:
    """Render a flat, one-row-per-change CSV report."""
    rows = build_comparison_rows(entries, definition_a, definition_b)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    header = CSV_FIXED_COLUMNS[:7] + [name_a, name_b] + CSV_FIXED_COLUMNS[7:]
    writer.writerow(header)
    for r in rows:
        writer.writerow(
            [
                r.category,
                r.table,
                r.change_type,
                "" if r.row is None else r.row,
                "" if r.col is None else r.col,
                r.x_axis,
                r.y_axis,
                r.value_a,
                r.value_b,
                r.delta,
                r.note,
            ]
        )
    return buf.getvalue()


def _escape_md(text: str) -> str:
    return str(text).replace("|", "\\|")


def export_comparison_markdown(
    entries: List[dict],
    definition_a: Optional[RomDefinition],
    definition_b: Optional[RomDefinition],
    name_a: str,
    name_b: str,
) -> str:
    """Render a per-table Markdown report grouped by category/table."""
    ordered = sorted(entries, key=lambda e: (e["category"] or "", e["name"]))
    # Row counts are derived from what rows_for_entry actually emits, not
    # from entry["change_count"] (a _compute_diffs-internal detection
    # metric — e.g. for a shape mismatch it's the union of both tables'
    # cell-index sets, not the single summary row the report shows).
    entry_rows = [
        (entry, rows_for_entry(entry, definition_a, definition_b)) for entry in ordered
    ]
    total_changes = sum(len(rows) for _, rows in entry_rows)

    lines = [
        f"# ROM Comparison — {name_a} vs {name_b}",
        "",
        f"- **{_escape_md(name_a)}** vs **{_escape_md(name_b)}**",
        f"- Tables changed: {len(ordered)}",
        f"- Total changes: {total_changes}",
        "",
    ]

    header = (
        "| Change Type | Row | Col | X Axis | Y Axis | "
        f"{_escape_md(name_a)} | {_escape_md(name_b)} | Delta | Note |"
    )
    separator = "|---|---|---|---|---|---|---|---|---|"

    for entry, rows in entry_rows:
        category = entry["category"] or "Uncategorized"
        lines.append(f"## {_escape_md(category)} / {_escape_md(entry['name'])}")
        lines.append("")
        lines.append(f"_{len(rows)} change(s)_")
        lines.append("")
        lines.append(header)
        lines.append(separator)
        for r in rows:
            lines.append(
                "| "
                + " | ".join(
                    _escape_md(v)
                    for v in (
                        r.change_type,
                        "" if r.row is None else r.row,
                        "" if r.col is None else r.col,
                        r.x_axis,
                        r.y_axis,
                        r.value_a,
                        r.value_b,
                        r.delta,
                        r.note,
                    )
                )
                + " |"
            )
        lines.append("")

    return "\n".join(lines) + "\n"
