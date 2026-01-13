#!/usr/bin/env python3
"""
Test Script Runner for Automated GUI Testing

A CLI tool that enables automated testing of the NC ROM Editor GUI.
Supports loading ROMs, opening tables, performing operations, and taking screenshots.

Usage:
    python tools/test_runner.py [--rom ROM_PATH] [--table TABLE_NAME] [--script SCRIPT_FILE]

Interactive mode:
    python tools/test_runner.py --interactive

Script mode:
    python tools/test_runner.py --script test_script.txt
"""

import sys
import os
import argparse
import time
from pathlib import Path
from datetime import datetime

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QTimer


class TestRunner:
    """
    Automated test runner for NC ROM Editor GUI

    Provides programmatic control over the application for testing purposes.
    """

    def __init__(self, metadata_dir: str = None, quiet: bool = False):
        """
        Initialize the test runner

        Args:
            metadata_dir: Path to metadata directory (defaults to project's metadata/)
            quiet: If True, suppress non-essential output
        """
        self.quiet = quiet
        self.app = None
        self.main_window = None
        self.current_table_window = None
        self.rom_reader = None
        self.rom_definition = None

        # Default metadata directory
        if metadata_dir is None:
            self.metadata_dir = project_root / "metadata"
        else:
            self.metadata_dir = Path(metadata_dir)

        # Screenshots directory
        self.screenshots_dir = project_root / "docs" / "screenshots"
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)

        self._log(f"Test Runner initialized")
        self._log(f"  Metadata: {self.metadata_dir}")
        self._log(f"  Screenshots: {self.screenshots_dir}")

    def _log(self, message: str):
        """Log a message if not in quiet mode"""
        if not self.quiet:
            print(f"[TestRunner] {message}")

    def _ensure_app(self):
        """Ensure QApplication exists"""
        if self.app is None:
            # Check if one already exists
            self.app = QApplication.instance()
            if self.app is None:
                self.app = QApplication([])
                self.app.setApplicationName("NC ROM Editor Test Runner")

    def start_app(self) -> bool:
        """
        Start the application

        Returns:
            True if successful
        """
        try:
            self._ensure_app()

            # Import main window class
            from main import MainWindow
            from src.utils.settings import get_settings

            # Configure settings to use our metadata directory
            settings = get_settings()
            settings.settings.setValue("metadata_directory", str(self.metadata_dir))
            settings.settings.sync()

            # Create main window
            self.main_window = MainWindow()
            self.main_window.show()

            # Process events to ensure window is displayed
            self._process_events()

            self._log("Application started")
            return True

        except Exception as e:
            self._log(f"ERROR: Failed to start application: {e}")
            return False

    def load_rom(self, rom_path: str) -> bool:
        """
        Load a ROM file

        Args:
            rom_path: Path to ROM file

        Returns:
            True if successful
        """
        if self.main_window is None:
            self._log("ERROR: Application not started")
            return False

        rom_path = Path(rom_path)
        if not rom_path.exists():
            self._log(f"ERROR: ROM file not found: {rom_path}")
            return False

        try:
            self._log(f"Loading ROM: {rom_path}")
            self.main_window._open_rom_file(str(rom_path))
            self._process_events()

            # Get the loaded document
            document = self.main_window.get_current_document()
            if document:
                self.rom_reader = document.rom_reader
                self.rom_definition = document.rom_definition
                self._log(f"ROM loaded: {document.file_name}")
                self._log(f"  ROM ID: {self.rom_definition.romid.xmlid}")
                self._log(f"  Tables: {len(self.rom_definition.tables)}")
                return True
            else:
                self._log("ERROR: Failed to load ROM")
                return False

        except Exception as e:
            self._log(f"ERROR: {e}")
            return False

    def list_tables(self) -> list:
        """
        List all available tables in the loaded ROM

        Returns:
            List of table names
        """
        if self.rom_definition is None:
            self._log("ERROR: No ROM loaded")
            return []

        tables = [t.name for t in self.rom_definition.tables]
        return tables

    def open_table(self, table_name: str) -> bool:
        """
        Open a table by name

        Args:
            table_name: Name of the table to open

        Returns:
            True if successful
        """
        if self.rom_definition is None or self.rom_reader is None:
            self._log("ERROR: No ROM loaded")
            return False

        # Find the table
        table = None
        for t in self.rom_definition.tables:
            if t.name == table_name:
                table = t
                break

        if table is None:
            self._log(f"ERROR: Table not found: {table_name}")
            # Show similar tables
            similar = [t.name for t in self.rom_definition.tables
                      if table_name.lower() in t.name.lower()]
            if similar:
                self._log(f"  Similar tables: {similar[:5]}")
            return False

        try:
            self._log(f"Opening table: {table_name}")

            # Trigger table selection
            self.main_window.on_table_selected(table, self.rom_reader)
            self._process_events()

            # Find the opened table window
            for window in self.main_window.open_table_windows:
                if window.table.name == table_name:
                    self.current_table_window = window
                    break

            if self.current_table_window:
                self._log(f"Table opened: {table_name}")
                return True
            else:
                self._log("ERROR: Failed to open table window")
                return False

        except Exception as e:
            self._log(f"ERROR: {e}")
            return False

    def select_cells(self, start_row: int, start_col: int,
                     end_row: int = None, end_col: int = None) -> bool:
        """
        Select cells in the current table

        Args:
            start_row: Starting row (0-indexed, relative to data area)
            start_col: Starting column (0-indexed, relative to data area)
            end_row: Ending row (optional, defaults to start_row)
            end_col: Ending column (optional, defaults to start_col)

        Returns:
            True if successful
        """
        if self.current_table_window is None:
            self._log("ERROR: No table open")
            return False

        if end_row is None:
            end_row = start_row
        if end_col is None:
            end_col = start_col

        try:
            viewer = self.current_table_window.viewer
            table_widget = viewer.table_widget

            # Calculate actual widget row/col (accounting for axis rows)
            from src.core.rom_definition import TableType
            table = self.current_table_window.table

            # Determine row offset based on table type
            if table.type == TableType.THREE_D:
                row_offset = 1  # X-axis row
                col_offset = 1  # Y-axis column
            elif table.type == TableType.TWO_D:
                row_offset = 0  # No X-axis row
                col_offset = 1  # Y-axis column
            else:
                row_offset = 0
                col_offset = 0

            # Convert to widget coordinates
            widget_start_row = start_row + row_offset
            widget_start_col = start_col + col_offset
            widget_end_row = end_row + row_offset
            widget_end_col = end_col + col_offset

            # Clear current selection and select new range
            table_widget.clearSelection()
            from PySide6.QtWidgets import QTableWidgetSelectionRange
            selection_range = QTableWidgetSelectionRange(
                widget_start_row, widget_start_col,
                widget_end_row, widget_end_col
            )
            table_widget.setRangeSelected(selection_range, True)
            self._process_events()

            self._log(f"Selected cells: ({start_row},{start_col}) to ({end_row},{end_col})")
            return True

        except Exception as e:
            self._log(f"ERROR: {e}")
            return False

    def select_all_data(self) -> bool:
        """
        Select all data cells in the current table

        Returns:
            True if successful
        """
        if self.current_table_window is None:
            self._log("ERROR: No table open")
            return False

        try:
            self.current_table_window.viewer.select_all_data()
            self._process_events()
            self._log("Selected all data cells")
            return True
        except Exception as e:
            self._log(f"ERROR: {e}")
            return False

    def interpolate_vertical(self) -> bool:
        """
        Apply vertical interpolation to selected cells

        Returns:
            True if successful
        """
        if self.current_table_window is None:
            self._log("ERROR: No table open")
            return False

        try:
            self.current_table_window.viewer.interpolate_vertical()
            self._process_events()
            self._log("Applied vertical interpolation")
            return True
        except Exception as e:
            self._log(f"ERROR: {e}")
            return False

    def interpolate_horizontal(self) -> bool:
        """
        Apply horizontal interpolation to selected cells

        Returns:
            True if successful
        """
        if self.current_table_window is None:
            self._log("ERROR: No table open")
            return False

        try:
            self.current_table_window.viewer.interpolate_horizontal()
            self._process_events()
            self._log("Applied horizontal interpolation")
            return True
        except Exception as e:
            self._log(f"ERROR: {e}")
            return False

    def interpolate_2d(self) -> bool:
        """
        Apply 2D bilinear interpolation to selected cells

        Returns:
            True if successful
        """
        if self.current_table_window is None:
            self._log("ERROR: No table open")
            return False

        try:
            self.current_table_window.viewer.interpolate_2d()
            self._process_events()
            self._log("Applied 2D interpolation")
            return True
        except Exception as e:
            self._log(f"ERROR: {e}")
            return False

    def increment_selection(self) -> bool:
        """
        Increment selected cells

        Returns:
            True if successful
        """
        if self.current_table_window is None:
            self._log("ERROR: No table open")
            return False

        try:
            self.current_table_window.viewer.increment_selection()
            self._process_events()
            self._log("Incremented selection")
            return True
        except Exception as e:
            self._log(f"ERROR: {e}")
            return False

    def decrement_selection(self) -> bool:
        """
        Decrement selected cells

        Returns:
            True if successful
        """
        if self.current_table_window is None:
            self._log("ERROR: No table open")
            return False

        try:
            self.current_table_window.viewer.decrement_selection()
            self._process_events()
            self._log("Decremented selection")
            return True
        except Exception as e:
            self._log(f"ERROR: {e}")
            return False

    def set_value(self, value: float) -> bool:
        """
        Set selected cells to a specific value

        Args:
            value: Value to set

        Returns:
            True if successful
        """
        if self.current_table_window is None:
            self._log("ERROR: No table open")
            return False

        try:
            # Use the viewer's bulk operation method which handles change tracking
            viewer = self.current_table_window.viewer
            operation_fn = lambda v: value
            viewer._apply_bulk_operation(operation_fn, f"Set to {value}")
            self._process_events()
            self._log(f"Set selection to {value}")
            return True
        except Exception as e:
            self._log(f"ERROR: {e}")
            return False

    def multiply_selection(self, factor: float) -> bool:
        """
        Multiply selected cells by a factor

        Args:
            factor: Multiplication factor

        Returns:
            True if successful
        """
        if self.current_table_window is None:
            self._log("ERROR: No table open")
            return False

        try:
            # Use the viewer's bulk operation method which handles change tracking
            viewer = self.current_table_window.viewer
            operation_fn = lambda v: v * factor
            viewer._apply_bulk_operation(operation_fn, f"Multiply by {factor}")
            self._process_events()
            self._log(f"Multiplied selection by {factor}")
            return True
        except Exception as e:
            self._log(f"ERROR: {e}")
            return False

    def add_to_selection(self, value: float) -> bool:
        """
        Add a value to selected cells

        Args:
            value: Value to add

        Returns:
            True if successful
        """
        if self.current_table_window is None:
            self._log("ERROR: No table open")
            return False

        try:
            # Use the viewer's bulk operation method which handles change tracking
            viewer = self.current_table_window.viewer
            operation_fn = lambda v: v + value
            viewer._apply_bulk_operation(operation_fn, f"Add {value}")
            self._process_events()
            self._log(f"Added {value} to selection")
            return True
        except Exception as e:
            self._log(f"ERROR: {e}")
            return False

    def open_graph(self) -> bool:
        """
        Open graph viewer for current table

        Returns:
            True if successful
        """
        if self.current_table_window is None:
            self._log("ERROR: No table open")
            return False

        try:
            self.current_table_window._open_graph_viewer()
            self._process_events()
            self._log("Graph viewer opened")
            return True
        except Exception as e:
            self._log(f"ERROR: {e}")
            return False

    def close_graph(self) -> bool:
        """
        Close graph viewer if open

        Returns:
            True if successful
        """
        if self.current_table_window is None:
            self._log("ERROR: No table open")
            return False

        try:
            if self.current_table_window.graph_viewer:
                self.current_table_window.graph_viewer.close()
                self._process_events()
            self._log("Graph viewer closed")
            return True
        except Exception as e:
            self._log(f"ERROR: {e}")
            return False

    def close_table(self) -> bool:
        """
        Close current table window

        Returns:
            True if successful
        """
        if self.current_table_window is None:
            self._log("ERROR: No table open")
            return False

        try:
            self.current_table_window.close()
            self.current_table_window = None
            self._process_events()
            self._log("Table window closed")
            return True
        except Exception as e:
            self._log(f"ERROR: {e}")
            return False

    def screenshot(self, name: str = None, target: str = "table") -> str:
        """
        Take a screenshot

        Args:
            name: Optional name for the screenshot (defaults to timestamp)
            target: What to capture: "table", "graph", "main", or "all"

        Returns:
            Path to saved screenshot, or empty string on failure
        """
        if name is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            name = f"screenshot_{timestamp}"

        try:
            self._process_events()

            if target == "table" and self.current_table_window:
                widget = self.current_table_window
            elif target == "graph" and self.current_table_window and self.current_table_window.graph_viewer:
                widget = self.current_table_window.graph_viewer
            elif target == "main" and self.main_window:
                widget = self.main_window
            else:
                self._log(f"ERROR: Invalid target or widget not available: {target}")
                return ""

            # Grab the widget
            pixmap = widget.grab()

            # Save to file
            filepath = self.screenshots_dir / f"{name}.png"
            pixmap.save(str(filepath))

            self._log(f"Screenshot saved: {filepath}")
            return str(filepath)

        except Exception as e:
            self._log(f"ERROR: {e}")
            return ""

    def undo(self) -> bool:
        """
        Undo last action

        Returns:
            True if successful
        """
        if self.main_window is None:
            self._log("ERROR: Application not started")
            return False

        try:
            self.main_window.undo()
            self._process_events()
            self._log("Undo executed")
            return True
        except Exception as e:
            self._log(f"ERROR: {e}")
            return False

    def redo(self) -> bool:
        """
        Redo last undone action

        Returns:
            True if successful
        """
        if self.main_window is None:
            self._log("ERROR: Application not started")
            return False

        try:
            self.main_window.redo()
            self._process_events()
            self._log("Redo executed")
            return True
        except Exception as e:
            self._log(f"ERROR: {e}")
            return False

    def wait(self, milliseconds: int):
        """
        Wait for specified time while processing events

        Args:
            milliseconds: Time to wait in milliseconds
        """
        start = time.time()
        while (time.time() - start) * 1000 < milliseconds:
            self._process_events()
            time.sleep(0.01)

    def _process_events(self):
        """Process pending Qt events"""
        if self.app:
            self.app.processEvents()

    def run_script(self, script_path: str) -> bool:
        """
        Run a test script file

        Script format (one command per line):
            # Comment lines start with #
            load_rom /path/to/rom.bin
            open_table "Table Name"
            select 0 0 5 5
            interpolate_v
            screenshot result

        Args:
            script_path: Path to script file

        Returns:
            True if all commands succeeded
        """
        script_path = Path(script_path)
        if not script_path.exists():
            self._log(f"ERROR: Script not found: {script_path}")
            return False

        self._log(f"Running script: {script_path}")

        with open(script_path, 'r') as f:
            lines = f.readlines()

        success = True
        for line_num, line in enumerate(lines, 1):
            line = line.strip()

            # Skip empty lines and comments
            if not line or line.startswith('#'):
                continue

            self._log(f"[{line_num}] {line}")

            # Parse and execute command
            result = self._execute_command(line)
            if not result:
                self._log(f"ERROR at line {line_num}: {line}")
                success = False
                # Continue with remaining commands

        return success

    def _execute_command(self, command: str) -> bool:
        """
        Execute a single command

        Args:
            command: Command string

        Returns:
            True if successful
        """
        parts = self._parse_command(command)
        if not parts:
            return False

        cmd = parts[0].lower()
        args = parts[1:]

        try:
            if cmd == "start":
                return self.start_app()

            elif cmd == "load_rom" and len(args) >= 1:
                return self.load_rom(args[0])

            elif cmd == "list_tables":
                tables = self.list_tables()
                for t in tables:
                    print(f"  {t}")
                return True

            elif cmd == "open_table" and len(args) >= 1:
                return self.open_table(args[0])

            elif cmd == "select" and len(args) >= 2:
                start_row = int(args[0])
                start_col = int(args[1])
                end_row = int(args[2]) if len(args) > 2 else None
                end_col = int(args[3]) if len(args) > 3 else None
                return self.select_cells(start_row, start_col, end_row, end_col)

            elif cmd == "select_all":
                return self.select_all_data()

            elif cmd == "interpolate_v":
                return self.interpolate_vertical()

            elif cmd == "interpolate_h":
                return self.interpolate_horizontal()

            elif cmd == "interpolate_2d":
                return self.interpolate_2d()

            elif cmd == "increment":
                return self.increment_selection()

            elif cmd == "decrement":
                return self.decrement_selection()

            elif cmd == "set" and len(args) >= 1:
                return self.set_value(float(args[0]))

            elif cmd == "multiply" and len(args) >= 1:
                return self.multiply_selection(float(args[0]))

            elif cmd == "add" and len(args) >= 1:
                return self.add_to_selection(float(args[0]))

            elif cmd == "open_graph":
                return self.open_graph()

            elif cmd == "close_graph":
                return self.close_graph()

            elif cmd == "close_table":
                return self.close_table()

            elif cmd == "screenshot":
                name = args[0] if args else None
                target = args[1] if len(args) > 1 else "table"
                return bool(self.screenshot(name, target))

            elif cmd == "undo":
                return self.undo()

            elif cmd == "redo":
                return self.redo()

            elif cmd == "wait" and len(args) >= 1:
                self.wait(int(args[0]))
                return True

            else:
                self._log(f"Unknown command: {cmd}")
                return False

        except Exception as e:
            self._log(f"ERROR executing '{cmd}': {e}")
            return False

    def _parse_command(self, command: str) -> list:
        """
        Parse a command string into parts (handles quoted strings)

        Args:
            command: Command string

        Returns:
            List of command parts
        """
        parts = []
        current = ""
        in_quotes = False
        quote_char = None

        for char in command:
            if char in '"\'':
                if not in_quotes:
                    in_quotes = True
                    quote_char = char
                elif char == quote_char:
                    in_quotes = False
                    quote_char = None
                else:
                    current += char
            elif char.isspace() and not in_quotes:
                if current:
                    parts.append(current)
                    current = ""
            else:
                current += char

        if current:
            parts.append(current)

        return parts

    def interactive(self):
        """
        Start interactive mode with a REPL
        """
        print("\n" + "=" * 60)
        print("NC ROM Editor Test Runner - Interactive Mode")
        print("=" * 60)
        print("\nCommands:")
        print("  start                    - Start the application")
        print("  load_rom <path>          - Load a ROM file")
        print("  list_tables              - List available tables")
        print("  open_table <name>        - Open a table by name")
        print("  select <r1> <c1> [r2 c2] - Select cells")
        print("  select_all               - Select all data cells")
        print("  interpolate_v            - Vertical interpolation")
        print("  interpolate_h            - Horizontal interpolation")
        print("  interpolate_2d           - 2D interpolation")
        print("  increment / decrement    - Increment/decrement selection")
        print("  set <value>              - Set selection to value")
        print("  multiply <factor>        - Multiply selection")
        print("  add <value>              - Add to selection")
        print("  open_graph / close_graph - Toggle graph viewer")
        print("  screenshot [name] [target] - Take screenshot")
        print("  undo / redo              - Undo/redo last change")
        print("  wait <ms>                - Wait milliseconds")
        print("  close_table              - Close current table")
        print("  quit / exit              - Exit interactive mode")
        print("\n")

        while True:
            try:
                command = input("> ").strip()

                if not command:
                    continue

                if command.lower() in ('quit', 'exit', 'q'):
                    print("Exiting...")
                    break

                if command.lower() == 'help':
                    print("See command list above")
                    continue

                self._execute_command(command)

            except KeyboardInterrupt:
                print("\nExiting...")
                break
            except EOFError:
                break
            except Exception as e:
                print(f"Error: {e}")


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="NC ROM Editor Test Runner - Automated GUI Testing Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Interactive mode
  python tools/test_runner.py --interactive

  # Run a test script
  python tools/test_runner.py --script tests/gui/test_interpolation.txt

  # Quick test: load ROM and open a table
  python tools/test_runner.py --rom examples/lf9veb.bin --table "APP to TP Desired"
"""
    )

    parser.add_argument('--rom', '-r',
                        help='Path to ROM file to load')
    parser.add_argument('--table', '-t',
                        help='Name of table to open')
    parser.add_argument('--script', '-s',
                        help='Path to test script file')
    parser.add_argument('--interactive', '-i', action='store_true',
                        help='Start in interactive mode')
    parser.add_argument('--metadata', '-m',
                        help='Path to metadata directory')
    parser.add_argument('--quiet', '-q', action='store_true',
                        help='Suppress non-essential output')
    parser.add_argument('--screenshot',
                        help='Take screenshot and save with this name')

    args = parser.parse_args()

    # Create test runner
    runner = TestRunner(metadata_dir=args.metadata, quiet=args.quiet)

    # Handle script mode
    if args.script:
        if not runner.start_app():
            sys.exit(1)
        success = runner.run_script(args.script)
        sys.exit(0 if success else 1)

    # Handle interactive mode
    if args.interactive:
        runner.interactive()
        sys.exit(0)

    # Handle direct commands
    if args.rom or args.table:
        if not runner.start_app():
            sys.exit(1)

        if args.rom:
            if not runner.load_rom(args.rom):
                sys.exit(1)

        if args.table:
            if not runner.open_table(args.table):
                sys.exit(1)

        if args.screenshot:
            runner.screenshot(args.screenshot)

        # Keep running to see the result
        if not args.screenshot:
            print("\nPress Ctrl+C to exit...")
            try:
                while True:
                    runner._process_events()
                    time.sleep(0.1)
            except KeyboardInterrupt:
                pass

        sys.exit(0)

    # No arguments - show help
    parser.print_help()


if __name__ == "__main__":
    main()
