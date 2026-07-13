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

# Amber for warning states (e.g. the ECU window's unexpected-disconnect label).
WARNING_AMBER = "#cc6600"

# Muted grey for secondary/subtitle text (e.g. status subtitles under a button).
MUTED_TEXT = "#888888"


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
