"""
Change Tracker

Tracks pending (uncommitted) changes for the commit/save workflow.

Note: Undo/redo functionality has been moved to TableUndoManager (table_undo_manager.py)
which uses Qt's QUndoGroup pattern for per-table undo/redo.

Keys are composite (rom_path\0table_address) to isolate tracking
when multiple ROMs share the same table addresses.
"""

from typing import List, Dict, Callable
from dataclasses import dataclass, field
import logging

from .version_models import CellChange, AxisChange, TableChanges
from .rom_definition import Table
from .table_undo_manager import make_table_key, extract_table_address, extract_rom_path

logger = logging.getLogger(__name__)

# Sentinel col values to distinguish axis changes stored as CellChange
AXIS_COL_Y = -1
AXIS_COL_X = -2


def _axis_type_to_col(axis_type: str) -> int:
    """Encode axis type as a special column value for storage in CellChange."""
    return AXIS_COL_Y if axis_type == 'y_axis' else AXIS_COL_X


@dataclass
class PendingChanges:
    """Tracks uncommitted changes for a table"""
    table_name: str
    table_address: str  # Raw hex address (for ROM I/O and display)
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
                new_raw=change.new_raw,
                table_key=change.table_key,
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

    Keys are composite (rom_path\0table_address) to isolate per-ROM state.

    Note: Undo/redo functionality is handled by TableUndoManager.
    """

    def __init__(self):
        # Pending changes by composite key (rom_path\0table_address)
        self._pending: Dict[str, PendingChanges] = {}

        # Callbacks for UI updates
        self._change_callbacks: List[Callable] = []

    def record_pending_change(self, table: Table, row: int, col: int,
                              old_value: float, new_value: float,
                              old_raw: float, new_raw: float,
                              rom_path=None):
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
            rom_path: Path to ROM file for multi-ROM isolation
        """
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

        # Add to pending changes (keyed by composite key for multi-ROM isolation)
        if table_key not in self._pending:
            self._pending[table_key] = PendingChanges(
                table_name=table.name,
                table_address=table.address
            )
        self._pending[table_key].add_change(change)

        # Notify listeners
        self._notify_change()

        logger.debug(f"Recorded pending change: {table.name}[{row},{col}] {old_value} -> {new_value}")

    def record_pending_bulk_changes(self, table: Table, changes: List[tuple],
                                    rom_path=None):
        """
        Record multiple cell changes for pending/commit tracking.

        Args:
            table: Table being edited
            changes: List of (row, col, old_value, new_value, old_raw, new_raw) tuples
            rom_path: Path to ROM file for multi-ROM isolation
        """
        if not changes:
            return

        table_key = make_table_key(rom_path, table.address)

        for row, col, old_value, new_value, old_raw, new_raw in changes:
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

            # Add to pending changes (keyed by composite key)
            if table_key not in self._pending:
                self._pending[table_key] = PendingChanges(
                    table_name=table.name,
                    table_address=table.address
                )
            self._pending[table_key].add_change(change)

        # Notify listeners
        self._notify_change()

        logger.debug(f"Recorded {len(changes)} pending bulk changes in {table.name}")

    def record_pending_axis_change(self, table: Table, axis_type: str, index: int,
                                   old_value: float, new_value: float,
                                   old_raw: float, new_raw: float,
                                   rom_path=None):
        """
        Record an axis value change for pending/commit tracking.

        Axis changes are stored as CellChange with special col encoding
        (AXIS_COL_Y=-1 for y_axis, AXIS_COL_X=-2 for x_axis) so that
        existing has_changes() and get_modified_addresses_for_rom() work
        without modification.
        """
        table_key = make_table_key(rom_path, table.address)
        col = _axis_type_to_col(axis_type)

        change = CellChange(
            table_name=table.name,
            table_address=table.address,
            row=index,
            col=col,
            old_value=old_value,
            new_value=new_value,
            old_raw=old_raw,
            new_raw=new_raw,
            table_key=table_key,
        )

        if table_key not in self._pending:
            self._pending[table_key] = PendingChanges(
                table_name=table.name,
                table_address=table.address
            )
        self._pending[table_key].add_change(change)
        self._notify_change()

        logger.debug(f"Recorded pending axis change: {table.name}[{axis_type}][{index}] {old_value} -> {new_value}")

    def record_pending_axis_bulk_changes(self, table: Table, changes: List[tuple],
                                         rom_path=None):
        """
        Record multiple axis changes for pending/commit tracking.

        Args:
            table: Table being edited
            changes: List of (axis_type, index, old_value, new_value, old_raw, new_raw) tuples
            rom_path: Path to ROM file for multi-ROM isolation
        """
        if not changes:
            return

        table_key = make_table_key(rom_path, table.address)

        for axis_type, index, old_value, new_value, old_raw, new_raw in changes:
            col = _axis_type_to_col(axis_type)
            change = CellChange(
                table_name=table.name,
                table_address=table.address,
                row=index,
                col=col,
                old_value=old_value,
                new_value=new_value,
                old_raw=old_raw,
                new_raw=new_raw,
                table_key=table_key,
            )

            if table_key not in self._pending:
                self._pending[table_key] = PendingChanges(
                    table_name=table.name,
                    table_address=table.address
                )
            self._pending[table_key].add_change(change)

        self._notify_change()
        logger.debug(f"Recorded {len(changes)} pending axis bulk changes in {table.name}")

    def update_pending_from_axis_undo(self, change: AxisChange, is_undo: bool):
        """
        Update pending changes during axis undo/redo operations.

        Converts the AxisChange to CellChange encoding and delegates
        to the standard update_pending_from_undo logic.
        """
        col = _axis_type_to_col(change.axis_type)
        cell_change = CellChange(
            table_name=change.table_name,
            table_address=change.table_address,
            row=change.index,
            col=col,
            old_value=change.old_value,
            new_value=change.new_value,
            old_raw=change.old_raw,
            new_raw=change.new_raw,
            table_key=change.table_key,
        )
        self.update_pending_from_undo(cell_change, is_undo)

    def update_pending_from_undo(self, change: CellChange, is_undo: bool):
        """
        Update pending changes during undo/redo operations.

        Called by TableUndoManager to keep pending changes in sync with undo/redo.
        Uses change.table_key (composite key) to find the correct pending entry.

        Args:
            change: The CellChange being undone/redone
            is_undo: True if this is an undo operation, False for redo
        """
        if is_undo:
            self._handle_pending_undo(change)
        else:
            self._handle_pending_redo(change)

        self._notify_change()

    def _get_pending_key(self, change: CellChange) -> str:
        """Get the correct pending dict key from a CellChange."""
        return change.table_key if change.table_key else change.table_address

    def _handle_pending_undo(self, change: CellChange):
        """Handle pending changes update during undo"""
        key = self._get_pending_key(change)
        if key not in self._pending:
            return

        pending = self._pending[key]
        cell_key = (change.row, change.col)

        if cell_key in pending.changes:
            existing = pending.changes[cell_key]
            # Check if reverting to original value
            if abs(existing.old_value - change.old_value) < 1e-9:
                # Reverted to original, remove from pending
                pending.remove_change(change.row, change.col)
            else:
                # Update to previous value
                pending.changes[cell_key] = CellChange(
                    table_name=change.table_name,
                    table_address=change.table_address,
                    row=change.row,
                    col=change.col,
                    old_value=existing.old_value,
                    new_value=change.old_value,  # Now the "current" value
                    old_raw=existing.old_raw,
                    new_raw=change.old_raw,
                    table_key=change.table_key,
                )

    def _handle_pending_redo(self, change: CellChange):
        """Handle pending changes update during redo"""
        key = self._get_pending_key(change)
        # Re-apply the change to pending
        if key not in self._pending:
            self._pending[key] = PendingChanges(
                table_name=change.table_name,
                table_address=change.table_address
            )
        self._pending[key].add_change(change)

    def has_pending_changes(self) -> bool:
        """Check if there are any uncommitted changes"""
        return any(p.has_changes() for p in self._pending.values())

    def get_pending_changes(self) -> List[TableChanges]:
        """Get all pending changes grouped by table"""
        return [p.to_table_changes() for p in self._pending.values() if p.has_changes()]

    def get_modified_tables(self) -> List[str]:
        """Get list of table names with pending changes (deprecated - use get_modified_addresses_for_rom)"""
        return [p.table_name for p in self._pending.values() if p.has_changes()]

    def get_modified_table_addresses(self) -> List[str]:
        """Get list of raw table addresses with pending changes (all ROMs combined)."""
        return [p.table_address for p in self._pending.values() if p.has_changes()]

    def get_modified_addresses_for_rom(self, rom_path) -> List[str]:
        """Get list of raw table addresses with pending changes for a specific ROM.

        Args:
            rom_path: ROM path to filter by

        Returns:
            List of raw hex addresses (e.g., ["0x1000", "0x2000"])
        """
        rom_path_str = str(rom_path)
        result = []
        for key, pending in self._pending.items():
            if pending.has_changes():
                key_rom_path = extract_rom_path(key)
                if key_rom_path == rom_path_str:
                    result.append(pending.table_address)
        return result

    def get_pending_change_count(self) -> int:
        """Get total number of pending cell changes"""
        return sum(len(p.changes) for p in self._pending.values())

    def clear_pending_changes(self):
        """Clear all pending changes (after commit)"""
        self._pending.clear()
        self._notify_change()
        logger.debug("Cleared pending changes")

    def clear_pending_for_keys(self, keys):
        """Clear pending changes for specific composite keys (e.g., when closing a ROM)."""
        removed = 0
        for key in keys:
            if key in self._pending:
                del self._pending[key]
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
