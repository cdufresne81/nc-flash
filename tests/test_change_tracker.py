"""
Tests for ChangeTracker - undo/redo functionality

These tests verify that single and bulk undo/redo operations work correctly.
"""

import pytest
from src.core.change_tracker import ChangeTracker
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


def test_single_undo_redo(tracker, sample_table):
    """Test single cell change undo/redo"""
    # Record a change
    tracker.record_change(
        table=sample_table,
        row=0,
        col=0,
        old_value=10.0,
        new_value=20.0,
        old_raw=100,
        new_raw=200
    )

    # Should be able to undo
    assert tracker.can_undo()
    assert not tracker.can_redo()

    # Undo the change
    change = tracker.undo()
    assert change is not None
    assert change.new_value == 10.0  # Reverted to old value
    assert change.new_raw == 100

    # Should be able to redo now
    assert not tracker.can_undo()
    assert tracker.can_redo()

    # Redo the change
    change = tracker.redo()
    assert change is not None
    assert change.new_value == 20.0  # Back to new value
    assert change.new_raw == 200

    # Should be able to undo again
    assert tracker.can_undo()
    assert not tracker.can_redo()


def test_multiple_undos(tracker, sample_table):
    """Test multiple sequential undo operations"""
    # Record 3 changes
    for i in range(3):
        tracker.record_change(
            table=sample_table,
            row=i,
            col=0,
            old_value=float(i),
            new_value=float(i + 10),
            old_raw=i,
            new_raw=i + 10
        )

    # Undo all 3
    for i in range(3):
        assert tracker.can_undo()
        change = tracker.undo()
        assert change is not None

    # Should have no more undos
    assert not tracker.can_undo()
    assert tracker.can_redo()

    # Should be able to redo all 3
    for i in range(3):
        assert tracker.can_redo()
        change = tracker.redo()
        assert change is not None

    assert not tracker.can_redo()


def test_bulk_undo_redo(tracker, sample_table):
    """Test bulk change undo/redo (for interpolation operations)"""
    # Create bulk changes (simulating interpolation)
    changes = [
        (0, 0, 10.0, 15.0, 100, 150),
        (1, 0, 20.0, 25.0, 200, 250),
        (2, 0, 30.0, 35.0, 300, 350),
    ]

    # Record as bulk operation
    tracker.record_bulk_changes(
        table=sample_table,
        changes=changes,
        description="Vertical interpolation"
    )

    # Should be able to undo
    assert tracker.can_undo()
    assert not tracker.can_redo()

    # Undo the bulk change
    result = tracker.undo()
    assert isinstance(result, list)
    assert len(result) == 3

    # Verify each change was reversed
    for i, change in enumerate(result):
        # After undo, new_value should be old value (reverted)
        assert change.new_value == changes[i][2]  # old_value from original
        assert change.new_raw == changes[i][4]  # old_raw from original

    # Should be able to redo now
    assert not tracker.can_undo()
    assert tracker.can_redo()

    # Redo the bulk change
    result = tracker.redo()
    assert isinstance(result, list)
    assert len(result) == 3

    # Verify each change was reapplied
    for i, change in enumerate(result):
        # After redo, new_value should be new value (reapplied)
        assert change.new_value == changes[i][3]  # new_value from original
        assert change.new_raw == changes[i][5]  # new_raw from original

    # Should be able to undo again
    assert tracker.can_undo()
    assert not tracker.can_redo()


def test_undo_clears_redo_stack(tracker, sample_table):
    """Test that new change after undo clears redo stack"""
    # Record change 1
    tracker.record_change(
        table=sample_table,
        row=0,
        col=0,
        old_value=10.0,
        new_value=20.0,
        old_raw=100,
        new_raw=200
    )

    # Undo it
    tracker.undo()
    assert tracker.can_redo()

    # Make a new change
    tracker.record_change(
        table=sample_table,
        row=1,
        col=0,
        old_value=30.0,
        new_value=40.0,
        old_raw=300,
        new_raw=400
    )

    # Redo stack should be cleared
    assert not tracker.can_redo()
    assert tracker.can_undo()


def test_mixed_single_and_bulk_undo(tracker, sample_table):
    """Test undo/redo with mixed single and bulk changes"""
    # Single change
    tracker.record_change(
        table=sample_table,
        row=0,
        col=0,
        old_value=10.0,
        new_value=20.0,
        old_raw=100,
        new_raw=200
    )

    # Bulk change
    bulk_changes = [
        (1, 0, 30.0, 35.0, 300, 350),
        (2, 0, 40.0, 45.0, 400, 450),
    ]
    tracker.record_bulk_changes(
        table=sample_table,
        changes=bulk_changes,
        description="Interpolation"
    )

    # Another single change
    tracker.record_change(
        table=sample_table,
        row=3,
        col=0,
        old_value=50.0,
        new_value=60.0,
        old_raw=500,
        new_raw=600
    )

    # Undo last single change
    result = tracker.undo()
    assert not isinstance(result, list)
    assert result.new_value == 50.0

    # Undo bulk change
    result = tracker.undo()
    assert isinstance(result, list)
    assert len(result) == 2

    # Undo first single change
    result = tracker.undo()
    assert not isinstance(result, list)
    assert result.new_value == 10.0

    # No more undos
    assert not tracker.can_undo()

    # Redo all in reverse order
    result = tracker.redo()
    assert not isinstance(result, list)

    result = tracker.redo()
    assert isinstance(result, list)

    result = tracker.redo()
    assert not isinstance(result, list)

    assert not tracker.can_redo()


def test_pending_changes_tracking(tracker, sample_table):
    """Test that pending changes are tracked correctly"""
    # Initially no pending changes
    assert not tracker.has_pending_changes()

    # Record a change
    tracker.record_change(
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

    # Undo should still keep pending changes (reverted but tracked)
    tracker.undo()
    # After undo, pending changes should reflect the reverted state
    # The cell was changed and then reverted, so it's back to original
    # which means it can be removed from pending

    # Clear pending changes
    tracker.clear_pending_changes()
    assert not tracker.has_pending_changes()


def test_max_undo_stack_limit(tracker, sample_table):
    """Test that undo stack respects max size limit"""
    from src.core.change_tracker import MAX_UNDO_STACK

    # Record more changes than the max
    for i in range(MAX_UNDO_STACK + 10):
        tracker.record_change(
            table=sample_table,
            row=i % 10,
            col=0,
            old_value=float(i),
            new_value=float(i + 1),
            old_raw=i,
            new_raw=i + 1
        )

    # Count how many undos are available (should be limited)
    undo_count = 0
    while tracker.can_undo():
        tracker.undo()
        undo_count += 1

    # Should not exceed MAX_UNDO_STACK
    assert undo_count <= MAX_UNDO_STACK
