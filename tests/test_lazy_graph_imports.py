"""E2: graph libraries must stay OUT of the startup import graph.

Importing matplotlib eagerly cost ~1.6-1.8 s of cold start; pygfx/wgpu cost more
(and wgpu probes the GPU). A GraphWidget is only given a renderer on first G, and
GPU warm-up imports pygfx on a background thread after the window is idle. This
ratchet imports the startup chain in a CLEAN subprocess and fails if any graph
library got pulled in at import time.
"""

import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent


_GRAPH_LIBS = ("matplotlib", "pygfx", "wgpu", "rendercanvas")


def _matplotlib_loaded_after(import_stmt: str) -> bool:
    """Return whether any graph library is in sys.modules after `import_stmt`."""
    code = (
        f"import sys; {import_stmt}; "
        f"libs = {_GRAPH_LIBS!r}; "
        "mods = [m for m in sys.modules if m.split('.')[0] in libs]; "
        "print('LOADED ' + ' '.join(sorted(mods)[:5]) if mods else 'CLEAN')"
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


def test_gpu_runtime_and_main_window_import_do_not_load_graph_libs():
    # gpu_runtime is imported by main() at startup; it must defer pygfx/wgpu to
    # the warm-up thread.
    assert not _matplotlib_loaded_after(
        "import src.ui.gpu_runtime, src.ui.graph_viewer, src.ui.table_viewer_window"
    )
