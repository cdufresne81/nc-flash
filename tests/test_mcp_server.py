"""
Tests for MCP server ROM context.

All tools delegate to the NC Flash app's command API.  Tests verify
correct payload construction, response handling, and error propagation.
"""

import json
import pytest
from pathlib import Path

from src.mcp.rom_context import RomContext
from src.core.exceptions import RomEditorError
from src.utils.formatting import printf_to_python_format


@pytest.fixture
def ctx():
    """Create a RomContext (no metadata_dir needed — delegates to app)."""
    return RomContext()


# ------------------------------------------------------------------
# printf_to_python_format (shared utility)
# ------------------------------------------------------------------


class TestPrintfConversion:
    def test_float_format(self):
        assert printf_to_python_format("%0.2f") == ".2f"

    def test_integer_format(self):
        assert printf_to_python_format("%d") == "d"

    def test_width_and_precision(self):
        assert printf_to_python_format("%8.3f") == "8.3f"

    def test_empty_returns_default(self):
        assert printf_to_python_format("") == ".2f"

    def test_none_returns_default(self):
        assert printf_to_python_format(None) == ".2f"

    def test_unrecognized_returns_default(self):
        assert printf_to_python_format("not-a-format") == ".2f"


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


# ------------------------------------------------------------------
# get_rom_info — delegates to /api/rom-info
# ------------------------------------------------------------------


class TestGetRomInfo:
    def test_delegates_to_app(self, ctx, monkeypatch):
        captured = []
        mock_resp = {
            "success": True,
            "rom_path": "C:/test/rom.bin",
            "file_size_bytes": 1048576,
            "identification": {
                "xmlid": "LF4XEG",
                "ecuid": "test",
                "internalidstring": "LF4XEG",
                "make": "Mazda",
                "model": "MX5",
                "year": None,
                "market": None,
                "submodel": None,
                "transmission": None,
            },
            "table_count": 42,
            "category_summary": {"Engine": 10, "Fuel": 15},
        }
        monkeypatch.setattr(
            ctx,
            "_post_to_app",
            lambda p: (captured.append(p), mock_resp)[1],
        )

        result = ctx.get_rom_info("C:/test/rom.bin")

        assert captured[0]["endpoint"] == "/api/rom-info"
        assert captured[0]["rom_path"] == "C:/test/rom.bin"
        assert result["identification"]["xmlid"] == "LF4XEG"
        assert result["table_count"] == 42
        assert "success" not in result

    def test_raises_on_app_error(self, ctx, monkeypatch):
        monkeypatch.setattr(
            ctx,
            "_post_to_app",
            lambda p: {"success": False, "error": "ROM not open in app"},
        )
        with pytest.raises(RomEditorError, match="ROM not open"):
            ctx.get_rom_info("C:/test/rom.bin")


# ------------------------------------------------------------------
# list_tables — delegates to /api/list-tables
# ------------------------------------------------------------------


class TestListTables:
    def test_delegates_to_app(self, ctx, monkeypatch):
        captured = []
        mock_resp = {
            "success": True,
            "tables": [
                {
                    "name": "Fuel VE",
                    "category": "Fuel",
                    "type": "3D",
                    "address": "f0000",
                    "elements": 400,
                    "level": 1,
                    "units": "g/rev",
                    "dimensions": "20x20",
                    "x_axis": {"name": "RPM", "units": "rpm"},
                    "y_axis": {"name": "Load", "units": "g/rev"},
                }
            ],
        }
        monkeypatch.setattr(
            ctx,
            "_post_to_app",
            lambda p: (captured.append(p), mock_resp)[1],
        )

        result = ctx.list_tables("C:/test/rom.bin", search="fuel", level=1)

        assert captured[0]["endpoint"] == "/api/list-tables"
        assert captured[0]["rom_path"] == "C:/test/rom.bin"
        assert captured[0]["search"] == "fuel"
        assert captured[0]["level"] == 1
        assert "category" not in captured[0]
        assert len(result) == 1
        assert result[0]["name"] == "Fuel VE"

    def test_omits_none_filters(self, ctx, monkeypatch):
        captured = []
        monkeypatch.setattr(
            ctx,
            "_post_to_app",
            lambda p: (captured.append(p), {"success": True, "tables": []})[1],
        )
        ctx.list_tables("C:/test/rom.bin")
        assert "category" not in captured[0]
        assert "search" not in captured[0]
        assert "level" not in captured[0]

    def test_raises_on_app_error(self, ctx, monkeypatch):
        monkeypatch.setattr(
            ctx,
            "_post_to_app",
            lambda p: {"success": False, "error": "ROM not open in app"},
        )
        with pytest.raises(RomEditorError):
            ctx.list_tables("C:/test/rom.bin")


# ------------------------------------------------------------------
# read_table — delegates to /api/read-table
# ------------------------------------------------------------------


class TestReadTable:
    def test_delegates_to_app(self, ctx, monkeypatch):
        captured = []
        mock_resp = {
            "success": True,
            "metadata": {
                "name": "Fuel VE",
                "type": "3D",
                "address": "f0000",
                "elements": 4,
                "dimensions": "2x2",
            },
            "values": [["1.00", "2.00"], ["3.00", "4.00"]],
            "x_axis": {"name": "RPM", "units": "rpm", "values": ["1000", "2000"]},
            "y_axis": {"name": "Load", "units": "g/rev", "values": ["0.5", "1.0"]},
        }
        monkeypatch.setattr(
            ctx,
            "_post_to_app",
            lambda p: (captured.append(p), mock_resp)[1],
        )

        result = ctx.read_table("C:/test/rom.bin", "Fuel VE")

        assert captured[0]["endpoint"] == "/api/read-table"
        assert captured[0]["table_name"] == "Fuel VE"
        assert result["metadata"]["type"] == "3D"
        assert result["values"] == [["1.00", "2.00"], ["3.00", "4.00"]]
        assert "success" not in result

    def test_raises_on_table_not_found(self, ctx, monkeypatch):
        monkeypatch.setattr(
            ctx,
            "_post_to_app",
            lambda p: {"success": False, "error": "Table not found: Nope"},
        )
        with pytest.raises(RomEditorError, match="Table not found"):
            ctx.read_table("C:/test/rom.bin", "Nope")


# ------------------------------------------------------------------
# compare_tables — delegates to /api/compare-tables
# ------------------------------------------------------------------


class TestCompareTables:
    def test_delegates_summary(self, ctx, monkeypatch):
        captured = []
        mock_resp = {
            "success": True,
            "rom_a": "a.bin",
            "rom_b": "b.bin",
            "summary": {
                "total_common_tables": 50,
                "changed_table_count": 3,
                "a_only_count": 0,
                "b_only_count": 0,
            },
            "changed_tables": [],
            "a_only_tables": [],
            "b_only_tables": [],
        }
        monkeypatch.setattr(
            ctx,
            "_post_to_app",
            lambda p: (captured.append(p), mock_resp)[1],
        )

        result = ctx.compare_tables("C:/a.bin", "C:/b.bin")

        assert captured[0]["endpoint"] == "/api/compare-tables"
        assert captured[0]["rom_path_a"] == "C:/a.bin"
        assert captured[0]["rom_path_b"] == "C:/b.bin"
        assert "table_name" not in captured[0]
        assert result["summary"]["changed_table_count"] == 3

    def test_delegates_single_table(self, ctx, monkeypatch):
        captured = []
        mock_resp = {
            "success": True,
            "table_name": "Fuel VE",
            "type": "3D",
            "total_cells": 400,
            "changed_cells": 5,
            "diffs": [],
        }
        monkeypatch.setattr(
            ctx,
            "_post_to_app",
            lambda p: (captured.append(p), mock_resp)[1],
        )

        result = ctx.compare_tables("C:/a.bin", "C:/b.bin", table_name="Fuel VE")

        assert captured[0]["table_name"] == "Fuel VE"
        assert result["changed_cells"] == 5

    def test_raises_on_app_error(self, ctx, monkeypatch):
        monkeypatch.setattr(
            ctx,
            "_post_to_app",
            lambda p: {"success": False, "error": "ROM not open in app"},
        )
        with pytest.raises(RomEditorError):
            ctx.compare_tables("C:/a.bin", "C:/b.bin")


# ------------------------------------------------------------------
# get_table_statistics — delegates to /api/table-statistics
# ------------------------------------------------------------------


class TestGetTableStatistics:
    def test_delegates_to_app(self, ctx, monkeypatch):
        captured = []
        mock_resp = {
            "success": True,
            "table_name": "Fuel VE",
            "type": "3D",
            "total_cells": 400,
            "units": "g/rev",
            "min": 0.5,
            "max": 2.0,
            "mean": 1.25,
            "median": 1.20,
            "std_dev": 0.3,
            "percentiles": {"p25": 0.9, "p75": 1.5, "p90": 1.8, "p95": 1.9},
        }
        monkeypatch.setattr(
            ctx,
            "_post_to_app",
            lambda p: (captured.append(p), mock_resp)[1],
        )

        result = ctx.get_table_statistics("C:/test/rom.bin", "Fuel VE")

        assert captured[0]["endpoint"] == "/api/table-statistics"
        assert captured[0]["table_name"] == "Fuel VE"
        assert result["min"] == 0.5
        assert result["max"] == 2.0
        assert result["percentiles"]["p75"] == 1.5
        assert "success" not in result

    def test_raises_on_app_error(self, ctx, monkeypatch):
        monkeypatch.setattr(
            ctx,
            "_post_to_app",
            lambda p: {"success": False, "error": "Table not found: Nope"},
        )
        with pytest.raises(RomEditorError, match="Table not found"):
            ctx.get_table_statistics("C:/test/rom.bin", "Nope")


# ------------------------------------------------------------------
# Live bridge — list_modified_tables, read_live_table, write_table
# ------------------------------------------------------------------


class TestRomContextLiveBridge:
    def test_post_to_app_no_workspace(self, ctx, tmp_path, monkeypatch):
        """When workspace.json has no command_api_url, returns error."""
        monkeypatch.setattr("src.mcp.rom_context.get_app_root", lambda: tmp_path)
        result = ctx._post_to_app({"endpoint": "/api/modified", "rom_path": "x"})
        assert result["success"] is False
        assert "not available" in result["error"]

    def test_post_to_app_connection_refused(self, ctx, tmp_path, monkeypatch):
        """When the app is not running, returns connection error."""
        monkeypatch.setattr("src.mcp.rom_context.get_app_root", lambda: tmp_path)
        workspace = {"command_api_url": "http://127.0.0.1:19999"}
        (tmp_path / "workspace.json").write_text(json.dumps(workspace))

        result = ctx._post_to_app({"endpoint": "/api/modified", "rom_path": "x"})
        assert result["success"] is False
        assert "Cannot connect" in result["error"]

    def test_list_modified_tables_delegates(self, ctx, monkeypatch):
        captured = []
        monkeypatch.setattr(
            ctx, "_post_to_app", lambda p: (captured.append(p), {"success": True})[1]
        )
        ctx.list_modified_tables("/path/to/rom.bin")
        assert len(captured) == 1
        assert captured[0]["endpoint"] == "/api/modified"
        assert captured[0]["rom_path"] == "/path/to/rom.bin"

    def test_read_live_table_delegates(self, ctx, monkeypatch):
        captured = []
        monkeypatch.setattr(
            ctx, "_post_to_app", lambda p: (captured.append(p), {"success": True})[1]
        )
        ctx.read_live_table("/path/to/rom.bin", "Fuel VE")
        assert len(captured) == 1
        assert captured[0]["endpoint"] == "/api/read-table"
        assert captured[0]["table_name"] == "Fuel VE"

    def test_write_table_delegates(self, ctx, monkeypatch):
        captured = []
        monkeypatch.setattr(
            ctx, "_post_to_app", lambda p: (captured.append(p), {"success": True})[1]
        )
        cells = [{"row": 0, "col": 0, "value": 42.5}]
        ctx.write_table("/path/to/rom.bin", "Fuel VE", cells)
        assert len(captured) == 1
        assert captured[0]["endpoint"] == "/api/edit-table"
        assert captured[0]["cells"] == cells
