"""
MCP Server for NC Miata ECU ROM access.

Exposes ROM inspection and editing tools via the Model Context Protocol.
Supports STDIO transport (default, for CLI clients like Claude Code)
and SSE transport (for app-managed server, any client connects via HTTP).

Disk-based tools (read_table, list_tables, etc.) have no Qt dependency.
Live tools (read_live_table, write_table, list_modified_tables) communicate
with the running app via its command API HTTP bridge.

Usage:
    python -m src.mcp.server [--definitions-dir PATH] [--transport stdio|sse] [--port PORT]
"""

import argparse
import logging
import sys
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .rom_context import RomContext

# Log to stderr (stdout reserved for STDIO protocol)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

# Default SSE port
DEFAULT_SSE_PORT = 8765

# Module-level context, initialized in main()
_ctx: Optional[RomContext] = None


def _create_mcp(port: int = DEFAULT_SSE_PORT) -> FastMCP:
    """Create and configure the FastMCP server instance with all tools."""
    server = FastMCP(
        "nc-rom-editor",
        instructions="Access NC Miata ECU ROM files — inspect tables, values, compare ROMs, and edit live table values through the app",
        host="127.0.0.1",
        port=port,
    )

    def _get_ctx() -> RomContext:
        global _ctx
        if _ctx is None:
            _ctx = RomContext()
        return _ctx

    @server.tool()
    def get_workspace() -> dict:
        """Get the list of ROMs currently open in NC ROM Editor.

        Returns ROM paths, identification, and which ROM is active.
        Use this first to discover open ROMs instead of asking for paths.
        """
        return _get_ctx().get_workspace()

    @server.tool()
    def get_rom_info(rom_path: str) -> dict:
        """Auto-detect ROM type and return identification info.

        Returns make, model, year, ECU ID, xmlid, file size, table count,
        and a category summary (category name → table count).

        Args:
            rom_path: Path to the ROM binary file.
        """
        return _get_ctx().get_rom_info(rom_path)

    @server.tool()
    def list_tables(
        rom_path: str,
        category: Optional[str] = None,
        search: Optional[str] = None,
        level: Optional[int] = None,
    ) -> list:
        """List calibration tables with optional filtering.

        Returns table name, category, type (1D/2D/3D), dimensions, address,
        units, level. For 2D/3D tables, includes axis names and units.

        Args:
            rom_path: Path to the ROM binary file.
            category: Filter by category name (exact match).
            search: Filter by name substring (case-insensitive).
            level: Filter by complexity level (1-4).
        """
        return _get_ctx().list_tables(rom_path, category, search, level)

    @server.tool()
    def read_table(rom_path: str, table_name: str) -> dict:
        """Read a table's scaled display values with full axis context.

        1D → flat list of values.
        2D → column of values with Y-axis.
        3D → 2D grid of values with X/Y axes.

        Values are formatted using the definition's printf format spec.
        Axes include name, units, scaling expression, and formatted values.

        Args:
            rom_path: Path to the ROM binary file.
            table_name: Exact name of the table to read.
        """
        return _get_ctx().read_table(rom_path, table_name)

    @server.tool()
    def compare_tables(
        rom_path_a: str,
        rom_path_b: str,
        table_name: Optional[str] = None,
    ) -> dict:
        """Compare tables between two ROM files.

        Without table_name: returns a summary of all differing tables
        (count, names, change percentage).

        With table_name: returns a cell-by-cell diff with values from
        both ROMs and deltas.

        Supports cross-definition comparison (tables matched by name).

        Args:
            rom_path_a: Path to the first ROM binary file.
            rom_path_b: Path to the second ROM binary file.
            table_name: Optional table name for detailed comparison.
        """
        return _get_ctx().compare_tables(rom_path_a, rom_path_b, table_name)

    @server.tool()
    def get_table_statistics(rom_path: str, table_name: str) -> dict:
        """Statistical analysis of a table's values.

        Returns min, max, mean, median, std dev, percentiles (p25/p75/p90/p95),
        and axis ranges.

        Args:
            rom_path: Path to the ROM binary file.
            table_name: Exact name of the table to analyze.
        """
        return _get_ctx().get_table_statistics(rom_path, table_name)

    # ------------------------------------------------------------------
    # Live app bridge tools (require app running with MCP server enabled)
    # ------------------------------------------------------------------

    @server.tool()
    def list_modified_tables(rom_path: str) -> dict:
        """List tables with unsaved modifications in the running app.

        Returns table names and change counts for the given ROM.
        Requires the app to be running with MCP server enabled.

        Args:
            rom_path: Path to the ROM binary file (as shown in get_workspace).
        """
        return _get_ctx().list_modified_tables(rom_path)

    @server.tool()
    def read_live_table(rom_path: str, table_name: str) -> dict:
        """Read a table's current in-memory values from the running app.

        Unlike read_table (which reads from disk), this returns unsaved edits.
        Same output format as read_table.
        Requires the app to be running with MCP server enabled.

        Args:
            rom_path: Path to the ROM binary file (as shown in get_workspace).
            table_name: Exact name of the table to read.
        """
        return _get_ctx().read_live_table(rom_path, table_name)

    @server.tool()
    def write_table(rom_path: str, table_name: str, cells: list[dict]) -> dict:
        """Write values to a ROM table through the app's editing pipeline.

        Changes appear in the app with full undo support.
        Values are in display units (same as read_table/read_live_table).
        Requires the app to be running with MCP server enabled.

        Args:
            rom_path: Path to the ROM binary file (as shown in get_workspace).
            table_name: Exact name of the table to edit.
            cells: List of cell edits, each a dict with "row", "col", "value" keys.
                   Example: [{"row": 0, "col": 0, "value": 42.5}]
        """
        return _get_ctx().write_table(rom_path, table_name, cells)

    return server


def main():
    parser = argparse.ArgumentParser(description="NC ROM Editor MCP Server")
    parser.add_argument(
        "--definitions-dir",
        help="Path to ROM definitions directory (default: <app_root>/definitions)",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport protocol (default: stdio)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_SSE_PORT,
        help=f"Port for SSE transport (default: {DEFAULT_SSE_PORT})",
    )
    args = parser.parse_args()

    global _ctx
    _ctx = RomContext(definitions_dir=args.definitions_dir)

    server = _create_mcp(port=args.port)

    logger.info(
        f"Starting NC ROM Editor MCP server ({args.transport} transport"
        f"{f', port {args.port}' if args.transport == 'sse' else ''})"
    )
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()
