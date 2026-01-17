"""
Graph Viewer

Displays 3D visualization of table data with rotation and selection highlighting.
Provides both a standalone window (GraphViewer) and embeddable widget (GraphWidget).
"""

import numpy as np
from PySide6.QtWidgets import QMainWindow, QVBoxLayout, QWidget, QSizePolicy
from PySide6.QtCore import Qt

from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from ..core.rom_definition import Table, TableType, RomDefinition, AxisType
from ..utils.constants import APP_NAME
from ..utils.colormap import get_colormap


class GraphWidget(QWidget):
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
        self.selected_cells = []
        self.ax_3d = None

        # Create matplotlib figure and canvas
        # Use constrained_layout for stable sizing (avoids resizing on redraws)
        self.figure = Figure(figsize=(8, 6), layout='constrained')
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
        self.canvas.mpl_connect('button_press_event', self._on_canvas_click)

    def set_data(self, table: Table, data: dict, rom_definition: RomDefinition = None,
                 selected_cells: list = None):
        """
        Set or update the graph data

        Args:
            table: Table definition
            data: Table data dictionary
            rom_definition: ROM definition containing scalings
            selected_cells: List of (row, col) tuples for selected cells
        """
        self.table = table
        self.data = data
        self.rom_definition = rom_definition
        self.selected_cells = selected_cells or []
        self._plot_data()

    def update_selection(self, selected_cells: list):
        """Update the selected cells and redraw the graph"""
        self.selected_cells = selected_cells
        if self.table is not None:
            self._plot_data()

    def _on_canvas_click(self, event):
        """Handle canvas click - grab focus for keyboard events"""
        self.setFocus()

    def update_data(self, data: dict):
        """Update just the data values (e.g., after cell edit)"""
        self.data = data
        if self.table is not None:
            self._plot_data()

    def _plot_data(self):
        """Plot the table data based on table type"""
        if self.table is None or self.data is None:
            return

        # Save current view angles before clearing (for 3D plots)
        saved_elev = None
        saved_azim = None
        if self.ax_3d is not None:
            saved_elev = self.ax_3d.elev
            saved_azim = self.ax_3d.azim

        self.figure.clear()

        # Reset ax_3d for non-3D plots
        self.ax_3d = None

        if self.table.type == TableType.THREE_D:
            self._plot_3d()
            # Restore view angles if we had them
            if saved_elev is not None and saved_azim is not None and self.ax_3d is not None:
                self.ax_3d.view_init(elev=saved_elev, azim=saved_azim)
        elif self.table.type == TableType.TWO_D:
            self._plot_2d()
        else:
            self._plot_1d()

        self.canvas.draw()

    def _plot_3d(self):
        """Plot 3D table as surface"""
        ax = self.figure.add_subplot(111, projection='3d')

        values = self.data['values']
        x_axis = self.data.get('x_axis')
        y_axis = self.data.get('y_axis')

        rows, cols = values.shape

        # Extend axes by one point (extrapolate with same spacing)
        if x_axis is not None and y_axis is not None:
            x_step = x_axis[1] - x_axis[0] if len(x_axis) > 1 else 1
            y_step = y_axis[1] - y_axis[0] if len(y_axis) > 1 else 1
            x_extended = np.append(x_axis, x_axis[-1] + x_step)
            y_extended = np.append(y_axis, y_axis[-1] + y_step)
            X, Y = np.meshgrid(x_extended, y_extended)
        else:
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
            blue_color = (0.0, 0.5, 1.0, 1.0)
            for row, col in self.selected_cells:
                if row < colors.shape[0] and col < colors.shape[1]:
                    colors[row, col] = blue_color

        # Plot surface
        surf = ax.plot_surface(X, Y, Z, facecolors=colors,
                               linewidth=0.5, edgecolor='gray',
                               antialiased=True, shade=False)

        # Labels
        x_label = self._get_axis_label(AxisType.X_AXIS) if x_axis is not None else 'Column'
        y_label = self._get_axis_label(AxisType.Y_AXIS) if y_axis is not None else 'Row'
        ax.set_xlabel(x_label)
        ax.set_ylabel(y_label)
        ax.set_zlabel('Value')

        ax.mouse_init()
        self.ax_3d = ax

    def _plot_2d(self):
        """Plot 2D table as line"""
        ax = self.figure.add_subplot(111)

        values = self.data['values']
        y_axis = self.data.get('y_axis')

        if y_axis is not None:
            x = y_axis
        else:
            x = np.arange(len(values))

        colors = self._calculate_colors_1d(values)

        for i in range(len(x) - 1):
            ax.plot(x[i:i+2], values[i:i+2], color=colors[i], linewidth=2)

        # Highlight selected cells
        if self.selected_cells:
            selected_x = []
            selected_y = []
            for row, col in self.selected_cells:
                if row < len(values):
                    selected_x.append(x[row])
                    selected_y.append(values[row])
            if selected_x:
                ax.scatter(selected_x, selected_y, color='blue', s=100, zorder=10, alpha=0.8)

        y_label = self._get_axis_label(AxisType.Y_AXIS) if y_axis is not None else 'Index'
        ax.set_xlabel(y_label)
        ax.set_ylabel('Value')
        ax.grid(True, alpha=0.3)

    def _plot_1d(self):
        """Plot 1D table as single bar"""
        ax = self.figure.add_subplot(111)
        values = self.data['values']
        color = self._ratio_to_color(0.5)
        ax.bar([0], [values[0]], color=color, width=0.5)
        ax.set_ylabel('Value')
        ax.set_xticks([])

    def _calculate_colors(self, values: np.ndarray):
        """Calculate color array matching table viewer gradient"""
        min_val = np.min(values)
        max_val = np.max(values)

        if max_val == min_val:
            ratios = np.full_like(values, 0.5)
        else:
            ratios = (values - min_val) / (max_val - min_val)

        colors = np.zeros((*values.shape, 4))
        for i in range(values.shape[0]):
            for j in range(values.shape[1]):
                rgba = self._ratio_to_rgba(ratios[i, j])
                colors[i, j] = rgba

        return colors

    def _calculate_colors_1d(self, values: np.ndarray):
        """Calculate colors for 1D array"""
        min_val = np.min(values)
        max_val = np.max(values)

        if max_val == min_val:
            ratios = np.full_like(values, 0.5)
        else:
            ratios = (values - min_val) / (max_val - min_val)

        return [self._ratio_to_color(r) for r in ratios]

    def _ratio_to_rgba(self, ratio: float):
        """Convert ratio to RGBA tuple"""
        return get_colormap().ratio_to_rgba_float(ratio)

    def _ratio_to_color(self, ratio: float):
        """Convert ratio to RGB tuple"""
        rgba = self._ratio_to_rgba(ratio)
        return (rgba[0], rgba[1], rgba[2])

    def _get_axis_label(self, axis_type: AxisType) -> str:
        """Get axis label with unit"""
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

    def keyPressEvent(self, event):
        """Handle key presses for graph rotation and zoom"""
        if self.ax_3d is None:
            super().keyPressEvent(event)
            return

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
            super().keyPressEvent(event)

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

        self.ax_3d.set_xlim(x_center - x_range/2, x_center + x_range/2)
        self.ax_3d.set_ylim(y_center - y_range/2, y_center + y_range/2)
        self.ax_3d.set_zlim(z_center - z_range/2, z_center + z_range/2)

        self.canvas.draw()


class GraphViewer(QMainWindow):
    """
    3D Graph viewer for table data

    Features:
    - 3D surface plot for 3D tables
    - 2D plot for 2D tables
    - Interactive rotation with mouse
    - Highlight selected cells
    - Color gradient matching table viewer
    """

    def __init__(self, table: Table, data: dict, rom_definition: RomDefinition = None,
                 selected_cells: list = None, parent=None):
        """
        Initialize graph viewer

        Args:
            table: Table definition
            data: Table data dictionary
            rom_definition: ROM definition containing scalings
            selected_cells: List of (row, col) tuples for selected cells
            parent: Parent widget
        """
        super().__init__(parent)

        self.table = table
        self.data = data
        self.rom_definition = rom_definition
        self.selected_cells = selected_cells or []
        self.ax_3d = None  # Store 3D axes for rotation control

        # Set window properties
        self.setWindowTitle(f"{table.name} - Graph View - {APP_NAME}")
        self.setWindowFlags(
            Qt.Window |
            Qt.WindowCloseButtonHint |
            Qt.CustomizeWindowHint |
            Qt.WindowTitleHint
        )

        # Create matplotlib figure and canvas
        self.figure = Figure(figsize=(10, 8))
        self.canvas = FigureCanvas(self.figure)

        # Set up layout
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        layout.addWidget(self.canvas)

        # Plot the data
        self._plot_data()

        # Set window size
        self.resize(800, 600)

    def _plot_data(self):
        """Plot the table data based on table type"""
        # Save current view angles before clearing (for 3D plots)
        saved_elev = None
        saved_azim = None
        if self.ax_3d is not None:
            saved_elev = self.ax_3d.elev
            saved_azim = self.ax_3d.azim

        self.figure.clear()

        if self.table.type == TableType.THREE_D:
            self._plot_3d()
            # Restore view angles if we had them
            if saved_elev is not None and saved_azim is not None and self.ax_3d is not None:
                self.ax_3d.view_init(elev=saved_elev, azim=saved_azim)
        elif self.table.type == TableType.TWO_D:
            self._plot_2d()
        else:
            self._plot_1d()

        self.canvas.draw()

    def _plot_3d(self):
        """Plot 3D table as surface"""
        ax = self.figure.add_subplot(111, projection='3d')

        values = self.data['values']
        x_axis = self.data.get('x_axis')
        y_axis = self.data.get('y_axis')

        rows, cols = values.shape

        # For plot_surface with facecolors, we need vertices at cell corners
        # If we have NxM data cells, we need (N+1)x(M+1) vertices
        # The colors array represents faces (NxM), not vertices

        # Extend axes by one point (extrapolate with same spacing)
        if x_axis is not None and y_axis is not None:
            # Calculate axis spacing
            x_step = x_axis[1] - x_axis[0] if len(x_axis) > 1 else 1
            y_step = y_axis[1] - y_axis[0] if len(y_axis) > 1 else 1

            # Extend axes
            x_extended = np.append(x_axis, x_axis[-1] + x_step)
            y_extended = np.append(y_axis, y_axis[-1] + y_step)

            X, Y = np.meshgrid(x_extended, y_extended)
        else:
            X, Y = np.meshgrid(np.arange(cols + 1), np.arange(rows + 1))

        # Extend Z values by duplicating last row and column
        Z_extended = np.zeros((rows + 1, cols + 1))
        Z_extended[:rows, :cols] = values
        Z_extended[rows, :cols] = values[-1, :]  # Duplicate last row
        Z_extended[:rows, cols] = values[:, -1]  # Duplicate last column
        Z_extended[rows, cols] = values[-1, -1]  # Corner value
        Z = Z_extended

        # Calculate colors based on gradient (matching table viewer)
        # Colors represent faces (original data size), not vertices
        colors = self._calculate_colors(values)

        # Override colors for selected cells with blue
        if self.selected_cells:
            blue_color = (0.0, 0.5, 1.0, 1.0)  # Bright blue RGBA
            for row, col in self.selected_cells:
                if row < colors.shape[0] and col < colors.shape[1]:
                    colors[row, col] = blue_color

        # Plot surface
        surf = ax.plot_surface(X, Y, Z, facecolors=colors,
                               linewidth=0.5, edgecolor='gray',
                               antialiased=True, shade=False)

        # Labels
        x_label = self._get_axis_label(AxisType.X_AXIS) if x_axis is not None else 'Column'
        y_label = self._get_axis_label(AxisType.Y_AXIS) if y_axis is not None else 'Row'
        ax.set_xlabel(x_label)
        ax.set_ylabel(y_label)
        ax.set_zlabel('Value')
        ax.set_title(f'{self.table.name}')

        # Enable rotation with mouse
        ax.mouse_init()

        # Store axes for keyboard rotation
        self.ax_3d = ax

    def keyPressEvent(self, event):
        """Handle key presses for graph rotation and zoom"""
        if self.ax_3d is None:
            super().keyPressEvent(event)
            return

        # Get current view angles
        elev = self.ax_3d.elev
        azim = self.ax_3d.azim

        # Rotation step size (degrees)
        rotation_step = 10

        # Handle arrow keys for rotation
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
        # Handle +/= for zoom in
        elif event.key() in (Qt.Key_Plus, Qt.Key_Equal):
            self._zoom(1.1)
        # Handle - for zoom out
        elif event.key() == Qt.Key_Minus:
            self._zoom(0.9)
        else:
            # Not a handled key, pass to parent
            super().keyPressEvent(event)

    def _zoom(self, factor):
        """Zoom in or out by adjusting axis limits"""
        if self.ax_3d is None:
            return

        # Get current limits
        xlim = self.ax_3d.get_xlim()
        ylim = self.ax_3d.get_ylim()
        zlim = self.ax_3d.get_zlim()

        # Calculate centers
        x_center = (xlim[0] + xlim[1]) / 2
        y_center = (ylim[0] + ylim[1]) / 2
        z_center = (zlim[0] + zlim[1]) / 2

        # Calculate new ranges
        x_range = (xlim[1] - xlim[0]) / factor
        y_range = (ylim[1] - ylim[0]) / factor
        z_range = (zlim[1] - zlim[0]) / factor

        # Set new limits
        self.ax_3d.set_xlim(x_center - x_range/2, x_center + x_range/2)
        self.ax_3d.set_ylim(y_center - y_range/2, y_center + y_range/2)
        self.ax_3d.set_zlim(z_center - z_range/2, z_center + z_range/2)

        self.canvas.draw()

    def update_selection(self, selected_cells: list):
        """Update the selected cells and redraw the graph"""
        self.selected_cells = selected_cells
        self._plot_data()

    def _plot_2d(self):
        """Plot 2D table as line/surface"""
        ax = self.figure.add_subplot(111)

        values = self.data['values']
        y_axis = self.data.get('y_axis')

        if y_axis is not None:
            x = y_axis
        else:
            x = np.arange(len(values))

        # Plot with color gradient
        colors = self._calculate_colors_1d(values)

        for i in range(len(x) - 1):
            ax.plot(x[i:i+2], values[i:i+2], color=colors[i], linewidth=2)

        # Highlight selected cells with blue markers
        if self.selected_cells:
            selected_x = []
            selected_y = []
            for row, col in self.selected_cells:
                if row < len(values):
                    selected_x.append(x[row])
                    selected_y.append(values[row])
            if selected_x:
                ax.scatter(selected_x, selected_y, color='blue', s=100, zorder=10, alpha=0.8)

        y_label = self._get_axis_label(AxisType.Y_AXIS) if y_axis is not None else 'Index'
        ax.set_xlabel(y_label)
        ax.set_ylabel('Value')
        ax.set_title(f'{self.table.name}')
        ax.grid(True, alpha=0.3)

    def _plot_1d(self):
        """Plot 1D table as single bar"""
        ax = self.figure.add_subplot(111)

        values = self.data['values']

        # Single value bar
        color = self._ratio_to_color(0.5)
        ax.bar([0], [values[0]], color=color, width=0.5)

        ax.set_ylabel('Value')
        ax.set_title(f'{self.table.name}')
        ax.set_xticks([])

    def _calculate_colors(self, values: np.ndarray):
        """Calculate color array matching table viewer gradient"""
        min_val = np.min(values)
        max_val = np.max(values)

        if max_val == min_val:
            ratios = np.full_like(values, 0.5)
        else:
            ratios = (values - min_val) / (max_val - min_val)

        # Convert ratios to RGBA colors
        colors = np.zeros((*values.shape, 4))
        for i in range(values.shape[0]):
            for j in range(values.shape[1]):
                rgba = self._ratio_to_rgba(ratios[i, j])
                colors[i, j] = rgba

        return colors

    def _calculate_colors_1d(self, values: np.ndarray):
        """Calculate colors for 1D array"""
        min_val = np.min(values)
        max_val = np.max(values)

        if max_val == min_val:
            ratios = np.full_like(values, 0.5)
        else:
            ratios = (values - min_val) / (max_val - min_val)

        colors = [self._ratio_to_color(r) for r in ratios]
        return colors

    def _ratio_to_rgba(self, ratio: float):
        """Convert ratio to RGBA tuple using the configured color map"""
        return get_colormap().ratio_to_rgba_float(ratio)

    def _ratio_to_color(self, ratio: float):
        """Convert ratio to matplotlib color (RGB tuple)"""
        rgba = self._ratio_to_rgba(ratio)
        return (rgba[0], rgba[1], rgba[2])

    def _get_axis_label(self, axis_type: AxisType) -> str:
        """
        Get axis label with unit, e.g., 'Engine Speed (RPM)'

        Args:
            axis_type: AxisType.X_AXIS or AxisType.Y_AXIS

        Returns:
            Formatted axis label with unit if available
        """
        axis_table = self.table.get_axis(axis_type)
        if not axis_table:
            return "X Axis" if axis_type == AxisType.X_AXIS else "Y Axis"

        name = axis_table.name
        unit = ""

        # Get unit from scaling if available
        if self.rom_definition and axis_table.scaling:
            scaling = self.rom_definition.get_scaling(axis_table.scaling)
            if scaling and scaling.units:
                unit = scaling.units

        if unit:
            return f"{name} ({unit})"
        return name
