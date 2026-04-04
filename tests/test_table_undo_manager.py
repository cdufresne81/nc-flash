"""
Tests for TableUndoManager - per-table undo/redo functionality

These tests verify that undo/redo operations are isolated per-table
using Qt's QUndoGroup pattern.
"""

import pytest
from PySide6.QtWidgets import QApplication
from src.core.table_undo_manager import (
    TableUndoManager,
    MAX_UNDO_PER_TABLE,
    make_table_key,
)
from src.core.version_models import CellChange, AxisChange
from src.core.rom_definition import Table, TableType


@pytest.fixture(scope="module")
def qapp():
    """Create QApplication for Qt tests (required for QUndoStack)"""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def manager(qapp):
    """Create a fresh TableUndoManager for each test"""
    mgr = TableUndoManager()
    applied_changes = []

    def apply_cell(change: CellChange):
        applied_changes.append(("cell", change))

    def apply_axis(change: AxisChange):
        applied_changes.append(("axis", change))

    def update_pending(change: CellChange, is_undo: bool):
        applied_changes.append(("pending", change, is_undo))

    mgr.set_callbacks(
        apply_cell=apply_cell,
        apply_axis=apply_axis,
        update_pending=update_pending,
    )
    mgr._test_applied_changes = applied_changes
    return mgr


@pytest.fixture
def table_a():
    """Sample table A for testing"""
    return Table(
        name="Table A",
        address="0x1000",
        type=TableType.TWO_D,
        elements=10,
        scaling="TestScaling",
    )


@pytest.fixture
def table_b():
    """Sample table B for testing"""
    return Table(
        name="Table B",
        address="0x2000",
        type=TableType.TWO_D,
        elements=10,
        scaling="TestScaling",
    )


def test_separate_undo_stacks(manager, table_a, table_b):
    """Test that changes to different tables have separate undo stacks"""
    # Record change to table A
    manager.record_cell_change(table_a, 0, 0, 10.0, 20.0, 100, 200)

    # Record change to table B
    manager.record_cell_change(table_b, 0, 0, 30.0, 40.0, 300, 400)

    # Activate table A's stack
    manager.set_active_stack(make_table_key(None, table_a.address))

    # Undo should only affect table A
    assert manager.can_undo()
    initial_count = len(manager._test_applied_changes)
    manager.undo_group.undo()

    # Check that the applied change was for table A (filter out pending updates)
    cell_changes = [
        e for e in manager._test_applied_changes[initial_count:] if e[0] == "cell"
    ]
    assert len(cell_changes) == 1
    last_cell = cell_changes[-1]
    assert last_cell[1].table_address == table_a.address
    assert last_cell[1].new_value == 10.0  # Reverted to old value

    # Table B's change should still be undoable when we switch to it
    manager.set_active_stack(make_table_key(None, table_b.address))
    assert manager.can_undo()

    count_before = len(manager._test_applied_changes)
    manager.undo_group.undo()
    cell_changes = [
        e for e in manager._test_applied_changes[count_before:] if e[0] == "cell"
    ]
    assert len(cell_changes) == 1
    last_cell = cell_changes[-1]
    assert last_cell[1].table_address == table_b.address
    assert last_cell[1].new_value == 30.0  # Reverted to old value


def test_focus_switches_active_stack(manager, table_a, table_b):
    """Test that setting active stack affects undo/redo"""
    # Record changes to both tables
    manager.record_cell_change(table_a, 0, 0, 10.0, 20.0, 100, 200)
    manager.record_cell_change(table_b, 0, 0, 30.0, 40.0, 300, 400)

    # With no stack active, can't undo
    manager.set_active_stack(None)
    assert not manager.can_undo()

    # Activate table B
    manager.set_active_stack(make_table_key(None, table_b.address))
    assert manager.can_undo()

    # Undo should affect table B only
    manager.undo_group.undo()
    last_change = manager._test_applied_changes[-1]
    assert last_change[1].table_address == table_b.address

    # Can't undo table B anymore
    assert not manager.can_undo()

    # Switch to table A - should still have undo
    manager.set_active_stack(make_table_key(None, table_a.address))
    assert manager.can_undo()


def test_bulk_changes_single_undo(manager, table_a):
    """Test that bulk changes are undone as single operation"""
    changes = [
        (0, 0, 10.0, 15.0, 100, 150),
        (1, 0, 20.0, 25.0, 200, 250),
        (2, 0, 30.0, 35.0, 300, 350),
    ]

    manager.record_bulk_cell_changes(table_a, changes, "Vertical Interpolation")
    manager.set_active_stack(make_table_key(None, table_a.address))

    # One undo should revert all 3 changes
    initial_count = len(manager._test_applied_changes)
    manager.undo_group.undo()

    # Should have added 3 reverse changes (plus 3 pending updates)
    new_entries = manager._test_applied_changes[initial_count:]
    cell_changes = [e for e in new_entries if e[0] == "cell"]
    assert len(cell_changes) == 3

    # Verify all changes were reversed to old values
    for entry in cell_changes:
        change = entry[1]
        assert change.table_address == table_a.address
        # new_value should be the old_value (reverted)
        assert change.new_value in [10.0, 20.0, 30.0]


def test_undo_redo_cycle(manager, table_a):
    """Test complete undo/redo cycle"""
    manager.record_cell_change(table_a, 0, 0, 10.0, 20.0, 100, 200)
    manager.set_active_stack(make_table_key(None, table_a.address))

    assert manager.can_undo()
    assert not manager.can_redo()

    # Undo
    manager.undo_group.undo()
    assert not manager.can_undo()
    assert manager.can_redo()

    # Redo
    manager.undo_group.redo()
    assert manager.can_undo()
    assert not manager.can_redo()


def test_new_change_clears_redo(manager, table_a):
    """Test that new change after undo clears redo stack"""
    manager.record_cell_change(table_a, 0, 0, 10.0, 20.0, 100, 200)
    manager.set_active_stack(make_table_key(None, table_a.address))

    # Undo
    manager.undo_group.undo()
    assert manager.can_redo()

    # New change should clear redo
    manager.record_cell_change(table_a, 1, 0, 30.0, 40.0, 300, 400)
    assert not manager.can_redo()
    assert manager.can_undo()


def test_undo_limit(manager, table_a):
    """Test that undo stack respects limit"""
    # Record more changes than limit
    for i in range(MAX_UNDO_PER_TABLE + 10):
        manager.record_cell_change(table_a, i % 10, 0, float(i), float(i + 1), i, i + 1)

    manager.set_active_stack(make_table_key(None, table_a.address))

    # Count available undos
    undo_count = 0
    while manager.can_undo():
        manager.undo_group.undo()
        undo_count += 1

    assert undo_count <= MAX_UNDO_PER_TABLE


def test_axis_change_undo(manager, table_a):
    """Test axis change undo/redo"""
    manager.record_axis_change(table_a, "x_axis", 0, 1000.0, 1500.0, 100, 150)
    manager.set_active_stack(make_table_key(None, table_a.address))

    assert manager.can_undo()

    initial_count = len(manager._test_applied_changes)
    manager.undo_group.undo()

    # Check that axis change was applied
    last_change = manager._test_applied_changes[-1]
    assert last_change[0] == "axis"
    assert last_change[1].axis_type == "x_axis"
    assert last_change[1].new_value == 1000.0  # Reverted


def test_bulk_axis_change_undo(manager, table_a):
    """Test bulk axis change undo"""
    changes = [
        ("y_axis", 0, 100.0, 150.0, 10, 15),
        ("y_axis", 1, 200.0, 250.0, 20, 25),
    ]

    manager.record_axis_bulk_changes(table_a, changes, "Y-Axis Interpolation")
    manager.set_active_stack(make_table_key(None, table_a.address))

    initial_count = len(manager._test_applied_changes)
    manager.undo_group.undo()

    # Should have 2 axis changes
    new_entries = manager._test_applied_changes[initial_count:]
    axis_changes = [e for e in new_entries if e[0] == "axis"]
    assert len(axis_changes) == 2


def test_clear_all_stacks(manager, table_a, table_b):
    """Test clearing all undo stacks"""
    manager.record_cell_change(table_a, 0, 0, 10.0, 20.0, 100, 200)
    manager.record_cell_change(table_b, 0, 0, 30.0, 40.0, 300, 400)

    manager.set_active_stack(make_table_key(None, table_a.address))
    assert manager.can_undo()

    manager.clear_all()

    # No more undos on any table
    manager.set_active_stack(make_table_key(None, table_a.address))
    assert not manager.can_undo()

    manager.set_active_stack(make_table_key(None, table_b.address))
    assert not manager.can_undo()


def test_get_active_table_address(manager, table_a, table_b):
    """Test getting currently active table address"""
    manager.record_cell_change(table_a, 0, 0, 10.0, 20.0, 100, 200)
    manager.record_cell_change(table_b, 0, 0, 30.0, 40.0, 300, 400)

    manager.set_active_stack(make_table_key(None, table_a.address))
    assert manager.get_active_table_address() == make_table_key(None, table_a.address)

    manager.set_active_stack(make_table_key(None, table_b.address))
    assert manager.get_active_table_address() == make_table_key(None, table_b.address)

    manager.set_active_stack(None)
    assert manager.get_active_table_address() is None


def test_undo_text(manager, table_a):
    """Test that undo text reflects the command description"""
    manager.record_bulk_cell_changes(
        table_a,
        [(0, 0, 10.0, 20.0, 100, 200)],
        "Multiply by 1.5",
    )
    manager.set_active_stack(make_table_key(None, table_a.address))

    assert "Multiply by 1.5" in manager.undo_text()
