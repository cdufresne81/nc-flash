"""GPU graph backend (pygfx / WebGPU).

Renders the table graph on the GPU: a lit, flat-shaded 3D surface (one color
per cell, exactly the table's colors) or a gradient 2D line, on a dark stage
with crisp screen-space labels. Designed around what the Sep 2026 evaluation
measured (``tools/graph_eval``):

- **No per-window QWidget subclass** capturing this view: PySide6 never frees
  Python subclass types, so a closure-captured view leaked every graph. We use
  a plain ``QRenderWidget`` and an event filter instead.
- ``shutdown()`` MUST ``close()`` the canvas, or its GPU buffers stay alive.
- Render-target pixel ratio is capped (``gpu_runtime.PIXEL_RATIO``).
- pygfx buffer colors are **linear**; table colors are sRGB → converted here.

Backend contract (shared with ``graph_classic.ClassicGraphView``):
``widget``, ``engine_name``, ``show_model``, ``update_data``, ``update_colors``,
``is_3d``, ``get_view``, ``set_view``, ``rotate``, ``zoom``, ``get_zoom``,
``reset_view``, ``request_draw``, ``grab``, ``shutdown``, counters
``n_data_updates`` / ``n_color_updates``.
"""

import logging
import math
from typing import List, Optional, Tuple

import numpy as np
from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer
from PySide6.QtWidgets import QLabel, QSizePolicy

from ..core.rom_definition import TableType
from . import theme
from .gpu_runtime import PIXEL_RATIO
from .graph_model import (
    GraphModel,
    extend_grid,
    format_tick,
    hover_text,
    nice_value_ticks,
    selected_points,
)

logger = logging.getLogger(__name__)

# World box for the 3D surface (matplotlib-like 4:4:3 box, any table shape).
BX, BY, BZ = 10.0, 10.0, 7.0
DEFAULT_VIEW = (30.0, -60.0)  # elevation, azimuth — same as the classic graph
FOV = 22.0
CAM_DIST = 42.0
FIT_MARGIN = 10.0  # logical px kept free around the fitted plot
TICK_FONT = 11.0
TITLE_FONT = 12.5
CALLOUT_FONT = 12.5

# 2D plot area margins (logical px): room for tick labels and titles.
M_LEFT, M_RIGHT, M_TOP, M_BOTTOM = 70.0, 22.0, 18.0, 52.0


def srgb_to_linear(rgb: np.ndarray) -> np.ndarray:
    rgb = np.asarray(rgb, dtype=np.float64)
    return np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)


def _to_linear(colors: np.ndarray) -> np.ndarray:
    out = np.array(colors, dtype=np.float64, copy=True)
    out[..., :3] = srgb_to_linear(out[..., :3])
    return out


def _text(gfx, text, size, color, weight=0, anchor="middle-center"):
    return gfx.Text(
        text=text,
        font_size=size,
        screen_space=True,
        anchor=anchor,
        material=gfx.TextMaterial(
            color=color,
            outline_color=theme.GRAPH_TEXT_OUTLINE,
            outline_thickness=0.18,
            weight_offset=weight,
        ),
    )


def _text_extent(text: str, size: float) -> Tuple[float, float]:
    """Half extents (px) of a middle-centered screen-space label (estimate)."""
    return 0.31 * size * max(1, len(text)) + 3.0, 0.62 * size + 2.0


def single_cell(cells):
    """The one selected (row, col), or None when 0 or several are selected."""
    unique = set(map(tuple, cells))
    return next(iter(unique)) if len(unique) == 1 else None


def _fit_axis(p: np.ndarray, e: np.ndarray, avail: float) -> float:
    """Largest scale s with (max(s*p+e) - min(s*p-e)) <= avail (bisection)."""
    if avail <= 0:
        return 1e-3
    lo, hi = 1e-3, 1e3
    for _ in range(40):
        mid = math.sqrt(lo * hi)
        if (mid * p + e).max() - (mid * p - e).min() <= avail:
            lo = mid
        else:
            hi = mid
    return lo


# =============================================================================
# 3D surface scene
# =============================================================================
class Surface3D:
    """pygfx objects for a 3D table: surface, edges, floor, axes, labels."""

    def __init__(self, gfx):
        self.gfx = gfx
        self.root = gfx.Group()
        self.shape = None
        self.mesh = None
        self.edges = None
        self.static = gfx.Group()  # floor grid, axis lines (rebuilt per model)
        self.labels = gfx.Group()
        self.root.add(self.static, self.labels)
        self.model: Optional[GraphModel] = None
        self.zlo, self.zhi = 0.0, 1.0
        # label records: [text_obj, axis, base_pos(3,), text, font, enabled]
        self._labels = []
        self._xhair = []
        self._crosshair = None  # (row, col) of the single selected cell
        self._zgrid = None

    # -- geometry -------------------------------------------------------------
    def _zw(self, v):
        return (np.asarray(v, dtype=np.float64) - self.zlo) / (self.zhi - self.zlo) * BZ

    def _positions(self, values):
        rows, cols = values.shape
        gx, gy = np.meshgrid(np.linspace(0, BX, cols + 1), np.linspace(0, BY, rows + 1))
        z = self._zw(np.nan_to_num(extend_grid(values), nan=self.zlo))
        return np.column_stack([gx.ravel(), gy.ravel(), z.ravel()]).astype(np.float32)

    def _face_colors(self, colors):
        lin = _to_linear(colors).reshape(-1, 4)
        return np.repeat(lin, 2, axis=0).astype(np.float32)  # 2 triangles / cell

    def _edge_positions(self, pos, rows, cols):
        P = pos.reshape(rows + 1, cols + 1, 3).copy()
        P[..., 2] += 0.015
        a, b = P[:, :-1].reshape(-1, 3), P[:, 1:].reshape(-1, 3)
        c, d = P[:-1, :].reshape(-1, 3), P[1:, :].reshape(-1, 3)
        return np.concatenate(
            [np.stack([a, b], 1).reshape(-1, 3), np.stack([c, d], 1).reshape(-1, 3)]
        ).astype(np.float32)

    def set_model(self, model: GraphModel):
        gfx = self.gfx
        self.model = model
        vmin, vmax = model.value_range
        if vmax == vmin:
            vmin, vmax = vmin - 1.0, vmax + 1.0
        self.zlo, self.zhi = vmin, vmax
        rows, cols = model.values.shape
        pos = self._positions(model.values)
        self._zgrid = pos[:, 2].reshape(rows + 1, cols + 1).astype(np.float64)
        colors = self._face_colors(model.colors)

        if self.shape == (rows, cols):
            # same topology: update buffers in place (keeps GPU objects)
            self.mesh.geometry.positions.data[:] = pos
            self.mesh.geometry.positions.update_full()
            self.mesh.geometry.colors.data[:] = colors
            self.mesh.geometry.colors.update_full()
            self.edges.geometry.positions.data[:] = self._edge_positions(
                pos, rows, cols
            )
            self.edges.geometry.positions.update_full()
        else:
            if self.mesh is not None:
                self.root.remove(self.mesh, self.edges)
            W = cols + 1
            r, c = np.meshgrid(np.arange(rows), np.arange(cols), indexing="ij")
            v0 = (r * W + c).ravel()
            idx = np.empty((rows * cols * 2, 3), np.int32)
            idx[0::2] = np.column_stack([v0, v0 + 1, v0 + W + 1])
            idx[1::2] = np.column_stack([v0, v0 + W + 1, v0 + W])
            geom = gfx.Geometry(
                positions=pos,
                indices=idx,
                normals=np.tile([0, 0, 1], (len(pos), 1)).astype(np.float32),
                colors=colors,
            )
            self.mesh = gfx.Mesh(
                geom,
                gfx.MeshPhongMaterial(
                    color_mode="face",
                    flat_shading=True,
                    shininess=28,
                    specular=theme.GRAPH_SPECULAR,
                    side="both",
                    pick_write=True,
                ),
            )
            self.edges = gfx.Line(
                gfx.Geometry(positions=self._edge_positions(pos, rows, cols)),
                gfx.LineSegmentMaterial(
                    color=theme.GRAPH_CELL_EDGE, thickness=0.9, aa=True
                ),
            )
            self.root.add(self.mesh, self.edges)
            self.shape = (rows, cols)
        self._build_static_and_labels()

    def set_colors(self, colors: np.ndarray):
        if self.mesh is None:
            return
        self.model.colors = colors
        self.mesh.geometry.colors.data[:] = self._face_colors(colors)
        self.mesh.geometry.colors.update_full()

    def _build_static_and_labels(self):
        gfx, m = self.gfx, self.model
        rows, cols = m.values.shape
        self.static.clear()
        self.labels.clear()
        self._labels = []

        # Floor grid (every k cells) + outline.
        kx, ky = max(1, cols // 8), max(1, rows // 8)
        floor = []
        for i in range(0, cols + 1, kx):
            x = i / cols * BX
            floor += [(x, 0, 0), (x, BY, 0)]
        for j in range(0, rows + 1, ky):
            y = j / rows * BY
            floor += [(0, y, 0), (BX, y, 0)]
        floor += [(BX, 0, 0), (BX, BY, 0), (0, BY, 0), (BX, BY, 0)]
        self.static.add(
            gfx.Line(
                gfx.Geometry(positions=np.array(floor, np.float32)),
                gfx.LineSegmentMaterial(color=theme.GRAPH_FLOOR_GRID, thickness=1.0),
            )
        )
        # Value ticks → faint horizontal guides on the four vertical walls'
        # bottom edges are enough; draw them as short rings on the Z axis line
        # (placed per frame in layout_labels, see _z_axis).
        self._z_ticks = nice_value_ticks(self.zlo, self.zhi, 5)
        self._z_axis = gfx.Line(
            gfx.Geometry(
                positions=np.zeros((2 + 2 * len(self._z_ticks), 3), np.float32)
            ),
            gfx.LineSegmentMaterial(color=theme.GRAPH_AXIS, thickness=1.6),
        )
        self.static.add(self._z_axis)

        for t in m.x_ticks:
            self._add_label("x", (t.position / cols * BX, 0, 0), t.label, TICK_FONT)
        for t in m.y_ticks:
            self._add_label("y", (0, t.position / rows * BY, 0), t.label, TICK_FONT)
        for zv in self._z_ticks:
            self._add_label(
                "z", (0, 0, float(self._zw(zv))), format_tick(zv), TICK_FONT
            )
        self._add_label("xt", (BX / 2, 0, 0), m.x_title, TITLE_FONT, title=True)
        self._add_label("yt", (0, BY / 2, 0), m.y_title, TITLE_FONT, title=True)
        self._add_label("zt", (0, 0, BZ), m.value_title, TITLE_FONT, title=True)

        # Selected-cell crosshair: white guides along the cell's row and column
        # (through the face centres) + callouts of its axis values and value.
        # Hidden until exactly one cell is selected (set_crosshair).
        self._xhair = []
        for n in (2 * cols + 1, 2 * rows + 1):
            line = gfx.Line(
                gfx.Geometry(positions=np.zeros((n, 3), np.float32)),
                gfx.LineMaterial(color=theme.GRAPH_CROSSHAIR, thickness=2.4, aa=True),
            )
            line.visible = False
            self.static.add(line)
            self._xhair.append(line)
        for axis in ("hx", "hy", "hz"):
            self._add_label(axis, (0, 0, 0), "", CALLOUT_FONT, callout=True)
        self.set_crosshair(self._crosshair)

    def _crosshair_paths(self, r, c):
        """Row/column guide polylines lying exactly ON the surface.

        Each cell quad is two triangles split along the (r,c)-(r+1,c+1)
        diagonal. Sampling every cell-edge midpoint and every diagonal midpoint
        keeps each segment inside one triangle, i.e. on its plane, so the guide
        hugs the surface instead of cutting through it.
        """
        g = self._zgrid
        rows, cols = g.shape[0] - 1, g.shape[1] - 1
        lift = 0.03
        row = []
        yr = (r + 0.5) / rows * BY
        for k in range(cols + 1):
            row.append((k / cols * BX, yr, (g[r, k] + g[r + 1, k]) / 2 + lift))
            if k < cols:
                z = (g[r, k] + g[r + 1, k + 1]) / 2 + lift
                row.append(((k + 0.5) / cols * BX, yr, z))
        col = []
        xc = (c + 0.5) / cols * BX
        for k in range(rows + 1):
            col.append((xc, k / rows * BY, (g[k, c] + g[k, c + 1]) / 2 + lift))
            if k < rows:
                z = (g[k, c] + g[k + 1, c + 1]) / 2 + lift
                col.append((xc, (k + 0.5) / rows * BY, z))
        return np.array(row, float), np.array(col, float)

    def _add_label(self, axis, base, text, font, title=False, callout=False):
        color = theme.GRAPH_TEXT
        if title:
            color = theme.GRAPH_TITLE
        if callout:
            color = theme.GRAPH_CALLOUT
        obj = _text(
            self.gfx,
            text or " ",
            font,
            color,
            weight=300 if callout else (150 if title else 0),
        )
        obj.visible = not callout
        self.labels.add(obj)
        # [obj, axis, base_pos, text, font, enabled]
        self._labels.append([obj, axis, np.array(base, float), text, font, not callout])

    def set_crosshair(self, cell):
        """Show the crosshair + axis callouts for ``cell`` (row, col), or hide."""
        self._crosshair = cell
        if not self._xhair:
            return
        m = self.model
        rows, cols = m.values.shape
        if cell is not None and not (0 <= cell[0] < rows and 0 <= cell[1] < cols):
            cell = None
        for line in self._xhair:
            line.visible = cell is not None
        callouts = {rec[1]: rec for rec in self._labels if rec[1][0] == "h"}
        for rec in callouts.values():
            rec[5] = cell is not None
            rec[0].visible = cell is not None
        if cell is None:
            return
        r, c = cell
        row_pts, col_pts = self._crosshair_paths(r, c)
        yr, xc = (r + 0.5) / rows * BY, (c + 0.5) / cols * BX
        for line, pts in zip(self._xhair, (row_pts, col_pts)):
            line.geometry.positions.data[:] = pts.astype(np.float32)
            line.geometry.positions.update_full()
        xv = format_tick(m.x_values[c]) if m.x_values is not None else str(c)
        yv = format_tick(m.y_values[r]) if m.y_values is not None else str(r)
        v = float(m.values[r, c])
        for key, text, base in (
            ("hx", xv, (xc, 0, 0)),
            ("hy", yv, (0, yr, 0)),
            ("hz", format_tick(v), (0, 0, float(self._zw(v)))),
        ):
            rec = callouts[key]
            rec[0].set_text(text)
            rec[2] = np.array(base, float)
            rec[3] = text

    # -- per-frame layout -----------------------------------------------------
    def layout_labels(self, cam_pos: np.ndarray, project):
        """Put tick labels on the floor edges facing the camera, Z on a side corner.

        ``project`` maps (N,3) world points to (N,2) base-pixel coords.
        """
        cx, cy = cam_pos[0], cam_pos[1]
        y_edge, y_out = (0.0, -1.0) if cy < BY / 2 else (BY, 1.0)
        x_edge, x_out = (BX, 1.0) if cx > BX / 2 else (0.0, -1.0)
        # Z axis on the side corner that projects left-most on screen.
        # side corners = neither nearest nor farthest from the camera
        cands = [(x_edge, BY - y_edge), (BX - x_edge, y_edge)]
        pts = project(np.array([[a, b, 0.0] for a, b in cands]))
        zx, zy = cands[int(np.argmin(pts[:, 0]))]
        zox = -1.0 if zx < BX / 2 else 1.0
        zoy = -1.0 if zy < BY / 2 else 1.0

        zpos = [(zx, zy, 0.0), (zx, zy, BZ)]
        for zv in self._z_ticks:
            h = float(self._zw(zv))
            zpos += [(zx, zy, h), (zx + 0.25 * zox, zy + 0.25 * zoy, h)]
        self._z_axis.geometry.positions.data[:] = np.array(zpos, np.float32)
        self._z_axis.geometry.positions.update_full()

        for rec in self._labels:
            obj, axis, base = rec[0], rec[1], rec[2]
            if axis[0] == "h":  # callouts sit where that axis's ticks sit
                axis = axis[1]
            if axis == "x":
                p = (base[0], y_edge + 0.75 * y_out, 0.0)
            elif axis == "y":
                p = (x_edge + 0.75 * x_out, base[1], 0.0)
            elif axis == "z":
                p = (zx + 0.5 * zox, zy + 0.5 * zoy, base[2])
            elif axis == "xt":
                p = (BX / 2, y_edge + 2.1 * y_out, 0.0)
            elif axis == "yt":
                p = (x_edge + 2.3 * x_out, BY / 2, 0.0)
            else:  # zt
                p = (zx + 0.4 * zox, zy + 0.4 * zoy, BZ + 1.0)
            obj.local.position = p

    def fit_items(self):
        """World points + label half-extents used by the fit/declutter pass."""
        pts, ext, recs = [], [], []
        for x in (0, BX):
            for y in (0, BY):
                for z in (0, BZ):
                    pts.append((x, y, z))
                    ext.append((0.0, 0.0))
                    recs.append(None)
        for rec in self._labels:
            if not rec[5]:
                continue  # inactive callout: neither fitted nor shown
            pts.append(tuple(rec[0].local.position))
            ext.append(_text_extent(rec[3], rec[4]))
            recs.append(rec)
        return np.array(pts, float), np.array(ext, float), recs

    def cell_at_face(self, face_index: int) -> Tuple[int, int]:
        rows, cols = self.shape
        cell = int(face_index) // 2
        return cell // cols, cell % cols


# =============================================================================
# 2D line scene
# =============================================================================
class Line2D:
    """pygfx objects for a 2D table: gradient line, soft fill, points, grid."""

    def __init__(self, gfx):
        self.gfx = gfx
        self.plot = gfx.Group()  # data coordinates (ortho camera)
        self.overlay = gfx.Group()  # screen coordinates (labels)
        self.model: Optional[GraphModel] = None
        self.rect = (0.0, 1.0, 0.0, 1.0)
        self._selected = []
        self.points = None
        self._ticks_x: List[float] = []
        self._ticks_y: List[float] = []

    def set_model(self, model: GraphModel, selected_cells):
        gfx = self.gfx
        self.model = model
        self._selected = list(selected_cells)
        self.plot.clear()
        x = np.asarray(model.x_values, float)
        y = np.nan_to_num(np.asarray(model.values, float))
        n = len(x)
        xmin, xmax = (float(x.min()), float(x.max())) if n else (0.0, 1.0)
        if xmax == xmin:
            xmin, xmax = xmin - 1, xmax + 1
        ymin, ymax = (float(y.min()), float(y.max())) if n else (0.0, 1.0)
        if ymax == ymin:
            ymin, ymax = ymin - 1, ymax + 1
        px, py = 0.03 * (xmax - xmin), 0.08 * (ymax - ymin)
        self.base_rect = (xmin - px, xmax + px, ymin - py, ymax + py)
        self.rect = self.base_rect  # current view rect (zoom applied in draw)
        lin = _to_linear(model.colors).astype(np.float32)

        # Grid (behind): nice ticks in both directions.
        self._ticks_y = nice_value_ticks(ymin, ymax, 5)
        self._ticks_x = nice_value_ticks(xmin, xmax, 6)
        x0, x1, y0, y1 = self.base_rect
        grid = []
        for ty in self._ticks_y:
            grid += [(x0, ty, 0), (x1, ty, 0)]
        for tx in self._ticks_x:
            grid += [(tx, y0, 0), (tx, y1, 0)]
        if grid:
            self.plot.add(
                gfx.Line(
                    gfx.Geometry(positions=np.array(grid, np.float32)),
                    gfx.LineSegmentMaterial(
                        color=theme.GRAPH_FLOOR_GRID, thickness=1.0
                    ),
                )
            )
        if n >= 2:
            # Soft gradient fill under the curve.
            fill_pos, fill_col, fill_idx = [], [], []
            for i in range(n):
                fill_pos += [(x[i], y0, 0), (x[i], y[i], 0)]
                c = lin[i].copy()
                c_top, c_bot = c.copy(), c.copy()
                c_top[3], c_bot[3] = 0.30, 0.02
                fill_col += [c_bot, c_top]
            for i in range(n - 1):
                a = 2 * i
                fill_idx += [(a, a + 2, a + 3), (a, a + 3, a + 1)]
            self.plot.add(
                gfx.Mesh(
                    gfx.Geometry(
                        positions=np.array(fill_pos, np.float32),
                        colors=np.array(fill_col, np.float32),
                        indices=np.array(fill_idx, np.int32),
                    ),
                    gfx.MeshBasicMaterial(
                        color_mode="vertex", alpha_mode="blend", side="both"
                    ),
                )
            )
            line_pos = np.column_stack([x, y, np.zeros(n)]).astype(np.float32)
            self.plot.add(
                gfx.Line(
                    gfx.Geometry(positions=line_pos, colors=lin),
                    gfx.LineMaterial(thickness=3.5, color_mode="vertex", aa=True),
                )
            )
        # Data points.
        pts = np.column_stack([x, y, np.zeros(n)]).astype(np.float32)
        self.points = gfx.Points(
            gfx.Geometry(positions=pts, colors=lin),
            gfx.PointsMarkerMaterial(
                size=8.0,
                color_mode="vertex",
                edge_width=1.0,
                edge_color=theme.GRAPH_CELL_EDGE,
                pick_write=True,
            ),
        )
        self.plot.add(self.points)
        self._build_selection()

    def _build_selection(self):
        gfx = self.gfx
        if getattr(self, "_sel_obj", None) is not None:
            self.plot.remove(self._sel_obj)
            self._sel_obj = None
        for obj in getattr(self, "_guides", []):
            self.plot.remove(obj)
        self._guides = []
        self.cross = None
        cell = single_cell(self._selected)
        n = len(self.model.values)
        if cell is not None and 0 <= cell[0] < n:
            i = cell[0]
            xi = float(self.model.x_values[i])
            yi = float(np.nan_to_num(self.model.values[i]))
            self.cross = (xi, yi)
            x0, _, y0, _ = self.base_rect
            guide = gfx.Line(
                gfx.Geometry(
                    positions=np.array(
                        [
                            (xi, y0, 0.05),
                            (xi, yi, 0.05),
                            (x0, yi, 0.05),
                            (xi, yi, 0.05),
                        ],
                        np.float32,
                    )
                ),
                gfx.LineSegmentMaterial(
                    color=theme.GRAPH_CROSSHAIR,
                    thickness=1.6,
                    dash_pattern=(5, 4),
                    aa=True,
                ),
            )
            self.plot.add(guide)
            self._guides.append(guide)
        sx, sy = selected_points(self.model, self._selected)
        if len(sx):
            pos = np.column_stack([sx, np.nan_to_num(sy), np.full(len(sx), 0.1)])
            self._sel_obj = gfx.Points(
                gfx.Geometry(positions=pos.astype(np.float32)),
                gfx.PointsMarkerMaterial(
                    size=15.0,
                    color=tuple(srgb_to_linear((0.0, 0.5, 1.0))) + (1.0,),
                    edge_width=2.0,
                    edge_color=theme.GRAPH_MARKER_RING,
                ),
            )
            self.plot.add(self._sel_obj)

    def set_selection(self, colors, selected_cells):
        self.model.colors = colors
        self._selected = list(selected_cells)
        self._build_selection()

    def to_px(self, xd, yd, vp):
        """Data → logical pixel (y down) inside viewport rect vp=(x,y,w,h)."""
        x0, x1, y0, y1 = self.rect
        vx, vy, vw, vh = vp
        return (
            vx + (np.asarray(xd) - x0) / (x1 - x0) * vw,
            vy + (1 - (np.asarray(yd) - y0) / (y1 - y0)) * vh,
        )

    def build_overlay(self, vp, size):
        gfx, m = self.gfx, self.model
        self.overlay.clear()
        vx, vy, vw, vh = vp
        # thin x ticks if they'd collide
        # Callouts for the single selected point: its x and y values, bold, on
        # the axes; plain ticks that would collide with them are skipped.
        cx_px = cy_px = None
        if getattr(self, "cross", None) is not None:
            xi, yi = self.cross
            cpx, cpy = self.to_px(xi, yi, vp)
            cx_px, cy_px = float(cpx), float(cpy)
            for text, pos, anchor in (
                (format_tick(xi), (cx_px, vy + vh + 6, 0), "top-center"),
                (format_tick(yi), (vx - 8, cy_px, 0), "middle-right"),
            ):
                t = _text(gfx, text, CALLOUT_FONT, theme.GRAPH_CALLOUT, 300, anchor)
                t.local.position = pos
                self.overlay.add(t)
        xs = self._ticks_x
        if cx_px is not None:
            half = _text_extent(format_tick(self.cross[0]), CALLOUT_FONT)[0]
            xs = [
                v
                for v in xs
                if abs(float(self.to_px(v, 0.0, vp)[0]) - cx_px)
                > half + _text_extent(format_tick(v), TICK_FONT)[0] + 4
            ]
        if xs:
            px, _ = self.to_px(np.array(xs), np.zeros(len(xs)), vp)
            widest = max(_text_extent(format_tick(v), TICK_FONT)[0] for v in xs) * 2
            step = 1
            while (
                len(xs) > 1
                and np.min(np.diff(px[::step])) < widest + 6
                and step < len(xs)
            ):
                step += 1
            for v, p in zip(xs[::step], px[::step]):
                t = _text(
                    gfx,
                    format_tick(v),
                    TICK_FONT,
                    theme.GRAPH_TEXT,
                    anchor="top-center",
                )
                t.local.position = (p, vy + vh + 6, 0)
                self.overlay.add(t)
        for v in self._ticks_y:
            _, py = self.to_px(0.0, v, vp)
            if cy_px is not None and abs(float(py) - cy_px) < 2 * TICK_FONT:
                continue  # the callout owns this spot
            t = _text(
                gfx, format_tick(v), TICK_FONT, theme.GRAPH_TEXT, anchor="middle-right"
            )
            t.local.position = (vx - 8, float(py), 0)
            self.overlay.add(t)
        xt = _text(gfx, m.x_title, TITLE_FONT, theme.GRAPH_TITLE, 150, "bottom-center")
        xt.local.position = (vx + vw / 2, size[1] - 6, 0)
        self.overlay.add(xt)
        yt = _text(gfx, m.value_title, TITLE_FONT, theme.GRAPH_TITLE, 150, "top-left")
        yt.local.position = (8, 4, 0)
        self.overlay.add(yt)


# =============================================================================
# The view (one per GraphWidget)
# =============================================================================
class GpuGraphView(QObject):
    engine_name = "gpu"

    def __init__(self, parent=None):
        super().__init__(parent)
        import pygfx as gfx
        from rendercanvas.qt import QRenderWidget

        self.gfx = gfx
        self.widget = QRenderWidget(parent, update_mode="ondemand", max_fps=60)
        self.widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.widget.setMouseTracking(True)
        self.widget.installEventFilter(self)
        self.renderer = gfx.renderers.WgpuRenderer(self.widget, pixel_ratio=PIXEL_RATIO)
        self.bg_scene = gfx.Scene()
        self.bg_scene.add(
            gfx.Background(
                None,
                gfx.BackgroundMaterial(
                    theme.GRAPH_STAGE_BOTTOM,
                    theme.GRAPH_STAGE_BOTTOM,
                    theme.GRAPH_STAGE_TOP,
                    theme.GRAPH_STAGE_TOP,
                ),
            )
        )
        self.screen_cam = gfx.ScreenCoordsCamera(invert_y=True)
        self.scene = None
        self.camera = None
        self.controller = None
        self.surface: Optional[Surface3D] = None
        self.line: Optional[Line2D] = None
        self.model: Optional[GraphModel] = None
        self._selected = []
        self.autofit = True
        self._fit = (1.0, 0.0, 0.0)  # scale, cx, cy (base px, from centre)
        self.n_data_updates = 0
        self.n_color_updates = 0
        self.n_draws = 0
        # hover readout
        self.readout = QLabel(parent)
        self.readout.setStyleSheet(theme.get_graph_readout_stylesheet())
        self.readout.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.readout.hide()
        self._hover_pos: Optional[QPoint] = None
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(30)
        self._hover_timer.timeout.connect(self._update_hover)
        self.widget.request_draw(self._draw)

    # -- scene building -------------------------------------------------------
    def _new_3d_scene(self):
        gfx = self.gfx
        self.scene = gfx.Scene()
        self.scene.add(gfx.AmbientLight(theme.GRAPH_AMBIENT_LIGHT, 1.2))
        self.surface = Surface3D(gfx)
        self.scene.add(self.surface.root)
        self.camera = gfx.PerspectiveCamera(FOV, 1.0)
        key = gfx.DirectionalLight(theme.GRAPH_KEY_LIGHT, 0.85)
        key.local.position = (-1, -2, 3)
        fill = gfx.DirectionalLight(theme.GRAPH_FILL_LIGHT, 0.3)
        fill.local.position = (2, 1, 1)
        self.camera.add(key, fill)
        self.scene.add(self.camera)
        self._place_camera(*DEFAULT_VIEW)
        self.controller = gfx.OrbitController(
            self.camera, register_events=self.renderer
        )
        self.controller.target = (BX / 2, BY / 2, BZ / 2)

    def _new_2d_scene(self):
        gfx = self.gfx
        self.scene = gfx.Scene()
        self.line = Line2D(gfx)
        self.scene.add(self.line.plot)
        self.overlay_scene = gfx.Scene()
        self.overlay_scene.add(self.line.overlay)
        self.camera = gfx.OrthographicCamera(1, 1, maintain_aspect=False)
        self._zoom2d = 1.0

    def _place_camera(self, elev, azim):
        e, a = math.radians(elev), math.radians(azim)
        tgt = np.array([BX / 2, BY / 2, BZ / 2])
        pos = tgt + CAM_DIST * np.array(
            [math.cos(e) * math.cos(a), math.cos(e) * math.sin(a), math.sin(e)]
        )
        self.camera.local.position = tuple(pos)
        self.camera.world.reference_up = (0, 0, 1)
        self.camera.look_at(tuple(tgt))

    def show_model(self, model: GraphModel, selected_cells):
        """First show / structural change: (re)build the scene."""
        kind_changed = self.model is None or self.model.kind != model.kind
        self.model, self._selected = model, list(selected_cells)
        if model.kind == TableType.THREE_D:
            if kind_changed or self.surface is None:
                self._new_3d_scene()
            self.surface.set_model(model)
            self.surface.set_crosshair(single_cell(self._selected))
        else:
            if kind_changed or self.line is None:
                self._new_2d_scene()
            self.line.set_model(model, self._selected)
        self.request_draw()

    def update_data(self, model: GraphModel, selected_cells):
        """Values/axes changed: rebuild geometry + labels; camera untouched."""
        self.n_data_updates += 1
        if self.model is None or self.model.kind != model.kind:
            self.show_model(model, selected_cells)
            return
        self.model, self._selected = model, list(selected_cells)
        if self.surface is not None and model.kind == TableType.THREE_D:
            self.surface.set_model(model)
            self.surface.set_crosshair(single_cell(self._selected))
        elif self.line is not None:
            self.line.set_model(model, self._selected)
        self.request_draw()

    def update_colors(self, colors: np.ndarray, selected_cells):
        """Selection-only change: colors buffer only (no geometry rebuild)."""
        self.n_color_updates += 1
        self._selected = list(selected_cells)
        if self.surface is not None and self.is_3d():
            self.surface.set_colors(colors)
            self.surface.set_crosshair(single_cell(self._selected))
        elif self.line is not None and self.model is not None:
            self.line.set_selection(colors, self._selected)
        self.request_draw()

    # -- view -----------------------------------------------------------------
    def is_3d(self):
        return self.model is not None and self.model.kind == TableType.THREE_D

    def get_view(self):
        if not self.is_3d():
            return None
        v = np.array(self.camera.world.position) - np.array(self.controller.target)
        r = np.linalg.norm(v) or 1.0
        elev = math.degrees(math.asin(max(-1.0, min(1.0, v[2] / r))))
        azim = math.degrees(math.atan2(v[1], v[0]))
        return (elev, azim)

    def set_view(self, elev, azim):
        if not self.is_3d():
            return
        elev = max(-89.0, min(89.0, float(elev)))
        zoom = self.camera.zoom
        self._place_camera(elev, azim)
        self.camera.zoom = zoom
        self.controller.target = (BX / 2, BY / 2, BZ / 2)
        self.request_draw()

    def rotate(self, d_azim, d_elev):
        view = self.get_view()
        if view:
            self.set_view(view[0] + d_elev, view[1] + d_azim)

    def get_zoom(self):
        if self.camera is None:
            return 1.0
        return float(self.camera.zoom) if self.is_3d() else self._zoom2d

    def zoom(self, factor):
        if self.camera is None:
            return
        self.autofit = False
        if self.is_3d():
            self.camera.zoom = float(self.camera.zoom) * factor
        else:
            self._zoom2d *= factor
        self.request_draw()

    def reset_view(self):
        if self.camera is None:
            return
        self.autofit = True
        if self.is_3d():
            self.camera.zoom = 1.0
            self.set_view(*DEFAULT_VIEW)
        else:
            self._zoom2d = 1.0
        self.request_draw()

    def request_draw(self):
        self.widget.request_draw()

    # -- drawing --------------------------------------------------------------
    def _logical_size(self):
        w, h = self.widget.get_logical_size()
        return max(1.0, float(w)), max(1.0, float(h))

    def _project_base(self, pts: np.ndarray, size) -> np.ndarray:
        """World → base pixel coords (relative to centre, y down), no offset."""
        W, H = size
        cam = self.camera
        cam.clear_view_offset()
        cam.set_view_size(W, H)
        m = np.asarray(cam.projection_matrix) @ np.asarray(cam.view_matrix)
        hom = np.column_stack([pts, np.ones(len(pts))]) @ m.T
        ndc = hom[:, :2] / hom[:, 3:4]
        return np.column_stack([ndc[:, 0] * W / 2, -ndc[:, 1] * H / 2])

    def _fit_3d(self, size):
        W, H = size
        cam_pos = np.array(self.camera.world.position)
        self.surface.layout_labels(cam_pos, lambda p: self._project_base(p, size))
        pts, ext, recs = self.surface.fit_items()
        base = self._project_base(pts, size)
        if self.autofit:
            sx = _fit_axis(base[:, 0], ext[:, 0], W - 2 * FIT_MARGIN)
            sy = _fit_axis(base[:, 1], ext[:, 1], H - 2 * FIT_MARGIN)
            s = min(sx, sy)
            cx = (
                (
                    (s * base[:, 0] + ext[:, 0]).max()
                    + (s * base[:, 0] - ext[:, 0]).min()
                )
                / 2
                / s
            )
            cy = (
                (
                    (s * base[:, 1] + ext[:, 1]).max()
                    + (s * base[:, 1] - ext[:, 1]).min()
                )
                / 2
                / s
            )
            self._fit = (s, cx, cy)
        s, cx, cy = self._fit
        fw, fh = W * s, H * s
        self.camera.set_view_offset(
            fw, fh, fw / 2 + s * cx - W / 2, fh / 2 + s * cy - H / 2, W, H
        )
        # Declutter: greedy over ALL labels — titles first, then ticks in axis
        # order; a label is hidden if its screen rect overlaps any shown one.
        scr = s * (base - np.array([cx, cy]))
        order = sorted(
            (i for i, rec in enumerate(recs) if rec is not None),
            # callouts first (never hidden), then titles, then plain ticks
            key=lambda i: (
                (
                    0
                    if recs[i][1][0] == "h"
                    else 1 if recs[i][1] in ("xt", "yt", "zt") else 2
                ),
                i,
            ),
        )
        shown = []
        for i in order:
            ex, ey = ext[i]
            ok = all(
                abs(scr[i][0] - scr[j][0]) >= ex + ext[j][0] + 2
                or abs(scr[i][1] - scr[j][1]) >= ey + ext[j][1]
                for j in shown
            )
            recs[i][0].visible = ok
            if ok:
                shown.append(i)

    def _draw(self):
        if self.scene is None or self.model is None:
            self.renderer.render(self.bg_scene, self.screen_cam)
            return
        self.n_draws += 1
        size = self._logical_size()
        self.renderer.render(self.bg_scene, self.screen_cam, flush=False)
        if self.is_3d():
            self._fit_3d(size)
            self.renderer.render(self.scene, self.camera, flush=False)
        else:
            W, H = size
            vp = (
                M_LEFT,
                M_TOP,
                max(1.0, W - M_LEFT - M_RIGHT),
                max(1.0, H - M_TOP - M_BOTTOM),
            )
            x0, x1, y0, y1 = self.line.base_rect
            cxm, cym = (x0 + x1) / 2, (y0 + y1) / 2
            hw, hh = (x1 - x0) / 2 / self._zoom2d, (y1 - y0) / 2 / self._zoom2d
            self.line.rect = (cxm - hw, cxm + hw, cym - hh, cym + hh)
            self.camera.show_rect(*self.line.rect)
            self.gfx.Viewport(self.renderer, rect=vp).render(self.scene, self.camera)
            self.line.build_overlay(vp, size)
            self.renderer.render(self.overlay_scene, self.screen_cam, flush=False)
            self._vp = vp
        self.renderer.flush()

    # -- events ---------------------------------------------------------------
    def eventFilter(self, obj, event):  # noqa: N802
        et = event.type()
        if et == QEvent.Wheel:
            self.autofit = False
        elif et == QEvent.MouseButtonPress and event.button() == Qt.RightButton:
            self.autofit = False
        elif et == QEvent.MouseButtonDblClick:
            self.reset_view()
            return True
        elif et == QEvent.MouseMove:
            self._hover_pos = event.position().toPoint()
            self._hover_timer.start()
        elif et == QEvent.Leave:
            self._hover_pos = None
            self.readout.hide()
        elif et == QEvent.Resize:
            self.request_draw()
        return False

    def _update_hover(self):
        if self._hover_pos is None or self.model is None:
            return
        text = self.hover_text_at(self._hover_pos.x(), self._hover_pos.y())
        if not text:
            self.readout.hide()
            return
        self.readout.setText(text)
        self.readout.adjustSize()
        p = (
            self.widget.mapTo(self.readout.parentWidget(), self._hover_pos)
            if self.readout.parentWidget()
            else self._hover_pos
        )
        x = min(p.x() + 14, self.widget.width() - self.readout.width() - 4)
        y = max(4, p.y() - self.readout.height() - 10)
        self.readout.move(max(4, x), y)
        self.readout.show()
        self.readout.raise_()

    def hover_text_at(self, x: float, y: float) -> str:
        """Readout for logical pixel (x, y) — '' when nothing is under it."""
        if self.is_3d():
            try:
                info = self.renderer.get_pick_info((x, y))
            except Exception:  # noqa: BLE001
                return ""
            if not info or info.get("world_object") is not self.surface.mesh:
                return ""
            fi = info.get("face_index")
            if fi is None:
                return ""
            row, col = self.surface.cell_at_face(fi)
            return hover_text(self.model, row, col)
        if self.line is None or not hasattr(self, "_vp"):
            return ""
        px, _ = self.line.to_px(self.model.x_values, self.model.values, self._vp)
        if not len(px):
            return ""
        i = int(np.argmin(np.abs(px - x)))
        if abs(px[i] - x) > 30:
            return ""
        return hover_text(self.model, i)

    # -- misc -----------------------------------------------------------------
    def grab(self):
        self.widget.force_draw()
        return self.widget.grab()

    def snapshot(self) -> np.ndarray:
        self.widget.force_draw()
        return np.asarray(self.renderer.snapshot())

    def shutdown(self):
        """Release every GPU resource (canvas.close() is the part that matters)."""
        self._hover_timer.stop()
        try:
            self.widget.removeEventFilter(self)
        except RuntimeError:
            pass
        if self.controller is not None:
            try:
                self.controller.register_events(None)
            except Exception:  # noqa: BLE001
                pass
        self.controller = None
        if self.scene is not None:
            self.scene.clear()
        self.scene = self.camera = self.surface = self.line = None
        self.renderer = None
        try:
            self.widget.close()
        except RuntimeError:
            pass
        self.readout.deleteLater()
