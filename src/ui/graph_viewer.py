"""
Graph Viewer

Embeddable graph panel (``GraphWidget``) for 2D/3D table data. Renders on the
GPU (pygfx, ``graph_gpu``) when a WebGPU adapter is available, else falls back
to the classic matplotlib renderer (``graph_classic``). Both are fed by the
single engine-neutral model in ``graph_model``.

The widget is constructed hidden for every 2D/3D table window, so it creates NO
canvas/device until the first ``set_data`` (first press of G), and imports no
graph library at module import time (startup stays lean).
"""

import logging

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

from ..core.rom_definition import RomDefinition, Table, TableType
from . import gpu_runtime, theme
from .graph_model import build_model, selection_colors

logger = logging.getLogger(__name__)

ROTATE_STEP = 10.0
ZOOM_STEP = 1.15

#: Keys the graph owns while it has focus. ``+``/``-``/``=`` are ALSO window
#: Edit shortcuts (Increment/Decrement/Set Value) — accepting ShortcutOverride
#: for them stops a zoom key press from silently editing ROM data (H1).
_GRAPH_KEYS = {
    Qt.Key_Left,
    Qt.Key_Right,
    Qt.Key_Up,
    Qt.Key_Down,
    Qt.Key_Plus,
    Qt.Key_Equal,
    Qt.Key_Minus,
    Qt.Key_Home,
    Qt.Key_R,
}

_engine_logged = set()


class GraphWidget(QWidget):
    """Embeddable graph widget for table data visualization.

    Public API (used by ``TableViewerWindow`` and ``tools/test_runner.py``):
    ``set_data``, ``update_data``, ``update_selection``, ``selected_cells``,
    ``get_view``, ``set_view``, ``reset_view``, ``engine_name``, ``shutdown``.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.table = None
        self.data = None
        self.rom_definition = None
        self.selected_cells = []
        self.backend = None
        self._pending = False
        self._model = None

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._status = QLabel("Preparing graph…", self)
        self._status.setAlignment(Qt.AlignCenter)
        self._status.setStyleSheet(theme.get_graph_status_stylesheet())
        self._status.hide()
        self._layout.addWidget(self._status)

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setFocusPolicy(Qt.StrongFocus)

    # ------------------------------------------------------------------ API
    @property
    def engine_name(self):
        return self.backend.engine_name if self.backend else None

    def set_data(
        self,
        table: Table,
        data: dict,
        rom_definition: RomDefinition = None,
        selected_cells: list = None,
    ):
        """Set (or fully rebuild) the graph for ``table``."""
        self.table = table
        self.data = data
        self.rom_definition = rom_definition
        self.selected_cells = list(selected_cells or [])
        if self.backend is None:
            self._request_backend()
            return
        self._guard(lambda: self.backend.show_model(self._build(), self.selected_cells))

    def update_data(self, data: dict):
        """Values/axes changed (edit, undo): rebuild geometry, keep the view."""
        self.data = data
        if self.table is None or self.backend is None:
            return
        self._guard(
            lambda: self.backend.update_data(self._build(), self.selected_cells)
        )

    def update_selection(self, selected_cells: list):
        """Selection-only change: recolor, no geometry rebuild."""
        self.selected_cells = list(selected_cells)
        if self.backend is None or self._model is None:
            return
        colors = selection_colors(self._model, self.selected_cells)
        self._guard(lambda: self.backend.update_colors(colors, self.selected_cells))

    def get_view(self):
        return self.backend.get_view() if self.backend else None

    def set_view(self, elev: float, azim: float):
        if self.backend:
            self.backend.set_view(elev, azim)

    def reset_view(self):
        if self.backend:
            self.backend.reset_view()

    def shutdown(self):
        """Release renderer resources (called from the window's closeEvent)."""
        if self.backend is not None:
            try:
                self.backend.shutdown()
            except Exception:  # noqa: BLE001 - teardown must not raise
                logger.debug("graph backend shutdown failed", exc_info=True)
            self.backend = None

    # ------------------------------------------------------------ internals
    def _build(self):
        self._model = build_model(
            self.table, self.data, self.rom_definition, self.selected_cells
        )
        return self._model

    def _request_backend(self):
        if self._pending:
            return
        self._pending = True
        self._status.show()
        gpu_runtime.when_ready(self._create_backend)

    def _create_backend(self, force_classic=False):
        self._pending = False
        engine, reason = gpu_runtime.engine_decision()
        if force_classic:
            engine = gpu_runtime.ENGINE_CLASSIC
        backend = None
        if engine == gpu_runtime.ENGINE_GPU:
            try:
                from .graph_gpu import GpuGraphView

                backend = GpuGraphView(self)
            except Exception as exc:  # noqa: BLE001
                logger.warning("GPU graph failed to start, using classic: %s", exc)
                reason = f"GPU init failed: {exc}"
        if backend is None:
            from .graph_classic import ClassicGraphView

            backend = ClassicGraphView(self)
        if backend.engine_name not in _engine_logged:
            _engine_logged.add(backend.engine_name)
            logger.info("Graph engine: %s (%s)", backend.engine_name, reason)
        self._install(backend)
        if self.table is not None and self.data is not None:
            self._guard(lambda: backend.show_model(self._build(), self.selected_cells))

    def _install(self, backend):
        self.backend = backend
        self._status.hide()
        w = backend.widget
        w.setFocusPolicy(Qt.StrongFocus)
        w.installEventFilter(self)
        self._layout.addWidget(w)
        self.setFocusProxy(w)
        if self.hasFocus() or self.isVisible():
            w.setFocus()

    def _guard(self, call):
        """Run a backend call; on a GPU failure swap to the classic renderer."""
        try:
            call()
        except Exception as exc:  # noqa: BLE001
            if self.backend is None or self.backend.engine_name != "gpu":
                raise
            logger.warning("GPU graph error, switching this graph to classic: %s", exc)
            old = self.backend
            self.backend = None
            try:
                old.widget.removeEventFilter(self)
                old.shutdown()
                old.widget.setParent(None)
            except Exception:  # noqa: BLE001
                pass
            self._create_backend(force_classic=True)

    # --------------------------------------------------------------- keys
    def eventFilter(self, obj, event):  # noqa: N802
        if self.backend is not None and obj is self.backend.widget:
            et = event.type()
            if et == QEvent.ShortcutOverride and self._owns_key(event):
                event.accept()
                return True
            if et == QEvent.KeyPress and self._handle_key(event):
                return True
        return super().eventFilter(obj, event)

    def _owns_key(self, event) -> bool:
        return event.key() in _GRAPH_KEYS and not (
            event.modifiers() & (Qt.ControlModifier | Qt.AltModifier)
        )

    def _handle_key(self, event) -> bool:
        if self.backend is None or not self._owns_key(event):
            return False
        key = event.key()
        b = self.backend
        if key in (Qt.Key_Plus, Qt.Key_Equal):
            b.zoom(ZOOM_STEP)
        elif key == Qt.Key_Minus:
            b.zoom(1.0 / ZOOM_STEP)
        elif key in (Qt.Key_Home, Qt.Key_R):
            b.reset_view()
        elif b.is_3d():
            if key == Qt.Key_Left:
                b.rotate(-ROTATE_STEP, 0)
            elif key == Qt.Key_Right:
                b.rotate(ROTATE_STEP, 0)
            elif key == Qt.Key_Up:
                b.rotate(0, ROTATE_STEP)
            elif key == Qt.Key_Down:
                b.rotate(0, -ROTATE_STEP)
        return True

    def event(self, event):  # noqa: N802 - focus may sit on the facade itself
        if event.type() == QEvent.ShortcutOverride and self._owns_key(event):
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event):  # noqa: N802
        if not self._handle_key(event):
            super().keyPressEvent(event)

    def is_2d_or_3d(self):
        return self.table is not None and self.table.type in (
            TableType.TWO_D,
            TableType.THREE_D,
        )
