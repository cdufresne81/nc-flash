"""Graph model — the ONE copy of engine-neutral graph math.

Both graph backends (GPU ``graph_gpu`` and classic matplotlib ``graph_classic``)
build their scene from a :class:`GraphModel`: per-cell colors matching the table
gradient, tick positions/labels, axis titles, and the extended Z grid for the
uniform-cell surface. Nothing here imports a rendering library.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import numpy as np

from ..core.rom_definition import AxisType, RomDefinition, Table, TableType
from ..utils.colormap import get_colormap
from ..utils.formatting import get_scaling_range

#: Selection highlight (RGBA, sRGB) — blue faces in 3D, blue markers in 2D.
SELECTION_RGBA = (0.0, 0.5, 1.0, 1.0)

#: Target number of tick labels per axis before thinning kicks in.
MAX_TICKS = 6

#: Default 3D view (elevation, azimuth) — same for both renderers.
DEFAULT_VIEW = (30.0, -60.0)

_lut_cache = {}  # id(ColorMap) -> (ColorMap, float LUT); colormaps are immutable


def _lut(cmap) -> np.ndarray:
    hit = _lut_cache.get(id(cmap))
    if hit is None or hit[0] is not cmap:
        hit = (cmap, np.array(cmap.colors, dtype=np.float64) / 255.0)
        _lut_cache.clear()  # one active colormap at a time
        _lut_cache[id(cmap)] = hit
    return hit[1]


def cell_colors(values: np.ndarray, scaling_range=None) -> np.ndarray:
    """Per-cell sRGB RGBA floats matching the table viewer gradient.

    ``values`` may be 1D (2D tables) or 2D (3D tables); the result has the same
    leading shape plus a trailing RGBA axis. The ratio comes from the scaling
    min/max when available, else the data range; it is clamped to [0, 1], a
    degenerate range maps to the midpoint, and NaN maps to the midpoint.
    """
    values = np.asarray(values, dtype=np.float64)
    if scaling_range:
        min_val, max_val = scaling_range
    else:
        finite = values[np.isfinite(values)]
        min_val = float(finite.min()) if finite.size else 0.0
        max_val = float(finite.max()) if finite.size else 0.0

    if max_val == min_val:
        ratios = np.full_like(values, 0.5)
    else:
        with np.errstate(invalid="ignore"):
            ratios = np.clip((values - min_val) / (max_val - min_val), 0.0, 1.0)

    scaled = np.nan_to_num(ratios * 255, nan=127.0)
    indices = np.clip(scaled, 0, 255).astype(np.intp)
    lut = _lut(get_colormap())
    colors = np.empty((*values.shape, 4))
    colors[..., :3] = lut[indices]
    colors[..., 3] = 1.0
    return colors


def extend_grid(values: np.ndarray) -> np.ndarray:
    """(rows+1, cols+1) vertex heights for a uniform-cell surface.

    The last row/column is duplicated so every cell — including the last row
    and column — gets its own quad (face count == rows * cols).
    """
    rows, cols = values.shape
    z = np.empty((rows + 1, cols + 1), dtype=np.float64)
    z[:rows, :cols] = values
    z[rows, :cols] = values[-1, :]
    z[:rows, cols] = values[:, -1]
    z[rows, cols] = values[-1, -1]
    return z


def tick_indices(n: int) -> np.ndarray:
    """Indices of the axis points that get a tick label (thinned to ~MAX_TICKS)."""
    if n <= MAX_TICKS:
        return np.arange(n)
    return np.arange(0, n, max(1, n // MAX_TICKS))


def format_tick(value: float) -> str:
    return f"{value:.4g}"


def nice_value_ticks(vmin: float, vmax: float, target: int = 5) -> List[float]:
    """'Nice' round tick values spanning [vmin, vmax] for the value axis."""
    if not np.isfinite(vmin) or not np.isfinite(vmax):
        return []
    if vmax == vmin:
        return [vmin]
    raw = (vmax - vmin) / max(1, target)
    mag = 10 ** np.floor(np.log10(raw))
    step = mag * min((m for m in (1, 2, 2.5, 5, 10) if m * mag >= raw), default=10)
    start = np.ceil(vmin / step) * step
    ticks = []
    v = start
    while v <= vmax + step * 1e-9:
        ticks.append(float(round(v, 10)) + 0.0)  # + 0.0: no '-0' tick label
        v += step
    return ticks


@dataclass
class Tick:
    position: float  # cell-centre coordinate in grid units (index + 0.5)
    label: str


@dataclass
class GraphModel:
    """Everything a backend needs to draw one table, engine-agnostic."""

    kind: TableType
    values: np.ndarray
    colors: np.ndarray
    x_ticks: List[Tick] = field(default_factory=list)
    y_ticks: List[Tick] = field(default_factory=list)
    x_title: str = ""
    y_title: str = ""
    value_title: str = "Value"
    x_values: Optional[np.ndarray] = None  # real axis values (2D: line x)
    y_values: Optional[np.ndarray] = None
    scaling_range: Optional[Tuple[float, float]] = None

    @property
    def value_range(self) -> Tuple[float, float]:
        finite = self.values[np.isfinite(self.values)]
        if not finite.size:
            return (0.0, 1.0)
        return float(finite.min()), float(finite.max())


def axis_title(
    table: Table, rom_definition: Optional[RomDefinition], axis_type: AxisType
) -> str:
    """'Name (units)' for an axis, falling back to 'X Axis' / 'Y Axis'."""
    axis_table = table.get_axis(axis_type)
    if not axis_table:
        return "X Axis" if axis_type == AxisType.X_AXIS else "Y Axis"
    unit = ""
    if rom_definition and axis_table.scaling:
        scaling = rom_definition.get_scaling(axis_table.scaling)
        if scaling and scaling.units:
            unit = scaling.units
    return f"{axis_table.name} ({unit})" if unit else axis_table.name


def value_title(table: Table, rom_definition: Optional[RomDefinition]) -> str:
    """Title for the value (Z / vertical) axis: the table's units, else 'Value'."""
    if rom_definition and table.scaling:
        scaling = rom_definition.get_scaling(table.scaling)
        if scaling and scaling.units:
            return f"Value ({scaling.units})"
    return "Value"


def _ticks_for(axis: Optional[Sequence[float]], n: int) -> List[Tick]:
    if axis is None:
        return [Tick(i + 0.5, str(i)) for i in tick_indices(n)]
    return [Tick(i + 0.5, format_tick(axis[i])) for i in tick_indices(len(axis))]


def build_model(
    table: Table,
    data: dict,
    rom_definition: Optional[RomDefinition],
    selected_cells: Sequence[Tuple[int, int]] = (),
) -> GraphModel:
    """Build the render model for ``table`` from the shared ``data`` dict.

    Re-reads ``data`` every call (the viewer mutates it in place on edits), so
    callers must not cache the result across edits.
    """
    scaling_range = get_scaling_range(rom_definition, table.scaling if table else None)
    # Copy: the viewer mutates data["values"] in place; the model must be a
    # snapshot of what was rendered, not a live alias.
    values = np.array(data["values"], dtype=np.float64, copy=True)
    x_axis = data.get("x_axis")
    y_axis = data.get("y_axis")
    colors = cell_colors(values, scaling_range)
    apply_selection(colors, selected_cells)

    model = GraphModel(
        kind=table.type,
        values=values,
        colors=colors,
        value_title=value_title(table, rom_definition),
        scaling_range=scaling_range,
    )
    if table.type == TableType.THREE_D:
        rows, cols = values.shape
        model.x_ticks = _ticks_for(x_axis, cols)
        model.y_ticks = _ticks_for(y_axis, rows)
        model.x_title = (
            axis_title(table, rom_definition, AxisType.X_AXIS)
            if x_axis is not None
            else "Column"
        )
        model.y_title = (
            axis_title(table, rom_definition, AxisType.Y_AXIS)
            if y_axis is not None
            else "Row"
        )
        model.x_values = None if x_axis is None else np.asarray(x_axis, float)
        model.y_values = None if y_axis is None else np.asarray(y_axis, float)
    elif table.type == TableType.TWO_D:
        n = len(values)
        model.x_values = (
            np.asarray(y_axis, float)
            if y_axis is not None
            else np.arange(n, dtype=float)
        )
        model.x_title = (
            axis_title(table, rom_definition, AxisType.Y_AXIS)
            if y_axis is not None
            else "Index"
        )
    return model


def selection_colors(model: GraphModel, selected_cells) -> np.ndarray:
    """Fresh per-cell colors for ``model`` with ``selected_cells`` in blue."""
    colors = cell_colors(model.values, model.scaling_range)
    return apply_selection(colors, selected_cells)


def apply_selection(colors: np.ndarray, selected_cells: Sequence[Tuple[int, int]]):
    """Recolor selected cells blue in place (2D tables index by row only)."""
    if not selected_cells:
        return colors
    if colors.ndim == 3:
        rows, cols = colors.shape[:2]
        for row, col in selected_cells:
            if 0 <= row < rows and 0 <= col < cols:
                colors[row, col] = SELECTION_RGBA
    else:
        n = colors.shape[0]
        for row, _col in selected_cells:
            if 0 <= row < n:
                colors[row] = SELECTION_RGBA
    return colors


def selected_points(
    model: GraphModel, selected_cells: Sequence[Tuple[int, int]]
) -> Tuple[np.ndarray, np.ndarray]:
    """(x, y) of selected points on a 2D line (for the blue markers)."""
    n = len(model.values)
    rows = sorted({r for r, _ in selected_cells if 0 <= r < n})
    if not rows:
        return np.empty(0), np.empty(0)
    return model.x_values[rows], model.values[rows]


def axis_value_labels(model: GraphModel, row: int, col: int) -> Tuple[str, str]:
    """(x, y) axis-value labels of a 3D cell (index when the axis is missing)."""
    xv = format_tick(model.x_values[col]) if model.x_values is not None else str(col)
    yv = format_tick(model.y_values[row]) if model.y_values is not None else str(row)
    return xv, yv


def hover_text(model: GraphModel, row: int, col: int = 0) -> str:
    """Readout for the cell under the cursor: 'X x · Y y → value'."""
    if model.kind == TableType.THREE_D:
        rows, cols = model.values.shape
        if not (0 <= row < rows and 0 <= col < cols):
            return ""
        xv, yv = axis_value_labels(model, row, col)
        return (
            f"{model.x_title} {xv} · {model.y_title} {yv} → "
            f"{format_tick(model.values[row, col])}"
        )
    n = len(model.values)
    if not 0 <= row < n:
        return ""
    return (
        f"{model.x_title} {format_tick(model.x_values[row])} → "
        f"{format_tick(model.values[row])}"
    )
