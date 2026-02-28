"""
Tests for MCP server ROM context.

Uses the sample ROM at examples/lf9veb.bin.
"""

import json
import pytest
from pathlib import Path

from src.mcp.rom_context import RomContext, _printf_to_python_format


@pytest.fixture
def ctx(definitions_dir):
    """Create a RomContext with the project's definitions directory."""
    return RomContext(definitions_dir=str(definitions_dir))


@pytest.fixture
def rom_path(sample_rom_path):
    """Return ROM path as string (matching MCP tool interface)."""
    return str(sample_rom_path)


# ------------------------------------------------------------------
# _printf_to_python_format
# ------------------------------------------------------------------


class TestPrintfConversion:
    def test_float_format(self):
        assert _printf_to_python_format("%0.2f") == ".2f"

    def test_integer_format(self):
        assert _printf_to_python_format("%d") == "d"

    def test_width_and_precision(self):
        assert _printf_to_python_format("%8.3f") == "8.3f"

    def test_empty_returns_default(self):
        assert _printf_to_python_format("") == ".2f"

    def test_none_returns_default(self):
        assert _printf_to_python_format(None) == ".2f"

    def test_unrecognized_returns_default(self):
        assert _printf_to_python_format("not-a-format") == ".2f"


# ------------------------------------------------------------------
# get_rom_info
# ------------------------------------------------------------------


class TestGetRomInfo:
    def test_returns_identification(self, ctx, rom_path):
        info = ctx.get_rom_info(rom_path)
        assert "identification" in info
        ident = info["identification"]
        assert ident["xmlid"]
        assert ident["make"]
        assert ident["model"]

    def test_returns_table_count(self, ctx, rom_path):
        info = ctx.get_rom_info(rom_path)
        assert info["table_count"] > 0

    def test_returns_category_summary(self, ctx, rom_path):
        info = ctx.get_rom_info(rom_path)
        assert "category_summary" in info
        summary = info["category_summary"]
        assert len(summary) > 0
        # All values should be positive integers
        for count in summary.values():
            assert count > 0

    def test_returns_file_size(self, ctx, rom_path):
        info = ctx.get_rom_info(rom_path)
        assert info["file_size_bytes"] > 0

    def test_invalid_rom_raises(self, ctx, tmp_path):
        fake_rom = tmp_path / "fake.bin"
        fake_rom.write_bytes(b"\x00" * 100)
        with pytest.raises(Exception):
            ctx.get_rom_info(str(fake_rom))


# ------------------------------------------------------------------
# list_tables
# ------------------------------------------------------------------


class TestListTables:
    def test_returns_all_tables(self, ctx, rom_path):
        tables = ctx.list_tables(rom_path)
        assert len(tables) > 0

    def test_table_has_required_fields(self, ctx, rom_path):
        tables = ctx.list_tables(rom_path)
        first = tables[0]
        assert "name" in first
        assert "category" in first
        assert "type" in first
        assert "address" in first
        assert "level" in first

    def test_filter_by_category(self, ctx, rom_path):
        all_tables = ctx.list_tables(rom_path)
        # Pick a category from the first table
        category = all_tables[0]["category"]
        filtered = ctx.list_tables(rom_path, category=category)
        assert len(filtered) > 0
        assert all(t["category"] == category for t in filtered)

    def test_filter_by_search(self, ctx, rom_path):
        # Search for a common substring
        filtered = ctx.list_tables(rom_path, search="ign")
        # All results should contain the search term
        for t in filtered:
            assert "ign" in t["name"].lower()

    def test_filter_by_level(self, ctx, rom_path):
        filtered = ctx.list_tables(rom_path, level=1)
        assert all(t["level"] == 1 for t in filtered)

    def test_3d_table_has_axes(self, ctx, rom_path):
        tables = ctx.list_tables(rom_path)
        tables_3d = [t for t in tables if t["type"] == "3D"]
        if tables_3d:
            t = tables_3d[0]
            assert "x_axis" in t
            assert "y_axis" in t
            assert "name" in t["x_axis"]
            assert "units" in t["x_axis"]

    def test_empty_category_returns_empty(self, ctx, rom_path):
        result = ctx.list_tables(rom_path, category="Nonexistent Category XYZ")
        assert result == []


# ------------------------------------------------------------------
# read_table
# ------------------------------------------------------------------


class TestReadTable:
    def _get_table_by_type(self, ctx, rom_path, table_type):
        """Helper: find a table of the given type."""
        tables = ctx.list_tables(rom_path)
        for t in tables:
            if t["type"] == table_type:
                return t["name"]
        return None

    def test_read_1d_table(self, ctx, rom_path):
        name = self._get_table_by_type(ctx, rom_path, "1D")
        if name is None:
            pytest.skip("No 1D table found")
        result = ctx.read_table(rom_path, name)
        assert result["metadata"]["type"] == "1D"
        assert isinstance(result["values"], list)
        assert len(result["values"]) > 0
        # Values should be formatted strings
        assert isinstance(result["values"][0], str)

    def test_read_2d_table(self, ctx, rom_path):
        name = self._get_table_by_type(ctx, rom_path, "2D")
        if name is None:
            pytest.skip("No 2D table found")
        result = ctx.read_table(rom_path, name)
        assert result["metadata"]["type"] == "2D"
        assert isinstance(result["values"], list)
        if "y_axis" in result:
            assert "name" in result["y_axis"]
            assert "values" in result["y_axis"]

    def test_read_3d_table(self, ctx, rom_path):
        name = self._get_table_by_type(ctx, rom_path, "3D")
        if name is None:
            pytest.skip("No 3D table found")
        result = ctx.read_table(rom_path, name)
        assert result["metadata"]["type"] == "3D"
        # 3D values should be a grid (list of lists)
        assert isinstance(result["values"], list)
        assert isinstance(result["values"][0], list)
        # Should have axis info
        assert "x_axis" in result
        assert "y_axis" in result
        assert "name" in result["x_axis"]
        assert "units" in result["x_axis"]
        assert "values" in result["x_axis"]
        assert "scaling_expression" in result["x_axis"]

    def test_read_table_metadata(self, ctx, rom_path):
        tables = ctx.list_tables(rom_path)
        name = tables[0]["name"]
        result = ctx.read_table(rom_path, name)
        meta = result["metadata"]
        assert "name" in meta
        assert "type" in meta
        assert "address" in meta

    def test_read_nonexistent_table_raises(self, ctx, rom_path):
        with pytest.raises(Exception, match="Table not found"):
            ctx.read_table(rom_path, "Nonexistent Table XYZ")


# ------------------------------------------------------------------
# compare_tables
# ------------------------------------------------------------------


class TestCompareTables:
    def test_compare_same_rom_no_diffs(self, ctx, rom_path):
        """Comparing a ROM to itself should show zero changes."""
        result = ctx.compare_tables(rom_path, rom_path)
        assert result["summary"]["changed_table_count"] == 0
        assert result["changed_tables"] == []

    def test_compare_summary_structure(self, ctx, rom_path):
        result = ctx.compare_tables(rom_path, rom_path)
        assert "summary" in result
        assert "total_common_tables" in result["summary"]
        assert "changed_table_count" in result["summary"]
        assert "a_only_count" in result["summary"]
        assert "b_only_count" in result["summary"]

    def test_compare_single_table_same_rom(self, ctx, rom_path):
        tables = ctx.list_tables(rom_path)
        name = tables[0]["name"]
        result = ctx.compare_tables(rom_path, rom_path, table_name=name)
        assert result["changed_cells"] == 0
        assert result["diffs"] == []

    def test_compare_nonexistent_table_raises(self, ctx, rom_path):
        with pytest.raises(Exception):
            ctx.compare_tables(
                rom_path, rom_path, table_name="Nonexistent XYZ"
            )


# ------------------------------------------------------------------
# get_table_statistics
# ------------------------------------------------------------------


class TestGetTableStatistics:
    def test_returns_statistics(self, ctx, rom_path):
        tables = ctx.list_tables(rom_path)
        name = tables[0]["name"]
        stats = ctx.get_table_statistics(rom_path, name)
        assert "min" in stats
        assert "max" in stats
        assert "mean" in stats
        assert "median" in stats
        assert "std_dev" in stats
        assert "percentiles" in stats

    def test_percentiles_structure(self, ctx, rom_path):
        tables = ctx.list_tables(rom_path)
        name = tables[0]["name"]
        stats = ctx.get_table_statistics(rom_path, name)
        p = stats["percentiles"]
        assert "p25" in p
        assert "p75" in p
        assert "p90" in p
        assert "p95" in p

    def test_3d_table_has_axis_ranges(self, ctx, rom_path):
        tables = ctx.list_tables(rom_path)
        tables_3d = [t for t in tables if t["type"] == "3D"]
        if not tables_3d:
            pytest.skip("No 3D table found")
        stats = ctx.get_table_statistics(rom_path, tables_3d[0]["name"])
        assert "x_axis_range" in stats
        assert "y_axis_range" in stats

    def test_nonexistent_table_raises(self, ctx, rom_path):
        with pytest.raises(Exception, match="Table not found"):
            ctx.get_table_statistics(rom_path, "Nonexistent Table XYZ")

    def test_statistics_are_consistent(self, ctx, rom_path):
        """min <= p25 <= median <= p75 <= max"""
        tables = ctx.list_tables(rom_path)
        name = tables[0]["name"]
        stats = ctx.get_table_statistics(rom_path, name)
        assert stats["min"] <= stats["percentiles"]["p25"]
        assert stats["percentiles"]["p25"] <= stats["median"]
        assert stats["median"] <= stats["percentiles"]["p75"]
        assert stats["percentiles"]["p75"] <= stats["max"]


# ------------------------------------------------------------------
# Caching
# ------------------------------------------------------------------


class TestCaching:
    def test_same_rom_reuses_cache(self, ctx, rom_path):
        ctx.get_rom_info(rom_path)
        assert len(ctx._cache) == 1
        ctx.get_rom_info(rom_path)
        assert len(ctx._cache) == 1

    def test_cache_eviction(self, ctx, rom_path):
        # Fill cache with the same ROM (only 1 unique key)
        ctx.get_rom_info(rom_path)
        assert len(ctx._cache) <= RomContext.MAX_CACHE_SIZE


# ------------------------------------------------------------------
# get_workspace
# ------------------------------------------------------------------


class TestGetWorkspace:
    def test_no_file_returns_empty(self, ctx, monkeypatch):
        """When workspace.json does not exist, return empty open_roms with message."""
        import src.mcp.rom_context as rc_mod
        monkeypatch.setattr(rc_mod, "get_app_root", lambda: Path("/nonexistent/path"))
        result = ctx.get_workspace()
        assert result["open_roms"] == []
        assert "message" in result

    def test_reads_valid_workspace(self, ctx, tmp_path, monkeypatch):
        """When workspace.json exists with valid data, return its contents."""
        import src.mcp.rom_context as rc_mod
        workspace_data = {
            "updated_at": "2026-02-28T14:30:00",
            "active_rom": "C:/test/rom.bin",
            "open_roms": [
                {
                    "rom_path": "C:/test/rom.bin",
                    "file_name": "rom.bin",
                    "xmlid": "lf9veb",
                    "make": "Mazda",
                    "model": "RX-8",
                    "year": "2004",
                    "is_modified": False,
                }
            ],
        }
        workspace_file = tmp_path / "workspace.json"
        workspace_file.write_text(json.dumps(workspace_data), encoding="utf-8")
        monkeypatch.setattr(rc_mod, "get_app_root", lambda: tmp_path)

        result = ctx.get_workspace()
        assert result["active_rom"] == "C:/test/rom.bin"
        assert len(result["open_roms"]) == 1
        assert result["open_roms"][0]["xmlid"] == "lf9veb"

    def test_corrupt_json_returns_empty(self, ctx, tmp_path, monkeypatch):
        """When workspace.json contains invalid JSON, return empty gracefully."""
        import src.mcp.rom_context as rc_mod
        workspace_file = tmp_path / "workspace.json"
        workspace_file.write_text("not valid json {{{", encoding="utf-8")
        monkeypatch.setattr(rc_mod, "get_app_root", lambda: tmp_path)

        result = ctx.get_workspace()
        assert result["open_roms"] == []
        assert "message" in result
