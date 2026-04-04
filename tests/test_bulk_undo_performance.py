"""
Regression test for bulk undo/redo performance.

Verifies that _update_project_ui() is called exactly ONCE during a bulk undo,
not once per cell. This was a critical performance bug where a 400-cell undo
caused 400 tree browser repaints (80,000 item updates with 200 tables).

Root cause: change_tracker._notify_change() fired _on_changes_updated() callback
for every cell during bulk undo, bypassing the _in_bulk_undo guard. The guard
only blocked the direct call path, not the callback path.

Fix: Added _in_bulk_undo guard to _on_changes_updated() and removed redundant
direct _update_project_ui() call from _update_pending_from_undo().
"""

import pytest
from PySide6.QtWidgets import QApplication
from src.core.table_undo_manager import TableUndoManager, make_table_key
from src.core.change_tracker import ChangeTracker
from src.core.version_models import CellChange
from src.core.rom_definition import Table, TableType


@pytest.fixture(scope="module")
def qapp():
    """Create QApplication for Qt tests"""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def table():
    return Table(
        name="Test Table",
        address="0x1000",
        type=TableType.TWO_D,
        elements=20,
        scaling="TestScaling",
    )


class FakeMainWindow:
    """
    Simulates main.py's callback structure for undo/redo.

    Mirrors the exact callback wiring from MainWindow:
    - _apply_cell_change_from_undo (apply_cell callback)
    - _update_pending_from_undo (update_pending callback)
    - _on_changes_updated (change_tracker callback)
    - _begin_bulk_update / _end_bulk_update (bulk callbacks)
    """

    def __init__(self):
        self.change_tracker = ChangeTracker()
        self.undo_manager = TableUndoManager()

        # Track call counts for assertions
        self.update_project_ui_calls = 0

        # Wire callbacks exactly like MainWindow.__init__
        self.change_tracker.add_change_callback(self._on_changes_updated)
        self.undo_manager.set_callbacks(
            apply_cell=self._apply_cell_change_from_undo,
            apply_axis=lambda change: None,
            update_pending=self._update_pending_from_undo,
            begin_bulk_update=self._begin_bulk_update,
            end_bulk_update=self._end_bulk_update,
        )

    def _apply_cell_change_from_undo(self, change: CellChange):
        """Simulates main.py _apply_cell_change_from_undo (ROM write + UI update)"""
        pass  # ROM write and viewer update are irrelevant for this test

    def _update_pending_from_undo(self, change: CellChange, is_undo: bool):
        """Mirrors main.py _update_pending_from_undo exactly"""
        # This fires _notify_change() which fires _on_changes_updated()
        self.change_tracker.update_pending_from_undo(change, is_undo)
        # NO direct _update_project_ui() call here — covered by callback

    def _on_changes_updated(self):
        """Mirrors main.py _on_changes_updated exactly"""
        if not getattr(self, "_in_bulk_undo", False):
            self._update_project_ui()

    def _begin_bulk_update(self, table_address=None):
        """Mirrors main.py _begin_bulk_update"""
        self._in_bulk_undo = True

    def _end_bulk_update(self, table_address=None):
        """Mirrors main.py _end_bulk_update"""
        self._in_bulk_undo = False
        self._update_project_ui()

    def _update_project_ui(self):
        """The expensive call we're counting"""
        self.update_project_ui_calls += 1


def test_bulk_undo_calls_update_project_ui_once(qapp, table):
    """
    REGRESSION: Bulk undo must call _update_project_ui exactly once.

    Before the fix, a 400-cell bulk undo called _update_project_ui 401 times
    (once per cell via _notify_change callback + once at end_bulk_update).
    Each call iterated ALL tree browser items, causing massive slowdown.
    """
    window = FakeMainWindow()

    # Record initial changes so change_tracker has pending state
    changes = []
    for i in range(50):
        changes.append((i, 0, float(i), float(i + 10), i, i + 10))
        # Also record in change_tracker (mirrors _on_table_bulk_changes)
        window.change_tracker.record_pending_change(
            table, i, 0, float(i), float(i + 10), i, i + 10
        )

    # Record bulk in undo manager
    window.undo_manager.record_bulk_cell_changes(table, changes, "Test Bulk Op")
    window.undo_manager.set_active_stack(make_table_key(None, table.address))

    # Reset counter before undo
    window.update_project_ui_calls = 0

    # Perform bulk undo
    window.undo_manager.undo_group.undo()

    # CRITICAL: Must be exactly 1, not 50 or 51
    assert window.update_project_ui_calls == 1, (
        f"_update_project_ui called {window.update_project_ui_calls} times during "
        f"bulk undo of 50 cells. Expected exactly 1. "
        f"If this fails, the _in_bulk_undo guard on _on_changes_updated is broken."
    )


def test_bulk_redo_calls_update_project_ui_once(qapp, table):
    """Same regression test for redo direction."""
    window = FakeMainWindow()

    changes = []
    for i in range(50):
        changes.append((i, 0, float(i), float(i + 10), i, i + 10))
        window.change_tracker.record_pending_change(
            table, i, 0, float(i), float(i + 10), i, i + 10
        )

    window.undo_manager.record_bulk_cell_changes(table, changes, "Test Bulk Op")
    window.undo_manager.set_active_stack(make_table_key(None, table.address))

    # Undo first
    window.undo_manager.undo_group.undo()

    # Reset counter before redo
    window.update_project_ui_calls = 0

    # Perform bulk redo
    window.undo_manager.undo_group.redo()

    assert window.update_project_ui_calls == 1, (
        f"_update_project_ui called {window.update_project_ui_calls} times during "
        f"bulk redo. Expected exactly 1."
    )


def test_single_cell_undo_calls_update_project_ui_once(qapp, table):
    """Single cell undo should also call _update_project_ui exactly once."""
    window = FakeMainWindow()

    window.change_tracker.record_pending_change(table, 0, 0, 10.0, 20.0, 100, 200)
    window.undo_manager.record_cell_change(table, 0, 0, 10.0, 20.0, 100, 200)
    window.undo_manager.set_active_stack(make_table_key(None, table.address))

    window.update_project_ui_calls = 0
    window.undo_manager.undo_group.undo()

    assert window.update_project_ui_calls == 1, (
        f"_update_project_ui called {window.update_project_ui_calls} times for "
        f"single cell undo. Expected exactly 1."
    )
