"""
Graph Viewer

Displays 3D visualization of table data with rotation and selection highlighting.
Provides an embeddable widget (GraphWidget) for table data visualization.
"""

import logging

import numpy as np
from PySide6.QtWidgets import QVBoxLayout, QWidget, QSizePolicy
from PySide6.QtCore import Qt, QTimer

from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from ..core.rom_definition import Table, TableType, RomDefinition, AxisType
from ..utils.colormap import get_colormap
from ..utils.formatting import get_scaling_range

logger = logging.getLogger(__name__)


class _GraphPlotMixin:
    """Shared plotting logic for graph widgets.

    Subclasses must provide these attributes:
        table, data, rom_definition, selected_cells,
        _scaling_range, ax_3d, figure, canvas
    """

    _show_plot_title = False

    def _get_scaling_range(self):
        """Get min/max from the table's scaling definition, or None."""
        scaling_name = self.table.scaling if self.table else None
        return get_scaling_range(self.rom_definition, scaling_name)

    def _do_plot(self):
        """Core plot logic: save angles, clear, plot by type, restore angles.

        Does NOT call canvas.draw — subclasses handle that in _plot_data().
        Returns False if there was nothing to plot.
        """
        if self.table is None or self.data is None:
            return False

        # Save current view angles before clearing (for 3D plots)
        saved_elev = None
        saved_azim = None
        if self.ax_3d is not None:
            saved_elev = self.ax_3d.elev
            saved_azim = self.ax_3d.azim

        self.figure.clear()
        self.ax_3d = None

        if self.table.type == TableType.THREE_D:
            self._plot_3d()
            # Restore view angles if we had them
            if (
                saved_elev is not None
                and saved_azim is not None
                and self.ax_3d is not None
            ):
                self.ax_3d.view_init(elev=saved_elev, azim=saved_azim)
        elif self.table.type == TableType.TWO_D:
            self._plot_2d()
        else:
            self._plot_1d()

        return True

    def _plot_3d(self):
        """Plot 3D table as surface with uniform cell sizes"""
        ax = self.figure.add_subplot(111, projection="3d")

        values = self.data["values"]
        x_axis = self.data.get("x_axis")
        y_axis = self.data.get("y_axis")

        rows, cols = values.shape

        # Use uniform indices so all cells are the same size
        X, Y = np.meshgrid(np.arange(cols + 1), np.arange(rows + 1))

        # Extend Z values by duplicating last row and column
        Z_extended = np.zeros((rows + 1, cols + 1))
        Z_extended[:rows, :cols] = values
        Z_extended[rows, :cols] = values[-1, :]
        Z_extended[:rows, cols] = values[:, -1]
        Z_extended[rows, cols] = values[-1, -1]
        Z = Z_extended

        # Calculate colors based on gradient
        colors = self._calculate_colors(values)

        # Override colors for selected cells with blue
        if self.selected_cells:
            blue_color = np.array([0.0, 0.5, 1.0, 1.0])
            for row, col in self.selected_cells:
                if row < colors.shape[0] and col < colors.shape[1]:
                    colors[row, col] = blue_color

        # Plot surface
        ax.plot_surface(
            X,
            Y,
            Z,
            facecolors=colors,
            linewidth=0.5,
            edgecolor="gray",
            antialiased=True,
            shade=False,
        )

        # Set tick labels to actual axis values
        if x_axis is not None:
            if len(x_axis) > 6:
                step = max(1, len(x_axis) // 6)
                tick_idx = np.arange(0, len(x_axis), step)
            else:
                tick_idx = np.arange(len(x_axis))
            ax.set_xticks(tick_idx + 0.5)
            ax.set_xticklabels([f"{x_axis[i]:.4g}" for i in tick_idx])

        if y_axis is not None:
            if len(y_axis) > 6:
                step = max(1, len(y_axis) // 6)
                tick_idx = np.arange(0, len(y_axis), step)
            else:
                tick_idx = np.arange(len(y_axis))
            ax.set_yticks(tick_idx + 0.5)
            ax.set_yticklabels([f"{y_axis[i]:.4g}" for i in tick_idx])

        # Labels
        x_label = (
            self._get_axis_label(AxisType.X_AXIS) if x_axis is not None else "Column"
        )
        y_label = self._get_axis_label(AxisType.Y_AXIS) if y_axis is not None else "Row"
        ax.set_xlabel(x_label)
        ax.set_ylabel(y_label)
        ax.set_zlabel("Value")
        if self._show_plot_title:
            ax.set_title(f"{self.table.name}")

        ax.mouse_init()
        self.ax_3d = ax

    def _plot_2d(self):
        """Plot 2D table as line"""
        ax = self.figure.add_subplot(111)

        values = self.data["values"]
        y_axis = self.data.get("y_axis")

        if y_axis is not None:
            x = y_axis
        else:
            x = np.arange(len(values))

        colors = self._calculate_colors_1d(values)

        for i in range(len(x) - 1):
            ax.plot(x[i : i + 2], values[i : i + 2], color=colors[i], linewidth=2)

        # Highlight selected cells
        if self.selected_cells:
            selected_x = []
            selected_y = []
            for row, col in self.selected_cells:
                if row < len(values):
                    selected_x.append(x[row])
                    selected_y.append(values[row])
            if selected_x:
                ax.scatter(
                    selected_x, selected_y, color="blue", s=100, zorder=10, alpha=0.8
                )

        y_label = (
            self._get_axis_label(AxisType.Y_AXIS) if y_axis is not None else "Index"
        )
        ax.set_xlabel(y_label)
        ax.set_ylabel("Value")
        if self._show_plot_title:
            ax.set_title(f"{self.table.name}")
        ax.grid(True, alpha=0.3)

    def _plot_1d(self):
        """Plot 1D table as single bar"""
        ax = self.figure.add_subplot(111)
        values = self.data["values"]
        color = self._ratio_to_color(0.5)
        ax.bar([0], [values[0]], color=color, width=0.5)
        ax.set_ylabel("Value")
        if self._show_plot_title:
            ax.set_title(f"{self.table.name}")
        ax.set_xticks([])

    def _calculate_colors(self, values: np.ndarray):
        """Calculate color array matching table viewer gradient using scaling range.

        Uses vectorized numpy operations with the colormap's 256-entry LUT
        instead of per-cell Python function calls.
        """
        if self._scaling_range:
            min_val, max_val = self._scaling_range
        else:
            min_val = np.min(values)
            max_val = np.max(values)

        if max_val == min_val:
            ratios = np.full_like(values, 0.5)
        else:
            ratios = (values - min_val) / (max_val - min_val)
            ratios = np.clip(ratios, 0.0, 1.0)

        # Vectorized color lookup: map ratios to 0-255 indices, then batch-lookup
        # in the colormap's color table (avoids per-cell Python function calls)
        cmap = get_colormap()
        scaled = np.nan_to_num(ratios * 255, nan=127.0)
        indices = np.clip(scaled, 0, 255).astype(np.intp)
        color_lut = np.array(cmap.colors, dtype=np.float64) / 255.0
        colors = np.empty((*values.shape, 4))
        colors[..., :3] = color_lut[indices]
        colors[..., 3] = 1.0

        return colors

    def _calculate_colors_1d(self, values: np.ndarray):
        """Calculate colors for 1D array using scaling range"""
        if self._scaling_range:
            min_val, max_val = self._scaling_range
        else:
            min_val = np.min(values)
            max_val = np.max(values)

        if max_val == min_val:
            ratios = np.full_like(values, 0.5)
        else:
            ratios = (values - min_val) / (max_val - min_val)
            ratios = np.clip(ratios, 0.0, 1.0)

        # Vectorized: batch lookup into colormap LUT
        cmap = get_colormap()
        scaled = np.nan_to_num(ratios * 255, nan=127.0)
        indices = np.clip(scaled, 0, 255).astype(np.intp)
        color_lut = np.array(cmap.colors, dtype=np.float64) / 255.0
        return [tuple(color_lut[idx]) for idx in indices]

    def _ratio_to_rgba(self, ratio: float):
        """Convert ratio to RGBA tuple"""
        return get_colormap().ratio_to_rgba_float(ratio)

    def _ratio_to_color(self, ratio: float):
        """Convert ratio to RGB tuple"""
        rgba = self._ratio_to_rgba(ratio)
        return (rgba[0], rgba[1], rgba[2])

    def _get_axis_label(self, axis_type: AxisType) -> str:
        """Get axis label with unit, e.g., 'Engine Speed (RPM)'"""
        axis_table = self.table.get_axis(axis_type)
        if not axis_table:
            return "X Axis" if axis_type == AxisType.X_AXIS else "Y Axis"

        name = axis_table.name
        unit = ""

        if self.rom_definition and axis_table.scaling:
            scaling = self.rom_definition.get_scaling(axis_table.scaling)
            if scaling and scaling.units:
                unit = scaling.units

        if unit:
            return f"{name} ({unit})"
        return name

    def _handle_graph_key(self, event) -> bool:
        """Handle graph rotation/zoom keys. Returns True if handled."""
        if self.ax_3d is None:
            return False

        elev = self.ax_3d.elev
        azim = self.ax_3d.azim
        rotation_step = 10

        if event.key() == Qt.Key_Left:
            azim -= rotation_step
            self.ax_3d.view_init(elev=elev, azim=azim)
            self.canvas.draw()
        elif event.key() == Qt.Key_Right:
            azim += rotation_step
            self.ax_3d.view_init(elev=elev, azim=azim)
            self.canvas.draw()
        elif event.key() == Qt.Key_Up:
            elev += rotation_step
            self.ax_3d.view_init(elev=elev, azim=azim)
            self.canvas.draw()
        elif event.key() == Qt.Key_Down:
            elev -= rotation_step
            self.ax_3d.view_init(elev=elev, azim=azim)
            self.canvas.draw()
        elif event.key() in (Qt.Key_Plus, Qt.Key_Equal):
            self._zoom(1.1)
        elif event.key() == Qt.Key_Minus:
            self._zoom(0.9)
        else:
            return False
        return True

    def _zoom(self, factor):
        """Zoom in or out by adjusting axis limits"""
        if self.ax_3d is None:
            return

        xlim = self.ax_3d.get_xlim()
        ylim = self.ax_3d.get_ylim()
        zlim = self.ax_3d.get_zlim()

        x_center = (xlim[0] + xlim[1]) / 2
        y_center = (ylim[0] + ylim[1]) / 2
        z_center = (zlim[0] + zlim[1]) / 2

        x_range = (xlim[1] - xlim[0]) / factor
        y_range = (ylim[1] - ylim[0]) / factor
        z_range = (zlim[1] - zlim[0]) / factor

        self.ax_3d.set_xlim(x_center - x_range / 2, x_center + x_range / 2)
        self.ax_3d.set_ylim(y_center - y_range / 2, y_center + y_range / 2)
        self.ax_3d.set_zlim(z_center - z_range / 2, z_center + z_range / 2)

        self.canvas.draw()

    def update_selection(self, selected_cells: list):
        """Update the selected cells and redraw the graph"""
        self.selected_cells = selected_cells
        if self.table is None:
            return
        # For 3D plots, update facecolors in-place without recreating the surface.
        # This avoids the expensive plot_surface() call (~55-94ms) and reduces
        # the draw cost since the polygon collection doesn't change.
        if self.table.type == TableType.THREE_D and self.ax_3d is not None:
            self._update_3d_facecolors()
            self.canvas.draw_idle()
        else:
            self._plot_data()

    def _update_3d_facecolors(self):
        """Update 3D surface facecolors in-place (selection highlight only).

        Much faster than _update_3d_surface() because it reuses the existing
        Poly3DCollection instead of removing and recreating it.
        """
        ax = self.ax_3d
        if not ax.collections:
            return

        values = self.data["values"]
        colors = self._calculate_colors(values)

        if self.selected_cells:
            blue_color = np.array([0.0, 0.5, 1.0, 1.0])
            for row, col in self.selected_cells:
                if row < colors.shape[0] and col < colors.shape[1]:
                    colors[row, col] = blue_color

        # Flatten colors to match Poly3DCollection's face order
        # plot_surface creates one polygon per cell: (rows) x (cols) faces
        rows, cols = values.shape
        face_colors = colors.reshape(-1, 4)
        ax.collections[0].set_facecolors(face_colors)

    def _update_3d_surface(self):
        """Update 3D surface in-place without full replot (preserves view).

        Used when data values change (not just selection). Rebuilds the surface
        geometry since Z values may have changed.
        """
        ax = self.ax_3d

        # Save axis limits before modifying collections
        xlim = ax.get_xlim()
        ylim = ax.get_ylim()
        zlim = ax.get_zlim()

        # Remove old surface collections
        while ax.collections:
            ax.collections[0].remove()

        # Rebuild surface with updated data on the same axes
        values = self.data["values"]
        rows, cols = values.shape
        X, Y = np.meshgrid(np.arange(cols + 1), np.arange(rows + 1))

        Z_extended = np.zeros((rows + 1, cols + 1))
        Z_extended[:rows, :cols] = values
        Z_extended[rows, :cols] = values[-1, :]
        Z_extended[:rows, cols] = values[:, -1]
        Z_extended[rows, cols] = values[-1, -1]

        colors = self._calculate_colors(values)

        if self.selected_cells:
            blue_color = np.array([0.0, 0.5, 1.0, 1.0])
            for row, col in self.selected_cells:
                if row < colors.shape[0] and col < colors.shape[1]:
                    colors[row, col] = blue_color

        ax.plot_surface(
            X,
            Y,
            Z_extended,
            facecolors=colors,
            linewidth=0.5,
            edgecolor="gray",
            antialiased=True,
            shade=False,
        )

        # Restore axis limits to prevent auto-rescale
        ax.set_xlim(xlim)
        ax.set_ylim(ylim)
        ax.set_zlim(zlim)


class GraphWidget(_GraphPlotMixin, QWidget):
    """
    Embeddable graph widget for table data visualization

    Features:
    - 3D surface plot for 3D tables
    - 2D plot for 2D tables
    - Interactive rotation with mouse
    - Highlight selected cells
    - Color gradient matching table viewer
    """

    def __init__(self, parent=None):
        """Initialize graph widget without data (set later with set_data)"""
        super().__init__(parent)

        self.table = None
        self.data = None
        self.rom_definition = None
        self._scaling_range = None
        self.selected_cells = []
        self.ax_3d = None
        self._first_plot = True

        # Create matplotlib figure and canvas
        # No layout engine — constrained_layout is broken for 3D axes (collapses
        # to zero and adds ~200ms overhead per draw). Subplot params are set
        # manually in _plot_3d to provide stable sizing.
        self.figure = Figure(figsize=(8, 6))
        self.canvas = FigureCanvas(self.figure)

        # Set up layout
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.canvas)
        self.setLayout(layout)

        # Set size policy to expand
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumWidth(300)

        # Enable focus for keyboard handling
        self.setFocusPolicy(Qt.StrongFocus)

        # Connect canvas mouse press to grab focus
        self.canvas.mpl_connect("button_press_event", self._on_canvas_click)

    def set_data(
        self,
        table: Table,
        data: dict,
        rom_definition: RomDefinition = None,
        selected_cells: list = None,
    ):
        """Set or update the graph data"""
        self.table = table
        self.data = data
        self.rom_definition = rom_definition
        self.selected_cells = selected_cells or []
        self._scaling_range = self._get_scaling_range()
        self._plot_data()

    def _on_canvas_click(self, event):
        """Handle canvas click - grab focus for keyboard events"""
        self.setFocus()

    def update_data(self, data: dict):
        """Update just the data values (e.g., after cell edit)"""
        self.data = data
        if self.table is None:
            return
        if self.table.type == TableType.THREE_D and self.ax_3d is not None:
            self._update_3d_surface()
            self.canvas.draw_idle()
        else:
            self._plot_data()

    def _plot_data(self):
        """Plot the table data and schedule canvas redraw"""
        if not self._do_plot():
            return
        self.canvas.draw_idle()
        # On first plot, Qt may still have pending layout/resize events that
        # change the canvas size. Schedule one more redraw to prevent a visible
        # reframing snap. Skip on subsequent plots to avoid double-rendering.
        if self._first_plot:
            self._first_plot = False
            QTimer.singleShot(0, self.canvas.draw_idle)

    def keyPressEvent(self, event):
        """Handle key presses for graph rotation and zoom"""
        if not self._handle_graph_key(event):
            super().keyPressEvent(event)
