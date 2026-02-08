"""
History Viewer Widget

Displays commit history with ability to view details.
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QTreeWidget, QTreeWidgetItem, QTextEdit, QLabel,
    QListWidget, QListWidgetItem, QGroupBox,
    QDialog, QPushButton, QLineEdit
)
from PySide6.QtCore import Qt, Signal

from ..core.version_models import Commit
from ..core.project_manager import ProjectManager


class HistoryViewer(QDialog):
    """Dialog for browsing commit history"""

    commit_selected = Signal(str)  # Emits commit ID
    view_table_diff = Signal(str, object)  # Emits (table_name, Commit)

    def __init__(self, project_manager: ProjectManager, parent=None):
        super().__init__(parent)
        self.project_manager = project_manager
        self.setWindowTitle("Version History")
        self.setMinimumSize(900, 600)
        self._init_ui()
        self.refresh()

    def _init_ui(self):
        layout = QVBoxLayout()
        self.setLayout(layout)

        # Search bar
        search_layout = QHBoxLayout()
        search_layout.addWidget(QLabel("Search:"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Filter by message or table name...")
        self.search_edit.textChanged.connect(self._filter_commits)
        search_layout.addWidget(self.search_edit)
        layout.addLayout(search_layout)

        # Splitter for list and details
        splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(splitter)

        # Left: Commit list
        left_widget = QWidget()
        left_layout = QVBoxLayout()
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_widget.setLayout(left_layout)

        left_layout.addWidget(QLabel("<b>Versions</b> (newest first)"))

        self.commit_tree = QTreeWidget()
        self.commit_tree.setHeaderLabels(["Version", "Message", "Date", "Tables"])
        self.commit_tree.setColumnWidth(0, 60)
        self.commit_tree.setColumnWidth(1, 200)
        self.commit_tree.setColumnWidth(2, 100)
        self.commit_tree.setRootIsDecorated(False)
        self.commit_tree.itemClicked.connect(self._on_commit_selected)
        left_layout.addWidget(self.commit_tree)

        splitter.addWidget(left_widget)

        # Right: Commit details
        right_widget = QWidget()
        right_layout = QVBoxLayout()
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_widget.setLayout(right_layout)

        right_layout.addWidget(QLabel("<b>Version Details</b>"))

        # Details area
        self.details_widget = CommitDetailsWidget()
        self.details_widget.view_table_requested.connect(self._on_view_table_requested)
        right_layout.addWidget(self.details_widget)

        splitter.addWidget(right_widget)
        splitter.setSizes([400, 500])

        # Close button
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        button_layout.addWidget(close_btn)
        layout.addLayout(button_layout)

    def refresh(self):
        """Reload commit history"""
        self.commit_tree.clear()
        self.details_widget.clear()

        commits = self.project_manager.get_recent_commits(100)

        for commit in commits:
            self._add_commit_item(commit)

    def _add_commit_item(self, commit: Commit):
        """Add a commit to the tree"""
        version_str = f"v{commit.version}"
        date_str = commit.timestamp.strftime("%Y-%m-%d %H:%M")

        tables_count = len(commit.tables_modified)
        if tables_count == 0:
            tables_str = "(original)"
        else:
            tables_str = str(tables_count)

        # Truncate message for display
        msg = commit.message.split('\n')[0]  # First line only
        if len(msg) > 35:
            msg = msg[:32] + "..."

        item = QTreeWidgetItem([version_str, msg, date_str, tables_str])
        item.setData(0, Qt.UserRole, commit.id)
        item.setData(0, Qt.UserRole + 1, commit)  # Store commit object
        item.setToolTip(1, commit.message)

        # Style initial commit (v0) differently
        if commit.version == 0:
            for col in range(4):
                item.setForeground(col, Qt.gray)

        self.commit_tree.addTopLevelItem(item)

    def _on_view_table_requested(self, table_name: str):
        """Handle request to view table diff"""
        commit = self.details_widget.current_commit
        if commit:
            self.view_table_diff.emit(table_name, commit)

    def _filter_commits(self, text: str):
        """Filter commits by search text using stored item data"""
        text = text.lower()

        for i in range(self.commit_tree.topLevelItemCount()):
            item = self.commit_tree.topLevelItem(i)
            commit = item.data(0, Qt.UserRole + 1)

            if commit:
                visible = (
                    text in commit.message.lower() or
                    any(text in t.lower() for t in commit.tables_modified)
                )
                item.setHidden(not visible)
            else:
                item.setHidden(True)

    def _on_commit_selected(self, item, column):
        """Handle commit selection"""
        commit_id = item.data(0, Qt.UserRole)
        commit = self.project_manager.get_commit(commit_id)
        if commit:
            self.details_widget.show_commit(commit)
            self.commit_selected.emit(commit_id)


class CommitDetailsWidget(QWidget):
    """Shows detailed information about a single commit"""

    view_table_requested = Signal(str)  # Emits table name

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_commit = None
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(layout)

        # Commit info
        info_group = QGroupBox("Information")
        info_layout = QVBoxLayout()
        info_group.setLayout(info_layout)

        self.info_text = QTextEdit()
        self.info_text.setReadOnly(True)
        self.info_text.setMaximumHeight(160)
        info_layout.addWidget(self.info_text)

        layout.addWidget(info_group)

        # Modified tables list (simple, no cell details)
        tables_group = QGroupBox("Modified Tables")
        tables_layout = QVBoxLayout()
        tables_group.setLayout(tables_layout)

        self.tables_list = QListWidget()
        self.tables_list.setAlternatingRowColors(True)
        self.tables_list.itemDoubleClicked.connect(self._on_table_double_clicked)
        tables_layout.addWidget(self.tables_list)

        # View button
        view_btn_layout = QHBoxLayout()
        view_btn_layout.addStretch()
        self.view_diff_btn = QPushButton("View Table Changes...")
        self.view_diff_btn.setEnabled(False)
        self.view_diff_btn.clicked.connect(self._on_view_diff_clicked)
        view_btn_layout.addWidget(self.view_diff_btn)
        tables_layout.addLayout(view_btn_layout)

        layout.addWidget(tables_group)

        # Help text
        help_label = QLabel("Double-click a table to view changes")
        help_label.setStyleSheet("color: #666; font-size: 11px;")
        layout.addWidget(help_label)

    def show_commit(self, commit: Commit):
        """Display commit details"""
        self.current_commit = commit

        # Show info with version number
        snapshot_str = commit.snapshot_filename if commit.snapshot_filename else "No"
        info = (
            f"<b>Version:</b> v{commit.version}<br>"
            f"<b>Date:</b> {commit.timestamp.strftime('%Y-%m-%d %H:%M:%S')}<br>"
            f"<b>Author:</b> {commit.author}<br>"
            f"<b>Snapshot:</b> {snapshot_str}<br>"
            f"<hr>"
            f"<b>Message:</b><br>{commit.message}"
        )
        self.info_text.setHtml(info)

        # Show modified tables (simple list)
        self.tables_list.clear()

        if not commit.tables_modified:
            item = QListWidgetItem("(Original ROM - no changes)")
            item.setForeground(Qt.gray)
            self.tables_list.addItem(item)
            self.view_diff_btn.setEnabled(False)
        else:
            for table_name in commit.tables_modified:
                # Find cell count for this table
                cell_count = 0
                for tc in commit.changes:
                    if tc.table_name == table_name:
                        cell_count = len(tc.cell_changes)
                        break

                item = QListWidgetItem(f"{table_name} ({cell_count} cells)")
                item.setData(Qt.UserRole, table_name)
                self.tables_list.addItem(item)

            self.view_diff_btn.setEnabled(True)

    def _on_table_double_clicked(self, item: QListWidgetItem):
        """Handle double-click on table"""
        table_name = item.data(Qt.UserRole)
        if table_name:
            self.view_table_requested.emit(table_name)

    def _on_view_diff_clicked(self):
        """Handle view diff button click"""
        current_item = self.tables_list.currentItem()
        if current_item:
            table_name = current_item.data(Qt.UserRole)
            if table_name:
                self.view_table_requested.emit(table_name)

    def clear(self):
        """Clear the details view"""
        self.info_text.clear()
        self.tables_list.clear()
        self.current_commit = None
        self.view_diff_btn.setEnabled(False)


class HistoryPanel(QWidget):
    """Compact history panel for embedding in main window (optional)"""

    commit_selected = Signal(str)

    def __init__(self, project_manager: ProjectManager, parent=None):
        super().__init__(parent)
        self.project_manager = project_manager
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(layout)

        # Header
        header = QLabel("<b>Recent Versions</b>")
        layout.addWidget(header)

        # Commit list
        self.commit_list = QTreeWidget()
        self.commit_list.setHeaderHidden(True)
        self.commit_list.setRootIsDecorated(False)
        self.commit_list.itemClicked.connect(self._on_commit_clicked)
        layout.addWidget(self.commit_list)

    def refresh(self):
        """Refresh the commit list"""
        self.commit_list.clear()

        commits = self.project_manager.get_recent_commits(10)

        for commit in commits:
            date_str = commit.timestamp.strftime("%m/%d %H:%M")
            msg = commit.message.split('\n')[0][:25]

            item = QTreeWidgetItem([f"v{commit.version} - {date_str} - {msg}"])
            item.setData(0, Qt.UserRole, commit.id)
            item.setToolTip(0, commit.message)

            # Style v0 differently
            if commit.version == 0:
                item.setForeground(0, Qt.gray)

            self.commit_list.addTopLevelItem(item)

    def _on_commit_clicked(self, item, column):
        """Handle commit click"""
        commit_id = item.data(0, Qt.UserRole)
        self.commit_selected.emit(commit_id)
