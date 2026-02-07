"""
Change Tracker

Tracks pending (uncommitted) changes for the commit/save workflow.

Note: Undo/redo functionality has been moved to TableUndoManager (table_undo_manager.py)
which uses Qt's QUndoGroup pattern for per-table undo/redo.
"""

from typing import List, Dict, Callable
from dataclasses import dataclass, field
import logging

from .version_models import CellChange, TableChanges
from .rom_definition import Table

logger = logging.getLogger(__name__)


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
    Tracks pending (uncommitted) changes for the commit/save workflow.

    This class manages:
    - Pending changes that need to be saved/committed
    - Change notifications for UI updates

    Note: Undo/redo functionality is handled by TableUndoManager.
    """

    def __init__(self):
        # Pending changes by table address
        self._pending: Dict[str, PendingChanges] = {}

        # Callbacks for UI updates
        self._change_callbacks: List[Callable] = []

    def record_pending_change(self, table: Table, row: int, col: int,
                              old_value: float, new_value: float,
                              old_raw: float, new_raw: float):
        """
        Record a cell value change for pending/commit tracking.

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

        # Add to pending changes (keyed by address to handle tables with same name)
        if table.address not in self._pending:
            self._pending[table.address] = PendingChanges(
                table_name=table.name,
                table_address=table.address
            )
        self._pending[table.address].add_change(change)

        # Notify listeners
        self._notify_change()

        logger.debug(f"Recorded pending change: {table.name}[{row},{col}] {old_value} -> {new_value}")

    def record_pending_bulk_changes(self, table: Table, changes: List[tuple]):
        """
        Record multiple cell changes for pending/commit tracking.

        Args:
            table: Table being edited
            changes: List of (row, col, old_value, new_value, old_raw, new_raw) tuples
        """
        if not changes:
            return

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

            # Add to pending changes (keyed by address to handle tables with same name)
            if table.address not in self._pending:
                self._pending[table.address] = PendingChanges(
                    table_name=table.name,
                    table_address=table.address
                )
            self._pending[table.address].add_change(change)

        # Notify listeners
        self._notify_change()

        logger.debug(f"Recorded {len(changes)} pending bulk changes in {table.name}")

    def update_pending_from_undo(self, change: CellChange, is_undo: bool):
        """
        Update pending changes during undo/redo operations.

        Called by TableUndoManager to keep pending changes in sync with undo/redo.

        Args:
            change: The CellChange being undone/redone
            is_undo: True if this is an undo operation, False for redo
        """
        if is_undo:
            self._handle_pending_undo(change)
        else:
            self._handle_pending_redo(change)

        self._notify_change()

    def _handle_pending_undo(self, change: CellChange):
        """Handle pending changes update during undo"""
        if change.table_address not in self._pending:
            return

        pending = self._pending[change.table_address]
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

    def _handle_pending_redo(self, change: CellChange):
        """Handle pending changes update during redo"""
        # Re-apply the change to pending
        if change.table_address not in self._pending:
            self._pending[change.table_address] = PendingChanges(
                table_name=change.table_name,
                table_address=change.table_address
            )
        self._pending[change.table_address].add_change(change)

    def has_pending_changes(self) -> bool:
        """Check if there are any uncommitted changes"""
        return any(p.has_changes() for p in self._pending.values())

    def get_pending_changes(self) -> List[TableChanges]:
        """Get all pending changes grouped by table"""
        return [p.to_table_changes() for p in self._pending.values() if p.has_changes()]

    def get_modified_tables(self) -> List[str]:
        """Get list of table names with pending changes (deprecated - use get_modified_table_addresses)"""
        return [p.table_name for p in self._pending.values() if p.has_changes()]

    def get_modified_table_addresses(self) -> List[str]:
        """Get list of table addresses with pending changes"""
        return [p.table_address for p in self._pending.values() if p.has_changes()]

    def get_pending_change_count(self) -> int:
        """Get total number of pending cell changes"""
        return sum(len(p.changes) for p in self._pending.values())

    def clear_pending_changes(self):
        """Clear all pending changes (after commit)"""
        self._pending.clear()
        self._notify_change()
        logger.debug("Cleared pending changes")

    def clear_pending_for_addresses(self, addresses):
        """Clear pending changes for specific table addresses (e.g., when closing a ROM)."""
        removed = 0
        for addr in addresses:
            if addr in self._pending:
                del self._pending[addr]
                removed += 1
        if removed:
            self._notify_change()
            logger.debug(f"Cleared pending changes for {removed} tables")

    def clear_all(self):
        """Clear all state (pending changes)"""
        self._pending.clear()
        self._notify_change()
        logger.debug("Cleared all change tracking state")

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
