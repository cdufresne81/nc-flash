"""
Table Viewer Window

Displays table data in a separate, independent window.
Allows opening multiple tables simultaneously for comparison.
Features an embedded graph panel that can be toggled with G key.
"""

import logging
from pathlib import Path

from PySide6.QtWidgets import (
    QMainWindow, QVBoxLayout, QHBoxLayout, QWidget, QApplication,
    QSplitter, QFrame, QMessageBox
)
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QKeySequence, QShortcut

from ..utils.constants import APP_NAME

logger = logging.getLogger(__name__)
from ..core.table_undo_manager import make_table_key
from .table_viewer import TableViewer
from .graph_viewer import GraphWidget
from .scaling_edit_dialog import TableScalingDialog
from ..core.rom_definition import Table, RomDefinition
from ..core.metadata_writer import update_scaling


class TableViewerWindow(QMainWindow):
    """
    Standalone window for viewing a single table

    Features:
    - Independent window that can be moved/resized
    - Shows table name in window title
    - Contains TableViewer widget for displaying data
    - Can have multiple windows open simultaneously
    - Editable cells with change tracking
    """

    # Forward cell_changed signal from viewer
    # Args: table, row, col, old_value, new_value, old_raw, new_raw
    cell_changed = Signal(Table, int, int, float, float, float, float)

    # Forward bulk_changes signal from viewer
    # Args: table, list of (row, col, old_value, new_value, old_raw, new_raw) tuples
    bulk_changes = Signal(Table, list)

    # Forward axis_changed signal from viewer
    # Args: table, axis_type ('x_axis' or 'y_axis'), index, old_value, new_value, old_raw, new_raw
    axis_changed = Signal(Table, str, int, float, float, float, float)

    # Forward axis_bulk_changes signal from viewer
    # Args: table, list of (axis_type, index, old_value, new_value, old_raw, new_raw) tuples
    axis_bulk_changes = Signal(Table, list)

    # Signal emitted when this window receives focus
    # Args: table_address (str)
    window_focused = Signal(str)

    def __init__(self, table: Table, data: dict, rom_definition: RomDefinition,
                 rom_path: str = None, parent=None,
                 modified_cells_dict: dict = None, original_values_dict: dict = None,
                 diff_mode: bool = False, diff_base_data: dict = None):
        """
        Initialize table viewer window

        Args:
            table: Table definition
            data: Table data dictionary from RomReader
            rom_definition: ROM definition containing scalings
            rom_path: Path to ROM file (for identifying duplicates)
            parent: Parent widget (optional)
            modified_cells_dict: Shared dict for tracking modified cells (persists across window instances)
            original_values_dict: Shared dict with original table values (for smart border removal)
            diff_mode: If True, show diff highlighting (read-only viewing)
            diff_base_data: Base version data to compare against in diff mode
        """
        super().__init__(parent)

        self.table = table
        self.data = data
        self.rom_definition = rom_definition
        self.rom_path = rom_path
        self._diff_mode = diff_mode
        self._graph_visible = False
        self._table_only_size = None  # Store size when graph is hidden

        # Remove minimize/maximize buttons, keep only close button
        self.setWindowFlags(
            Qt.Window |
            Qt.WindowCloseButtonHint |
            Qt.CustomizeWindowHint |
            Qt.WindowTitleHint
        )

        # Set window properties
        title = f"{table.name} ({table.address}) - {APP_NAME}"
        if diff_mode:
            title = f"{table.name} ({table.address}) (Changes) - {APP_NAME}"
        self.setWindowTitle(title)

        # Create central widget with minimal margins
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        # Main layout
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        central_widget.setLayout(layout)

        # Create splitter for table and graph
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setHandleWidth(3)
        layout.addWidget(self.splitter)

        # Create table viewer widget with shared tracking dicts
        self.viewer = TableViewer(
            rom_definition,
            modified_cells_dict=modified_cells_dict,
            original_values_dict=original_values_dict,
            diff_mode=diff_mode,
            diff_base_data=diff_base_data
        )

        # In diff mode, make the table read-only
        if diff_mode:
            self.viewer.set_read_only(True)

        self.splitter.addWidget(self.viewer)

        # Create graph widget only for 2D and 3D tables (initially hidden)
        from ..core.rom_definition import TableType
        if table.type != TableType.ONE_D:
            self.graph_widget = GraphWidget()
            self.splitter.addWidget(self.graph_widget)
            self.graph_widget.hide()
        else:
            self.graph_widget = None

        # Connect cell_changed signal
        self.viewer.cell_changed.connect(self._on_cell_changed)

        # Connect bulk_changes signal
        self.viewer.bulk_changes.connect(self._on_bulk_changes)

        # Connect axis_changed signal
        self.viewer.axis_changed.connect(self._on_axis_changed)

        # Connect axis_bulk_changes signal
        self.viewer.axis_bulk_changes.connect(self._on_axis_bulk_changes)

        # Connect selection changed to update graph
        self.viewer.table_widget.itemSelectionChanged.connect(self._on_table_selection_changed)

        # Set up debounce timer for graph refresh (prevents multiple refreshes during undo/redo)
        self._refresh_timer = QTimer()
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(50)  # 50ms debounce
        self._refresh_timer.timeout.connect(self._refresh_graph)

        # Debounce timer for selection-driven graph updates (arrow key navigation
        # fires itemSelectionChanged on every key press — full 3D re-render each
        # time makes the UI sluggish without debouncing)
        self._selection_timer = QTimer()
        self._selection_timer.setSingleShot(True)
        self._selection_timer.setInterval(100)  # 100ms debounce for selection
        self._selection_timer.timeout.connect(self._update_graph_selection)

        # Connect data_updated signal to debounced refresh (for undo/redo)
        self.viewer.data_updated.connect(self._schedule_graph_refresh)

        # Set up undo/redo shortcuts - route to main window's undo group
        undo_shortcut = QShortcut(QKeySequence.Undo, self)
        undo_shortcut.activated.connect(self._do_undo)
        redo_shortcut = QShortcut(QKeySequence.Redo, self)
        redo_shortcut.activated.connect(self._do_redo)

        # Set up Esc key to close window
        close_shortcut = QShortcut(QKeySequence(Qt.Key_Escape), self)
        close_shortcut.activated.connect(self.close)

        # Create menu bar
        self._create_menu_bar()

        # Display the table data
        self.viewer.display_table(table, data)

        # Auto-size window to fit content
        self._auto_size_window()

    def _create_menu_bar(self):
        """Create menu bar with File, Edit, and View menus"""
        menubar = self.menuBar()
        menubar.setStyleSheet("QMenuBar::item { padding: 2px 6px; }")

        # File menu (Alt+F)
        file_menu = menubar.addMenu("&File")

        copy_table_action = file_menu.addAction("Copy Table to Clipboard")
        copy_table_action.setShortcut("Ctrl+Shift+C")
        copy_table_action.triggered.connect(self.viewer.copy_table_to_clipboard)

        export_csv_action = file_menu.addAction("Export to CSV...")
        export_csv_action.setShortcut("Ctrl+E")
        export_csv_action.triggered.connect(self._export_to_csv)

        # Edit menu (Alt+E)
        edit_menu = menubar.addMenu("&Edit")

        increment_action = edit_menu.addAction("Increment")
        increment_action.setShortcut("+")
        increment_action.triggered.connect(self.viewer.increment_selection)

        decrement_action = edit_menu.addAction("Decrement")
        decrement_action.setShortcut("-")
        decrement_action.triggered.connect(self.viewer.decrement_selection)

        edit_menu.addSeparator()

        add_action = edit_menu.addAction("Add to Data...")
        add_action.triggered.connect(self.viewer.add_to_selection)

        multiply_action = edit_menu.addAction("Multiply Data...")
        multiply_action.setShortcut("*")
        multiply_action.triggered.connect(self.viewer.multiply_selection)

        set_action = edit_menu.addAction("Set Value...")
        set_action.setShortcut("=")
        set_action.triggered.connect(self.viewer.set_value_selection)

        edit_menu.addSeparator()

        interp_v_action = edit_menu.addAction("Interpolate Vertically")
        interp_v_action.setShortcut("V")
        interp_v_action.triggered.connect(self.viewer.interpolate_vertical)

        interp_h_action = edit_menu.addAction("Interpolate Horizontally")
        interp_h_action.setShortcut("H")
        interp_h_action.triggered.connect(self.viewer.interpolate_horizontal)

        interp_2d_action = edit_menu.addAction("Interpolate 2D")
        interp_2d_action.setShortcut("B")
        interp_2d_action.triggered.connect(self.viewer.interpolate_2d)

        smooth_action = edit_menu.addAction("Smooth Selection")
        smooth_action.setShortcut("S")
        smooth_action.triggered.connect(self.viewer.smooth_selection)

        edit_menu.addSeparator()

        edit_scaling_action = edit_menu.addAction("Edit Scaling...")
        edit_scaling_action.triggered.connect(self._edit_scaling)

        # View menu (Alt+V) - only for 2D/3D tables or diff mode
        from ..core.rom_definition import TableType
        has_view_options = self.table.type != TableType.ONE_D or self._diff_mode

        if has_view_options:
            view_menu = menubar.addMenu("&View")

            # Only show graph option for 2D and 3D tables
            if self.table.type != TableType.ONE_D:
                self.graph_action = view_menu.addAction("Show Graph")
                self.graph_action.setShortcut("G")
                self.graph_action.setCheckable(True)
                self.graph_action.setChecked(False)
                self.graph_action.triggered.connect(self._toggle_graph)
            else:
                self.graph_action = None

            # Add diff toggle when in diff mode
            if self._diff_mode:
                if self.table.type != TableType.ONE_D:
                    view_menu.addSeparator()

                self.toggle_diff_action = view_menu.addAction("Show Change Highlights")
                self.toggle_diff_action.setCheckable(True)
                self.toggle_diff_action.setChecked(True)
                self.toggle_diff_action.setShortcut("D")
                self.toggle_diff_action.triggered.connect(self._on_toggle_diff_highlights)
        else:
            self.graph_action = None

    def _on_toggle_diff_highlights(self):
        """Toggle diff highlighting visibility"""
        self.viewer.toggle_diff_highlights()
        self.toggle_diff_action.setChecked(self.viewer.show_diff_highlights())

    def _get_selected_data_cells(self):
        """Get list of selected data cells as (row, col) tuples"""
        selected_cells = []
        selected_ranges = self.viewer.table_widget.selectedRanges()

        for sel_range in selected_ranges:
            for row in range(sel_range.topRow(), sel_range.bottomRow() + 1):
                for col in range(sel_range.leftColumn(), sel_range.rightColumn() + 1):
                    item = self.viewer.table_widget.item(row, col)
                    if item and item.data(Qt.UserRole) is not None:
                        coords = item.data(Qt.UserRole)
                        # Only include data cells (not axis cells)
                        if not isinstance(coords[0], str):
                            # Store as (data_row, data_col) tuples
                            data_row = coords[0]
                            data_col = coords[1] if len(coords) > 1 else 0
                            selected_cells.append((data_row, data_col))

        return selected_cells

    def _on_table_selection_changed(self):
        """Handle table selection changes - debounce graph update"""
        if self._graph_visible and self.graph_widget:
            self._selection_timer.start()

    def _update_graph_selection(self):
        """Actually update graph selection (called after debounce)"""
        if self._graph_visible and self.graph_widget:
            selected_cells = self._get_selected_data_cells()
            self.graph_widget.update_selection(selected_cells)

    def _export_to_csv(self):
        """Export table to CSV and open with default application"""
        self.viewer.export_to_csv(self.rom_path)

    def _toggle_graph(self):
        """Toggle graph panel visibility"""
        # No graph for 1D tables
        if not self.graph_widget:
            return

        if self._graph_visible:
            # Hide graph
            self._graph_visible = False

            # Force graph widget to zero size (hidden widgets still affect splitter sizing)
            self.graph_widget.setMinimumWidth(0)
            self.graph_widget.setMaximumWidth(0)
            self.graph_widget.hide()
            self.graph_action.setChecked(False)

            # Resize window back to table-only size
            if self._table_only_size:
                self.resize(self._table_only_size)

        else:
            # Show graph
            # Save table-only size the first time (when None)
            if self._table_only_size is None:
                self._table_only_size = self.size()

            self._graph_visible = True
            # Reset size constraints before showing
            self.graph_widget.setMinimumWidth(0)
            self.graph_widget.setMaximumWidth(16777215)  # QWIDGETSIZE_MAX
            self.graph_widget.show()
            self.graph_action.setChecked(True)

            # Calculate new width based on saved table-only size
            table_width = self._table_only_size.width()
            graph_width = 550  # Default graph panel width
            new_width = table_width + graph_width

            # Limit to screen size
            screen = QApplication.primaryScreen()
            if screen:
                max_width = int(screen.availableGeometry().width() * 0.95)
                new_width = min(new_width, max_width)

            self.resize(new_width, self.height())

            # Set splitter proportions - table keeps its size, graph gets the rest
            self.splitter.setSizes([table_width, graph_width])

            # Defer graph initialization to the next event-loop iteration so
            # the splitter/resize layout is fully resolved before matplotlib's
            # constrained_layout measures widget dimensions.  Using
            # QTimer.singleShot(0, ...) is the safe alternative to the
            # re-entrant QApplication.processEvents() anti-pattern.
            QTimer.singleShot(0, self._init_graph_after_layout)

    def _init_graph_after_layout(self):
        """Initialize graph after layout has settled (deferred from _toggle_graph).

        Called via QTimer.singleShot(0, ...) so that the splitter resize is
        fully processed before matplotlib's constrained_layout measures the
        canvas dimensions.
        """
        if not self._graph_visible or not self.graph_widget:
            return
        selected_cells = self._get_selected_data_cells()
        self.graph_widget.set_data(
            self.table, self.data, self.rom_definition, selected_cells
        )
        # Set focus on graph so arrow keys work immediately
        self.graph_widget.setFocus()

    def _on_cell_changed(self, table_name: str, row: int, col: int,
                         old_value: float, new_value: float,
                         old_raw: float, new_raw: float):
        """Forward cell change signal with table object"""
        self.cell_changed.emit(
            self.table, row, col, old_value, new_value, old_raw, new_raw
        )
        # Schedule debounced graph refresh (consolidates with selection-change draws)
        self._schedule_graph_refresh()

    def _on_bulk_changes(self, changes: list):
        """Forward bulk changes signal with table object"""
        self.bulk_changes.emit(self.table, changes)
        # Schedule debounced graph refresh (consolidates with selection-change draws)
        self._schedule_graph_refresh()

    def _refresh_graph(self):
        """Refresh graph display after data changes (cell/axis edits).

        Uses update_data() which rebuilds the 3D surface geometry (Z values)
        in-place via _update_3d_surface(), preserving view angles and zoom.
        The graph widget holds a reference to the same data dict, so the
        new values are already visible to it — update_data re-reads them.

        Also cancels any pending selection-only timer since this refresh
        already includes the current selection, preventing a redundant draw.
        """
        if self._graph_visible and self.graph_widget:
            # Cancel pending selection update — this refresh supersedes it
            self._selection_timer.stop()
            selected_cells = self._get_selected_data_cells()
            self.graph_widget.selected_cells = selected_cells
            self.graph_widget.update_data(self.data)

    def _schedule_graph_refresh(self):
        """Schedule a debounced graph refresh (restarts timer on each call)"""
        self._refresh_timer.start()

    def _on_axis_changed(self, table_name: str, axis_type: str, index: int,
                         old_value: float, new_value: float,
                         old_raw: float, new_raw: float):
        """Forward axis change signal with table object"""
        self.axis_changed.emit(
            self.table, axis_type, index, old_value, new_value, old_raw, new_raw
        )
        # Schedule debounced graph refresh (consolidates with selection-change draws)
        self._schedule_graph_refresh()

    def _on_axis_bulk_changes(self, changes: list):
        """Forward axis bulk changes signal with table object"""
        self.axis_bulk_changes.emit(self.table, changes)
        # Schedule debounced graph refresh (consolidates with selection-change draws)
        self._schedule_graph_refresh()

    def _auto_size_window(self):
        """Auto-size window to fit table content, clamped to screen.

        Uses Qt header.length() for reliable column/row totals.
        resize() sets the client area — the OS title bar sits outside it.
        """
        table_widget = self.viewer.table_widget

        # Total width/height of all table columns/rows (reliable Qt API)
        table_w = table_widget.horizontalHeader().length()
        table_h = table_widget.verticalHeader().length()

        # Add visible header sizes
        if table_widget.verticalHeader().isVisible():
            table_w += table_widget.verticalHeader().width()
        if table_widget.horizontalHeader().isVisible():
            table_h += table_widget.horizontalHeader().height()

        # Table frame border (e.g. 1px per side)
        table_w += table_widget.frameWidth() * 2
        table_h += table_widget.frameWidth() * 2

        # Reserve space for scrollbars — they appear dynamically (AsNeeded
        # policy) and eat into the viewport, which can cascade: a vertical
        # scrollbar reduces viewport width, triggering a horizontal scrollbar
        # that reduces viewport height, etc.  Always reserving avoids this.
        table_w += table_widget.verticalScrollBar().sizeHint().width()
        table_h += table_widget.horizontalScrollBar().sizeHint().height()

        # Safety padding — scrollbar sizeHint() can underreport the actual
        # rendered size (especially on high-DPI or themed systems).  Add
        # one row/column as buffer so content is never clipped behind a
        # scrollbar, preventing unnecessary scrollbar appearance.
        if table_widget.rowCount() > 0:
            table_h += table_widget.rowHeight(0) - 10
        else:
            table_h += 14
        if table_widget.columnCount() > 0:
            table_w += table_widget.columnWidth(0) - 20
        else:
            table_w += 40

        # Build client area from layout components
        content_w = table_w
        content_h = table_h

        # Y-axis label (left of table, in horizontal layout with 2px spacing)
        if self.viewer.y_axis_label.isVisible():
            content_w += self.viewer.y_axis_label.sizeHint().width() + 2

        # X-axis label (above table, in vertical layout)
        if self.viewer.x_axis_label.isVisible():
            content_h += self.viewer.x_axis_label.sizeHint().height()

        # Menu bar
        content_h += self.menuBar().sizeHint().height()

        # Screen limits — availableGeometry() excludes the OS taskbar.
        # Subtract frame height (title bar + borders) so the on-screen
        # window fits; otherwise the OS silently shrinks the client area.
        screen = QApplication.primaryScreen()
        if screen:
            avail = screen.availableGeometry()
            max_w = avail.width()
            max_h = avail.height() - 40  # ~OS title bar + borders
        else:
            max_w = 1920
            max_h = 1040

        final_w = max(80, min(content_w, max_w))
        final_h = max(60, min(content_h, max_h))


        self.resize(final_w, final_h)

    def _edit_scaling(self):
        """Open dialog to edit all scalings for this table"""
        # Check if we have the XML path
        if not self.rom_definition.xml_path:
            QMessageBox.warning(
                self, "No XML Path",
                "Cannot edit scaling: XML path not available."
            )
            return

        dialog = TableScalingDialog(self.table, self.rom_definition, self)
        if dialog.exec():
            all_updates = dialog.get_all_updates()

            # Update each scaling in XML and memory
            xml_path = Path(self.rom_definition.xml_path)
            success_count = 0
            for scaling_name, (updates, scaling) in all_updates.items():
                if update_scaling(xml_path, scaling_name, updates):
                    self._apply_scaling_updates(scaling, updates)
                    success_count += 1

            # Refresh display to show updated format/units
            self.viewer.display_table(self.table, self.data)

            if success_count == len(all_updates):
                QMessageBox.information(
                    self, "Scalings Updated",
                    f"All scalings have been updated.\n"
                    "Changes are saved to the metadata file."
                )
            elif success_count > 0:
                QMessageBox.warning(
                    self, "Partial Update",
                    f"Only {success_count} of {len(all_updates)} scalings were updated.\n"
                    "Check the log for details."
                )
            else:
                QMessageBox.critical(
                    self, "Update Failed",
                    "Failed to update scalings in metadata file.\n"
                    "Check the log for details."
                )

    def _apply_scaling_updates(self, scaling, updates: dict):
        """Apply updates to in-memory scaling object"""
        if 'min' in updates:
            scaling.min = float(updates['min']) if updates['min'] else None
        if 'max' in updates:
            scaling.max = float(updates['max']) if updates['max'] else None
        if 'units' in updates:
            scaling.units = updates['units'] or ""
        if 'format' in updates:
            scaling.format = updates['format'] or "%0.2f"
        if 'inc' in updates:
            scaling.inc = float(updates['inc']) if updates['inc'] else None

    def _do_undo(self):
        """Perform undo via main window's undo group"""
        main_window = self.parent()
        if main_window and hasattr(main_window, 'table_undo_manager'):
            main_window.table_undo_manager.undo_group.undo()

    def _do_redo(self):
        """Perform redo via main window's undo group"""
        main_window = self.parent()
        if main_window and hasattr(main_window, 'table_undo_manager'):
            main_window.table_undo_manager.undo_group.redo()

    def closeEvent(self, event):
        """Handle window close event - deactivate undo stack and clean up resources"""
        main_window = self.parent()
        if main_window and hasattr(main_window, 'table_undo_manager'):
            main_window.table_undo_manager.set_active_stack(None)
        # Remove from parent's tracking list before deletion
        if main_window and hasattr(main_window, 'open_table_windows'):
            try:
                main_window.open_table_windows.remove(self)
            except ValueError:
                pass
        # Clean up matplotlib figure to prevent leak in global registry
        if self.graph_widget and hasattr(self.graph_widget, 'figure'):
            import matplotlib.pyplot as plt
            plt.close(self.graph_widget.figure)
        event.accept()
        # Schedule widget destruction for next event loop iteration
        self.deleteLater()

    def event(self, event):
        """Handle window events to detect activation/focus"""
        from PySide6.QtCore import QEvent
        # WindowActivate is fired when the window gains focus (clicked, alt-tabbed to, etc.)
        if event.type() == QEvent.WindowActivate:
            self.window_focused.emit(make_table_key(self.rom_path, self.table.address))
        return super().event(event)
