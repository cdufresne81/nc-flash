"""Phase 6a: theme ratchet — inline color literals in widgets may only SHRINK.

`src/ui/theme.py` is the single home for colors + QSS builders (the app force-pins
Light). This ratchet counts raw color literals (``#hex`` and ``rgb/rgba(...)``) in
each ``src/ui`` widget module and fails if any file grows past its allowance, or
if a file NOT on the (shrinking) allowlist introduces any. To migrate a site,
move its colors into ``theme.py`` and LOWER its number here — never raise one, and
never add a new file. New styling goes through ``theme.py`` (exempt).
"""

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_UI = _REPO / "src" / "ui"

# Legacy color-literal budget per file (2026-07-06 baseline). SHRINK-ONLY: as
# sites migrate to theme.py, lower these; do not add files or raise counts.
# main.py is scanned too — MainWindow is the app's largest widget and lives
# outside src/ui.
_ALLOWLIST = {
    "main.py": 4,
    "compare_window.py": 18,
    "data_operation_dialogs.py": 3,
    "ecu_window.py": 39,
    "graph_viewer.py": 2,
    "history_viewer.py": 1,
    "log_console.py": 9,
    "mcp_mixin.py": 1,
    "patch_dialog.py": 2,
    "project_wizard.py": 1,
    "settings_dialog.py": 16,
    "setup_wizard.py": 6,
    "table_viewer.py": 2,
    "widgets/toggle_switch.py": 3,
}

_HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
_RGB = re.compile(r"rgba?\(")


def _count_color_literals(text: str) -> int:
    return len(_HEX.findall(text)) + len(_RGB.findall(text))


def _rel(p: Path) -> str:
    return p.relative_to(_UI).as_posix() if p.is_relative_to(_UI) else p.name


def test_no_widget_exceeds_its_color_literal_budget():
    offenders = []
    new_files = []
    for path in [_REPO / "main.py", *sorted(_UI.rglob("*.py"))]:
        if path.name == "theme.py":
            continue  # the sanctioned home for colors
        rel = _rel(path)
        n = _count_color_literals(path.read_text(encoding="utf-8"))
        if n == 0:
            continue
        budget = _ALLOWLIST.get(rel)
        if budget is None:
            new_files.append((rel, n))
        elif n > budget:
            offenders.append((rel, n, budget))

    assert not new_files, (
        "New/unlisted widget files with raw color literals — route colors "
        f"through src/ui/theme.py instead: {new_files}"
    )
    assert not offenders, (
        "Color literals grew (ratchet is shrink-only) — put new colors in "
        f"src/ui/theme.py: {offenders}"
    )


def test_migrated_files_dropped_off_the_allowlist():
    # table_viewer_window migrated its toolbar QSS to theme.get_toolbar_stylesheet
    # and must stay off the allowlist (zero raw color literals).
    tvw = _UI / "table_viewer_window.py"
    assert _count_color_literals(tvw.read_text(encoding="utf-8")) == 0
    assert "table_viewer_window.py" not in _ALLOWLIST
