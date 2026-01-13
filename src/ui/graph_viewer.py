"""
Graph Viewer Window

Displays 3D visualization of table data with rotation and selection highlighting.
"""

import numpy as np
from PySide6.QtWidgets import QMainWindow, QVBoxLayout, QWidget
from PySide6.QtCore import Qt

from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from mpl_toolkits.mplot3d import Axes3D

from ..core.rom_definition import Table, TableType
from ..utils.constants import APP_NAME


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

    def __init__(self, table: Table, data: dict, selected_cells: list = None, parent=None):
        """
        Initialize graph viewer

        Args:
            table: Table definition
            data: Table data dictionary
            selected_cells: List of (row, col) tuples for selected cells
            parent: Parent widget
        """
        super().__init__(parent)

        self.table = table
        self.data = data
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
        self.figure.clear()

        if self.table.type == TableType.THREE_D:
            self._plot_3d()
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

        # Create meshgrid
        if x_axis is not None and y_axis is not None:
            X, Y = np.meshgrid(x_axis, y_axis)
        else:
            X, Y = np.meshgrid(np.arange(cols), np.arange(rows))

        Z = values

        # Calculate colors based on gradient (matching table viewer)
        colors = self._calculate_colors(Z)

        # Plot surface
        surf = ax.plot_surface(X, Y, Z, facecolors=colors,
                               linewidth=0.5, edgecolor='gray',
                               antialiased=True, shade=False)

        # Highlight selected cells
        if self.selected_cells:
            self._highlight_selected_3d(ax, X, Y, Z)

        # Labels
        ax.set_xlabel('X Axis' if x_axis is not None else 'Column')
        ax.set_ylabel('Y Axis' if y_axis is not None else 'Row')
        ax.set_zlabel('Value')
        ax.set_title(f'{self.table.name}')

        # Enable rotation with mouse
        ax.mouse_init()

        # Store axes for keyboard rotation
        self.ax_3d = ax

    def keyPressEvent(self, event):
        """Handle arrow key presses for graph rotation"""
        if self.ax_3d is None:
            return

        # Get current view angles
        elev = self.ax_3d.elev
        azim = self.ax_3d.azim

        # Rotation step size (degrees)
        step = 10

        # Update angles based on arrow keys
        if event.key() == Qt.Key_Left:
            azim -= step
        elif event.key() == Qt.Key_Right:
            azim += step
        elif event.key() == Qt.Key_Up:
            elev += step
        elif event.key() == Qt.Key_Down:
            elev -= step
        else:
            # Not an arrow key, pass to parent
            super().keyPressEvent(event)
            return

        # Apply new view angles
        self.ax_3d.view_init(elev=elev, azim=azim)
        self.canvas.draw()

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

        # Highlight selected cells
        if self.selected_cells:
            self._highlight_selected_2d(ax, x, values)

        ax.set_xlabel('Y Axis' if y_axis is not None else 'Index')
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
        """Convert ratio to RGBA tuple (matching table viewer gradient)"""
        ratio = max(0.0, min(1.0, ratio))

        if ratio <= 0.25:
            t = ratio / 0.25
            r, g, b = 0, t, 1.0
        elif ratio <= 0.5:
            t = (ratio - 0.25) / 0.25
            r, g, b = 0, 1.0, 1.0 - t
        elif ratio <= 0.75:
            t = (ratio - 0.5) / 0.25
            r, g, b = t, 1.0, 0
        else:
            t = (ratio - 0.75) / 0.25
            r, g, b = 1.0, 1.0 - t, 0

        return (r, g, b, 1.0)

    def _ratio_to_color(self, ratio: float):
        """Convert ratio to matplotlib color (RGB tuple)"""
        rgba = self._ratio_to_rgba(ratio)
        return (rgba[0], rgba[1], rgba[2])

    def _highlight_selected_3d(self, ax, X, Y, Z):
        """Highlight selected cells in 3D plot"""
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"Highlighting {len(self.selected_cells)} cells in 3D plot")
        logger.info(f"Z shape: {Z.shape}, X shape: {X.shape}, Y shape: {Y.shape}")

        for row, col in self.selected_cells:
            logger.info(f"  Trying to highlight cell ({row}, {col})")
            if row < Z.shape[0] and col < Z.shape[1]:
                # Draw a marker at the selected position
                x_val = X[row, col]
                y_val = Y[row, col]
                z_val = Z[row, col]
                logger.info(f"    Drawing marker at ({x_val}, {y_val}, {z_val})")
                ax.scatter([x_val], [y_val], [z_val],
                          color='black', s=200, marker='o', edgecolor='yellow', linewidth=3, zorder=10)
            else:
                logger.warning(f"    Cell ({row}, {col}) out of bounds for Z shape {Z.shape}")

    def _highlight_selected_2d(self, ax, x, values):
        """Highlight selected cells in 2D plot"""
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"Highlighting {len(self.selected_cells)} cells in 2D plot")
        logger.info(f"Values length: {len(values)}, x length: {len(x)}")

        for row, col in self.selected_cells:
            logger.info(f"  Trying to highlight cell ({row}, {col})")
            if row < len(values):
                logger.info(f"    Drawing marker at ({x[row]}, {values[row]})")
                ax.scatter([x[row]], [values[row]],
                          color='black', s=200, marker='o', edgecolor='yellow', linewidth=3, zorder=10)
            else:
                logger.warning(f"    Cell row {row} out of bounds for values length {len(values)}")
