"""Architecture ratchet — enforce the layered-import rules from CLAUDE.md.

Layering:  utils <- core <- {ecu, ui, api, mcp};  main.py composes ui.

Enforced here (static AST parse; no modules are imported/executed):
  1. core / ecu / utils / api must NOT import src.ui.
  2. core / utils must NOT import src.ecu.

Both absolute (``from src.ecu import x``) and relative (``from ..core import x``)
imports are resolved to their layer, including lazy imports inside functions.

Rationale and the incidents that motivated each rule: docs/internal/ARCHITECTURE.md.
If you are here to add a new cross-layer edge, read that doc first — you are
almost certainly meant to add a collaborator/signal, not a back-import.

Keep this test dependency-free and fast (<1s).
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LAYERS = {"core", "ecu", "ui", "api", "mcp", "utils"}

# (source layers) -> layer they must NOT import
FORBIDDEN = [
    ({"core", "ecu", "utils", "api", "mcp"}, "ui"),
    ({"core", "utils"}, "ecu"),
]


def _iter_py_files():
    """All source modules that participate in the layering rules."""
    yield REPO_ROOT / "main.py"
    yield from sorted((REPO_ROOT / "src").rglob("*.py"))


def _package_of(path):
    """Dotted package that CONTAINS this file, relative to the repo root.

    src/ecu/wican_transport.py -> 'src.ecu'
    src/ecu/__init__.py        -> 'src.ecu'
    main.py                    -> ''
    """
    rel = path.resolve().relative_to(REPO_ROOT)
    return ".".join(rel.parts[:-1])


def _layer_of(dotted):
    """Layer name for a dotted module/package, or None if it is not ours."""
    parts = dotted.split(".")
    if len(parts) >= 2 and parts[0] == "src" and parts[1] in LAYERS:
        return parts[1]
    return None


def _imported_modules(pkg, node):
    """Yield absolute dotted module names referenced by an import node.

    Relative imports are resolved against ``pkg`` the way Python resolves
    them at runtime (level 1 = current package, level 2 = parent, ...).

    ImportFrom also yields ``<module>.<name>`` for each imported name: a
    back-edge spelled ``from src import ui`` (or ``from .. import ui``)
    resolves to bare ``src`` otherwise, whose layer is None — invisible to
    the ratchet. Appending the alias can only change the detected layer in
    exactly that case; a plain symbol import just re-yields its module's
    layer, so no false positives.
    """
    if isinstance(node, ast.Import):
        for alias in node.names:
            yield alias.name
    elif isinstance(node, ast.ImportFrom):
        if node.level == 0:
            base = node.module.split(".") if node.module else []
        else:
            base = pkg.split(".") if pkg else []
            climb = node.level - 1
            if climb:
                base = base[:-climb] if climb <= len(base) else []
            if node.module:
                base = base + node.module.split(".")
        if base:
            yield ".".join(base)
        for alias in node.names:
            if alias.name != "*":
                yield ".".join(base + [alias.name])


def test_layered_imports():
    """No module imports a layer it is forbidden to depend on."""
    violations = []
    scanned = 0
    for path in _iter_py_files():
        src_layer = _layer_of(_package_of(path))
        if src_layer is None:
            continue  # main.py (composition root) and src/__init__.py
        scanned += 1
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        pkg = _package_of(path)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for module in _imported_modules(pkg, node):
                target = _layer_of(module)
                if target is None:
                    continue
                for sources, forbidden in FORBIDDEN:
                    if src_layer in sources and target == forbidden:
                        rel = path.resolve().relative_to(REPO_ROOT)
                        violations.append(
                            f"{rel.as_posix()} [{src_layer}] imports "
                            f"{module} [{target}]"
                        )

    # Guard: a path/glob bug must not let the ratchet pass vacuously.
    assert scanned > 20, f"only scanned {scanned} modules — walker is broken"
    assert not violations, "Layering violations:\n  " + "\n  ".join(violations)
