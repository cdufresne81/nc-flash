"""
Table Undo Manager

Manages per-table QUndoStacks using Qt's QUndoGroup pattern.
Each open table has its own undo stack, with the active stack
determined by window focus.

Stack keys are TableKey namedtuples (rom_path, table_address) to isolate
undo stacks when multiple ROMs share the same table addresses.
"""

from collections import namedtuple
from PySide6.QtGui import QUndoGroup, QUndoStack
from typing import Dict, Optional, Callable, List, Tuple, Union
import logging

from .undo_commands import (
    CellEditCommand,
    BulkCellEditCommand,
    AxisEditCommand,
    BulkAxisEditCommand,
)
from .version_models import CellChange, AxisChange
from .rom_definition import Table

logger = logging.getLogger(__name__)

MAX_UNDO_PER_TABLE = 100

TableKey = namedtuple("TableKey", ["rom_path", "table_address"])


def make_table_key(rom_path, table_address: str) -> TableKey:
    """Build a composite key unique per ROM per table.

    Args:
        rom_path: Path to the ROM file (str, Path, or None)
        table_address: Hex address string (e.g., "0x1000")

    Returns:
        TableKey namedtuple (hashable, usable as dict key)
    """
    return TableKey(str(rom_path) if rom_path else None, table_address)


def extract_table_address(table_key) -> str:
    """Extract the raw table address from a TableKey or legacy string key."""
    if isinstance(table_key, TableKey):
        return table_key.table_address
    # Legacy fallback for string keys
    if isinstance(table_key, str) and "\0" in table_key:
        return table_key.rsplit("\0", 1)[1]
    return str(table_key)


def extract_rom_path(table_key) -> Optional[str]:
    """Extract the ROM path from a TableKey or legacy string key."""
    if isinstance(table_key, TableKey):
        return table_key.rom_path
    # Legacy fallback for string keys
    if isinstance(table_key, str) and "\0" in table_key:
        return table_key.rsplit("\0", 1)[0]
    return None


class TableUndoManager:
    """
    Manages per-table undo/redo using Qt's QUndoGroup.

    - One QUndoStack per table (keyed by composite rom_path\0table_address)
    - QUndoGroup manages active stack based on focus
    - Provides unified interface for recording changes
    """

    def __init__(self):
        self._undo_group = QUndoGroup()
        self._stacks: Dict[str, QUndoStack] = {}  # table_key -> QUndoStack

        # Callbacks for applying changes to ROM/UI
        self._apply_cell_change: Optional[Callable[[CellChange], None]] = None
        self._apply_axis_change: Optional[Callable[[AxisChange], None]] = None
        self._update_pending: Optional[Callable[[CellChange, bool], None]] = None
        self._update_pending_axis: Optional[Callable[[AxisChange, bool], None]] = None
        # Callbacks for bulk update optimization (batching)
        self._begin_bulk_update: Optional[Callable[[], None]] = None
        self._end_bulk_update: Optional[Callable[[], None]] = None

    @property
    def undo_group(self) -> QUndoGroup:
        """Get the QUndoGroup for connecting to QActions"""
        return self._undo_group

    def set_callbacks(
        self,
        apply_cell: Callable[[CellChange], None],
        apply_axis: Callable[[AxisChange], None],
        update_pending: Optional[Callable[[CellChange, bool], None]] = None,
        update_pending_axis: Optional[Callable[[AxisChange, bool], None]] = None,
        begin_bulk_update: Optional[Callable[[], None]] = None,
        end_bulk_update: Optional[Callable[[], None]] = None,
    ):
        """
        Set callbacks for applying changes.

        Args:
            apply_cell: Called to apply CellChange to ROM/UI
            apply_axis: Called to apply AxisChange to ROM/UI
            update_pending: Called to update pending cell changes tracking
            update_pending_axis: Called to update pending axis changes tracking
            begin_bulk_update: Called before applying multiple changes (for performance)
            end_bulk_update: Called after applying multiple changes (for performance)
        """
        self._apply_cell_change = apply_cell
        self._apply_axis_change = apply_axis
        self._update_pending = update_pending
        self._update_pending_axis = update_pending_axis
        self._begin_bulk_update = begin_bulk_update
        self._end_bulk_update = end_bulk_update

    def get_or_create_stack(self, table_key: str) -> QUndoStack:
        """
        Get or create an undo stack for a table.

        Args:
            table_key: Composite key (rom_path\0table_address)

        Returns:
            QUndoStack for the table
        """
        if table_key not in self._stacks:
            stack = QUndoStack(self._undo_group)
            stack.setUndoLimit(MAX_UNDO_PER_TABLE)
            self._stacks[table_key] = stack
            logger.debug(f"Created undo stack for table {table_key}")

        return self._stacks[table_key]

    def set_active_stack(self, table_key: Optional[str]):
        """
        Set the active undo stack (called when table window gains focus).

        Creates the stack if it doesn't exist yet, so that undo is immediately
        available after the first edit in a newly opened table window.

        Args:
            table_key: Composite key of table to activate, or None to deactivate
        """
        if table_key:
            stack = self.get_or_create_stack(table_key)
            stack.setActive(True)
            logger.debug(f"Activated undo stack for table {table_key}")
        else:
            # Deactivate by setting no stack active
            self._undo_group.setActiveStack(None)
            logger.debug("Deactivated undo stack (no table focused)")

    def remove_stack(self, table_key: str):
        """Remove and delete the undo stack for a table."""
        if table_key not in self._stacks:
            return
        stack = self._stacks.pop(table_key)
        # Deactivate if this was the active stack
        if self._undo_group.activeStack() is stack:
            self._undo_group.setActiveStack(None)
        stack.clear()
        stack.deleteLater()
        logger.debug(f"Removed undo stack for table {table_key}")

    def remove_stacks_for_keys(self, keys):
        """Remove undo stacks for a collection of composite keys."""
        for key in list(keys):
            self.remove_stack(key)

    def rename_key(self, old_key: str, new_key: str):
        """Rename a stack key (e.g., after Save As changes the ROM path)."""
        if old_key in self._stacks:
            self._stacks[new_key] = self._stacks.pop(old_key)

    def clear_stack(self, table_key: str):
        """Clear the undo stack for a specific table."""
        if table_key in self._stacks:
            self._stacks[table_key].clear()
            logger.debug(f"Cleared undo stack for table {table_key}")

    def record_cell_change(
        self,
        table: Table,
        row: int,
        col: int,
        old_value: float,
        new_value: float,
        old_raw: float,
        new_raw: float,
        rom_path=None,
    ):
        """Record a single cell change"""
        if self._apply_cell_change is None:
            logger.warning("No apply_cell callback set, cannot record change")
            return

        table_key = make_table_key(rom_path, table.address)

        change = CellChange(
            table_name=table.name,
            table_address=table.address,
            row=row,
            col=col,
            old_value=old_value,
            new_value=new_value,
            old_raw=old_raw,
            new_raw=new_raw,
            table_key=table_key,
        )

        stack = self.get_or_create_stack(table_key)
        cmd = CellEditCommand(change, self._apply_cell_change, self._update_pending)
        stack.push(cmd)

        logger.debug(f"Recorded cell change: {table.name}[{row},{col}]")

    def record_bulk_cell_changes(
        self,
        table: Table,
        changes: List[Tuple[int, int, float, float, float, float]],
        description: str,
        rom_path=None,
    ):
        """
        Record multiple cell changes as single undo operation.

        Args:
            table: Table being edited
            changes: List of (row, col, old_value, new_value, old_raw, new_raw) tuples
            description: Description for undo menu (e.g., "Multiply by 1.1")
            rom_path: Path to ROM file for multi-ROM isolation
        """
        if not changes:
            return

        if self._apply_cell_change is None:
            logger.warning("No apply_cell callback set, cannot record bulk changes")
            return

        table_key = make_table_key(rom_path, table.address)

        cell_changes = [
            CellChange(
                table_name=table.name,
                table_address=table.address,
                row=row,
                col=col,
                old_value=old_value,
                new_value=new_value,
                old_raw=old_raw,
                new_raw=new_raw,
                table_key=table_key,
            )
            for row, col, old_value, new_value, old_raw, new_raw in changes
        ]

        stack = self.get_or_create_stack(table_key)
        cmd = BulkCellEditCommand(
            cell_changes,
            description,
            self._apply_cell_change,
            self._update_pending,
            self._begin_bulk_update,
            self._end_bulk_update,
        )
        stack.push(cmd)

        logger.debug(f"Recorded bulk change: {description} ({len(changes)} cells)")

    def record_axis_change(
        self,
        table: Table,
        axis_type: str,
        index: int,
        old_value: float,
        new_value: float,
        old_raw: float,
        new_raw: float,
        rom_path=None,
    ):
        """Record a single axis change"""
        if self._apply_axis_change is None:
            logger.warning("No apply_axis callback set, cannot record axis change")
            return

        table_key = make_table_key(rom_path, table.address)

        change = AxisChange(
            table_name=table.name,
            table_address=table.address,
            axis_type=axis_type,
            index=index,
            old_value=old_value,
            new_value=new_value,
            old_raw=old_raw,
            new_raw=new_raw,
            table_key=table_key,
        )

        stack = self.get_or_create_stack(table_key)
        cmd = AxisEditCommand(
            change, self._apply_axis_change, self._update_pending_axis
        )
        stack.push(cmd)

        logger.debug(f"Recorded axis change: {table.name}[{axis_type}][{index}]")

    def record_axis_bulk_changes(
        self,
        table: Table,
        changes: List[Tuple[str, int, float, float, float, float]],
        description: str,
        rom_path=None,
    ):
        """
        Record multiple axis changes as single undo operation.

        Args:
            table: Table being edited
            changes: List of (axis_type, index, old_value, new_value, old_raw, new_raw)
            description: Description for undo menu
            rom_path: Path to ROM file for multi-ROM isolation
        """
        if not changes:
            return

        if self._apply_axis_change is None:
            logger.warning(
                "No apply_axis callback set, cannot record axis bulk changes"
            )
            return

        table_key = make_table_key(rom_path, table.address)

        axis_changes = [
            AxisChange(
                table_name=table.name,
                table_address=table.address,
                axis_type=axis_type,
                index=index,
                old_value=old_value,
                new_value=new_value,
                old_raw=old_raw,
                new_raw=new_raw,
                table_key=table_key,
            )
            for axis_type, index, old_value, new_value, old_raw, new_raw in changes
        ]

        stack = self.get_or_create_stack(table_key)
        cmd = BulkAxisEditCommand(
            axis_changes,
            description,
            self._apply_axis_change,
            self._update_pending_axis,
            self._begin_bulk_update,
            self._end_bulk_update,
        )
        stack.push(cmd)

        logger.debug(f"Recorded axis bulk change: {description} ({len(changes)} cells)")

    def can_undo(self) -> bool:
        """Check if active stack can undo"""
        return self._undo_group.canUndo()

    def can_redo(self) -> bool:
        """Check if active stack can redo"""
        return self._undo_group.canRedo()

    def undo_text(self) -> str:
        """Get undo action text for active stack"""
        return self._undo_group.undoText()

    def redo_text(self) -> str:
        """Get redo action text for active stack"""
        return self._undo_group.redoText()

    def clear_all(self):
        """Clear and delete all undo stacks."""
        self._undo_group.setActiveStack(None)
        for stack in self._stacks.values():
            stack.clear()
            stack.deleteLater()
        self._stacks.clear()
        logger.debug("Cleared all undo stacks")

    def get_active_table_address(self) -> Optional[str]:
        """Get key of currently active table, if any"""
        active_stack = self._undo_group.activeStack()
        if active_stack:
            for key, stack in self._stacks.items():
                if stack is active_stack:
                    return key
        return None
