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
    QPushButton,
    QStyledItemDelegate
)
from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QShortcut, QKeySequence

from ..utils.constants import TABLE_BROWSER_COLUMN_WIDTH
from ..core.rom_definition import RomDefinition, Table


class HtmlDelegate(QStyledItemDelegate):
    """Custom delegate to render HTML in tree widget items"""

    def paint(self, painter, option, index):
        """Paint the item with HTML support"""
        from PySide6.QtGui import QTextDocument, QPalette
        from PySide6.QtCore import QRectF

        options = option
        self.initStyleOption(options, index)

        # Check if text contains HTML tags
        if '<span' not in options.text:
            # No HTML, use default painting
            super().paint(painter, option, index)
            return

        painter.save()

        # Create a text document for HTML rendering
        doc = QTextDocument()

        # Set default text color from the palette before setting HTML
        palette = options.palette
        text_color = palette.color(QPalette.Text)

        # Wrap HTML in a div with default text color
        wrapped_html = f'<div style="color: {text_color.name()};">{options.text}</div>'
        doc.setHtml(wrapped_html)
        doc.setDefaultFont(options.font)

        # Clear the text so the default drawing doesn't happen
        options.text = ""

        # Draw the item background
        if options.widget:
            style = options.widget.style()
        else:
            from PySide6.QtWidgets import QApplication
            style = QApplication.style()

        style.drawControl(style.CE_ItemViewItem, options, painter)

        # Calculate text rect
        text_rect = style.subElementRect(style.SE_ItemViewItemText, options)

        # Draw the HTML text
        painter.translate(text_rect.topLeft())
        clip = QRectF(0, 0, text_rect.width(), text_rect.height())
        doc.drawContents(painter, clip)

        painter.restore()

    def sizeHint(self, option, index):
        """Return size hint for the item"""
        # Use default size hint
        return super().sizeHint(option, index)


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
        self.tree.setColumnWidth(0, TABLE_BROWSER_COLUMN_WIDTH)
        self.tree.itemClicked.connect(self._on_item_clicked)

        # Set custom delegate for HTML rendering
        self.html_delegate = HtmlDelegate(self.tree)
        self.tree.setItemDelegate(self.html_delegate)

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

    def _highlight_text(self, text: str, search_text: str) -> str:
        """
        Highlight matching portions of text with HTML

        Args:
            text: Original text
            search_text: Text to highlight

        Returns:
            str: Text with HTML highlighting
        """
        if not search_text:
            return text

        # Case-insensitive search for position
        lower_text = text.lower()
        lower_search = search_text.lower()

        result = text
        pos = 0
        highlighted = []

        while True:
            index = lower_text.find(lower_search, pos)
            if index == -1:
                # Add remaining text
                highlighted.append(result[pos:])
                break

            # Add text before match
            highlighted.append(result[pos:index])

            # Add highlighted match (subtle background color)
            match_text = result[index:index + len(search_text)]
            highlighted.append(f'<span style="background-color: #E3F2FD; color: #1976D2; font-weight: 500;">{match_text}</span>')

            pos = index + len(search_text)

        return ''.join(highlighted)

    def _filter_tables(self, search_text: str):
        """
        Filter the table tree based on search text and highlight matches

        Args:
            search_text: Text to search for in table names and categories
        """
        search_text = search_text.lower().strip()

        # If search is empty, show all items and clear highlighting
        if not search_text:
            self._show_all_items()
            return

        # Iterate through all category items
        for i in range(self.tree.topLevelItemCount()):
            category_item = self.tree.topLevelItem(i)
            # Get original text (stored in data)
            original_category = category_item.data(0, Qt.UserRole)
            if original_category is None:
                original_category = category_item.text(0)
                category_item.setData(0, Qt.UserRole, original_category)

            category_name_lower = original_category.lower()
            category_has_match = False

            # Check if category name matches
            category_matches = search_text in category_name_lower

            # Set category text with or without highlighting
            if category_matches:
                category_item.setText(0, self._highlight_text(original_category, search_text))
            else:
                category_item.setText(0, original_category)

            # Check all table children
            for j in range(category_item.childCount()):
                table_item = category_item.child(j)

                # Get original texts (stored in data)
                original_name = table_item.data(0, Qt.UserRole)
                original_type = table_item.data(1, Qt.UserRole)
                original_address = table_item.data(2, Qt.UserRole)

                if original_name is None:
                    original_name = table_item.text(0)
                    original_type = table_item.text(1)
                    original_address = table_item.text(2)
                    table_item.setData(0, Qt.UserRole, original_name)
                    table_item.setData(1, Qt.UserRole, original_type)
                    table_item.setData(2, Qt.UserRole, original_address)

                # Match against table name, type, or address
                name_matches = search_text in original_name.lower()
                type_matches = search_text in original_type.lower()
                address_matches = search_text in original_address.lower()
                matches = name_matches or type_matches or address_matches

                # Set text with highlighting for matching columns
                if name_matches:
                    table_item.setText(0, self._highlight_text(original_name, search_text))
                else:
                    table_item.setText(0, original_name)

                if type_matches:
                    table_item.setText(1, self._highlight_text(original_type, search_text))
                else:
                    table_item.setText(1, original_type)

                if address_matches:
                    table_item.setText(2, self._highlight_text(original_address, search_text))
                else:
                    table_item.setText(2, original_address)

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
        """Show all items in the tree, collapse categories, and clear highlighting"""
        for i in range(self.tree.topLevelItemCount()):
            category_item = self.tree.topLevelItem(i)
            category_item.setHidden(False)

            # Restore original text without highlighting
            original_category = category_item.data(0, Qt.UserRole)
            if original_category:
                category_item.setText(0, original_category)

            for j in range(category_item.childCount()):
                table_item = category_item.child(j)
                table_item.setHidden(False)

                # Restore original texts without highlighting
                original_name = table_item.data(0, Qt.UserRole)
                original_type = table_item.data(1, Qt.UserRole)
                original_address = table_item.data(2, Qt.UserRole)

                if original_name:
                    table_item.setText(0, original_name)
                if original_type:
                    table_item.setText(1, original_type)
                if original_address:
                    table_item.setText(2, original_address)

            # Collapse categories when search is cleared
            category_item.setExpanded(False)
