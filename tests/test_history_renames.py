"""History shows a table's current name after a definition update renamed it.

A commit stores the table name it had when it was saved. When a newer bundled
definition renames a table without moving it, the history matches it by
address and shows the current name, with the stored one after "was:".
"""

from datetime import datetime
from unittest.mock import MagicMock

from PySide6.QtWidgets import QApplication

from src.core.version_models import CellChange, Commit, TableChanges
from src.ui.history_viewer import CommitDetailsWidget, HistoryViewer

_app = QApplication.instance() or QApplication([])


def _commit(*tables):
    changes = [
        TableChanges(
            table_name=name,
            table_address=address,
            cell_changes=[
                CellChange(name, address, 0, 0, 1.0, 2.0, 1.0, 2.0),
            ],
        )
        for name, address in tables
    ]
    return Commit(
        id="c1",
        version=1,
        parent_id=None,
        message="tune",
        timestamp=datetime(2026, 10, 8),
        author="me",
        tables_modified=[name for name, _ in tables],
        changes=changes,
    )


def test_renamed_table_maps_to_current_name():
    commit = _commit(("Old Name", "c5944"))
    assert commit.renamed_tables({0xC5944: ["New Name"]}) == {
        ("Old Name", "c5944"): "New Name"
    }


def test_unchanged_name_is_not_reported():
    commit = _commit(("Same", "c5944"))
    assert commit.renamed_tables({0xC5944: ["Same"]}) == {}


def test_ambiguous_or_missing_address_keeps_stored_name():
    commit = _commit(("Old", "c5944"), ("Gone", "fcac0"))
    names = {0xC5944: ["A", "B"]}  # two tables at one address: no guess
    assert commit.renamed_tables(names) == {}


def test_bad_stored_address_is_skipped():
    commit = _commit(("Old", "not-hex"))
    assert commit.renamed_tables({0: ["X"]}) == {}


def test_details_show_current_name_and_keep_stored_name_as_data():
    widget = CommitDetailsWidget(names_by_address={0xC5944: ["New Name"]})
    widget.show_commit(_commit(("Old Name", "c5944"), ("Kept", "c5948")))
    labels = [widget.tables_list.item(i).text() for i in range(2)]
    assert labels[0] == "New Name (1 cells) - was: Old Name"
    assert labels[1] == "Kept (1 cells)"
    # The item still carries the stored name (signals use it unchanged)
    assert widget.tables_list.item(0).data(0x0100) == "Old Name"


def test_search_finds_commit_by_current_name():
    pm = MagicMock()
    pm.get_recent_commits.return_value = [_commit(("Old Name", "c5944"))]
    viewer = HistoryViewer(pm, names_by_address={0xC5944: ["New Name"]})
    viewer._filter_commits("new name")
    assert not viewer.commit_tree.topLevelItem(0).isHidden()
    viewer._filter_commits("old name")
    assert not viewer.commit_tree.topLevelItem(0).isHidden()
    viewer._filter_commits("nothing")
    assert viewer.commit_tree.topLevelItem(0).isHidden()


def test_two_tables_that_shared_a_name_get_one_row_each():
    # Older definitions named a table and its Data Integrity copy the same
    commit = _commit(("APP 2nd", "c92cc"), ("APP 2nd", "f8618"))
    commit.changes[1].cell_changes.append(
        CellChange("APP 2nd", "f8618", 0, 1, 1.0, 2.0, 1.0, 2.0)
    )
    names = {0xC92CC: ["APP 2nd (pair with f8618)"], 0xF8618: ["APP 2nd DI"]}
    widget = CommitDetailsWidget(names_by_address=names)
    widget.show_commit(commit)
    labels = [
        widget.tables_list.item(i).text() for i in range(widget.tables_list.count())
    ]
    assert labels == [
        "APP 2nd (pair with f8618) (1 cells) - was: APP 2nd",
        "APP 2nd DI (2 cells) - was: APP 2nd",
    ]
