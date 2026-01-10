"""
Change Tracker

Tracks pending changes and manages undo/redo stack for cell edits.
"""

from typing import Optional, List, Dict, Callable, Union
from collections import deque
from dataclasses import dataclass, field
import logging

from .version_models import (
    CellChange, TableChanges, UndoableChange, BulkChange,
    AxisChange, UndoableAxisChange, AxisBulkChange
)
from .rom_definition import Table

logger = logging.getLogger(__name__)

MAX_UNDO_STACK = 100  # Maximum undo operations to keep


@dataclass
class PendingChanges:
    """Tracks uncommitted changes for a table"""
    table_name: str
    table_address: str
    changes: Dict[tuple, CellChange] = field(default_factory=dict)  # (row, col) -> CellChange

    def add_change(self, change: CellChange):
        """Add or update a cell change"""
        key = (change.row, change.col)
        if key in self.changes:
            # Update existing change, keeping original old_value
            existing = self.changes[key]
            self.changes[key] = CellChange(
                table_name=change.table_name,
                table_address=change.table_address,
                row=change.row,
                col=change.col,
                old_value=existing.old_value,  # Keep original
                new_value=change.new_value,
                old_raw=existing.old_raw,
                new_raw=change.new_raw
            )
        else:
            self.changes[key] = change

    def remove_change(self, row: int, col: int):
        """Remove a change if value reverted to original"""
        key = (row, col)
        if key in self.changes:
            del self.changes[key]

    def has_changes(self) -> bool:
        return len(self.changes) > 0

    def to_table_changes(self) -> TableChanges:
        """Convert to TableChanges for committing"""
        return TableChanges(
            table_name=self.table_name,
            table_address=self.table_address,
            cell_changes=list(self.changes.values())
        )


class ChangeTracker:
    """
    Tracks all pending changes and manages undo/redo functionality.

    - Per-cell undo/redo with Ctrl+Z / Ctrl+Y
    - Aggregates changes by table for commit
    - In-memory only (doesn't persist between sessions)
    """

    def __init__(self):
        # Pending changes by table name
        self._pending: Dict[str, PendingChanges] = {}

        # Global undo/redo stacks (across all tables)
        # Can hold single changes, bulk changes, axis changes, or axis bulk changes
        self._undo_stack: deque[Union[UndoableChange, BulkChange, UndoableAxisChange, AxisBulkChange]] = deque(maxlen=MAX_UNDO_STACK)
        self._redo_stack: deque[Union[UndoableChange, BulkChange, UndoableAxisChange, AxisBulkChange]] = deque(maxlen=MAX_UNDO_STACK)

        # Callbacks for UI updates
        self._change_callbacks: List[Callable] = []

    def record_change(self, table: Table, row: int, col: int,
                      old_value: float, new_value: float,
                      old_raw: float, new_raw: float):
        """
        Record a cell value change

        Args:
            table: Table being edited
            row: Row index
            col: Column index
            old_value: Previous display value
            new_value: New display value
            old_raw: Previous raw binary value
            new_raw: New raw binary value
        """
        change = CellChange(
            table_name=table.name,
            table_address=table.address,
            row=row,
            col=col,
            old_value=old_value,
            new_value=new_value,
            old_raw=old_raw,
            new_raw=new_raw
        )

        # Add to pending changes
        if table.name not in self._pending:
            self._pending[table.name] = PendingChanges(
                table_name=table.name,
                table_address=table.address
            )
        self._pending[table.name].add_change(change)

        # Add to undo stack
        self._undo_stack.append(UndoableChange(cell_change=change))

        # Clear redo stack (new change invalidates redo history)
        self._redo_stack.clear()

        # Notify listeners
        self._notify_change()

        logger.debug(f"Recorded change: {table.name}[{row},{col}] {old_value} -> {new_value}")

    def record_bulk_changes(self, table: Table, changes: List[tuple], description: str):
        """
        Record multiple cell changes as a single undo operation

        Args:
            table: Table being edited
            changes: List of (row, col, old_value, new_value, old_raw, new_raw) tuples
            description: Description of the operation (e.g., "Multiply by 1.1")
        """
        if not changes:
            return

        cell_changes = []
        for row, col, old_value, new_value, old_raw, new_raw in changes:
            change = CellChange(
                table_name=table.name,
                table_address=table.address,
                row=row,
                col=col,
                old_value=old_value,
                new_value=new_value,
                old_raw=old_raw,
                new_raw=new_raw
            )
            cell_changes.append(change)

            # Add to pending changes
            if table.name not in self._pending:
                self._pending[table.name] = PendingChanges(
                    table_name=table.name,
                    table_address=table.address
                )
            self._pending[table.name].add_change(change)

        # Add as single bulk change to undo stack
        bulk_change = BulkChange(
            changes=cell_changes,
            description=description
        )
        self._undo_stack.append(bulk_change)

        # Clear redo stack (new change invalidates redo history)
        self._redo_stack.clear()

        # Notify listeners
        self._notify_change()

        logger.debug(f"Recorded bulk change: {description} ({len(changes)} cells in {table.name})")

    def record_axis_change(self, table: Table, axis_type: str, index: int,
                           old_value: float, new_value: float,
                           old_raw: float, new_raw: float):
        """
        Record an axis value change

        Args:
            table: Table being edited
            axis_type: 'x_axis' or 'y_axis'
            index: Index in the axis array
            old_value: Previous display value
            new_value: New display value
            old_raw: Previous raw binary value
            new_raw: New raw binary value
        """
        change = AxisChange(
            table_name=table.name,
            table_address=table.address,
            axis_type=axis_type,
            index=index,
            old_value=old_value,
            new_value=new_value,
            old_raw=old_raw,
            new_raw=new_raw
        )

        # Add to undo stack
        self._undo_stack.append(UndoableAxisChange(axis_change=change))

        # Clear redo stack (new change invalidates redo history)
        self._redo_stack.clear()

        # Notify listeners
        self._notify_change()

        logger.debug(f"Recorded axis change: {table.name}[{axis_type}][{index}] {old_value} -> {new_value}")

    def record_axis_bulk_changes(self, table: Table, changes: List[tuple], description: str):
        """
        Record multiple axis changes as a single undo operation

        Args:
            table: Table being edited
            changes: List of (axis_type, index, old_value, new_value, old_raw, new_raw) tuples
            description: Description of the operation (e.g., "Interpolate Y-Axis")
        """
        if not changes:
            return

        axis_changes = []
        for axis_type, index, old_value, new_value, old_raw, new_raw in changes:
            change = AxisChange(
                table_name=table.name,
                table_address=table.address,
                axis_type=axis_type,
                index=index,
                old_value=old_value,
                new_value=new_value,
                old_raw=old_raw,
                new_raw=new_raw
            )
            axis_changes.append(change)

        # Add as single bulk change to undo stack
        bulk_change = AxisBulkChange(
            changes=axis_changes,
            description=description
        )
        self._undo_stack.append(bulk_change)

        # Clear redo stack (new change invalidates redo history)
        self._redo_stack.clear()

        # Notify listeners
        self._notify_change()

        logger.debug(f"Recorded axis bulk change: {description} ({len(changes)} cells in {table.name})")

    def undo(self) -> Optional[Union[CellChange, List[CellChange], AxisChange, List[AxisChange]]]:
        """
        Undo the last change (single or bulk, cell or axis)

        Returns:
            The change(s) that was undone (with old/new swapped), or None if stack empty
            Returns single CellChange/AxisChange for single undo, List for bulk undo
        """
        if not self._undo_stack:
            return None

        item = self._undo_stack.pop()

        # Handle axis bulk changes
        if isinstance(item, AxisBulkChange):
            reversed_changes = []
            for change in item.changes:
                # Create reverse change
                reverse = AxisChange(
                    table_name=change.table_name,
                    table_address=change.table_address,
                    axis_type=change.axis_type,
                    index=change.index,
                    old_value=change.new_value,  # Swap old/new
                    new_value=change.old_value,
                    old_raw=change.new_raw,
                    new_raw=change.old_raw
                )
                reversed_changes.append(reverse)

            # Push reversed bulk to redo stack
            reverse_bulk = AxisBulkChange(
                changes=reversed_changes,
                description=item.description
            )
            self._redo_stack.append(reverse_bulk)

            self._notify_change()
            logger.debug(f"Undo axis bulk: {item.description} ({len(item.changes)} cells)")

            return reversed_changes

        # Handle cell bulk changes
        if isinstance(item, BulkChange):
            reversed_changes = []
            for change in item.changes:
                # Create reverse change
                reverse = CellChange(
                    table_name=change.table_name,
                    table_address=change.table_address,
                    row=change.row,
                    col=change.col,
                    old_value=change.new_value,  # Swap old/new
                    new_value=change.old_value,
                    old_raw=change.new_raw,
                    new_raw=change.old_raw
                )
                reversed_changes.append(reverse)

                # Update pending changes
                self._update_pending_for_undo(change)

            # Push reversed bulk to redo stack
            reverse_bulk = BulkChange(
                changes=reversed_changes,
                description=item.description
            )
            self._redo_stack.append(reverse_bulk)

            self._notify_change()
            logger.debug(f"Undo bulk: {item.description} ({len(item.changes)} cells)")

            return reversed_changes

        # Handle single axis changes
        if isinstance(item, UndoableAxisChange):
            change = item.axis_change

            # Create reverse change for redo
            reverse = AxisChange(
                table_name=change.table_name,
                table_address=change.table_address,
                axis_type=change.axis_type,
                index=change.index,
                old_value=change.new_value,  # Swap old/new
                new_value=change.old_value,
                old_raw=change.new_raw,
                new_raw=change.old_raw
            )

            # Push to redo stack
            self._redo_stack.append(UndoableAxisChange(axis_change=reverse))

            self._notify_change()
            logger.debug(f"Undo axis: {change.table_name}[{change.axis_type}][{change.index}]")

            return reverse

        # Handle single cell changes
        else:
            change = item.cell_change

            # Create reverse change for redo
            reverse = CellChange(
                table_name=change.table_name,
                table_address=change.table_address,
                row=change.row,
                col=change.col,
                old_value=change.new_value,  # Swap old/new
                new_value=change.old_value,
                old_raw=change.new_raw,
                new_raw=change.old_raw
            )

            # Push to redo stack
            self._redo_stack.append(UndoableChange(cell_change=reverse))

            # Update pending changes
            self._update_pending_for_undo(change)

            self._notify_change()
            logger.debug(f"Undo: {change.table_name}[{change.row},{change.col}]")

            return reverse

    def redo(self) -> Optional[Union[CellChange, List[CellChange], AxisChange, List[AxisChange]]]:
        """
        Redo the last undone change (single or bulk, cell or axis)

        Returns:
            The change(s) that was redone, or None if stack empty
            Returns single CellChange/AxisChange for single redo, List for bulk redo
        """
        if not self._redo_stack:
            return None

        item = self._redo_stack.pop()

        # Handle axis bulk changes
        if isinstance(item, AxisBulkChange):
            reversed_changes = []
            for change in item.changes:
                # Create reverse for undo again
                reverse = AxisChange(
                    table_name=change.table_name,
                    table_address=change.table_address,
                    axis_type=change.axis_type,
                    index=change.index,
                    old_value=change.new_value,
                    new_value=change.old_value,
                    old_raw=change.new_raw,
                    new_raw=change.old_raw
                )
                reversed_changes.append(reverse)

            # Push reversed bulk back to undo stack
            reverse_bulk = AxisBulkChange(
                changes=reversed_changes,
                description=item.description
            )
            self._undo_stack.append(reverse_bulk)

            self._notify_change()
            logger.debug(f"Redo axis bulk: {item.description} ({len(item.changes)} cells)")

            return reversed_changes

        # Handle cell bulk changes
        if isinstance(item, BulkChange):
            reversed_changes = []
            for change in item.changes:
                # Create reverse for undo again
                reverse = CellChange(
                    table_name=change.table_name,
                    table_address=change.table_address,
                    row=change.row,
                    col=change.col,
                    old_value=change.new_value,
                    new_value=change.old_value,
                    old_raw=change.new_raw,
                    new_raw=change.old_raw
                )
                reversed_changes.append(reverse)

                # Re-apply to pending
                if change.table_name in self._pending:
                    reapply = CellChange(
                        table_name=change.table_name,
                        table_address=change.table_address,
                        row=change.row,
                        col=change.col,
                        old_value=change.old_value,
                        new_value=change.new_value,
                        old_raw=change.old_raw,
                        new_raw=change.new_raw
                    )
                    self._pending[change.table_name].add_change(reapply)

            # Push reversed bulk back to undo stack
            reverse_bulk = BulkChange(
                changes=reversed_changes,
                description=item.description
            )
            self._undo_stack.append(reverse_bulk)

            self._notify_change()
            logger.debug(f"Redo bulk: {item.description} ({len(item.changes)} cells)")

            return reversed_changes

        # Handle single axis changes
        if isinstance(item, UndoableAxisChange):
            change = item.axis_change

            # Create reverse for undo again
            reverse = AxisChange(
                table_name=change.table_name,
                table_address=change.table_address,
                axis_type=change.axis_type,
                index=change.index,
                old_value=change.new_value,
                new_value=change.old_value,
                old_raw=change.new_raw,
                new_raw=change.old_raw
            )

            # Push back to undo stack
            self._undo_stack.append(UndoableAxisChange(axis_change=reverse))

            self._notify_change()
            logger.debug(f"Redo axis: {change.table_name}[{change.axis_type}][{change.index}]")

            return reverse

        # Handle single cell changes
        else:
            change = item.cell_change

            # Create reverse for undo again
            reverse = CellChange(
                table_name=change.table_name,
                table_address=change.table_address,
                row=change.row,
                col=change.col,
                old_value=change.new_value,
                new_value=change.old_value,
                old_raw=change.new_raw,
                new_raw=change.old_raw
            )

            # Push back to undo stack
            self._undo_stack.append(UndoableChange(cell_change=reverse))

            # Update pending changes
            if change.table_name in self._pending:
                # Re-apply the change
                reapply = CellChange(
                    table_name=change.table_name,
                    table_address=change.table_address,
                    row=change.row,
                    col=change.col,
                    old_value=change.old_value,
                    new_value=change.new_value,
                    old_raw=change.old_raw,
                    new_raw=change.new_raw
                )
                self._pending[change.table_name].add_change(reapply)

            self._notify_change()
            logger.debug(f"Redo: {change.table_name}[{change.row},{change.col}]")

            return reverse

    def _update_pending_for_undo(self, change: CellChange):
        """Update pending changes when undoing"""
        if change.table_name not in self._pending:
            return

        pending = self._pending[change.table_name]
        key = (change.row, change.col)

        if key in pending.changes:
            existing = pending.changes[key]
            # Check if reverting to original value
            if abs(existing.old_value - change.old_value) < 1e-9:
                # Reverted to original, remove from pending
                pending.remove_change(change.row, change.col)
            else:
                # Update to previous value
                pending.changes[key] = CellChange(
                    table_name=change.table_name,
                    table_address=change.table_address,
                    row=change.row,
                    col=change.col,
                    old_value=existing.old_value,
                    new_value=change.old_value,  # Now the "current" value
                    old_raw=existing.old_raw,
                    new_raw=change.old_raw
                )

    def has_pending_changes(self) -> bool:
        """Check if there are any uncommitted changes"""
        return any(p.has_changes() for p in self._pending.values())

    def get_pending_changes(self) -> List[TableChanges]:
        """Get all pending changes grouped by table"""
        return [p.to_table_changes() for p in self._pending.values() if p.has_changes()]

    def get_modified_tables(self) -> List[str]:
        """Get list of tables with pending changes"""
        return [name for name, p in self._pending.items() if p.has_changes()]

    def get_pending_change_count(self) -> int:
        """Get total number of pending cell changes"""
        return sum(len(p.changes) for p in self._pending.values())

    def clear_pending_changes(self):
        """Clear all pending changes (after commit)"""
        self._pending.clear()
        self._notify_change()
        logger.debug("Cleared pending changes")

    def clear_all(self):
        """Clear all state (pending changes, undo/redo stacks)"""
        self._pending.clear()
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._notify_change()
        logger.debug("Cleared all change tracking state")

    def can_undo(self) -> bool:
        return len(self._undo_stack) > 0

    def can_redo(self) -> bool:
        return len(self._redo_stack) > 0

    def add_change_callback(self, callback: Callable):
        """Register a callback for change notifications"""
        self._change_callbacks.append(callback)

    def remove_change_callback(self, callback: Callable):
        """Remove a callback"""
        if callback in self._change_callbacks:
            self._change_callbacks.remove(callback)

    def _notify_change(self):
        """Notify all registered callbacks"""
        for callback in self._change_callbacks:
            try:
                callback()
            except Exception as e:
                logger.error(f"Change callback error: {e}")
