"""Classic (matplotlib) graph backend — the fallback when no GPU is usable.

Same look as the pre-GPU graph. Implements the backend interface used by
:class:`src.ui.graph_viewer.GraphWidget` (see ``graph_gpu.GpuGraphView`` for the
contract). matplotlib is imported lazily in ``__init__`` so it stays out of the
startup import graph.
"""

import numpy as np
from PySide6.QtWidgets import QSizePolicy

from ..core.rom_definition import TableType
from .gpu_runtime import ENGINE_CLASSIC
from .graph_model import (
    DEFAULT_VIEW,
    SELECTION_RGBA,
    GraphModel,
    extend_grid,
    selected_points,
)


class ClassicGraphView:
    engine_name = ENGINE_CLASSIC

    def __init__(self, parent=None):
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from matplotlib.figure import Figure

        self.figure = Figure(figsize=(8, 6))
        self.widget = FigureCanvasQTAgg(self.figure)
        self.widget.setParent(parent)
        self.widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.ax = None
        self.model = None
        self._selected = []
        # counters used by tests (A4/A11) — identical names on both backends
        self.n_data_updates = 0
        self.n_color_updates = 0
        self._zoom = 1.0  # cumulative keyboard zoom, re-applied after rebuilds

    # -- building -------------------------------------------------------------
    def show_model(self, model: GraphModel, selected_cells):
        view = self.get_view() if self.ax is not None else None
        self.model, self._selected = model, list(selected_cells)
        self.figure.clear()
        self.ax = None
        if model.kind == TableType.THREE_D:
            self._plot_3d()
            if view:
                self.ax.view_init(elev=view[0], azim=view[1])
        elif model.kind == TableType.TWO_D:
            self._plot_2d()
        self.widget.draw_idle()

    def _plot_3d(self):
        m = self.model
        ax = self.figure.add_subplot(111, projection="3d")
        rows, cols = m.values.shape
        X, Y = np.meshgrid(np.arange(cols + 1), np.arange(rows + 1))
        self._surface(ax, X, Y)
        ax.set_xticks([t.position for t in m.x_ticks])
        ax.set_xticklabels([t.label for t in m.x_ticks])
        ax.set_yticks([t.position for t in m.y_ticks])
        ax.set_yticklabels([t.label for t in m.y_ticks])
        ax.set_xlabel(m.x_title)
        ax.set_ylabel(m.y_title)
        ax.set_zlabel(m.value_title)
        ax.mouse_init()
        self.ax = ax

    def _surface(self, ax, X, Y):
        m = self.model
        ax.plot_surface(
            X,
            Y,
            np.nan_to_num(extend_grid(m.values)),
            facecolors=m.colors,
            linewidth=0.5,
            edgecolor="gray",
            antialiased=True,
            shade=False,
        )

    def _plot_2d(self):
        m = self.model
        ax = self.figure.add_subplot(111)
        x, v = m.x_values, m.values
        base = m.colors
        for i in range(len(x) - 1):
            ax.plot(x[i : i + 2], v[i : i + 2], color=tuple(base[i, :3]), linewidth=2)
        sx, sy = selected_points(m, self._selected)
        if len(sx):
            ax.scatter(sx, sy, color=SELECTION_RGBA, s=100, zorder=10, alpha=0.8)
        ax.set_xlabel(m.x_title)
        ax.set_ylabel(m.value_title)
        ax.grid(True, alpha=0.3)
        self.ax = ax

    # -- updates --------------------------------------------------------------
    def update_data(self, model: GraphModel, selected_cells):
        """Values/axes changed: rebuild geometry + labels, keep the view."""
        self.n_data_updates += 1
        if self.ax is None or model.kind != TableType.THREE_D:
            self.show_model(model, selected_cells)
            return
        view = self.get_view()
        self.show_model(model, selected_cells)  # fresh limits: Z follows data (H4)
        self.ax.view_init(elev=view[0], azim=view[1])
        if self._zoom != 1.0:
            self._scale_limits(self._zoom)

    def update_colors(self, colors: np.ndarray, selected_cells):
        """Selection-only change: recolor faces in place."""
        self.n_color_updates += 1
        self._selected = list(selected_cells)
        if self.model is None:
            return
        self.model.colors = colors
        if self.model.kind == TableType.THREE_D and self.ax is not None:
            if self.ax.collections:
                self.ax.collections[0].set_facecolors(colors.reshape(-1, 4))
            self.widget.draw_idle()
        else:
            self.show_model(self.model, selected_cells)

    # -- view -----------------------------------------------------------------
    def is_3d(self):
        return self.model is not None and self.model.kind == TableType.THREE_D

    def get_view(self):
        if self.ax is None or not hasattr(self.ax, "elev"):
            return None
        return (float(self.ax.elev), float(self.ax.azim))

    def set_view(self, elev, azim):
        if self.ax is None or not hasattr(self.ax, "elev"):
            return
        self.ax.view_init(elev=elev, azim=azim)
        self.widget.draw_idle()

    def get_zoom(self):
        return self._zoom

    def zoom(self, factor):
        if self.ax is None or not hasattr(self.ax, "elev"):
            return
        self._zoom *= factor
        self._scale_limits(factor)

    def _scale_limits(self, factor):
        for get, set_ in (
            (self.ax.get_xlim, self.ax.set_xlim),
            (self.ax.get_ylim, self.ax.set_ylim),
            (self.ax.get_zlim, self.ax.set_zlim),
        ):
            lo, hi = get()
            c, r = (lo + hi) / 2, (hi - lo) / factor
            set_(c - r / 2, c + r / 2)
        self.widget.draw_idle()

    def reset_view(self):
        self._zoom = 1.0
        if self.model is not None:
            self.show_model(self.model, self._selected)
            if self.is_3d():
                self.set_view(*DEFAULT_VIEW)

    def request_draw(self):
        self.widget.draw_idle()

    def grab(self):
        self.widget.draw()
        return self.widget.grab()

    def shutdown(self):
        try:
            self.figure.clear()
        except Exception:  # noqa: BLE001
            pass
        self.ax = None
        self.model = None
