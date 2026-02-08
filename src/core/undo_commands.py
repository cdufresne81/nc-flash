"""
Qt Undo Commands for Per-Table Undo/Redo

Wraps existing change models (CellChange, AxisChange, etc.) in QUndoCommand
subclasses for integration with Qt's undo framework.
"""

from PySide6.QtGui import QUndoCommand
from typing import List, Callable, Optional

from .version_models import CellChange, AxisChange


class CellEditCommand(QUndoCommand):
    """Undo command for single cell edits"""

    def __init__(
        self,
        change: CellChange,
        apply_callback: Callable[[CellChange], None],
        update_pending_callback: Optional[Callable[[CellChange, bool], None]] = None,
    ):
        """
        Args:
            change: CellChange with old and new values
            apply_callback: Function to apply change to ROM/UI
            update_pending_callback: Function to update pending changes tracking
        """
        super().__init__(f"Edit {change.table_name}[{change.row},{change.col}]")
        self.change = change
        self.apply_callback = apply_callback
        self.update_pending = update_pending_callback
        self._first_redo = True  # Skip first redo since change already applied

    def undo(self):
        """Revert cell to old value"""
        reverse = CellChange(
            table_name=self.change.table_name,
            table_address=self.change.table_address,
            row=self.change.row,
            col=self.change.col,
            old_value=self.change.new_value,
            new_value=self.change.old_value,
            old_raw=self.change.new_raw,
            new_raw=self.change.old_raw,
            table_key=self.change.table_key,
        )
        self.apply_callback(reverse)
        if self.update_pending:
            self.update_pending(self.change, True)

    def redo(self):
        """Apply cell change (new value)"""
        if self._first_redo:
            # Skip first redo - change was already applied when command was created
            self._first_redo = False
            return
        self.apply_callback(self.change)
        if self.update_pending:
            self.update_pending(self.change, False)


class BulkCellEditCommand(QUndoCommand):
    """Undo command for bulk cell edits (interpolation, multiply, etc.)"""

    def __init__(
        self,
        changes: List[CellChange],
        description: str,
        apply_callback: Callable[[CellChange], None],
        update_pending_callback: Optional[Callable[[CellChange, bool], None]] = None,
        begin_bulk_callback: Optional[Callable[[], None]] = None,
        end_bulk_callback: Optional[Callable[[], None]] = None,
    ):
        super().__init__(description)
        self.changes = changes
        self.apply_callback = apply_callback
        self.update_pending = update_pending_callback
        self.begin_bulk = begin_bulk_callback
        self.end_bulk = end_bulk_callback
        self._first_redo = True

    def undo(self):
        """Revert all cells to old values"""
        # Use table_key for targeted bulk update
        table_key = self.changes[0].table_key if self.changes else None

        # Begin bulk update for performance (if callback provided)
        if self.begin_bulk:
            self.begin_bulk(table_key)

        try:
            for change in self.changes:
                reverse = CellChange(
                    table_name=change.table_name,
                    table_address=change.table_address,
                    row=change.row,
                    col=change.col,
                    old_value=change.new_value,
                    new_value=change.old_value,
                    old_raw=change.new_raw,
                    new_raw=change.old_raw,
                    table_key=change.table_key,
                )
                self.apply_callback(reverse)
                if self.update_pending:
                    self.update_pending(change, True)
        finally:
            # End bulk update (if callback provided)
            if self.end_bulk:
                self.end_bulk(table_key)

    def redo(self):
        """Apply all changes"""
        if self._first_redo:
            self._first_redo = False
            return

        # Use table_key for targeted bulk update
        table_key = self.changes[0].table_key if self.changes else None

        # Begin bulk update for performance (if callback provided)
        if self.begin_bulk:
            self.begin_bulk(table_key)

        try:
            for change in self.changes:
                self.apply_callback(change)
                if self.update_pending:
                    self.update_pending(change, False)
        finally:
            # End bulk update (if callback provided)
            if self.end_bulk:
                self.end_bulk(table_key)


class AxisEditCommand(QUndoCommand):
    """Undo command for single axis edits"""

    def __init__(
        self,
        change: AxisChange,
        apply_callback: Callable[[AxisChange], None],
        update_pending_callback: Optional[Callable[[AxisChange, bool], None]] = None,
    ):
        super().__init__(
            f"Edit {change.table_name} {change.axis_type}[{change.index}]"
        )
        self.change = change
        self.apply_callback = apply_callback
        self.update_pending = update_pending_callback
        self._first_redo = True

    def undo(self):
        reverse = AxisChange(
            table_name=self.change.table_name,
            table_address=self.change.table_address,
            axis_type=self.change.axis_type,
            index=self.change.index,
            old_value=self.change.new_value,
            new_value=self.change.old_value,
            old_raw=self.change.new_raw,
            new_raw=self.change.old_raw,
            table_key=self.change.table_key,
        )
        self.apply_callback(reverse)
        if self.update_pending:
            self.update_pending(self.change, True)

    def redo(self):
        if self._first_redo:
            self._first_redo = False
            return
        self.apply_callback(self.change)
        if self.update_pending:
            self.update_pending(self.change, False)


class BulkAxisEditCommand(QUndoCommand):
    """Undo command for bulk axis edits (axis interpolation)"""

    def __init__(
        self,
        changes: List[AxisChange],
        description: str,
        apply_callback: Callable[[AxisChange], None],
        update_pending_callback: Optional[Callable[[AxisChange, bool], None]] = None,
        begin_bulk_callback: Optional[Callable[[], None]] = None,
        end_bulk_callback: Optional[Callable[[], None]] = None,
    ):
        super().__init__(description)
        self.changes = changes
        self.apply_callback = apply_callback
        self.update_pending = update_pending_callback
        self.begin_bulk = begin_bulk_callback
        self.end_bulk = end_bulk_callback
        self._first_redo = True

    def undo(self):
        """Revert all axis cells to old values"""
        # Use table_key for targeted bulk update
        table_key = self.changes[0].table_key if self.changes else None

        # Begin bulk update for performance (if callback provided)
        if self.begin_bulk:
            self.begin_bulk(table_key)

        try:
            for change in self.changes:
                reverse = AxisChange(
                    table_name=change.table_name,
                    table_address=change.table_address,
                    axis_type=change.axis_type,
                    index=change.index,
                    old_value=change.new_value,
                    new_value=change.old_value,
                    old_raw=change.new_raw,
                    new_raw=change.old_raw,
                    table_key=change.table_key,
                )
                self.apply_callback(reverse)
                if self.update_pending:
                    self.update_pending(change, True)
        finally:
            # End bulk update (if callback provided)
            if self.end_bulk:
                self.end_bulk(table_key)

    def redo(self):
        """Apply all axis changes"""
        if self._first_redo:
            self._first_redo = False
            return

        # Use table_key for targeted bulk update
        table_key = self.changes[0].table_key if self.changes else None

        # Begin bulk update for performance (if callback provided)
        if self.begin_bulk:
            self.begin_bulk(table_key)

        try:
            for change in self.changes:
                self.apply_callback(change)
                if self.update_pending:
                    self.update_pending(change, False)
        finally:
            # End bulk update (if callback provided)
            if self.end_bulk:
                self.end_bulk(table_key)
