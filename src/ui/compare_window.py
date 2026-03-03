"""
ROM Comparison Window

Side-by-side comparison of two ROM files, showing only tables that differ.
Read-only view with synchronized scrolling and keyboard navigation.
"""

import logging

import numpy as np
from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QTreeWidget,
    QTreeWidgetItem,
    QLabel,
    QToolBar,
    QToolButton,
    QApplication,
    QStyledItemDelegate,
    QMessageBox,
)
from PySide6.QtCore import Qt, QSettings, QSize
from PySide6.QtGui import (
    QColor,
    QBrush,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
    QKeySequence,
    QShortcut,
)

from ..core.rom_definition import Table, TableType, AxisType, RomDefinition
from ..core.rom_reader import RomReader
from ..utils.colormap import get_colormap
from ..utils.formatting import (
    all_nan as _all_nan,
    format_value as _format_value,
    get_axis_format as _get_axis_format,
    get_scaling_format as _get_scaling_format,
    get_scaling_range as _get_scaling_range,
)
from ..utils.settings import get_settings

logger = logging.getLogger(__name__)


class _CompareCellDelegate(QStyledItemDelegate):
    """Draws a 2px gray border around changed cells, matching ModifiedCellDelegate."""

    BORDER_COLOR = QColor(100, 100, 100)
    BORDER_WIDTH = 2

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        if index.data(Qt.UserRole):
            painter.save()
            pen = QPen(self.BORDER_COLOR, self.BORDER_WIDTH)
            pen.setJoinStyle(Qt.MiterJoin)
            painter.setPen(pen)
            rect = option.rect.adjusted(1, 1, -1, -1)
            painter.drawRect(rect)
            painter.restore()


class CompareWindow(QMainWindow):
    """Side-by-side ROM comparison window"""

    def __init__(
        self,
        rom_reader_a: RomReader,
        rom_reader_b: RomReader,
        definition_a: RomDefinition,
        definition_b: RomDefinition,
        color_a: QColor,
        color_b: QColor,
        name_a: str,
        name_b: str,
        parent=None,
        readonly: bool = False,
    ):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        self._reader_a = rom_reader_a
        self._reader_b = rom_reader_b
        self._definition_a = definition_a
        self._definition_b = definition_b
        self._color_a = color_a
        self._color_b = color_b
        self._name_a = name_a
        self._name_b = name_b
        self._readonly = readonly
        self._cross_def = definition_a.romid.xmlid != definition_b.romid.xmlid
        self._changed_only = False
        self._syncing_scroll = False
        self._current_index = -1

        self.setWindowFlags(
            Qt.Window
            | Qt.WindowCloseButtonHint
            | Qt.WindowMaximizeButtonHint
            | Qt.CustomizeWindowHint
            | Qt.WindowTitleHint
        )
        title = f"ROM Compare \u2014 {name_a} vs {name_b}"
        if self._cross_def:
            title += f"  [{definition_a.romid.xmlid} \u2194 {definition_b.romid.xmlid}]"
        self.setWindowTitle(title)

        # Compute diffs for all tables
        self._modified_tables = []
        self._compute_diffs()

        if not self._modified_tables:
            # Will be handled by caller — show message and don't open
            return

        # Build UI
        self._build_ui()
        self._setup_shortcuts()

        # Select first table
        self._select_table(0)

        # Auto-size window
        self._auto_size()

    @property
    def has_diffs(self):
        return len(self._modified_tables) > 0

    def _match_tables(self):
        """Build name-based lookup matching tables across two definitions."""
        by_name_a = {t.name: t for t in self._definition_a.tables}
        by_name_b = {t.name: t for t in self._definition_b.tables}
        all_names = sorted(set(by_name_a) | set(by_name_b))
        matched = []
        for name in all_names:
            ta = by_name_a.get(name)
            tb = by_name_b.get(name)
            cat = (ta or tb).category or ""
            matched.append((name, cat, ta, tb))
        matched.sort(key=lambda m: (m[1], m[0]))
        return matched

    def _compute_diffs(self):
        """Compare all tables between the two ROMs and collect differences."""
        for name, category, table_a, table_b in self._match_tables():
            a_only = table_a is not None and table_b is None
            b_only = table_b is not None and table_a is None

            # Read data for each side
            data_a = None
            data_b = None
            if table_a is not None:
                try:
                    data_a = self._reader_a.read_table_data(table_a)
                except Exception as e:
                    logger.warning(f"Skipping table {name} (ROM A): {e}")
                    continue
            if table_b is not None:
                try:
                    data_b = self._reader_b.read_table_data(table_b)
                except Exception as e:
                    logger.warning(f"Skipping table {name} (ROM B): {e}")
                    continue

            # One-sided: mark all cells as changed
            if a_only:
                values_a = data_a.get("values")
                if values_a is None:
                    continue
                # Skip one-sided tables that are entirely NaN
                if _all_nan(values_a):
                    continue
                changed = set()
                if values_a.ndim == 1:
                    for i in range(len(values_a)):
                        changed.add((i, 0))
                else:
                    for idx in np.ndindex(values_a.shape):
                        changed.add(idx)
                self._modified_tables.append(
                    {
                        "table_a": table_a,
                        "table_b": None,
                        "name": name,
                        "category": category,
                        "data_a": data_a,
                        "data_b": None,
                        "changed_cells": changed,
                        "changed_axes": {},
                        "change_count": len(changed),
                        "shape_mismatch": False,
                        "a_only": True,
                        "b_only": False,
                    }
                )
                continue

            if b_only:
                values_b = data_b.get("values")
                if values_b is None:
                    continue
                # Skip one-sided tables that are entirely NaN
                if _all_nan(values_b):
                    continue
                changed = set()
                if values_b.ndim == 1:
                    for i in range(len(values_b)):
                        changed.add((i, 0))
                else:
                    for idx in np.ndindex(values_b.shape):
                        changed.add(idx)
                self._modified_tables.append(
                    {
                        "table_a": None,
                        "table_b": table_b,
                        "name": name,
                        "category": category,
                        "data_a": None,
                        "data_b": data_b,
                        "changed_cells": changed,
                        "changed_axes": {},
                        "change_count": len(changed),
                        "shape_mismatch": False,
                        "a_only": False,
                        "b_only": True,
                    }
                )
                continue

            # Both sides exist
            values_a = data_a.get("values")
            values_b = data_b.get("values")
            if values_a is None or values_b is None:
                continue

            # Skip tables where both sides are entirely NaN (unpatched tables)
            if _all_nan(values_a) and _all_nan(values_b):
                continue

            shape_mismatch = values_a.shape != values_b.shape

            if shape_mismatch:
                # Mark all cells as changed on both sides
                changed = set()
                for idx in np.ndindex(values_a.shape):
                    changed.add(idx)
                for idx in np.ndindex(values_b.shape):
                    changed.add(idx)
            elif np.array_equal(values_a, values_b):
                # Also check axes
                axes_differ = False
                for key in ("x_axis", "y_axis"):
                    ax_a = data_a.get(key)
                    ax_b = data_b.get(key)
                    if ax_a is not None and ax_b is not None:
                        if not np.array_equal(ax_a, ax_b):
                            axes_differ = True
                            break
                if not axes_differ:
                    continue
                changed = set()  # Only axes differ, no data cell changes
            else:
                # Find which cells differ
                changed = set()
                if values_a.ndim == 1:
                    for i in range(len(values_a)):
                        if values_a[i] != values_b[i]:
                            changed.add((i, 0))
                else:
                    diff_mask = values_a != values_b
                    for idx in zip(*np.where(diff_mask)):
                        changed.add(tuple(idx))

            # Check axis changes
            changed_axes = {}
            for key in ("x_axis", "y_axis"):
                ax_a = data_a.get(key)
                ax_b = data_b.get(key)
                if (
                    ax_a is not None
                    and ax_b is not None
                    and not np.array_equal(ax_a, ax_b)
                ):
                    axis_changed = set()
                    for i in range(min(len(ax_a), len(ax_b))):
                        if ax_a[i] != ax_b[i]:
                            axis_changed.add(i)
                    if axis_changed:
                        changed_axes[key] = axis_changed

            total_changes = len(changed) + sum(len(v) for v in changed_axes.values())
            if total_changes == 0 and not changed_axes:
                continue

            self._modified_tables.append(
                {
                    "table_a": table_a,
                    "table_b": table_b,
                    "name": name,
                    "category": category,
                    "data_a": data_a,
                    "data_b": data_b,
                    "changed_cells": changed,
                    "changed_axes": changed_axes,
                    "change_count": total_changes,
                    "shape_mismatch": shape_mismatch,
                    "a_only": False,
                    "b_only": False,
                }
            )

        # Sort by category then name for consistent ordering
        self._modified_tables.sort(key=lambda d: (d["category"] or "", d["name"]))

    def _build_ui(self):
        """Build the complete window UI."""
        # Central widget
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Toolbar
        self._build_toolbar()

        # Main splitter: sidebar + compare area
        self._main_splitter = QSplitter(Qt.Horizontal)
        main_layout.addWidget(self._main_splitter)

        # Sidebar
        self._build_sidebar()
        self._main_splitter.addWidget(self._sidebar)

        # Compare area (right side)
        compare_widget = QWidget()
        compare_layout = QVBoxLayout(compare_widget)
        compare_layout.setContentsMargins(0, 0, 0, 0)
        compare_layout.setSpacing(0)

        # Table panels splitter (includes compact "Original" / "Modified" labels)
        self._table_splitter = QSplitter(Qt.Horizontal)
        self._build_table_panels()
        compare_layout.addWidget(self._table_splitter)

        self._main_splitter.addWidget(compare_widget)
        self._main_splitter.setSizes([220, 800])
        self._main_splitter.setStretchFactor(0, 0)  # Sidebar fixed
        self._main_splitter.setStretchFactor(1, 1)  # Compare area stretches

        # Status bar
        self._status_label = QLabel()
        self.statusBar().addWidget(self._status_label, 1)
        self._shortcut_label = QLabel()
        self._shortcut_label.setStyleSheet("color: #888; font-size: 11px;")
        self.statusBar().addPermanentWidget(self._shortcut_label)
        self._shortcut_label.setText(
            "\u2191\u2193 Navigate   T Toggle changed only   Esc Close"
        )

    def _build_toolbar(self):
        """Create the toolbar with navigation and toggle controls."""
        tb = self.addToolBar("Compare")
        tb.setObjectName("compareToolbar")
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setIconSize(QSize(20, 20))
        tb.setStyleSheet("""
            QToolBar {
                spacing: 1px;
                padding: 1px 4px;
                border: none;
            }
            QToolButton {
                padding: 3px;
                border: 1px solid transparent;
                border-radius: 3px;
            }
            QToolButton:hover {
                background: rgba(128, 128, 128, 0.15);
                border: 1px solid rgba(128, 128, 128, 0.25);
            }
            QToolButton:pressed {
                background: rgba(128, 128, 128, 0.3);
            }
        """)

        # ROM labels with color swatches
        rom_label_a = self._make_rom_label(self._name_a, self._color_a)
        tb.addWidget(rom_label_a)

        vs_label = QLabel("  vs  ")
        vs_label.setStyleSheet("color: #999; font-size: 12px;")
        tb.addWidget(vs_label)

        rom_label_b = self._make_rom_label(self._name_b, self._color_b)
        tb.addWidget(rom_label_b)

        # Spacer
        spacer = QWidget()
        spacer.setSizePolicy(
            spacer.sizePolicy().horizontalPolicy().Expanding,
            spacer.sizePolicy().verticalPolicy().Preferred,
        )
        tb.addWidget(spacer)

        # Navigation
        prev_btn = tb.addAction(self._make_nav_icon("up"), "")
        prev_btn.setToolTip("Previous table (\u2191)")
        prev_btn.triggered.connect(self._prev_table)

        self._counter_label = QLabel(" 0 / 0 ")
        self._counter_label.setStyleSheet(
            "color: #555; font-size: 12px; padding: 0 4px;"
        )
        tb.addWidget(self._counter_label)

        next_btn = tb.addAction(self._make_nav_icon("down"), "")
        next_btn.setToolTip("Next table (\u2193)")
        next_btn.triggered.connect(self._next_table)

        # Separator
        tb.addSeparator()

        # Changed-only toggle
        toggle_label = QLabel(" Changed only ")
        toggle_label.setStyleSheet("font-size: 12px; color: #555;")
        tb.addWidget(toggle_label)

        from .widgets.toggle_switch import ToggleSwitch

        self._toggle = ToggleSwitch()
        self._toggle.setChecked(False)
        self._toggle.toggled.connect(self._on_toggle_changed)
        tb.addWidget(self._toggle)

        # Copy actions are in the center column between table panels (see _build_table_panels)

    def _make_rom_label(self, name: str, color: QColor) -> QWidget:
        """Create a ROM label widget with color swatch."""
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(4, 2, 8, 2)
        layout.setSpacing(4)

        # Color swatch
        swatch = QLabel()
        swatch.setFixedSize(12, 12)
        fill_color = color if color else self.palette().window().color()
        swatch.setStyleSheet(
            f"background-color: {fill_color.name()}; "
            f"border: 1px solid #888; border-radius: 2px;"
        )
        layout.addWidget(swatch)

        # Name
        label = QLabel(name)
        label.setStyleSheet("font-size: 12px; font-weight: 500;")
        layout.addWidget(label)

        return widget

    def _make_nav_icon(self, direction: str) -> QIcon:
        """Create navigation arrow icons matching existing toolbar style."""
        s = 20
        dpr = self.devicePixelRatioF()
        pm = QPixmap(int(s * dpr), int(s * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)

        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        c = self.palette().windowText().color()
        pen = QPen(c, 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        p.setPen(pen)

        if direction == "up":
            p.drawLine(4, 12, 10, 6)
            p.drawLine(10, 6, 16, 12)
        elif direction == "down":
            p.drawLine(4, 8, 10, 14)
            p.drawLine(10, 14, 16, 8)
        elif direction == "copy_right":
            # Right arrow with bar: document → right
            p.drawLine(4, 10, 14, 10)
            p.drawLine(11, 7, 14, 10)
            p.drawLine(11, 13, 14, 10)
            p.setPen(QPen(c, 2.0, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(17, 5, 17, 15)
        elif direction == "copy_left":
            # Left arrow with bar: document ← left
            p.drawLine(16, 10, 6, 10)
            p.drawLine(9, 7, 6, 10)
            p.drawLine(9, 13, 6, 10)
            p.setPen(QPen(c, 2.0, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(3, 5, 3, 15)

        p.end()
        return QIcon(pm)

    def _build_sidebar(self):
        """Build the modified tables sidebar with a category tree."""
        self._sidebar = QWidget()
        sidebar_layout = QVBoxLayout(self._sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(0)

        # Header
        header = QLabel(f"  Modified Tables ({len(self._modified_tables)})")
        header.setStyleSheet(
            "padding: 4px 10px; font-weight: 600; font-size: 12px; "
            "background: #f5f5f5; border-bottom: 1px solid #d0d0d0; "
            "color: #444;"
        )
        sidebar_layout.addWidget(header)

        # Tree widget grouped by category
        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setRootIsDecorated(True)
        self._tree.setIndentation(14)
        self._tree.setStyleSheet("""
            QTreeWidget {
                border: none;
                outline: none;
                font-size: 12px;
            }
            QTreeWidget::item {
                padding: 1px 0px;
            }
            QTreeWidget::item:selected {
                background: #e0ecf8;
                color: black;
            }
            QTreeWidget::item:hover:!selected {
                background: #f0f4fa;
            }
        """)

        # Group tables by category
        self._tree_items = {}  # diff index -> QTreeWidgetItem
        categories = {}
        for i, entry in enumerate(self._modified_tables):
            cat = entry["category"] or "Uncategorized"
            if cat not in categories:
                categories[cat] = []
            categories[cat].append((i, entry))

        for cat_name in sorted(categories.keys()):
            entries = categories[cat_name]
            cat_item = QTreeWidgetItem([f"{cat_name} ({len(entries)})"])
            cat_item.setData(0, Qt.UserRole, None)  # Mark as category
            self._tree.addTopLevelItem(cat_item)

            for idx, entry in entries:
                name = entry["name"]
                count = entry["change_count"]
                suffix = "cell" if count == 1 else "cells"
                label = f"{name}  ({count} {suffix})"
                if entry["a_only"]:
                    label += "  \u25c0"  # ◀ ROM A only
                elif entry["b_only"]:
                    label += "  \u25b6"  # ▶ ROM B only
                elif entry["shape_mismatch"]:
                    label += "  \u2260"  # ≠ shape mismatch
                item = QTreeWidgetItem([label])
                item.setData(0, Qt.UserRole, idx)
                cat_item.addChild(item)
                self._tree_items[idx] = item

            cat_item.setExpanded(True)

        self._tree.currentItemChanged.connect(self._on_tree_selection)
        sidebar_layout.addWidget(self._tree)

        self._sidebar.setMinimumWidth(200)
        self._sidebar.setMaximumWidth(300)

    def _build_table_panels(self):
        """Create the two QTableWidget panels with compact labels for side-by-side comparison."""
        font_size = get_settings().get_table_font_size()
        table_css = f"""
            QTableWidget {{
                font-size: {font_size}px;
                gridline-color: #a0a0a0;
            }}
            QTableWidget::item {{
                padding: 0px 1px;
            }}
        """
        row_height = font_size + 2
        label_css = "font-size: 11px; color: #888; padding: 2px 4px; border-bottom: 1px solid #d0d0d0;"

        # Left panel: ROM A name label + table
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)
        left_label = QLabel(f"  {self._name_a}")
        left_label.setStyleSheet(label_css)
        left_layout.addWidget(left_label)

        self._table_left = QTableWidget()
        self._table_left.setStyleSheet(table_css)
        self._table_left.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table_left.setSelectionMode(QTableWidget.NoSelection)
        self._table_left.verticalHeader().setVisible(False)
        self._table_left.horizontalHeader().setVisible(False)
        self._table_left.verticalHeader().setDefaultSectionSize(row_height)
        self._table_left.setItemDelegate(_CompareCellDelegate(self._table_left))
        left_layout.addWidget(self._table_left)

        # Right panel: ROM B name label + table
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        right_label = QLabel(f"  {self._name_b}")
        right_label.setStyleSheet(label_css)
        right_layout.addWidget(right_label)

        self._table_right = QTableWidget()
        self._table_right.setStyleSheet(table_css)
        self._table_right.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table_right.setSelectionMode(QTableWidget.NoSelection)
        self._table_right.verticalHeader().setVisible(False)
        self._table_right.horizontalHeader().setVisible(False)
        self._table_right.verticalHeader().setDefaultSectionSize(row_height)
        self._table_right.setItemDelegate(_CompareCellDelegate(self._table_right))
        right_layout.addWidget(self._table_right)

        # Center column with copy buttons (hidden in readonly mode)
        center_col = QWidget()
        center_layout = QVBoxLayout(center_col)
        center_layout.setContentsMargins(2, 0, 2, 0)
        center_layout.setSpacing(4)
        center_layout.addStretch()

        self._copy_a_to_b_btn = QToolButton()
        self._copy_a_to_b_btn.setIcon(self._make_nav_icon("copy_right"))
        self._copy_a_to_b_btn.setIconSize(QSize(20, 20))
        self._copy_a_to_b_btn.setToolTip(
            f"Copy table from {self._name_a} \u2192 {self._name_b}"
        )
        self._copy_a_to_b_btn.clicked.connect(lambda: self._copy_table("a_to_b"))
        self._copy_a_to_b_btn.setStyleSheet("""
            QToolButton {
                padding: 4px; border: 1px solid transparent; border-radius: 3px;
            }
            QToolButton:hover {
                background: rgba(128, 128, 128, 0.15);
                border: 1px solid rgba(128, 128, 128, 0.25);
            }
            QToolButton:pressed { background: rgba(128, 128, 128, 0.3); }
        """)
        center_layout.addWidget(self._copy_a_to_b_btn)

        self._copy_b_to_a_btn = QToolButton()
        self._copy_b_to_a_btn.setIcon(self._make_nav_icon("copy_left"))
        self._copy_b_to_a_btn.setIconSize(QSize(20, 20))
        self._copy_b_to_a_btn.setToolTip(
            f"Copy table from {self._name_b} \u2192 {self._name_a}"
        )
        self._copy_b_to_a_btn.clicked.connect(lambda: self._copy_table("b_to_a"))
        self._copy_b_to_a_btn.setStyleSheet(self._copy_a_to_b_btn.styleSheet())
        center_layout.addWidget(self._copy_b_to_a_btn)

        center_layout.addStretch()

        if self._readonly:
            center_col.setVisible(False)
            center_col.setFixedWidth(0)
        else:
            center_col.setFixedWidth(32)

        self._table_splitter.addWidget(left_panel)
        self._table_splitter.addWidget(center_col)
        self._table_splitter.addWidget(right_panel)

        # Prevent center column from being resized by splitter
        self._table_splitter.setCollapsible(1, False)
        self._table_splitter.setStretchFactor(0, 1)
        self._table_splitter.setStretchFactor(1, 0)
        self._table_splitter.setStretchFactor(2, 1)

        # Sync scrolling
        self._connect_scroll_sync(
            self._table_left.horizontalScrollBar(),
            self._table_right.horizontalScrollBar(),
        )
        self._connect_scroll_sync(
            self._table_left.verticalScrollBar(),
            self._table_right.verticalScrollBar(),
        )

    def _connect_scroll_sync(self, bar_a, bar_b):
        """Synchronize two scrollbars without recursive loops."""

        def sync_a_to_b(value):
            if not self._syncing_scroll:
                self._syncing_scroll = True
                bar_b.setValue(value)
                self._syncing_scroll = False

        def sync_b_to_a(value):
            if not self._syncing_scroll:
                self._syncing_scroll = True
                bar_a.setValue(value)
                self._syncing_scroll = False

        bar_a.valueChanged.connect(sync_a_to_b)
        bar_b.valueChanged.connect(sync_b_to_a)

    def _setup_shortcuts(self):
        """Set up keyboard shortcuts."""
        up = QShortcut(QKeySequence(Qt.Key_Up), self)
        up.activated.connect(self._prev_table)

        down = QShortcut(QKeySequence(Qt.Key_Down), self)
        down.activated.connect(self._next_table)

        toggle = QShortcut(QKeySequence(Qt.Key_T), self)
        toggle.activated.connect(
            lambda: self._toggle.setChecked(not self._toggle.isChecked())
        )

        close = QShortcut(QKeySequence(Qt.Key_Escape), self)
        close.activated.connect(self.close)

    # ========== Navigation ==========

    def _select_table(self, index: int):
        """Display the table at the given index in both panels."""
        if index < 0 or index >= len(self._modified_tables):
            return

        self._current_index = index
        entry = self._modified_tables[index]

        # Update sidebar tree
        self._tree.blockSignals(True)
        item = self._tree_items.get(index)
        if item:
            self._tree.setCurrentItem(item)
            self._tree.scrollToItem(item)
        self._tree.blockSignals(False)

        # Update counter
        self._counter_label.setText(f" {index + 1} / {len(self._modified_tables)} ")

        # Display table data in both panels
        table_a = entry["table_a"]
        table_b = entry["table_b"]

        if table_a is not None and entry["data_a"] is not None:
            self._populate_table(
                self._table_left,
                table_a,
                self._definition_a,
                entry["data_a"],
                entry["changed_cells"],
                entry["changed_axes"],
            )
        else:
            self._clear_panel(self._table_left)

        if table_b is not None and entry["data_b"] is not None:
            self._populate_table(
                self._table_right,
                table_b,
                self._definition_b,
                entry["data_b"],
                entry["changed_cells"],
                entry["changed_axes"],
            )
        else:
            self._clear_panel(self._table_right)

        # Sync column widths between panels (only when both have content)
        if table_a is not None and table_b is not None:
            self._sync_column_widths()

        # Update status bar
        name = entry["name"]
        count = entry["change_count"]
        suffix = "cell" if count == 1 else "cells"
        if entry["a_only"]:
            status = f"  {name} \u2014 {self._name_a} only ({count} {suffix})"
        elif entry["b_only"]:
            status = f"  {name} \u2014 {self._name_b} only ({count} {suffix})"
        elif entry["shape_mismatch"]:
            shape_a = entry["data_a"]["values"].shape if entry["data_a"] else "?"
            shape_b = entry["data_b"]["values"].shape if entry["data_b"] else "?"
            status = f"  {name} \u2014 shape mismatch {shape_a} vs {shape_b}"
        else:
            addr = table_a.address if table_a else table_b.address
            status = f"  {name} ({addr}) \u2014 {count} changed {suffix}"
        self._status_label.setText(status)

        # Update copy button state
        self._update_copy_buttons()

        # Reset scroll positions
        self._table_left.horizontalScrollBar().setValue(0)
        self._table_left.verticalScrollBar().setValue(0)

    def _clear_panel(self, widget: QTableWidget):
        """Clear a table panel (for one-sided tables)."""
        widget.blockSignals(True)
        widget.setUpdatesEnabled(False)
        widget.setRowCount(0)
        widget.setColumnCount(0)
        widget.blockSignals(False)
        widget.setUpdatesEnabled(True)
        widget.viewport().update()

    def _on_tree_selection(self, current, previous):
        """Handle sidebar tree selection."""
        if current:
            idx = current.data(0, Qt.UserRole)
            if idx is not None:
                self._select_table(idx)

    def _prev_table(self):
        """Navigate to the previous table."""
        if self._current_index > 0:
            self._select_table(self._current_index - 1)

    def _next_table(self):
        """Navigate to the next table."""
        if self._current_index < len(self._modified_tables) - 1:
            self._select_table(self._current_index + 1)

    # ========== Toggle ==========

    def _on_toggle_changed(self, checked: bool):
        """Handle changed-only toggle."""
        self._changed_only = checked
        # Re-display current table to apply dimming
        if self._current_index >= 0:
            self._select_table(self._current_index)

    # ========== Copy Table Data ==========

    def _update_copy_buttons(self):
        """Enable/disable copy buttons based on current table entry."""
        if self._readonly or self._current_index < 0:
            self._copy_a_to_b_btn.setEnabled(False)
            self._copy_b_to_a_btn.setEnabled(False)
            return
        entry = self._modified_tables[self._current_index]
        # Can copy A→B only if A has data and B's table exists (to write into)
        self._copy_a_to_b_btn.setEnabled(
            entry["table_a"] is not None
            and entry["table_b"] is not None
            and entry["data_a"] is not None
            and not entry["shape_mismatch"]
        )
        # Can copy B→A only if B has data and A's table exists (to write into)
        self._copy_b_to_a_btn.setEnabled(
            entry["table_a"] is not None
            and entry["table_b"] is not None
            and entry["data_b"] is not None
            and not entry["shape_mismatch"]
        )

    def _copy_table(self, direction: str):
        """Copy table values from one ROM to the other.

        Routes through MainWindow.apply_compare_copy() so the change goes
        through the full edit pipeline: undo, change tracking, modified
        indicator (*), and cell highlighting.
        """
        if self._current_index < 0:
            return
        entry = self._modified_tables[self._current_index]

        if direction == "a_to_b":
            src_name, dst_name = self._name_a, self._name_b
            src_data = entry["data_a"]
            dst_table, dst_reader, dst_def = (
                entry["table_b"],
                self._reader_b,
                self._definition_b,
            )
        else:
            src_name, dst_name = self._name_b, self._name_a
            src_data = entry["data_b"]
            dst_table, dst_reader, dst_def = (
                entry["table_a"],
                self._reader_a,
                self._definition_a,
            )

        if src_data is None or dst_table is None:
            return

        name = entry["name"]

        try:
            # Route through MainWindow for full undo/tracking pipeline
            main_window = self.parent()
            if main_window and hasattr(main_window, "apply_compare_copy"):
                main_window.apply_compare_copy(dst_reader, dst_table, dst_def, src_data)

            # Re-read destination data and refresh display
            if direction == "a_to_b":
                entry["data_b"] = dst_reader.read_table_data(dst_table)
            else:
                entry["data_a"] = dst_reader.read_table_data(dst_table)

            # Recompute changed cells for this entry
            self._recompute_entry_diff(entry)
            self._select_table(self._current_index)

            self.statusBar().showMessage(
                f'Copied "{name}" from {src_name} to {dst_name}'
            )
        except Exception as e:
            logger.error(f"Failed to copy table {name}: {e}")
            QMessageBox.warning(
                self, "Copy Failed", f"Failed to copy table:\n{type(e).__name__}: {e}"
            )

    def _recompute_entry_diff(self, entry: dict):
        """Recompute changed_cells/changed_axes for an entry after a copy."""
        data_a = entry["data_a"]
        data_b = entry["data_b"]

        if data_a is None or data_b is None:
            return

        values_a = data_a.get("values")
        values_b = data_b.get("values")
        if values_a is None or values_b is None:
            return

        if values_a.shape != values_b.shape:
            changed = set()
            for idx in np.ndindex(values_a.shape):
                changed.add(idx)
            for idx in np.ndindex(values_b.shape):
                changed.add(idx)
            entry["shape_mismatch"] = True
        elif np.array_equal(values_a, values_b):
            changed = set()
            entry["shape_mismatch"] = False
        else:
            changed = set()
            if values_a.ndim == 1:
                for i in range(len(values_a)):
                    if values_a[i] != values_b[i]:
                        changed.add((i, 0))
            else:
                diff_mask = values_a != values_b
                for idx in zip(*np.where(diff_mask)):
                    changed.add(tuple(idx))
            entry["shape_mismatch"] = False

        changed_axes = {}
        for key in ("x_axis", "y_axis"):
            ax_a = data_a.get(key)
            ax_b = data_b.get(key)
            if ax_a is not None and ax_b is not None and not np.array_equal(ax_a, ax_b):
                axis_changed = set()
                for i in range(min(len(ax_a), len(ax_b))):
                    if ax_a[i] != ax_b[i]:
                        axis_changed.add(i)
                if axis_changed:
                    changed_axes[key] = axis_changed

        entry["changed_cells"] = changed
        entry["changed_axes"] = changed_axes
        entry["change_count"] = len(changed) + sum(
            len(v) for v in changed_axes.values()
        )

    # ========== Table Population ==========

    def _populate_table(
        self,
        widget: QTableWidget,
        table: Table,
        rom_definition: RomDefinition,
        data: dict,
        changed_cells: set,
        changed_axes: dict,
    ):
        """Populate a QTableWidget with table data, highlighting changed cells."""
        widget.blockSignals(True)
        widget.setUpdatesEnabled(False)
        try:
            values = data["values"]

            if table.type == TableType.ONE_D:
                self._populate_1d(widget, table, rom_definition, values, changed_cells)
            elif table.type == TableType.TWO_D:
                self._populate_2d(
                    widget,
                    table,
                    rom_definition,
                    values,
                    data.get("y_axis"),
                    changed_cells,
                    changed_axes,
                )
            elif table.type == TableType.THREE_D:
                self._populate_3d(
                    widget,
                    table,
                    rom_definition,
                    values,
                    data.get("x_axis"),
                    data.get("y_axis"),
                    changed_cells,
                    changed_axes,
                )
        finally:
            widget.blockSignals(False)
            widget.setUpdatesEnabled(True)
            widget.viewport().update()

    def _populate_1d(
        self,
        widget: QTableWidget,
        table: Table,
        rom_definition: RomDefinition,
        values: np.ndarray,
        changed_cells: set,
    ):
        """Populate 1D table (single value)."""
        widget.setRowCount(1)
        widget.setColumnCount(1)

        value_fmt = _get_scaling_format(rom_definition, table.scaling)
        item = QTableWidgetItem(_format_value(values[0], value_fmt))

        color = self._get_cell_color(values[0], values, table, rom_definition)
        is_changed = (0, 0) in changed_cells

        if self._changed_only and not is_changed:
            item.setBackground(QBrush(self._dim_color(color)))
            item.setForeground(QBrush(QColor(0, 0, 0, 60)))
        else:
            item.setBackground(QBrush(color))
        if is_changed:
            item.setData(Qt.UserRole, True)

        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        widget.setItem(0, 0, item)
        widget.resizeColumnsToContents()

    def _populate_2d(
        self,
        widget: QTableWidget,
        table: Table,
        rom_definition: RomDefinition,
        values: np.ndarray,
        y_axis: np.ndarray,
        changed_cells: set,
        changed_axes: dict,
    ):
        """Populate 2D table (Y-axis + data column)."""
        num_values = len(values)
        widget.setRowCount(num_values)
        widget.setColumnCount(2)

        value_fmt = _get_scaling_format(rom_definition, table.scaling)
        y_fmt = _get_axis_format(rom_definition, table, AxisType.Y_AXIS)

        # Flip
        flipy = table.flipy
        display_values = values[::-1] if flipy else values
        display_y = y_axis[::-1] if (y_axis is not None and flipy) else y_axis

        # Y-axis gradient range
        y_min, y_max = self._get_axis_range(
            table, AxisType.Y_AXIS, display_y, rom_definition
        )
        y_changed = changed_axes.get("y_axis", set())

        for i in range(num_values):
            data_idx = (num_values - 1 - i) if flipy else i

            # Y-axis cell
            if display_y is not None and i < len(display_y):
                y_item = QTableWidgetItem(_format_value(display_y[i], y_fmt))
                y_color = self._gradient_color(display_y[i], y_min, y_max)
                is_y_changed = data_idx in y_changed
                if self._changed_only and not is_y_changed:
                    y_item.setBackground(QBrush(self._dim_color(y_color)))
                    y_item.setForeground(QBrush(QColor(0, 0, 0, 60)))
                else:
                    y_item.setBackground(QBrush(y_color))
                if is_y_changed:
                    y_item.setData(Qt.UserRole, True)
            else:
                y_item = QTableWidgetItem(str(i))
                y_item.setBackground(QBrush(QColor(220, 220, 220)))
            y_item.setFlags(y_item.flags() & ~Qt.ItemIsEditable)
            widget.setItem(i, 0, y_item)

            # Data cell
            val_item = QTableWidgetItem(_format_value(display_values[i], value_fmt))
            color = self._get_cell_color(
                display_values[i], values, table, rom_definition
            )
            is_changed = (data_idx, 0) in changed_cells

            if self._changed_only and not is_changed:
                val_item.setBackground(QBrush(self._dim_color(color)))
                val_item.setForeground(QBrush(QColor(0, 0, 0, 60)))
            else:
                val_item.setBackground(QBrush(color))
            if is_changed:
                val_item.setData(Qt.UserRole, True)
            val_item.setFlags(val_item.flags() & ~Qt.ItemIsEditable)
            widget.setItem(i, 1, val_item)

        widget.resizeColumnsToContents()

    def _populate_3d(
        self,
        widget: QTableWidget,
        table: Table,
        rom_definition: RomDefinition,
        values: np.ndarray,
        x_axis: np.ndarray,
        y_axis: np.ndarray,
        changed_cells: set,
        changed_axes: dict,
    ):
        """Populate 3D table with ECUFlash-style layout."""
        if values.ndim != 2:
            self._populate_1d(
                widget, table, rom_definition, values.flatten(), changed_cells
            )
            return

        rows, cols = values.shape

        widget.setRowCount(rows + 1)
        widget.setColumnCount(cols + 1)

        value_fmt = _get_scaling_format(rom_definition, table.scaling)
        x_fmt = _get_axis_format(rom_definition, table, AxisType.X_AXIS)
        y_fmt = _get_axis_format(rom_definition, table, AxisType.Y_AXIS)

        # Flips
        flipx = table.flipx
        flipy = table.flipy
        display_x = x_axis[::-1] if (x_axis is not None and flipx) else x_axis
        display_y = y_axis[::-1] if (y_axis is not None and flipy) else y_axis
        display_values = values.copy()
        if flipy:
            display_values = display_values[::-1, :]
        if flipx:
            display_values = display_values[:, ::-1]

        label_bg = QBrush(QColor(220, 220, 220))
        x_changed = changed_axes.get("x_axis", set())
        y_changed = changed_axes.get("y_axis", set())

        # Cell (0,0) - empty corner
        corner = QTableWidgetItem("")
        corner.setFlags(corner.flags() & ~Qt.ItemIsEditable)
        corner.setBackground(label_bg)
        widget.setItem(0, 0, corner)

        # X-axis range
        x_min, x_max = self._get_axis_range(
            table, AxisType.X_AXIS, display_x, rom_definition
        )

        # Row 0: X-axis values
        if display_x is not None and len(display_x) == cols:
            for col in range(cols):
                data_idx = (cols - 1 - col) if flipx else col
                x_item = QTableWidgetItem(_format_value(display_x[col], x_fmt))
                x_color = self._gradient_color(display_x[col], x_min, x_max)
                is_x_changed = data_idx in x_changed

                if self._changed_only and not is_x_changed:
                    x_item.setBackground(QBrush(self._dim_color(x_color)))
                    x_item.setForeground(QBrush(QColor(0, 0, 0, 60)))
                else:
                    x_item.setBackground(QBrush(x_color))
                if is_x_changed:
                    x_item.setData(Qt.UserRole, True)
                x_item.setFlags(x_item.flags() & ~Qt.ItemIsEditable)
                widget.setItem(0, col + 1, x_item)
        else:
            for col in range(cols):
                x_item = QTableWidgetItem(str(col))
                x_item.setFlags(x_item.flags() & ~Qt.ItemIsEditable)
                x_item.setBackground(label_bg)
                widget.setItem(0, col + 1, x_item)

        # Y-axis range
        y_min, y_max = self._get_axis_range(
            table, AxisType.Y_AXIS, display_y, rom_definition
        )

        # Column 0: Y-axis values (rows 1+)
        if display_y is not None and len(display_y) == rows:
            for row in range(rows):
                data_idx = (rows - 1 - row) if flipy else row
                y_item = QTableWidgetItem(_format_value(display_y[row], y_fmt))
                y_color = self._gradient_color(display_y[row], y_min, y_max)
                is_y_changed = data_idx in y_changed

                if self._changed_only and not is_y_changed:
                    y_item.setBackground(QBrush(self._dim_color(y_color)))
                    y_item.setForeground(QBrush(QColor(0, 0, 0, 60)))
                else:
                    y_item.setBackground(QBrush(y_color))
                if is_y_changed:
                    y_item.setData(Qt.UserRole, True)
                y_item.setFlags(y_item.flags() & ~Qt.ItemIsEditable)
                widget.setItem(row + 1, 0, y_item)
        else:
            for row in range(rows):
                y_item = QTableWidgetItem(str(row))
                y_item.setFlags(y_item.flags() & ~Qt.ItemIsEditable)
                y_item.setBackground(label_bg)
                widget.setItem(row + 1, 0, y_item)

        # Data cells (rows 1+, cols 1+)
        for row in range(rows):
            for col in range(cols):
                data_row = (rows - 1 - row) if flipy else row
                data_col = (cols - 1 - col) if flipx else col

                val_item = QTableWidgetItem(
                    _format_value(display_values[row, col], value_fmt)
                )

                color = self._get_cell_color(
                    display_values[row, col], values, table, rom_definition
                )

                is_changed = (data_row, data_col) in changed_cells

                if self._changed_only and not is_changed:
                    val_item.setBackground(QBrush(self._dim_color(color)))
                    val_item.setForeground(QBrush(QColor(0, 0, 0, 60)))
                else:
                    val_item.setBackground(QBrush(color))
                if is_changed:
                    val_item.setData(Qt.UserRole, True)

                val_item.setFlags(val_item.flags() & ~Qt.ItemIsEditable)
                widget.setItem(row + 1, col + 1, val_item)

        # Resize columns
        widget.resizeColumnsToContents()

        # Uniform data column width (match existing pattern)
        max_width = 0
        for col in range(1, widget.columnCount()):
            w = widget.columnWidth(col)
            if w > max_width:
                max_width = w
        if max_width > 0:
            header = widget.horizontalHeader()
            header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
            for col in range(1, widget.columnCount()):
                header.setSectionResizeMode(col, QHeaderView.Fixed)
                widget.setColumnWidth(col, max_width)

    def _sync_column_widths(self):
        """Ensure both table panels have identical column widths."""
        left = self._table_left
        right = self._table_right
        count = min(left.columnCount(), right.columnCount())
        for col in range(count):
            w = max(left.columnWidth(col), right.columnWidth(col))
            left.setColumnWidth(col, w)
            right.setColumnWidth(col, w)
            left.horizontalHeader().setSectionResizeMode(col, QHeaderView.Fixed)
            right.horizontalHeader().setSectionResizeMode(col, QHeaderView.Fixed)

    # ========== Color Helpers ==========

    def _gradient_color(self, value: float, min_val: float, max_val: float) -> QColor:
        """Get thermal gradient color for a value within a range."""
        if max_val == min_val:
            ratio = 0.5
        else:
            ratio = (value - min_val) / (max_val - min_val)
            ratio = max(0.0, min(1.0, ratio))
        return get_colormap().ratio_to_color(ratio)

    def _get_cell_color(
        self,
        value: float,
        values: np.ndarray,
        table: Table,
        rom_definition: RomDefinition,
    ) -> QColor:
        """Get thermal gradient color for a data cell value."""
        scaling_range = _get_scaling_range(rom_definition, table.scaling)
        if scaling_range:
            min_val, max_val = scaling_range
        else:
            min_val = float(np.min(values))
            max_val = float(np.max(values))
        return self._gradient_color(value, min_val, max_val)

    def _get_axis_range(
        self,
        table: Table,
        axis_type: AxisType,
        display_axis: np.ndarray,
        rom_definition: RomDefinition,
    ):
        """Get min/max for axis gradient coloring."""
        axis_table = table.get_axis(axis_type)
        if axis_table and axis_table.scaling:
            sr = _get_scaling_range(rom_definition, axis_table.scaling)
            if sr:
                return sr
        if display_axis is not None and len(display_axis) > 0:
            return float(np.min(display_axis)), float(np.max(display_axis))
        return 0.0, 1.0

    def _dim_color(self, color: QColor) -> QColor:
        """Return a dimmed version of a color for unchanged cells."""
        # Blend toward white with high transparency
        r, g, b, _ = color.getRgb()
        # Mix 75% white + 25% original
        return QColor(
            r + (255 - r) * 3 // 4,
            g + (255 - g) * 3 // 4,
            b + (255 - b) * 3 // 4,
        )

    # ========== Window Sizing ==========

    def _auto_size(self):
        """Size the window to fit the content, or restore saved geometry."""
        settings = QSettings()
        geometry = settings.value("ui/compare_window_geometry")
        if geometry:
            self.restoreGeometry(geometry)
            return
        screen = QApplication.primaryScreen()
        if screen:
            avail = screen.availableGeometry()
            max_w = int(avail.width() * 0.9)
            max_h = int(avail.height() * 0.85)
        else:
            max_w = 1400
            max_h = 800

        # Start with a reasonable default
        self.resize(min(1200, max_w), min(700, max_h))

    def closeEvent(self, event):
        """Save geometry and clean up on close."""
        settings = QSettings()
        settings.setValue("ui/compare_window_geometry", self.saveGeometry())
        parent = self.parent()
        if parent and hasattr(parent, "compare_window"):
            parent.compare_window = None
        event.accept()
        self.deleteLater()
