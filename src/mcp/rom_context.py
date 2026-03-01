"""
ROM Context — loading, caching, and formatting helpers for MCP tools.

Provides a RomContext class that manages ROM loading with LRU caching,
and implements the core logic for all MCP tools.
"""

import json
import re
import urllib.request
import urllib.error
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from ..core.definition_parser import load_definition
from ..core.exceptions import RomEditorError
from ..core.rom_definition import (
    AxisType,
    RomDefinition,
    Scaling,
    Table,
    TableType,
)
from ..core.rom_detector import RomDetector
from ..core.rom_reader import RomReader
from ..utils.paths import get_app_root

_PRINTF_PATTERN = re.compile(r"%[-+0 #]*(\d*)\.?(\d*)([diouxXeEfFgGaAcspn%])")


def _printf_to_python_format(printf_format: str) -> str:
    """Convert printf-style format to Python format spec.

    Duplicated from compare_window.py to avoid Qt import dependency.
    """
    if not printf_format:
        return ".2f"
    match = _PRINTF_PATTERN.match(printf_format)
    if not match:
        return ".2f"
    width = match.group(1)
    precision = match.group(2)
    specifier = match.group(3)
    result = ""
    if width:
        result += width
    if precision:
        result += f".{precision}"
    result += specifier
    return result


def _format_value(value: float, fmt_spec: str) -> str:
    """Format a single value using a Python format spec."""
    try:
        return f"{value:{fmt_spec}}"
    except (ValueError, TypeError):
        return str(value)


class _CacheEntry:
    """A cached ROM entry holding definition, reader, and ROM ID."""

    __slots__ = ("definition", "reader", "rom_id_string")

    def __init__(
        self,
        definition: RomDefinition,
        reader: RomReader,
        rom_id_string: Optional[str],
    ):
        self.definition = definition
        self.reader = reader
        self.rom_id_string = rom_id_string


class RomContext:
    """Manages ROM loading with LRU caching and provides tool implementations."""

    MAX_CACHE_SIZE = 4

    def __init__(self, definitions_dir: Optional[str] = None):
        if definitions_dir:
            self._definitions_dir = str(Path(definitions_dir).resolve())
        else:
            self._definitions_dir = str(get_app_root() / "definitions")
        self._detector = RomDetector(self._definitions_dir)
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()

    def _get_entry(self, rom_path: str) -> _CacheEntry:
        """Load a ROM (with LRU caching) and return its cache entry."""
        key = str(Path(rom_path).resolve())

        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]

        rom_id_string, xml_path = self._detector.detect_rom_id(rom_path)
        if xml_path is None:
            raise RomEditorError(f"No matching definition found for ROM: {rom_path}")

        definition = load_definition(xml_path)
        reader = RomReader(rom_path, definition)
        entry = _CacheEntry(definition, reader, rom_id_string)

        self._cache[key] = entry
        if len(self._cache) > self.MAX_CACHE_SIZE:
            self._cache.popitem(last=False)

        return entry

    # ------------------------------------------------------------------
    # Tool 0: get_workspace
    # ------------------------------------------------------------------

    def get_workspace(self) -> Dict[str, Any]:
        """Read workspace.json written by the GUI app.

        Returns the file contents (open ROMs, active ROM) if the file
        exists, or a message indicating no ROMs are open.
        """
        workspace_path = get_app_root() / "workspace.json"
        if not workspace_path.exists():
            return {
                "open_roms": [],
                "message": "No ROMs currently open in the app (or app is not running)",
            }
        try:
            data = json.loads(workspace_path.read_text(encoding="utf-8"))
            return data
        except (json.JSONDecodeError, OSError):
            return {
                "open_roms": [],
                "message": "No ROMs currently open in the app (or app is not running)",
            }

    # ------------------------------------------------------------------
    # Tool 1: get_rom_info
    # ------------------------------------------------------------------

    def get_rom_info(self, rom_path: str) -> Dict[str, Any]:
        """Auto-detect ROM type and return identification and summary."""
        entry = self._get_entry(rom_path)
        defn = entry.definition
        romid = defn.romid

        categories = defn.get_tables_by_category()
        category_summary = {
            cat: len(tables) for cat, tables in sorted(categories.items())
        }

        # Count non-axis tables
        table_count = sum(1 for t in defn.tables if not t.is_axis)

        file_size = Path(rom_path).resolve().stat().st_size

        return {
            "rom_path": str(Path(rom_path).resolve()),
            "file_size_bytes": file_size,
            "identification": {
                "xmlid": romid.xmlid,
                "ecuid": romid.ecuid,
                "internalidstring": romid.internalidstring,
                "make": romid.make,
                "model": romid.model,
                "year": romid.year,
                "market": romid.market,
                "submodel": romid.submodel,
                "transmission": romid.transmission,
            },
            "table_count": table_count,
            "category_summary": category_summary,
        }

    # ------------------------------------------------------------------
    # Tool 2: list_tables
    # ------------------------------------------------------------------

    def list_tables(
        self,
        rom_path: str,
        category: Optional[str] = None,
        search: Optional[str] = None,
        level: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """List tables with optional filtering."""
        entry = self._get_entry(rom_path)
        defn = entry.definition

        if category:
            categories = defn.get_tables_by_category()
            tables = categories.get(category, [])
        else:
            tables = [t for t in defn.tables if not t.is_axis]

        if search:
            search_lower = search.lower()
            tables = [t for t in tables if search_lower in t.name.lower()]

        if level is not None:
            tables = [t for t in tables if t.level == level]

        result = []
        for table in tables:
            info = self._table_summary(table, defn)
            result.append(info)

        return result

    def _table_summary(self, table: Table, defn: RomDefinition) -> Dict[str, Any]:
        """Build a summary dict for a table."""
        scaling = defn.get_scaling(table.scaling)
        info: Dict[str, Any] = {
            "name": table.name,
            "category": table.category or "Uncategorized",
            "type": table.type.value,
            "address": table.address,
            "elements": table.elements,
            "level": table.level,
        }
        if scaling:
            info["units"] = scaling.units
            info["storage_type"] = scaling.storagetype

        # Dimensions
        if table.type == TableType.ONE_D:
            info["dimensions"] = str(table.elements)
        elif table.type == TableType.TWO_D:
            y_axis = table.y_axis
            if y_axis:
                info["dimensions"] = str(y_axis.elements)
                y_scaling = defn.get_scaling(y_axis.scaling)
                info["y_axis"] = {
                    "name": y_axis.name,
                    "units": y_scaling.units if y_scaling else "",
                }
        elif table.type == TableType.THREE_D:
            x_axis = table.x_axis
            y_axis = table.y_axis
            cols = x_axis.elements if x_axis else 0
            rows = y_axis.elements if y_axis else 0
            info["dimensions"] = f"{cols}x{rows}"
            if x_axis:
                x_scaling = defn.get_scaling(x_axis.scaling)
                info["x_axis"] = {
                    "name": x_axis.name,
                    "units": x_scaling.units if x_scaling else "",
                }
            if y_axis:
                y_scaling = defn.get_scaling(y_axis.scaling)
                info["y_axis"] = {
                    "name": y_axis.name,
                    "units": y_scaling.units if y_scaling else "",
                }

        return info

    # ------------------------------------------------------------------
    # Tool 3: read_table
    # ------------------------------------------------------------------

    def read_table(self, rom_path: str, table_name: str) -> Dict[str, Any]:
        """Read a table's scaled display values with full axis context."""
        entry = self._get_entry(rom_path)
        defn = entry.definition
        reader = entry.reader

        table = defn.get_table_by_name(table_name)
        if table is None:
            raise RomEditorError(f"Table not found: {table_name}")

        data = reader.read_table_data(table)
        if data is None:
            raise RomEditorError(f"Failed to read table data: {table_name}")

        scaling = defn.get_scaling(table.scaling)
        fmt_spec = _printf_to_python_format(scaling.format) if scaling else ".2f"

        result: Dict[str, Any] = {
            "metadata": {
                "name": table.name,
                "type": table.type.value,
                "address": table.address,
                "elements": table.elements,
            },
        }

        if scaling:
            result["metadata"]["units"] = scaling.units
            result["metadata"]["scaling_expression"] = scaling.toexpr
            result["metadata"]["storage_type"] = scaling.storagetype
            result["metadata"]["min"] = scaling.min
            result["metadata"]["max"] = scaling.max

        values = data["values"]

        if table.type == TableType.ONE_D:
            result["metadata"]["dimensions"] = str(table.elements)
            result["values"] = [_format_value(v, fmt_spec) for v in values.flat]

        elif table.type == TableType.TWO_D:
            y_axis_table = table.y_axis
            result["metadata"]["dimensions"] = str(
                y_axis_table.elements if y_axis_table else table.elements
            )
            result["values"] = [_format_value(v, fmt_spec) for v in values.flat]
            if "y_axis" in data and y_axis_table:
                result["y_axis"] = self._format_axis(y_axis_table, data["y_axis"], defn)

        elif table.type == TableType.THREE_D:
            x_axis_table = table.x_axis
            y_axis_table = table.y_axis
            cols = x_axis_table.elements if x_axis_table else 0
            rows = y_axis_table.elements if y_axis_table else 0
            result["metadata"]["dimensions"] = f"{cols}x{rows}"

            # values is shaped (rows, cols) from read_table_data
            grid = []
            for row_idx in range(values.shape[0]):
                grid.append([_format_value(v, fmt_spec) for v in values[row_idx]])
            result["values"] = grid

            if "x_axis" in data and x_axis_table:
                result["x_axis"] = self._format_axis(x_axis_table, data["x_axis"], defn)
            if "y_axis" in data and y_axis_table:
                result["y_axis"] = self._format_axis(y_axis_table, data["y_axis"], defn)

        return result

    def _format_axis(
        self, axis_table: Table, axis_values: np.ndarray, defn: RomDefinition
    ) -> Dict[str, Any]:
        """Format an axis with name, units, and formatted values."""
        axis_scaling = defn.get_scaling(axis_table.scaling)
        axis_fmt = (
            _printf_to_python_format(axis_scaling.format) if axis_scaling else ".2f"
        )
        return {
            "name": axis_table.name,
            "units": axis_scaling.units if axis_scaling else "",
            "scaling_expression": axis_scaling.toexpr if axis_scaling else "",
            "values": [_format_value(v, axis_fmt) for v in axis_values.flat],
        }

    # ------------------------------------------------------------------
    # Tool 4: compare_tables
    # ------------------------------------------------------------------

    def compare_tables(
        self,
        rom_path_a: str,
        rom_path_b: str,
        table_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Compare tables between two ROMs."""
        entry_a = self._get_entry(rom_path_a)
        entry_b = self._get_entry(rom_path_b)

        if table_name:
            return self._compare_single_table(entry_a, entry_b, table_name)
        else:
            return self._compare_all_tables(entry_a, entry_b)

    def _compare_all_tables(
        self, entry_a: _CacheEntry, entry_b: _CacheEntry
    ) -> Dict[str, Any]:
        """Summary of all differing tables between two ROMs."""
        defn_a = entry_a.definition
        defn_b = entry_b.definition

        tables_a = {t.name: t for t in defn_a.tables if not t.is_axis}
        tables_b = {t.name: t for t in defn_b.tables if not t.is_axis}

        common_names = set(tables_a.keys()) & set(tables_b.keys())
        a_only = sorted(set(tables_a.keys()) - set(tables_b.keys()))
        b_only = sorted(set(tables_b.keys()) - set(tables_a.keys()))

        changed_tables = []
        for name in sorted(common_names):
            table_a = tables_a[name]
            table_b = tables_b[name]

            try:
                data_a = entry_a.reader.read_table_data(table_a)
                data_b = entry_b.reader.read_table_data(table_b)
            except RomEditorError:
                continue

            if data_a is None or data_b is None:
                continue

            vals_a = data_a["values"].flat
            vals_b = data_b["values"].flat

            if len(vals_a) != len(vals_b):
                changed_tables.append(
                    {
                        "name": name,
                        "reason": "shape_mismatch",
                        "dimensions_a": str(data_a["values"].shape),
                        "dimensions_b": str(data_b["values"].shape),
                    }
                )
                continue

            # Skip tables where both are all-NaN
            all_nan_a = (
                np.all(np.isnan(vals_a))
                if np.issubdtype(data_a["values"].dtype, np.floating)
                else False
            )
            all_nan_b = (
                np.all(np.isnan(vals_b))
                if np.issubdtype(data_b["values"].dtype, np.floating)
                else False
            )
            if all_nan_a and all_nan_b:
                continue

            diffs = np.array(vals_a) != np.array(vals_b)
            if np.any(diffs):
                total = len(vals_a)
                changed = int(np.sum(diffs))
                changed_tables.append(
                    {
                        "name": name,
                        "changed_cells": changed,
                        "total_cells": total,
                        "change_percent": round(changed / total * 100, 1),
                    }
                )

        return {
            "rom_a": str(Path(entry_a.reader.rom_path).name),
            "rom_b": str(Path(entry_b.reader.rom_path).name),
            "summary": {
                "total_common_tables": len(common_names),
                "changed_table_count": len(changed_tables),
                "a_only_count": len(a_only),
                "b_only_count": len(b_only),
            },
            "changed_tables": changed_tables,
            "a_only_tables": a_only,
            "b_only_tables": b_only,
        }

    def _compare_single_table(
        self,
        entry_a: _CacheEntry,
        entry_b: _CacheEntry,
        table_name: str,
    ) -> Dict[str, Any]:
        """Cell-by-cell diff for a single table."""
        defn_a = entry_a.definition
        defn_b = entry_b.definition

        table_a = defn_a.get_table_by_name(table_name)
        table_b = defn_b.get_table_by_name(table_name)

        if table_a is None and table_b is None:
            raise RomEditorError(f"Table not found in either ROM: {table_name}")
        if table_a is None:
            raise RomEditorError(f"Table '{table_name}' only exists in ROM B")
        if table_b is None:
            raise RomEditorError(f"Table '{table_name}' only exists in ROM A")

        data_a = entry_a.reader.read_table_data(table_a)
        data_b = entry_b.reader.read_table_data(table_b)
        if data_a is None or data_b is None:
            raise RomEditorError(f"Failed to read table data: {table_name}")

        scaling_a = defn_a.get_scaling(table_a.scaling)
        fmt_a = _printf_to_python_format(scaling_a.format) if scaling_a else ".2f"
        scaling_b = defn_b.get_scaling(table_b.scaling)
        fmt_b = _printf_to_python_format(scaling_b.format) if scaling_b else ".2f"

        vals_a = data_a["values"].flatten()
        vals_b = data_b["values"].flatten()

        min_len = min(len(vals_a), len(vals_b))
        diffs = []
        for i in range(min_len):
            if vals_a[i] != vals_b[i]:
                delta = vals_b[i] - vals_a[i]
                diffs.append(
                    {
                        "index": i,
                        "value_a": _format_value(vals_a[i], fmt_a),
                        "value_b": _format_value(vals_b[i], fmt_b),
                        "delta": _format_value(delta, fmt_a),
                    }
                )

        result: Dict[str, Any] = {
            "table_name": table_name,
            "type": table_a.type.value,
            "total_cells": min_len,
            "changed_cells": len(diffs),
            "diffs": diffs,
        }

        # Include axis context for 3D tables
        if table_a.type == TableType.THREE_D:
            x_axis = table_a.x_axis
            y_axis = table_a.y_axis
            if x_axis and y_axis:
                result["dimensions"] = {
                    "cols": x_axis.elements,
                    "rows": y_axis.elements,
                }

        return result

    # ------------------------------------------------------------------
    # Tool 5: get_table_statistics
    # ------------------------------------------------------------------

    def get_table_statistics(self, rom_path: str, table_name: str) -> Dict[str, Any]:
        """Statistical analysis of a table's values."""
        entry = self._get_entry(rom_path)
        defn = entry.definition
        reader = entry.reader

        table = defn.get_table_by_name(table_name)
        if table is None:
            raise RomEditorError(f"Table not found: {table_name}")

        data = reader.read_table_data(table)
        if data is None:
            raise RomEditorError(f"Failed to read table data: {table_name}")

        scaling = defn.get_scaling(table.scaling)
        values = data["values"].flatten().astype(float)

        # Filter NaN values for statistics
        valid = values[~np.isnan(values)] if np.any(np.isnan(values)) else values

        stats: Dict[str, Any] = {
            "table_name": table_name,
            "type": table.type.value,
            "total_cells": len(values),
        }

        if scaling:
            stats["units"] = scaling.units
            stats["scaling_min"] = scaling.min
            stats["scaling_max"] = scaling.max

        if len(valid) > 0:
            stats["min"] = float(np.min(valid))
            stats["max"] = float(np.max(valid))
            stats["mean"] = round(float(np.mean(valid)), 4)
            stats["median"] = round(float(np.median(valid)), 4)
            stats["std_dev"] = round(float(np.std(valid)), 4)
            stats["percentiles"] = {
                "p25": round(float(np.percentile(valid, 25)), 4),
                "p75": round(float(np.percentile(valid, 75)), 4),
                "p90": round(float(np.percentile(valid, 90)), 4),
                "p95": round(float(np.percentile(valid, 95)), 4),
            }

        # Axis ranges
        if "x_axis" in data:
            x_vals = data["x_axis"].flatten().astype(float)
            stats["x_axis_range"] = {
                "min": float(np.min(x_vals)),
                "max": float(np.max(x_vals)),
                "count": len(x_vals),
            }
        if "y_axis" in data:
            y_vals = data["y_axis"].flatten().astype(float)
            stats["y_axis_range"] = {
                "min": float(np.min(y_vals)),
                "max": float(np.max(y_vals)),
                "count": len(y_vals),
            }

        return stats

    # ------------------------------------------------------------------
    # Live app bridge — these POST to the app's command API
    # ------------------------------------------------------------------

    def _post_to_app(self, payload: dict) -> dict:
        """POST JSON to the app's command API server.

        Reads the command_api_url from workspace.json. Returns the
        parsed JSON response, or an error dict on failure.
        """
        workspace = self.get_workspace()
        url = workspace.get("command_api_url")
        if not url:
            return {
                "success": False,
                "error": "App command API not available. Start MCP server from the app.",
            }

        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url + payload["endpoint"],
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.URLError as e:
            return {
                "success": False,
                "error": f"Cannot connect to app. Is the app running? ({e})",
            }
        except TimeoutError:
            return {"success": False, "error": "Request timed out"}
        except json.JSONDecodeError:
            return {"success": False, "error": "Invalid response from app"}

    def list_modified_tables(self, rom_path: str) -> dict:
        """List tables with unsaved modifications in the running app."""
        return self._post_to_app(
            {
                "endpoint": "/api/modified",
                "rom_path": rom_path,
            }
        )

    def read_live_table(self, rom_path: str, table_name: str) -> dict:
        """Read a table's current in-memory values from the running app."""
        return self._post_to_app(
            {
                "endpoint": "/api/read-table",
                "rom_path": rom_path,
                "table_name": table_name,
            }
        )

    def write_table(self, rom_path: str, table_name: str, cells: list) -> dict:
        """Write values to a ROM table through the app's editing pipeline."""
        return self._post_to_app(
            {
                "endpoint": "/api/edit-table",
                "rom_path": rom_path,
                "table_name": table_name,
                "cells": cells,
            }
        )
