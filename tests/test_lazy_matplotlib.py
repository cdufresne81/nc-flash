"""E2: matplotlib must stay OUT of the startup import graph.

Importing matplotlib eagerly cost ~1.6-1.8 s of cold start even though a
GraphWidget (the only matplotlib consumer) is built lazily — only when a 2D/3D
table is opened. This ratchet imports the startup chain in a CLEAN subprocess and
fails if matplotlib got pulled in at import time.
"""

import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent


def _matplotlib_loaded_after(import_stmt: str) -> bool:
    """Return whether `matplotlib` is in sys.modules after `import_stmt`."""
    code = (
        f"import sys; {import_stmt}; "
        "mods = [m for m in sys.modules if m == 'matplotlib' or m.startswith('matplotlib.')]; "
        "print('LOADED' if mods else 'CLEAN')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(_REPO),
    )
    assert result.returncode == 0, result.stderr
    return "LOADED" in result.stdout


def test_graph_viewer_module_does_not_import_matplotlib():
    # Importing the graph_viewer module must NOT load matplotlib (its Figure /
    # FigureCanvas imports live inside GraphWidget.__init__).
    assert not _matplotlib_loaded_after("import src.ui.graph_viewer")


def test_table_viewer_window_import_does_not_load_matplotlib():
    # The full startup chain (main -> table_viewer_window -> graph_viewer) must
    # stay matplotlib-free until a graph is actually built.
    assert not _matplotlib_loaded_after("import src.ui.table_viewer_window")
