"""
Tests for ChangeTracker - pending changes tracking functionality

Note: Undo/redo tests have been moved to test_table_undo_manager.py
since that functionality is now handled by TableUndoManager.
"""

import pytest
from src.core.change_tracker import ChangeTracker
from src.core.version_models import CellChange
from src.core.rom_definition import Table, TableType


@pytest.fixture
def tracker():
    """Create a fresh ChangeTracker instance for each test"""
    return ChangeTracker()


@pytest.fixture
def sample_table():
    """Create a sample table for testing"""
    return Table(
        name="Test Table",
        address="0x1000",
        type=TableType.TWO_D,
        elements=10,
        scaling="TestScaling"
    )


def test_pending_changes_tracking(tracker, sample_table):
    """Test that pending changes are tracked correctly"""
    # Initially no pending changes
    assert not tracker.has_pending_changes()

    # Record a pending change
    tracker.record_pending_change(
        table=sample_table,
        row=0,
        col=0,
        old_value=10.0,
        new_value=20.0,
        old_raw=100,
        new_raw=200
    )

    # Should have pending changes
    assert tracker.has_pending_changes()
    assert tracker.get_pending_change_count() == 1

    # Clear pending changes
    tracker.clear_pending_changes()
    assert not tracker.has_pending_changes()


def test_pending_bulk_changes(tracker, sample_table):
    """Test recording bulk pending changes"""
    changes = [
        (0, 0, 10.0, 15.0, 100, 150),
        (1, 0, 20.0, 25.0, 200, 250),
        (2, 0, 30.0, 35.0, 300, 350),
    ]

    tracker.record_pending_bulk_changes(sample_table, changes)

    assert tracker.has_pending_changes()
    assert tracker.get_pending_change_count() == 3


def test_pending_change_merge(tracker, sample_table):
    """Test that multiple changes to same cell keep original old_value"""
    # First change
    tracker.record_pending_change(
        table=sample_table,
        row=0,
        col=0,
        old_value=10.0,
        new_value=20.0,
        old_raw=100,
        new_raw=200
    )

    # Second change to same cell
    tracker.record_pending_change(
        table=sample_table,
        row=0,
        col=0,
        old_value=20.0,  # Current value (would be different)
        new_value=30.0,
        old_raw=200,
        new_raw=300
    )

    # Should still be 1 pending change (merged)
    assert tracker.get_pending_change_count() == 1

    # Get pending changes and verify old_value is preserved
    pending = tracker.get_pending_changes()
    assert len(pending) == 1
    assert len(pending[0].cell_changes) == 1
    change = pending[0].cell_changes[0]
    assert change.old_value == 10.0  # Original old_value preserved
    assert change.new_value == 30.0  # Latest new_value


def test_get_modified_table_addresses(tracker, sample_table):
    """Test getting modified table addresses"""
    tracker.record_pending_change(
        table=sample_table,
        row=0,
        col=0,
        old_value=10.0,
        new_value=20.0,
        old_raw=100,
        new_raw=200
    )

    addresses = tracker.get_modified_table_addresses()
    assert sample_table.address in addresses


def test_update_pending_from_undo(tracker, sample_table):
    """Test updating pending changes from undo operation"""
    # First record a pending change
    tracker.record_pending_change(
        table=sample_table,
        row=0,
        col=0,
        old_value=10.0,
        new_value=20.0,
        old_raw=100,
        new_raw=200
    )

    assert tracker.has_pending_changes()
    assert tracker.get_pending_change_count() == 1

    # Simulate undo - this would revert to old value
    change = CellChange(
        table_name=sample_table.name,
        table_address=sample_table.address,
        row=0,
        col=0,
        old_value=10.0,  # When undoing, old_value is the original
        new_value=20.0,
        old_raw=100,
        new_raw=200
    )
    tracker.update_pending_from_undo(change, is_undo=True)

    # After undo back to original, pending should be removed
    assert not tracker.has_pending_changes()


def test_update_pending_from_redo(tracker, sample_table):
    """Test updating pending changes from redo operation"""
    # Start with no pending changes
    assert not tracker.has_pending_changes()

    # Simulate redo - this would re-apply a change
    change = CellChange(
        table_name=sample_table.name,
        table_address=sample_table.address,
        row=0,
        col=0,
        old_value=10.0,
        new_value=20.0,
        old_raw=100,
        new_raw=200
    )
    tracker.update_pending_from_undo(change, is_undo=False)

    # After redo, should have pending changes
    assert tracker.has_pending_changes()
    assert tracker.get_pending_change_count() == 1


def test_clear_all(tracker, sample_table):
    """Test clearing all tracking state"""
    tracker.record_pending_change(
        table=sample_table,
        row=0,
        col=0,
        old_value=10.0,
        new_value=20.0,
        old_raw=100,
        new_raw=200
    )

    assert tracker.has_pending_changes()

    tracker.clear_all()

    assert not tracker.has_pending_changes()


def test_change_callbacks(tracker, sample_table):
    """Test that change callbacks are invoked"""
    callback_count = [0]

    def callback():
        callback_count[0] += 1

    tracker.add_change_callback(callback)

    tracker.record_pending_change(
        table=sample_table,
        row=0,
        col=0,
        old_value=10.0,
        new_value=20.0,
        old_raw=100,
        new_raw=200
    )

    assert callback_count[0] == 1

    tracker.remove_change_callback(callback)

    tracker.record_pending_change(
        table=sample_table,
        row=1,
        col=0,
        old_value=30.0,
        new_value=40.0,
        old_raw=300,
        new_raw=400
    )

    # Callback shouldn't be called after removal
    assert callback_count[0] == 1
