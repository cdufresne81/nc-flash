"""
Table Browser Widget

Shows a tree view of all available tables organized by category.
"""

from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QTreeWidget,
    QTreeWidgetItem,
    QLabel,
    QLineEdit,
    QPushButton
)
from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QShortcut, QKeySequence

from ..core.rom_definition import RomDefinition, Table


class TableBrowser(QWidget):
    """Widget for browsing tables by category"""

    # Signal emitted when a table is selected
    table_selected = Signal(Table)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.definition = None
        self.init_ui()

    def init_ui(self):
        """Initialize the user interface"""
        layout = QVBoxLayout()
        self.setLayout(layout)

        # Label
        label = QLabel("Tables")
        layout.addWidget(label)

        # Search box
        search_layout = QHBoxLayout()
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search tables... (Ctrl+F)")
        self.search_box.textChanged.connect(self._filter_tables)
        self.search_box.setClearButtonEnabled(True)
        search_layout.addWidget(self.search_box)

        clear_button = QPushButton("Clear")
        clear_button.clicked.connect(self.search_box.clear)
        search_layout.addWidget(clear_button)

        layout.addLayout(search_layout)

        # Tree widget for categories and tables
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Type", "Address"])
        self.tree.setColumnWidth(0, 400)
        self.tree.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self.tree)

        # Keyboard shortcut for search (Ctrl+F)
        self.search_shortcut = QShortcut(QKeySequence("Ctrl+F"), self)
        self.search_shortcut.activated.connect(self._focus_search)

    def load_definition(self, definition: RomDefinition):
        """
        Load ROM definition and populate tree

        Args:
            definition: ROM definition with tables
        """
        self.definition = definition
        self.tree.clear()

        # Get tables grouped by category
        categories = definition.get_tables_by_category()

        # Sort categories alphabetically
        for category in sorted(categories.keys()):
            tables = categories[category]

            # Create category item
            category_item = QTreeWidgetItem([category, "", ""])
            category_item.setData(0, 100, None)  # Store None for category items
            self.tree.addTopLevelItem(category_item)

            # Sort tables by name within category
            for table in sorted(tables, key=lambda t: t.name):
                # Create table item
                table_item = QTreeWidgetItem([
                    table.name,
                    table.type.value,
                    f"0x{table.address}"
                ])
                # Store table object
                table_item.setData(0, 100, table)
                category_item.addChild(table_item)

            # Collapse categories initially
            category_item.setExpanded(False)

    def _on_item_clicked(self, item, column):
        """Handle item click in tree"""
        # Get table object stored in item
        table = item.data(0, 100)
        if table is not None:  # Not a category
            self.table_selected.emit(table)

    def _focus_search(self):
        """Focus the search box and select all text"""
        self.search_box.setFocus()
        self.search_box.selectAll()

    def _filter_tables(self, search_text: str):
        """
        Filter the table tree based on search text

        Args:
            search_text: Text to search for in table names and categories
        """
        search_text = search_text.lower().strip()

        # If search is empty, show all items
        if not search_text:
            self._show_all_items()
            return

        # Iterate through all category items
        for i in range(self.tree.topLevelItemCount()):
            category_item = self.tree.topLevelItem(i)
            category_name = category_item.text(0).lower()
            category_has_match = False

            # Check if category name matches
            category_matches = search_text in category_name

            # Check all table children
            for j in range(category_item.childCount()):
                table_item = category_item.child(j)
                table_name = table_item.text(0).lower()
                table_type = table_item.text(1).lower()
                table_address = table_item.text(2).lower()

                # Match against table name, type, or address
                matches = (
                    search_text in table_name or
                    search_text in table_type or
                    search_text in table_address
                )

                # Show/hide table item
                table_item.setHidden(not matches and not category_matches)

                if matches:
                    category_has_match = True

            # Show category if it matches or has matching children
            category_item.setHidden(not (category_matches or category_has_match))

            # Expand categories that have matches
            if category_has_match or category_matches:
                category_item.setExpanded(True)

    def _show_all_items(self):
        """Show all items in the tree and collapse categories"""
        for i in range(self.tree.topLevelItemCount()):
            category_item = self.tree.topLevelItem(i)
            category_item.setHidden(False)

            for j in range(category_item.childCount()):
                table_item = category_item.child(j)
                table_item.setHidden(False)

            # Collapse categories when search is cleared
            category_item.setExpanded(False)
