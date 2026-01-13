"""
Color Map Loader

Loads and applies color maps from .map files for table cell coloring.
"""

import logging
from pathlib import Path
from typing import List, Tuple, Optional

from PySide6.QtGui import QColor

logger = logging.getLogger(__name__)


class ColorMap:
    """
    Loads and applies color maps from .map files

    File format: 256 lines, each with "R G B" (space-separated integers 0-255)
    Index 0 = lowest value (ratio 0.0), Index 255 = highest value (ratio 1.0)
    """

    # Default built-in gradient (blue -> cyan -> green -> yellow -> red)
    _builtin_gradient = None

    def __init__(self, map_path: str = None):
        """
        Initialize color map

        Args:
            map_path: Path to .map file, or None for built-in gradient
        """
        self.map_path = map_path
        self.colors: List[Tuple[int, int, int]] = []
        self.name = "Built-in"

        if map_path:
            self._load_from_file(map_path)
        else:
            self._use_builtin()

    def _load_from_file(self, map_path: str):
        """Load color map from file"""
        path = Path(map_path)

        if not path.exists():
            logger.warning(f"Color map file not found: {map_path}, using built-in")
            self._use_builtin()
            return

        try:
            self.colors = []
            with open(path, 'r') as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue

                    parts = line.split()
                    if len(parts) >= 3:
                        r = int(parts[0])
                        g = int(parts[1])
                        b = int(parts[2])
                        self.colors.append((r, g, b))

            if len(self.colors) < 256:
                logger.warning(f"Color map has only {len(self.colors)} entries, expected 256")
                # Pad with last color or use builtin
                if self.colors:
                    last_color = self.colors[-1]
                    while len(self.colors) < 256:
                        self.colors.append(last_color)
                else:
                    self._use_builtin()
                    return

            self.name = path.stem
            logger.info(f"Loaded color map: {self.name} ({len(self.colors)} colors)")

        except Exception as e:
            logger.error(f"Failed to load color map {map_path}: {e}")
            self._use_builtin()

    def _use_builtin(self):
        """Use built-in gradient (blue -> cyan -> green -> yellow -> red)"""
        if ColorMap._builtin_gradient is None:
            ColorMap._builtin_gradient = self._generate_builtin_gradient()

        self.colors = ColorMap._builtin_gradient
        self.name = "Built-in"
        self.map_path = None

    def _generate_builtin_gradient(self) -> List[Tuple[int, int, int]]:
        """Generate the built-in thermal gradient"""
        colors = []
        for i in range(256):
            ratio = i / 255.0

            if ratio <= 0.25:
                t = ratio / 0.25
                r, g, b = 0, int(t * 255), 255
            elif ratio <= 0.5:
                t = (ratio - 0.25) / 0.25
                r, g, b = 0, 255, int(255 * (1 - t))
            elif ratio <= 0.75:
                t = (ratio - 0.5) / 0.25
                r, g, b = int(t * 255), 255, 0
            else:
                t = (ratio - 0.75) / 0.25
                r, g, b = 255, int(255 * (1 - t)), 0

            colors.append((r, g, b))

        return colors

    def ratio_to_color(self, ratio: float) -> QColor:
        """
        Convert 0-1 ratio to QColor using the color map

        Args:
            ratio: Value between 0.0 and 1.0

        Returns:
            QColor for the given ratio
        """
        ratio = max(0.0, min(1.0, ratio))
        index = int(ratio * 255)
        index = min(index, 255)  # Ensure we don't exceed bounds

        r, g, b = self.colors[index]
        return QColor(r, g, b)

    def ratio_to_rgb(self, ratio: float) -> Tuple[int, int, int]:
        """
        Convert 0-1 ratio to RGB tuple

        Args:
            ratio: Value between 0.0 and 1.0

        Returns:
            Tuple of (r, g, b) integers 0-255
        """
        ratio = max(0.0, min(1.0, ratio))
        index = int(ratio * 255)
        index = min(index, 255)

        return self.colors[index]

    def ratio_to_rgba_float(self, ratio: float) -> Tuple[float, float, float, float]:
        """
        Convert 0-1 ratio to RGBA tuple with float values (for matplotlib)

        Args:
            ratio: Value between 0.0 and 1.0

        Returns:
            Tuple of (r, g, b, a) floats 0.0-1.0
        """
        r, g, b = self.ratio_to_rgb(ratio)
        return (r / 255.0, g / 255.0, b / 255.0, 1.0)


# Global color map instance
_current_colormap: Optional[ColorMap] = None


def get_colormap() -> ColorMap:
    """
    Get the current global color map

    Returns:
        ColorMap instance
    """
    global _current_colormap
    if _current_colormap is None:
        # Load from settings or use default
        from .settings import get_settings
        settings = get_settings()
        colormap_path = settings.get_colormap_path()
        _current_colormap = ColorMap(colormap_path)

    return _current_colormap


def set_colormap(map_path: str = None):
    """
    Set the global color map

    Args:
        map_path: Path to .map file, or None for built-in
    """
    global _current_colormap
    _current_colormap = ColorMap(map_path)
    logger.info(f"Color map set to: {_current_colormap.name}")


def reload_colormap():
    """Reload the color map from settings"""
    global _current_colormap
    _current_colormap = None
    get_colormap()  # This will reload from settings
