"""
ROM Context — MCP tool implementations that delegate to the NC Flash app.

All tools communicate with the running NC Flash application via its HTTP
command API.  The app is the single source of truth for ROM definitions,
table metadata, and data values.
"""

import json
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.exceptions import RomEditorError
from ..utils.paths import get_app_root


class RomContext:
    """Delegates all MCP tool operations to the running NC Flash app."""

    def __init__(self, metadata_dir: Optional[str] = None):
        # metadata_dir accepted for CLI compatibility but no longer used —
        # the app is the single source of truth for definitions.
        pass

    # ------------------------------------------------------------------
    # App bridge
    # ------------------------------------------------------------------

    def _post_to_app(self, payload: dict) -> dict:
        """POST JSON to the app's command API server.

        Reads the command_api_url from workspace.json.  Returns the
        parsed JSON response, or an error dict on failure.
        """
        workspace = self.get_workspace()
        url = workspace.get("command_api_url")
        if not url:
            return {
                "success": False,
                "error": (
                    "App command API not available. "
                    "Is NC Flash running with MCP enabled?"
                ),
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
                "error": f"Cannot connect to app. Is NC Flash running? ({e})",
            }
        except TimeoutError:
            return {"success": False, "error": "Request timed out"}
        except json.JSONDecodeError:
            return {"success": False, "error": "Invalid response from app"}

    def _require_success(self, result: dict) -> dict:
        """Strip the success flag and raise RomEditorError on failure."""
        if not result.get("success", False):
            raise RomEditorError(result.get("error", "Unknown error from app"))
        result.pop("success", None)
        return result

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
        result = self._post_to_app({"endpoint": "/api/rom-info", "rom_path": rom_path})
        return self._require_success(result)

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
        payload: Dict[str, Any] = {
            "endpoint": "/api/list-tables",
            "rom_path": rom_path,
        }
        if category is not None:
            payload["category"] = category
        if search is not None:
            payload["search"] = search
        if level is not None:
            payload["level"] = level
        result = self._post_to_app(payload)
        self._require_success(result)
        return result.get("tables", [])

    # ------------------------------------------------------------------
    # Tool 3: read_table
    # ------------------------------------------------------------------

    def read_table(self, rom_path: str, table_name: str) -> Dict[str, Any]:
        """Read a table's scaled display values with full axis context."""
        result = self._post_to_app(
            {
                "endpoint": "/api/read-table",
                "rom_path": rom_path,
                "table_name": table_name,
            }
        )
        return self._require_success(result)

    # ------------------------------------------------------------------
    # Tool 4: compare_tables
    # ------------------------------------------------------------------

    def compare_tables(
        self,
        rom_path_a: str,
        rom_path_b: str,
        table_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Compare tables between two ROMs (both must be open in the app)."""
        payload: Dict[str, Any] = {
            "endpoint": "/api/compare-tables",
            "rom_path_a": rom_path_a,
            "rom_path_b": rom_path_b,
        }
        if table_name is not None:
            payload["table_name"] = table_name
        result = self._post_to_app(payload)
        return self._require_success(result)

    # ------------------------------------------------------------------
    # Tool 5: get_table_statistics
    # ------------------------------------------------------------------

    def get_table_statistics(self, rom_path: str, table_name: str) -> Dict[str, Any]:
        """Statistical analysis of a table's values."""
        result = self._post_to_app(
            {
                "endpoint": "/api/table-statistics",
                "rom_path": rom_path,
                "table_name": table_name,
            }
        )
        return self._require_success(result)

    # ------------------------------------------------------------------
    # Live app bridge (unchanged — these already delegated to the app)
    # ------------------------------------------------------------------

    def list_modified_tables(self, rom_path: str) -> dict:
        """List tables with unsaved modifications in the running app."""
        return self._post_to_app(
            {
                "endpoint": "/api/modified",
                "rom_path": rom_path,
            }
        )

    def read_live_table(self, rom_path: str, table_name: str) -> dict:
        """Read a table's current in-memory values from the running app.

        Note: now equivalent to read_table — both read from the app.
        """
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
