"""
Workspace Directory Management

Creates workspace subdirectories and handles one-time migrations
(e.g. copying bundled metadata/colormaps on first run).
"""

import logging
import shutil
from pathlib import Path

from .paths import get_app_root
from .settings import get_settings

logger = logging.getLogger(__name__)

_SUBDIRS = [
    "roms",
    "projects",
    "metadata",
    "exports",
    "screenshots",
    "colormaps",
    "reads",
    "logs",
]


def ensure_workspace_directories():
    """Create workspace subdirectories if they don't exist, then run migrations."""
    settings = get_settings()
    workspace = Path(settings.get_workspace_directory())

    for subdir in _SUBDIRS:
        (workspace / subdir).mkdir(parents=True, exist_ok=True)

    _run_migrations(settings, workspace)


def _run_migrations(settings, workspace: Path):
    """One-time migrations gated by a QSettings flag."""
    if settings.settings.value("migration/workspace_v1_done", False, type=bool):
        return

    app_root = get_app_root()

    # Migrate bundled metadata XMLs
    _copy_if_empty(
        src=app_root / "examples" / "metadata",
        dst=workspace / "metadata",
        glob="*.xml",
    )

    # Migrate bundled colormaps
    _copy_if_empty(
        src=app_root / "colormaps",
        dst=workspace / "colormaps",
        glob="*.map",
    )

    settings.settings.setValue("migration/workspace_v1_done", True)
    logger.info("Workspace v1 migration complete")


def _copy_if_empty(src: Path, dst: Path, glob: str):
    """Copy files matching *glob* from *src* to *dst* if dst has none."""
    if not src.is_dir():
        return
    existing = list(dst.glob(glob))
    if existing:
        return
    for f in src.glob(glob):
        try:
            shutil.copy2(f, dst / f.name)
            logger.info(f"Migrated {f.name} -> {dst}")
        except OSError as e:
            logger.warning(f"Failed to copy {f.name}: {e}")
