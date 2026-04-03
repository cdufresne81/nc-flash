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
- self._make_icon(name) method
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

            mcp_args = ["--transport", "sse", "--port", str(self.MCP_SSE_PORT)]
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
            self._toolbar_mcp.setIcon(self._make_icon("mcp_on"))
            self._toolbar_mcp.setToolTip(f"MCP Server running — {url}\nClick to stop")
            self.statusBar().showMessage(f"MCP server started on {url}", 5000)
        else:
            self.mcp_action.setText("&MCP Server")
            self._toolbar_mcp.setIcon(self._make_icon("mcp_off"))
            self._toolbar_mcp.setToolTip("MCP Server (off) — click to start")

    def _show_mcp_connection_info(self):
        """Show connection instructions after manually starting the MCP server."""
        url = f"http://127.0.0.1:{self.MCP_SSE_PORT}/sse"

        mcp_command = f"{sys.executable} -m src.mcp.server"
        config_snippet = json.dumps(
            {"mcpServers": {"nc-flash": {"command": mcp_command, "args": []}}},
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

        from src.core.table_undo_manager import extract_rom_path as _extract_rom_path

        rom_path_str = str(Path(rom_path))
        tables = []
        for key, pending in self.change_tracker._pending.items():
            if not pending.has_changes():
                continue
            key_rom = _extract_rom_path(key)
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
        table_name = request.get("table_name", "")

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
        import numpy as np
        from src.core.rom_reader import ScalingConverter
        from src.core.table_undo_manager import make_table_key

        rom_path = request.get("rom_path", "")
        table_name = request.get("table_name", "")
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

        # Capture originals for border tracking
        rom_path_key = document.rom_reader.rom_path
        if rom_path_key not in self.original_table_values:
            self.original_table_values[rom_path_key] = {}
        if table.address not in self.original_table_values[rom_path_key]:
            self.original_table_values[rom_path_key][table.address] = {
                "values": np.copy(old_data["values"]),
                "x_axis": (
                    np.copy(old_data["x_axis"])
                    if old_data.get("x_axis") is not None
                    else None
                ),
                "y_axis": (
                    np.copy(old_data["y_axis"])
                    if old_data.get("y_axis") is not None
                    else None
                ),
            }

        # Record undo + change tracking
        desc = f"AI: edit {len(changes)} cell(s) in {table_name}"
        table_key = make_table_key(rom_path_key, table.address)
        self.table_undo_manager.record_bulk_cell_changes(
            table, changes, desc, rom_path=rom_path_key
        )
        self.change_tracker.record_pending_bulk_changes(
            table, changes, rom_path=rom_path_key
        )
        self.table_undo_manager.set_active_stack(table_key)

        # Write to ROM
        def write_cells():
            for row, col, _ov, _nv, _or, new_raw in changes:
                document.rom_reader.write_cell_value(table, row, col, new_raw)

        self._write_to_rom_and_mark_modified(document, write_cells, desc)

        # Update modified_cells for border highlighting
        if rom_path_key not in self.modified_cells:
            self.modified_cells[rom_path_key] = {}
        if table.address not in self.modified_cells[rom_path_key]:
            self.modified_cells[rom_path_key][table.address] = set()
        for row, col, _ov, _nv, _or, _nr in changes:
            self.modified_cells[rom_path_key][table.address].add((row, col))

        # Refresh open table viewer window if visible
        window = self._find_table_window(table_key)
        if window:
            viewer = window.viewer
            viewer.begin_bulk_update()
            try:
                for row, col, _ov, new_val, _or, _nr in changes:
                    viewer.update_cell_value(row, col, new_val)
            finally:
                viewer.end_bulk_update()

        self._update_tab_title(document)
        self._write_workspace_state()

        result = {"success": True, "cells_modified": len(changes)}
        if errors:
            result["warnings"] = errors
        return result
