"""
Table edit state

Per-ROM border/original-value bookkeeping for the table editor. This is the
single storage owner for "which cells are modified" (drawn with a border) and
the capture-once on-disk snapshots used to erase a border when a value is
edited back to its original (the 1e-10 smart-removal test lives in the viewer;
this class only stores).

Ownership (Phase 3, finding C1): one TableEditState instance is owned by each
RomDocument and shared with that ROM's open TableViewer(s) *by method*, never by
handing a raw mutable dict across an object boundary. A standalone TableViewer
(tests) makes its own throwaway instance. Because the state is an encapsulated
object rather than an aliased dict, both the viewer and MainWindow mutate it
through the same methods — no two owners of one dict.

Key format:
    modified cells  -> {table_address: set[(row, col)]}
    modified axis   -> {"table_address:axis_type": set[index]}
    originals       -> {table_address: {"values": np.ndarray,
                                        "x_axis": np.ndarray | None,
                                        "y_axis": np.ndarray | None}}
"""

import numpy as np


class TableEditState:
    """Storage for modified-cell borders and capture-once original values."""

    def __init__(self):
        self._modified_cells: dict = {}
        self._original_values: dict = {}

    # ── axis key (single source of the format) ──────────────────────────
    @staticmethod
    def _axis_key(table_address: str, axis_type: str) -> str:
        return f"{table_address}:{axis_type}"

    # ── cell borders ────────────────────────────────────────────────────
    def mark_cell_modified(self, table_address: str, row: int, col: int):
        self._modified_cells.setdefault(table_address, set()).add((row, col))

    def unmark_cell(self, table_address: str, row: int, col: int):
        cells = self._modified_cells.get(table_address)
        if cells is not None:
            cells.discard((row, col))

    def is_cell_modified(self, table_address: str, row: int, col: int) -> bool:
        return (row, col) in self._modified_cells.get(table_address, ())

    def mark_cells_modified(self, table_address: str, coords):
        """Bulk-mark a collection of (row, col) coordinates."""
        dest = self._modified_cells.setdefault(table_address, set())
        for row, col in coords:
            dest.add((row, col))

    # ── axis borders ────────────────────────────────────────────────────
    def mark_axis_modified(self, table_address: str, axis_type: str, index: int):
        key = self._axis_key(table_address, axis_type)
        self._modified_cells.setdefault(key, set()).add(index)

    def unmark_axis(self, table_address: str, axis_type: str, index: int):
        cells = self._modified_cells.get(self._axis_key(table_address, axis_type))
        if cells is not None:
            cells.discard(index)

    def is_axis_modified(self, table_address: str, axis_type: str, index: int) -> bool:
        key = self._axis_key(table_address, axis_type)
        return index in self._modified_cells.get(key, ())

    # ── capture-once originals ──────────────────────────────────────────
    def capture_originals(self, table_address: str, data: dict):
        """Snapshot on-disk values once per table (later calls are no-ops).

        Deep-copies the arrays so later in-place edits to the live table data
        never mutate the stored original.
        """
        if table_address in self._original_values:
            return
        self._original_values[table_address] = {
            "values": np.copy(data["values"]),
            "x_axis": (
                np.copy(data["x_axis"]) if data.get("x_axis") is not None else None
            ),
            "y_axis": (
                np.copy(data["y_axis"]) if data.get("y_axis") is not None else None
            ),
        }

    def get_original(self, table_address: str):
        return self._original_values.get(table_address)

    def reset_baseline(self):
        """Drop all borders and originals (used on commit/revert in Phase 3B).

        After this, the next table open re-captures the (now committed) bytes as
        the new original via the capture-once guard.
        """
        self._modified_cells.clear()
        self._original_values.clear()
