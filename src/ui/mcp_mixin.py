"""
MCP Mixin for MainWindow

Handles MCP server lifecycle (start/stop/toggle), the command API HTTP bridge,
API request handling (list modified, read table, edit table), and the MCP
connection info dialog.

This is a mixin class — it has no __init__ and relies on MainWindow providing:
- self._mcp_process (subprocess.Popen or None)
- self._command_server (CommandServer or None)
- self.mcp_action (QAction, checkable)
- self._toolbar_mcp (QAction for toolbar icon)
- self.settings (AppSettings instance)
- self.change_tracker (ChangeTracker instance)
- self.table_undo_manager (TableUndoManager instance)
- self.original_table_values (dict)
- self.modified_cells (dict)
- self._find_document_by_rom_path(path) method
- self._find_table_window(key) method
- self._write_to_rom_and_mark_modified(doc, fn, desc) method
- self._update_tab_title(doc) method
- self._write_workspace_state() method
- self.statusBar() method
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from src.ui.icons import make_icon
from src.utils.formatting import printf_to_python_format, format_value
from src.utils.logging_config import get_logger
from src.utils.paths import get_app_root

logger = get_logger(__name__)


class McpMixin:
    """MCP server and command API management for MainWindow."""

    MCP_SSE_PORT = 8765

    # ========== MCP Server Management ==========

    def _is_mcp_running(self) -> bool:
        """Check if the MCP server subprocess is alive."""
        return self._mcp_process is not None and self._mcp_process.poll() is None

    def _start_mcp_server(self):
        """Start the MCP server subprocess with SSE transport."""
        if self._is_mcp_running():
            return
        try:
            self._start_command_server()

            metadata_dir = self.settings.get_metadata_directory()
            mcp_args = [
                "--transport",
                "sse",
                "--port",
                str(self.MCP_SSE_PORT),
                "--metadata-dir",
                metadata_dir,
            ]
            env = os.environ.copy()
            kwargs = dict(cwd=str(get_app_root()), stderr=subprocess.PIPE)

            if getattr(sys, "frozen", False):
                # Frozen (PyInstaller) build: sys.executable is the app exe.
                # Set NCFLASH_MCP_MODE so the exe skips the GUI and runs
                # the MCP server directly.  Pass MCP args via sys.argv.
                env["NCFLASH_MCP_MODE"] = "1"
                cmd = [sys.executable] + mcp_args
                kwargs["env"] = env

                # On Windows, prevent the subprocess from creating a visible
                # window (the exe is a GUI app with console=False).
                if sys.platform == "win32":
                    si = subprocess.STARTUPINFO()
                    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    si.wShowWindow = 0  # SW_HIDE
                    kwargs["startupinfo"] = si
                    # CREATE_NO_WINDOW prevents a console flash as well.
                    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            else:
                # Dev mode: run as a normal Python module.
                cmd = [sys.executable, "-m", "src.mcp.server"] + mcp_args

            self._mcp_process = subprocess.Popen(cmd, **kwargs)
            logger.info(
                f"MCP server started (PID {self._mcp_process.pid},"
                f" SSE on http://127.0.0.1:{self.MCP_SSE_PORT}/sse)"
            )
            self._update_mcp_ui(running=True)
            self._write_workspace_state()
        except Exception as e:
            logger.error(f"Failed to start MCP server: {e}")
            self._mcp_process = None
            self._stop_command_server()
            self._update_mcp_ui(running=False)

    def _stop_mcp_server(self):
        """Stop the MCP server subprocess and command API server."""
        self._stop_command_server()
        if self._mcp_process is None:
            return
        try:
            self._mcp_process.terminate()
            self._mcp_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._mcp_process.kill()
        except Exception:
            logger.debug("Error terminating MCP process", exc_info=True)
        pid = self._mcp_process.pid if self._mcp_process else "?"
        self._mcp_process = None
        logger.info(f"MCP server stopped (PID {pid})")
        self._update_mcp_ui(running=False)
        self._write_workspace_state()

    def _toggle_mcp_server(self):
        """Toggle the MCP server on/off."""
        if self._is_mcp_running():
            self._stop_mcp_server()
        else:
            self._start_mcp_server()
            if self._is_mcp_running():
                self._show_mcp_connection_info()

    def _update_mcp_ui(self, running: bool):
        """Update menu, toolbar, and status bar to reflect MCP server state."""
        self.mcp_action.setChecked(running)
        url = f"http://127.0.0.1:{self.MCP_SSE_PORT}/sse"
        if running:
            self.mcp_action.setText(
                f"&MCP Server (Running on port {self.MCP_SSE_PORT})"
            )
            self._toolbar_mcp.setIcon(make_icon(self, "mcp_on"))
            self._toolbar_mcp.setToolTip(f"MCP Server running — {url}\nClick to stop")
            self.statusBar().showMessage(f"MCP server started on {url}", 5000)
        else:
            self.mcp_action.setText("&MCP Server")
            self._toolbar_mcp.setIcon(make_icon(self, "mcp_off"))
            self._toolbar_mcp.setToolTip("MCP Server (off) — click to start")

    def _show_mcp_connection_info(self):
        """Show connection instructions after manually starting the MCP server."""
        url = f"http://127.0.0.1:{self.MCP_SSE_PORT}/sse"

        if getattr(sys, "frozen", False):
            # Compiled build: run-mcp.bat sits next to NCFlash.exe
            bat_path = str(Path(sys.executable).parent / "run-mcp.bat")
        else:
            # Dev mode: run-mcp.bat is at the project root
            bat_path = str(get_app_root() / "run-mcp.bat")
        config_snippet = json.dumps(
            {"mcpServers": {"nc-flash": {"command": bat_path, "args": []}}},
            indent=2,
        )

        dlg = QDialog(self)
        dlg.setWindowTitle("MCP Server Running")
        dlg.setMinimumWidth(500)
        layout = QVBoxLayout(dlg)

        layout.addWidget(QLabel(f"MCP server is running at <b>{url}</b>"))
        layout.addWidget(QLabel(""))

        layout.addWidget(
            QLabel("<b>Claude Code</b> — already configured via .mcp.json")
        )
        layout.addWidget(QLabel(""))

        label = QLabel(
            "<b>Claude Desktop</b> — Go to Settings > Developer > Edit Config "
            "and merge the block below into your config file:"
        )
        label.setWordWrap(True)
        layout.addWidget(label)

        snippet_box = QTextEdit()
        snippet_box.setPlainText(config_snippet)
        snippet_box.setReadOnly(True)
        snippet_box.setFixedHeight(130)
        snippet_box.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 12px; background: #f5f5f5;"
        )
        layout.addWidget(snippet_box)

        btn_row = QHBoxLayout()
        copy_btn = QPushButton("Copy to Clipboard")
        copy_btn.clicked.connect(
            lambda: QApplication.clipboard().setText(config_snippet)
        )
        copy_btn.clicked.connect(lambda: copy_btn.setText("Copied!"))
        btn_row.addStretch()
        btn_row.addWidget(copy_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        note = QLabel(
            "If your config file already has a <code>mcpServers</code> section, "
            "just add the <code>nc-flash</code> entry inside it."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(note)

        layout.addWidget(QLabel(""))
        layout.addWidget(
            QLabel("The server will stay running until you stop it or close the app.")
        )

        buttons = QDialogButtonBox(QDialogButtonBox.Ok)
        buttons.accepted.connect(dlg.accept)
        layout.addWidget(buttons)

        dlg.exec()

    # ========== Command API Server (HTTP bridge for MCP) ==========

    def _start_command_server(self):
        """Start the command API HTTP server for MCP live reads/writes."""
        if self._command_server is not None and self._command_server.is_running:
            return
        from src.api.command_server import CommandServer

        self._command_server = CommandServer(self._handle_api_request, self)
        if not self._command_server.start():
            logger.warning(
                "Command API server failed to start — live MCP tools will be unavailable"
            )
            self._command_server = None

    def _stop_command_server(self):
        """Stop the command API HTTP server."""
        if self._command_server is not None:
            self._command_server.stop()
            self._command_server = None

    def _handle_api_request(self, request: dict) -> dict:
        """Central dispatcher for all command API requests.

        Called on the Qt main thread by CommandServer's queue poller.

        Args:
            request: Dict with "endpoint" plus endpoint-specific fields.

        Returns:
            Response dict with "success" and endpoint-specific data.
        """
        endpoint = request.get("endpoint", "")
        try:
            if endpoint == "/api/modified":
                return self._api_list_modified(request)
            elif endpoint == "/api/read-table":
                return self._api_read_table(request)
            elif endpoint == "/api/edit-table":
                return self._api_edit_table(request)
            elif endpoint == "/api/rom-info":
                return self._api_rom_info(request)
            elif endpoint == "/api/list-tables":
                return self._api_list_tables(request)
            elif endpoint == "/api/table-statistics":
                return self._api_table_statistics(request)
            elif endpoint == "/api/compare-tables":
                return self._api_compare_tables(request)
            else:
                return {"success": False, "error": f"Unknown endpoint: {endpoint}"}
        except Exception as e:
            logger.exception(f"API request error ({endpoint}): {e}")
            return {"success": False, "error": str(e)}

    def _api_list_modified(self, request: dict) -> dict:
        """Handle /api/modified — list tables with unsaved modifications."""
        rom_path = request.get("rom_path", "")
        document = self._find_document_by_rom_path(rom_path)
        if not document:
            return {"success": False, "error": f"ROM not open in app: {rom_path}"}

        from src.core.table_undo_manager import extract_rom_path

        rom_path_str = str(Path(rom_path))
        tables = []
        for key, pending in self.change_tracker._pending.items():
            if not pending.has_changes():
                continue
            key_rom = extract_rom_path(key)
            if key_rom == rom_path_str or key_rom == str(Path(rom_path).resolve()):
                tables.append(
                    {
                        "name": pending.table_name,
                        "changed_cells": len(pending.changes),
                    }
                )

        return {"success": True, "tables": tables}

    def _api_read_table(self, request: dict) -> dict:
        """Handle /api/read-table — read live in-memory values."""
        rom_path = request.get("rom_path", "")
        table_name = request.get("table_name", "").strip()

        document = self._find_document_by_rom_path(rom_path)
        if not document:
            return {"success": False, "error": f"ROM not open in app: {rom_path}"}

        table = document.rom_definition.get_table_by_name(table_name)
        if table is None:
            return {"success": False, "error": f"Table not found: {table_name}"}

        data = document.rom_reader.read_table_data(table)
        if data is None:
            return {
                "success": False,
                "error": f"Failed to read table data: {table_name}",
            }

        from src.core.rom_definition import TableType

        scaling = document.rom_definition.get_scaling(table.scaling)
        fmt_spec = printf_to_python_format(scaling.format) if scaling else ".2f"

        result = {
            "success": True,
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
            result["values"] = [format_value(v, fmt_spec) for v in values.flat]

        elif table.type == TableType.TWO_D:
            y_axis_table = table.y_axis
            result["metadata"]["dimensions"] = str(
                y_axis_table.elements if y_axis_table else table.elements
            )
            result["values"] = [format_value(v, fmt_spec) for v in values.flat]
            if "y_axis" in data and y_axis_table:
                result["y_axis"] = self._api_format_axis(
                    y_axis_table, data["y_axis"], document.rom_definition
                )

        elif table.type == TableType.THREE_D:
            x_axis_table = table.x_axis
            y_axis_table = table.y_axis
            cols = x_axis_table.elements if x_axis_table else 0
            rows = y_axis_table.elements if y_axis_table else 0
            result["metadata"]["dimensions"] = f"{cols}x{rows}"

            grid = []
            for row_idx in range(values.shape[0]):
                grid.append([format_value(v, fmt_spec) for v in values[row_idx]])
            result["values"] = grid

            if "x_axis" in data and x_axis_table:
                result["x_axis"] = self._api_format_axis(
                    x_axis_table, data["x_axis"], document.rom_definition
                )
            if "y_axis" in data and y_axis_table:
                result["y_axis"] = self._api_format_axis(
                    y_axis_table, data["y_axis"], document.rom_definition
                )

        return result

    def _api_format_axis(self, axis_table, axis_values, definition):
        """Format an axis for API response (mirrors RomContext._format_axis)."""
        axis_scaling = definition.get_scaling(axis_table.scaling)
        axis_fmt = (
            printf_to_python_format(axis_scaling.format) if axis_scaling else ".2f"
        )
        return {
            "name": axis_table.name,
            "units": axis_scaling.units if axis_scaling else "",
            "scaling_expression": axis_scaling.toexpr if axis_scaling else "",
            "values": [format_value(v, axis_fmt) for v in axis_values.flat],
        }

    def _api_edit_table(self, request: dict) -> dict:
        """Handle /api/edit-table — write values through the editing pipeline."""
        from src.core.rom_reader import ScalingConverter
        from src.core.table_undo_manager import make_table_key

        rom_path = request.get("rom_path", "")
        table_name = request.get("table_name", "").strip()
        cells = request.get("cells", [])

        if not cells:
            return {"success": False, "error": "No cells provided"}

        document = self._find_document_by_rom_path(rom_path)
        if not document:
            return {"success": False, "error": f"ROM not open in app: {rom_path}"}

        table = document.rom_definition.get_table_by_name(table_name)
        if table is None:
            return {"success": False, "error": f"Table not found: {table_name}"}

        old_data = document.rom_reader.read_table_data(table)
        if old_data is None:
            return {
                "success": False,
                "error": f"Failed to read table data: {table_name}",
            }

        old_vals = old_data["values"]
        scaling = document.rom_definition.get_scaling(table.scaling)
        converter = ScalingConverter(scaling) if scaling else None

        if table.type.value == "3D":
            x_axis = table.x_axis
            y_axis = table.y_axis
            max_cols = x_axis.elements if x_axis else 1
            max_rows = y_axis.elements if y_axis else 1
        elif table.type.value == "2D":
            max_rows = old_vals.shape[0] if old_vals.ndim >= 1 else 1
            max_cols = 1
        else:
            max_rows = old_vals.shape[0] if old_vals.ndim >= 1 else table.elements
            max_cols = 1

        changes = []
        errors = []
        for cell in cells:
            r = cell.get("row", 0)
            c = cell.get("col", 0)
            new_display_val = cell.get("value")

            if new_display_val is None:
                errors.append(f"Missing 'value' for cell ({r},{c})")
                continue

            if r < 0 or r >= max_rows or c < 0 or c >= max_cols:
                errors.append(
                    f"Cell ({r},{c}) out of range for {table_name} ({max_rows}x{max_cols})"
                )
                continue

            try:
                new_display_val = float(new_display_val)
            except (ValueError, TypeError) as e:
                errors.append(f"Failed to convert value {cell.get('value')}: {e}")
                continue

            if old_vals.ndim == 1:
                old_display_val = float(old_vals[r])
            else:
                old_display_val = float(old_vals[r, c])

            try:
                old_raw = (
                    float(converter.from_display(old_display_val))
                    if converter
                    else old_display_val
                )
                new_raw = (
                    float(converter.from_display(new_display_val))
                    if converter
                    else new_display_val
                )
            except Exception as e:
                errors.append(f"Failed to convert value {new_display_val}: {e}")
                continue

            changes.append((r, c, old_display_val, new_display_val, old_raw, new_raw))

        if errors and not changes:
            return {"success": False, "error": "; ".join(errors)}

        if not changes:
            return {
                "success": True,
                "cells_modified": 0,
                "message": "No changes needed",
            }

        # Capture originals and apply through shared pipeline
        rom_path_key = document.rom_reader.rom_path
        self._capture_table_originals(rom_path_key, table.address, old_data)

        desc = f"AI: edit {len(changes)} cell(s) in {table_name}"
        self._apply_external_cell_edits(
            document, table, changes, desc, rom_path=rom_path_key
        )

        # Activate undo stack so Ctrl+Z works immediately
        table_key = make_table_key(rom_path_key, table.address)
        self.table_undo_manager.set_active_stack(table_key)

        self._update_tab_title(document)
        self._write_workspace_state()

        result = {"success": True, "cells_modified": len(changes)}
        if errors:
            result["warnings"] = errors
        return result

    # ========== ROM metadata endpoints (single source of truth for MCP) ==========

    def _api_rom_info(self, request: dict) -> dict:
        """Handle /api/rom-info — return ROM identification and summary."""
        rom_path = request.get("rom_path", "")
        document = self._find_document_by_rom_path(rom_path)
        if not document:
            return {"success": False, "error": f"ROM not open in app: {rom_path}"}

        defn = document.rom_definition
        romid = defn.romid
        categories = defn.get_tables_by_category()
        category_summary = {
            cat: len(tables) for cat, tables in sorted(categories.items())
        }
        table_count = sum(1 for t in defn.tables if not t.is_axis)
        file_size = Path(rom_path).resolve().stat().st_size

        return {
            "success": True,
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

    def _api_list_tables(self, request: dict) -> dict:
        """Handle /api/list-tables — list tables with optional filtering."""
        from src.core.rom_definition import TableType

        rom_path = request.get("rom_path", "")
        category = request.get("category")
        search = request.get("search")
        level = request.get("level")

        document = self._find_document_by_rom_path(rom_path)
        if not document:
            return {"success": False, "error": f"ROM not open in app: {rom_path}"}

        defn = document.rom_definition

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
            info = self._api_table_summary(table, defn)
            result.append(info)

        return {"success": True, "tables": result}

    def _api_table_summary(self, table, defn):
        """Build a summary dict for a table (used by _api_list_tables)."""
        from src.core.rom_definition import TableType

        scaling = defn.get_scaling(table.scaling)
        info = {
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

    def _api_table_statistics(self, request: dict) -> dict:
        """Handle /api/table-statistics — statistical analysis of a table."""
        import numpy as np

        rom_path = request.get("rom_path", "")
        table_name = request.get("table_name", "").strip()

        document = self._find_document_by_rom_path(rom_path)
        if not document:
            return {"success": False, "error": f"ROM not open in app: {rom_path}"}

        table = document.rom_definition.get_table_by_name(table_name)
        if table is None:
            return {"success": False, "error": f"Table not found: {table_name}"}

        data = document.rom_reader.read_table_data(table)
        if data is None:
            return {
                "success": False,
                "error": f"Failed to read table data: {table_name}",
            }

        scaling = document.rom_definition.get_scaling(table.scaling)
        values = data["values"].flatten().astype(float)
        valid = values[~np.isnan(values)] if np.any(np.isnan(values)) else values

        stats = {
            "success": True,
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

    def _api_compare_tables(self, request: dict) -> dict:
        """Handle /api/compare-tables — compare tables between two open ROMs."""
        rom_path_a = request.get("rom_path_a", "")
        rom_path_b = request.get("rom_path_b", "")
        table_name = request.get("table_name")

        doc_a = self._find_document_by_rom_path(rom_path_a)
        if not doc_a:
            return {"success": False, "error": f"ROM not open in app: {rom_path_a}"}
        doc_b = self._find_document_by_rom_path(rom_path_b)
        if not doc_b:
            return {"success": False, "error": f"ROM not open in app: {rom_path_b}"}

        if table_name:
            return self._api_compare_single_table(doc_a, doc_b, table_name)
        else:
            return self._api_compare_all_tables(doc_a, doc_b)

    def _api_compare_all_tables(self, doc_a, doc_b) -> dict:
        """Summary of all differing tables between two ROMs."""
        import numpy as np

        defn_a = doc_a.rom_definition
        defn_b = doc_b.rom_definition

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
                data_a = doc_a.rom_reader.read_table_data(table_a)
                data_b = doc_b.rom_reader.read_table_data(table_b)
            except Exception:
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
            "success": True,
            "rom_a": Path(doc_a.rom_reader.rom_path).name,
            "rom_b": Path(doc_b.rom_reader.rom_path).name,
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

    def _api_compare_single_table(self, doc_a, doc_b, table_name: str) -> dict:
        """Cell-by-cell diff for a single table."""
        from src.core.rom_definition import TableType

        defn_a = doc_a.rom_definition
        defn_b = doc_b.rom_definition

        table_a = defn_a.get_table_by_name(table_name)
        table_b = defn_b.get_table_by_name(table_name)

        if table_a is None and table_b is None:
            return {
                "success": False,
                "error": f"Table not found in either ROM: {table_name}",
            }
        if table_a is None:
            return {
                "success": False,
                "error": f"Table '{table_name}' only exists in ROM B",
            }
        if table_b is None:
            return {
                "success": False,
                "error": f"Table '{table_name}' only exists in ROM A",
            }

        data_a = doc_a.rom_reader.read_table_data(table_a)
        data_b = doc_b.rom_reader.read_table_data(table_b)
        if data_a is None or data_b is None:
            return {
                "success": False,
                "error": f"Failed to read table data: {table_name}",
            }

        scaling_a = defn_a.get_scaling(table_a.scaling)
        fmt_a = printf_to_python_format(scaling_a.format) if scaling_a else ".2f"
        scaling_b = defn_b.get_scaling(table_b.scaling)
        fmt_b = printf_to_python_format(scaling_b.format) if scaling_b else ".2f"

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
                        "value_a": format_value(vals_a[i], fmt_a),
                        "value_b": format_value(vals_b[i], fmt_b),
                        "delta": format_value(delta, fmt_a),
                    }
                )

        result = {
            "success": True,
            "table_name": table_name,
            "type": table_a.type.value,
            "total_cells": min_len,
            "changed_cells": len(diffs),
            "diffs": diffs,
        }

        if table_a.type == TableType.THREE_D:
            x_axis = table_a.x_axis
            y_axis = table_a.y_axis
            if x_axis and y_axis:
                result["dimensions"] = {
                    "cols": x_axis.elements,
                    "rows": y_axis.elements,
                }

        return result
