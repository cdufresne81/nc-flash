"""
TableViewer Context

Shared state object for TableViewer helper classes.
Provides access to common state and the parent viewer's signals.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional, Dict, Any, List

from PySide6.QtWidgets import QTableWidget, QHeaderView

if TYPE_CHECKING:
    from ..table_viewer import TableViewer
    from ...core.rom_definition import Table, RomDefinition


@dataclass
class TableViewerContext:
    """
    Shared context for TableViewer helper classes.

    Provides access to:
    - The parent TableViewer widget (for signal emission)
    - The QTableWidget for UI operations
    - ROM definition and current table/data state
    - Editing flags
    """
    viewer: 'TableViewer'
    table_widget: QTableWidget
    rom_definition: Optional['RomDefinition'] = None
    current_table: Optional['Table'] = None
    current_data: Optional[Dict[str, Any]] = None

    @property
    def editing_in_progress(self) -> bool:
        """Check if editing is in progress (suppresses signals)"""
        return self.viewer._editing_in_progress

    @editing_in_progress.setter
    def editing_in_progress(self, value: bool):
        """Set editing in progress flag"""
        self.viewer._editing_in_progress = value

    @property
    def read_only(self) -> bool:
        """Check if viewer is in read-only mode"""
        return self.viewer._read_only



def save_header_resize_modes(table_widget: QTableWidget):
    """
    Save per-section resize modes for both horizontal and vertical headers.

    Returns:
        Tuple of (h_header, v_header, h_resize_modes, v_resize_modes)
    """
    h_header = table_widget.horizontalHeader()
    v_header = table_widget.verticalHeader()
    h_resize_modes = [h_header.sectionResizeMode(i) for i in range(h_header.count())]
    v_resize_modes = [v_header.sectionResizeMode(i) for i in range(v_header.count())]
    return h_header, v_header, h_resize_modes, v_resize_modes


def set_headers_fixed(h_header, v_header):
    """Set all header sections to Fixed mode (prevents resize calculations per cell)."""
    for i in range(h_header.count()):
        h_header.setSectionResizeMode(i, QHeaderView.Fixed)
    for i in range(v_header.count()):
        v_header.setSectionResizeMode(i, QHeaderView.Fixed)


def restore_header_resize_modes(h_header, v_header,
                                h_resize_modes: List, v_resize_modes: List):
    """Restore previously saved per-section resize modes."""
    for i, mode in enumerate(h_resize_modes):
        if i < h_header.count():
            h_header.setSectionResizeMode(i, mode)
    for i, mode in enumerate(v_resize_modes):
        if i < v_header.count():
            v_header.setSectionResizeMode(i, mode)


@contextmanager
def frozen_table_updates(table_widget: QTableWidget):
    """
    Context manager that freezes a QTableWidget for bulk operations.

    On entry:
      - Disables widget updates (setUpdatesEnabled(False))
      - Blocks signals (blockSignals(True))
      - Saves per-section header resize modes and sets all to Fixed

    On exit (guaranteed via finally):
      - Restores header resize modes
      - Unblocks signals
      - Re-enables widget updates
      - Triggers a single viewport repaint
    """
    table_widget.setUpdatesEnabled(False)
    table_widget.blockSignals(True)

    h_header, v_header, h_modes, v_modes = save_header_resize_modes(table_widget)
    set_headers_fixed(h_header, v_header)
    try:
        yield
    finally:
        restore_header_resize_modes(h_header, v_header, h_modes, v_modes)
        table_widget.blockSignals(False)
        table_widget.setUpdatesEnabled(True)
        table_widget.viewport().update()
