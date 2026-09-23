"""UI theme — the single source of colors, fonts, and QSS builders.

NC Flash force-pins the Light palette (``main.py``), so styling is a flat set of
named constants + small QSS builder functions, NOT a theming framework and NOT
dark mode (see the architecture "Not doing" fence). New widget styling should
pull colors from here and, where a stylesheet repeats across widgets, add a
builder here instead of pasting the QSS again. The hex-literal ratchet
(``tests/test_theme_ratchet.py``) keeps inline ``setStyleSheet`` from regrowing.

This lives alongside the older ``utils.constants.get_table_stylesheet``; new
style helpers land here (Phase 6a).
"""

# --- Named colors -----------------------------------------------------------
# Neutral hover/press wash used on flat tool buttons (theme-agnostic grey so it
# reads on either palette).
HOVER_WASH = "rgba(128, 128, 128, 0.15)"
HOVER_BORDER = "rgba(128, 128, 128, 0.25)"
PRESSED_WASH = "rgba(128, 128, 128, 0.3)"

# Accent (Windows-blue) used for a checked/toggled tool button.
ACCENT = "#0078d7"
ACCENT_WASH = "rgba(0, 120, 215, 0.15)"
ACCENT_BORDER = "rgba(0, 120, 215, 0.4)"

# Table axis header cell matching the current selection (row/column breakpoint).
AXIS_HIGHLIGHT = ACCENT

# Amber for warning states (e.g. the ECU window's unexpected-disconnect label).
WARNING_AMBER = "#cc6600"

# --- Table graph (GPU renderer) ----------------------------------------------
# The graph pane is a dark "stage" inside the light app: a vertical gradient
# with light text, so the per-cell table colors pop. sRGB hex; the GPU backend
# converts to linear where pygfx expects it.
GRAPH_STAGE_TOP = "#3a4760"
GRAPH_STAGE_BOTTOM = "#161b27"
GRAPH_FLOOR_GRID = "#56627a"
GRAPH_AXIS = "#c8d0e0"
GRAPH_CELL_EDGE = "#1c2029"
GRAPH_TEXT = "#e8ecf4"
GRAPH_TITLE = "#ffffff"
GRAPH_TEXT_OUTLINE = "#000000"
GRAPH_AMBIENT_LIGHT = "#ffffff"
GRAPH_KEY_LIGHT = "#fff4e0"
GRAPH_FILL_LIGHT = "#cfe0ff"
GRAPH_SPECULAR = "#555555"
GRAPH_MARKER_RING = "#ffffff"
GRAPH_CROSSHAIR = "#ffffff"  # row/column guide through the selected cell
GRAPH_CALLOUT = "#ffffff"  # axis values of the selected cell


def get_graph_readout_stylesheet() -> str:
    """Hover readout chip floating over the graph (value under the cursor)."""
    return (
        "QLabel {"
        " background-color: rgba(10, 14, 22, 0.82);"
        f" color: {GRAPH_TEXT};"
        " border: 1px solid rgba(200, 208, 224, 0.35);"
        " border-radius: 6px;"
        " padding: 4px 8px;"
        " font-size: 9pt;"
        "}"
    )


def get_graph_status_stylesheet() -> str:
    """'Preparing graph…' placeholder shown while the GPU warms up."""
    return (
        "QLabel {"
        f" background-color: {GRAPH_STAGE_BOTTOM};"
        f" color: {GRAPH_AXIS};"
        " font-size: 10pt;"
        "}"
    )


def get_toolbar_stylesheet(checked: bool = False) -> str:
    """QSS for the app's flat icon toolbars (main / table / compare windows).

    Args:
        checked: include a ``:checked`` rule (accent wash) for toolbars that have
            checkable buttons, e.g. the table window's graph toggle.
    """
    base = f"""
            QToolBar {{
                spacing: 1px;
                padding: 1px 4px;
                border: none;
            }}
            QToolButton {{
                padding: 3px;
                border: 1px solid transparent;
                border-radius: 3px;
            }}
            QToolButton:hover {{
                background: {HOVER_WASH};
                border: 1px solid {HOVER_BORDER};
            }}
            QToolButton:pressed {{
                background: {PRESSED_WASH};
            }}
    """
    if checked:
        base += f"""
            QToolButton:checked {{
                background: {ACCENT_WASH};
                border: 1px solid {ACCENT_BORDER};
            }}
    """
    return base
