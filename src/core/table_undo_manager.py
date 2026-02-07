"""
Table Undo Manager

Manages per-table QUndoStacks using Qt's QUndoGroup pattern.
Each open table has its own undo stack, with the active stack
determined by window focus.
"""

from PySide6.QtGui import QUndoGroup, QUndoStack
from typing import Dict, Optional, Callable, List, Tuple
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


class TableUndoManager:
    """
    Manages per-table undo/redo using Qt's QUndoGroup.

    - One QUndoStack per table (keyed by table_address)
    - QUndoGroup manages active stack based on focus
    - Provides unified interface for recording changes
    """

    def __init__(self):
        self._undo_group = QUndoGroup()
        self._stacks: Dict[str, QUndoStack] = {}  # table_address -> QUndoStack

        # Callbacks for applying changes to ROM/UI
        self._apply_cell_change: Optional[Callable[[CellChange], None]] = None
        self._apply_axis_change: Optional[Callable[[AxisChange], None]] = None
        self._update_pending: Optional[Callable[[CellChange, bool], None]] = None
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
        begin_bulk_update: Optional[Callable[[], None]] = None,
        end_bulk_update: Optional[Callable[[], None]] = None,
    ):
        """
        Set callbacks for applying changes.

        Args:
            apply_cell: Called to apply CellChange to ROM/UI
            apply_axis: Called to apply AxisChange to ROM/UI
            update_pending: Called to update pending changes tracking
            begin_bulk_update: Called before applying multiple changes (for performance)
            end_bulk_update: Called after applying multiple changes (for performance)
        """
        self._apply_cell_change = apply_cell
        self._apply_axis_change = apply_axis
        self._update_pending = update_pending
        self._begin_bulk_update = begin_bulk_update
        self._end_bulk_update = end_bulk_update

    def get_or_create_stack(self, table_address: str) -> QUndoStack:
        """
        Get or create an undo stack for a table.

        Args:
            table_address: Unique table identifier (hex address)

        Returns:
            QUndoStack for the table
        """
        if table_address not in self._stacks:
            stack = QUndoStack(self._undo_group)
            stack.setUndoLimit(MAX_UNDO_PER_TABLE)
            self._stacks[table_address] = stack
            logger.debug(f"Created undo stack for table {table_address}")

        return self._stacks[table_address]

    def set_active_stack(self, table_address: Optional[str]):
        """
        Set the active undo stack (called when table window gains focus).

        Creates the stack if it doesn't exist yet, so that undo is immediately
        available after the first edit in a newly opened table window.

        Args:
            table_address: Address of table to activate, or None to deactivate
        """
        if table_address:
            stack = self.get_or_create_stack(table_address)
            stack.setActive(True)
            logger.debug(f"Activated undo stack for table {table_address}")
        else:
            # Deactivate by setting no stack active
            self._undo_group.setActiveStack(None)
            logger.debug("Deactivated undo stack (no table focused)")

    def remove_stack(self, table_address: str):
        """Remove and delete the undo stack for a table."""
        if table_address not in self._stacks:
            return
        stack = self._stacks.pop(table_address)
        # Deactivate if this was the active stack
        if self._undo_group.activeStack() is stack:
            self._undo_group.setActiveStack(None)
        stack.clear()
        stack.deleteLater()
        logger.debug(f"Removed undo stack for table {table_address}")

    def remove_stacks_for_addresses(self, addresses):
        """Remove undo stacks for a collection of table addresses."""
        for addr in list(addresses):
            self.remove_stack(addr)

    def clear_stack(self, table_address: str):
        """Clear the undo stack for a specific table."""
        if table_address in self._stacks:
            self._stacks[table_address].clear()
            logger.debug(f"Cleared undo stack for table {table_address}")

    def record_cell_change(
        self,
        table: Table,
        row: int,
        col: int,
        old_value: float,
        new_value: float,
        old_raw: float,
        new_raw: float,
    ):
        """Record a single cell change"""
        if self._apply_cell_change is None:
            logger.warning("No apply_cell callback set, cannot record change")
            return

        change = CellChange(
            table_name=table.name,
            table_address=table.address,
            row=row,
            col=col,
            old_value=old_value,
            new_value=new_value,
            old_raw=old_raw,
            new_raw=new_raw,
        )

        stack = self.get_or_create_stack(table.address)
        cmd = CellEditCommand(change, self._apply_cell_change, self._update_pending)
        stack.push(cmd)

        logger.debug(f"Recorded cell change: {table.name}[{row},{col}]")

    def record_bulk_cell_changes(
        self,
        table: Table,
        changes: List[Tuple[int, int, float, float, float, float]],
        description: str,
    ):
        """
        Record multiple cell changes as single undo operation.

        Args:
            table: Table being edited
            changes: List of (row, col, old_value, new_value, old_raw, new_raw) tuples
            description: Description for undo menu (e.g., "Multiply by 1.1")
        """
        if not changes:
            return

        if self._apply_cell_change is None:
            logger.warning("No apply_cell callback set, cannot record bulk changes")
            return

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
            )
            for row, col, old_value, new_value, old_raw, new_raw in changes
        ]

        stack = self.get_or_create_stack(table.address)
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
    ):
        """Record a single axis change"""
        if self._apply_axis_change is None:
            logger.warning("No apply_axis callback set, cannot record axis change")
            return

        change = AxisChange(
            table_name=table.name,
            table_address=table.address,
            axis_type=axis_type,
            index=index,
            old_value=old_value,
            new_value=new_value,
            old_raw=old_raw,
            new_raw=new_raw,
        )

        stack = self.get_or_create_stack(table.address)
        cmd = AxisEditCommand(change, self._apply_axis_change)
        stack.push(cmd)

        logger.debug(f"Recorded axis change: {table.name}[{axis_type}][{index}]")

    def record_axis_bulk_changes(
        self,
        table: Table,
        changes: List[Tuple[str, int, float, float, float, float]],
        description: str,
    ):
        """
        Record multiple axis changes as single undo operation.

        Args:
            table: Table being edited
            changes: List of (axis_type, index, old_value, new_value, old_raw, new_raw)
            description: Description for undo menu
        """
        if not changes:
            return

        if self._apply_axis_change is None:
            logger.warning("No apply_axis callback set, cannot record axis bulk changes")
            return

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
            )
            for axis_type, index, old_value, new_value, old_raw, new_raw in changes
        ]

        stack = self.get_or_create_stack(table.address)
        cmd = BulkAxisEditCommand(
            axis_changes,
            description,
            self._apply_axis_change,
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
        """Clear all undo stacks"""
        for stack in self._stacks.values():
            stack.clear()
        self._stacks.clear()
        logger.debug("Cleared all undo stacks")

    def get_active_table_address(self) -> Optional[str]:
        """Get address of currently active table, if any"""
        active_stack = self._undo_group.activeStack()
        if active_stack:
            for addr, stack in self._stacks.items():
                if stack is active_stack:
                    return addr
        return None
