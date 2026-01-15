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
    QStyledItemDelegate,
    QStyle,
    QComboBox,
)
from PySide6.QtCore import Signal, Qt, QRect
from PySide6.QtGui import QShortcut, QKeySequence, QColor, QPen

from ..utils.constants import TABLE_BROWSER_COLUMN_WIDTH
from ..core.rom_definition import RomDefinition, Table


class HighlightDelegate(QStyledItemDelegate):
    """Custom delegate to highlight search text and color modified tables"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.search_text = ""
        self.highlight_color = QColor(255, 255, 0, 100)  # Yellow with transparency
        self.modified_color = QColor(220, 80, 140)  # Darker pink for modified tables

    def set_search_text(self, text: str):
        """Set the search text to highlight"""
        self.search_text = text.lower()

    def paint(self, painter, option, index):
        """Custom paint to highlight search matches and color modified tables"""
        # Get the text and whether this is a modified table
        text = index.data(Qt.DisplayRole)
        is_modified = index.data(Qt.UserRole + 1)  # Custom role for modified flag

        if not text:
            super().paint(painter, option, index)
            return

        # Draw selection/hover background first
        painter.save()

        # Draw background (selection, hover, etc.)
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
        elif option.state & QStyle.State_MouseOver:
            painter.fillRect(option.rect, option.palette.midlight())

        # Prepare text color
        if option.state & QStyle.State_Selected:
            text_color = option.palette.highlightedText().color()
        elif is_modified:
            text_color = self.modified_color
        else:
            text_color = option.palette.text().color()

        # Draw text with highlighting
        painter.setPen(QPen(text_color))

        # Calculate text position
        text_rect = option.rect.adjusted(4, 0, -4, 0)  # Add padding

        # If search text exists and column is 0 (name column), highlight matches
        if self.search_text and index.column() == 0 and self.search_text in text.lower():
            # Find all occurrences of search text
            search_len = len(self.search_text)
            lower_text = text.lower()
            pos = 0
            x_offset = text_rect.left()
            y_pos = text_rect.center().y()

            font_metrics = option.fontMetrics

            while pos < len(text):
                # Find next occurrence
                match_pos = lower_text.find(self.search_text, pos)

                if match_pos == -1:
                    # No more matches, draw remaining text
                    remaining_text = text[pos:]
                    if remaining_text:
                        painter.drawText(x_offset, y_pos + font_metrics.ascent() // 2, remaining_text)
                    break

                # Draw text before match
                if match_pos > pos:
                    before_text = text[pos:match_pos]
                    painter.drawText(x_offset, y_pos + font_metrics.ascent() // 2, before_text)
                    x_offset += font_metrics.horizontalAdvance(before_text)

                # Draw highlighted match
                match_text = text[match_pos:match_pos + search_len]
                match_width = font_metrics.horizontalAdvance(match_text)

                # Draw highlight background
                highlight_rect = QRect(x_offset, text_rect.top(), match_width, text_rect.height())
                painter.fillRect(highlight_rect, self.highlight_color)

                # Draw match text (with bold if not selected)
                if not (option.state & QStyle.State_Selected):
                    font = painter.font()
                    font.setBold(True)
                    painter.setFont(font)
                    painter.drawText(x_offset, y_pos + font_metrics.ascent() // 2, match_text)
                    font.setBold(False)
                    painter.setFont(font)
                else:
                    painter.drawText(x_offset, y_pos + font_metrics.ascent() // 2, match_text)

                x_offset += match_width
                pos = match_pos + search_len
        else:
            # No highlighting needed, just draw normal text
            painter.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter, text)

        painter.restore()


class TableBrowser(QWidget):
    """Widget for browsing tables by category"""

    # Signal emitted when a table is selected
    table_selected = Signal(Table)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.definition = None
        self.modified_tables = set()  # Track modified table addresses
        self.current_level_filter = 0  # 0 = show all levels
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

        # Level filter
        level_layout = QHBoxLayout()
        level_label = QLabel("User Level:")
        level_layout.addWidget(level_label)

        self.level_combo = QComboBox()
        self.level_combo.addItem("All Levels", 0)
        self.level_combo.addItem("1 - Basic", 1)
        self.level_combo.addItem("2 - Intermediate", 2)
        self.level_combo.addItem("3 - Advanced", 3)
        self.level_combo.addItem("4 - Expert", 4)
        self.level_combo.addItem("5 - Developer", 5)
        self.level_combo.currentIndexChanged.connect(self._on_level_changed)
        self.level_combo.setToolTip("Filter tables by complexity level")
        level_layout.addWidget(self.level_combo)
        level_layout.addStretch()

        layout.addLayout(level_layout)

        # Tree widget for categories and tables
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Type", "Address"])
        self.tree.setColumnWidth(0, TABLE_BROWSER_COLUMN_WIDTH)
        # Open tables on double-click or Enter key (not single-click)
        self.tree.itemActivated.connect(self._on_item_activated)

        # Install custom delegate for highlighting and modified table colors
        self.delegate = HighlightDelegate(self.tree)
        self.tree.setItemDelegate(self.delegate)

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
                # Store modified flag (will be updated when tables are modified)
                is_modified = table.address in self.modified_tables
                table_item.setData(0, Qt.UserRole + 1, is_modified)
                category_item.addChild(table_item)

            # Collapse categories initially
            category_item.setExpanded(False)

    def _on_item_activated(self, item, column):
        """Handle item activation (Enter key or double-click)"""
        # Get table object stored in item
        table = item.data(0, 100)
        if table is not None:  # Not a category
            self.table_selected.emit(table)

    def _focus_search(self):
        """Focus the search box and select all text"""
        self.search_box.setFocus()
        self.search_box.selectAll()

    def _on_level_changed(self, index: int):
        """Handle level filter change"""
        self.current_level_filter = self.level_combo.currentData()
        self._apply_filters()

    def _apply_filters(self):
        """Apply both search and level filters"""
        self._filter_tables(self.search_box.text())

    def _filter_tables(self, search_text: str):
        """
        Filter the table tree based on search text and level

        Args:
            search_text: Text to search for in table names and categories
        """
        search_text = search_text.lower().strip()
        level_filter = self.current_level_filter

        # Update delegate with search text for highlighting
        self.delegate.set_search_text(search_text)
        self.tree.viewport().update()  # Force repaint

        # If no filters active, show all items
        if not search_text and level_filter == 0:
            self._show_all_items()
            return

        # Iterate through all category items
        for i in range(self.tree.topLevelItemCount()):
            category_item = self.tree.topLevelItem(i)
            category_name = category_item.text(0)
            category_has_visible = False

            # Check if category name matches search
            category_matches_search = not search_text or search_text in category_name.lower()

            # Check all table children
            for j in range(category_item.childCount()):
                table_item = category_item.child(j)
                table = table_item.data(0, 100)

                # Check level filter
                level_ok = level_filter == 0 or (table and table.level <= level_filter)

                # Check search filter
                if search_text:
                    name = table_item.text(0)
                    type_text = table_item.text(1)
                    address = table_item.text(2)
                    search_ok = (
                        search_text in name.lower() or
                        search_text in type_text.lower() or
                        search_text in address.lower() or
                        category_matches_search
                    )
                else:
                    search_ok = True

                # Item is visible if it passes both filters
                is_visible = level_ok and search_ok
                table_item.setHidden(not is_visible)

                if is_visible:
                    category_has_visible = True

            # Show category if it has visible children
            category_item.setHidden(not category_has_visible)

            # Only auto-expand categories when searching (not when changing level filter)
            if category_has_visible and search_text:
                category_item.setExpanded(True)
            elif not search_text:
                category_item.setExpanded(False)

    def _show_all_items(self):
        """Show all items in the tree (respecting level filter) and collapse categories"""
        level_filter = self.current_level_filter

        for i in range(self.tree.topLevelItemCount()):
            category_item = self.tree.topLevelItem(i)
            category_has_visible = False

            for j in range(category_item.childCount()):
                table_item = category_item.child(j)
                table = table_item.data(0, 100)

                # Check level filter
                if level_filter == 0:
                    is_visible = True
                else:
                    is_visible = table and table.level <= level_filter

                table_item.setHidden(not is_visible)
                if is_visible:
                    category_has_visible = True

            category_item.setHidden(not category_has_visible)

            # Collapse categories when search is cleared
            category_item.setExpanded(False)

    def mark_table_modified(self, table_address: str):
        """
        Mark a table as modified (will be displayed in pink)

        Args:
            table_address: Address of the modified table (e.g., "0x1000")
        """
        self.modified_tables.add(table_address)
        self._update_table_colors()

    def clear_modified_tables(self):
        """Clear all modified table markers"""
        self.modified_tables.clear()
        self._update_table_colors()

    def update_modified_tables(self, modified_table_names: list):
        """
        Update the list of modified tables

        Args:
            modified_table_names: List of table names that have been modified
        """
        # Clear and rebuild the set using table addresses
        self.modified_tables.clear()

        if self.definition:
            for table_name in modified_table_names:
                table = self.definition.get_table_by_name(table_name)
                if table:
                    self.modified_tables.add(table.address)

        self._update_table_colors()

    def _update_table_colors(self):
        """Update the modified flag for all table items"""
        for i in range(self.tree.topLevelItemCount()):
            category_item = self.tree.topLevelItem(i)

            for j in range(category_item.childCount()):
                table_item = category_item.child(j)
                table = table_item.data(0, 100)

                if table:
                    is_modified = table.address in self.modified_tables
                    table_item.setData(0, Qt.UserRole + 1, is_modified)

        # Force repaint
        self.tree.viewport().update()
