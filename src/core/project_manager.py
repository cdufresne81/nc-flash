"""
Project Manager

Handles project creation, loading, saving, and file management.
"""

import json
import os
import shutil
import hashlib
from pathlib import Path
from typing import Optional, List
from datetime import datetime
import logging

from .version_models import (
    Project, OriginalRomInfo, Commit, TableChanges
)
from .rom_definition import RomDefinition
from .exceptions import (
    ProjectError, ProjectNotFoundError, ProjectCorruptError, ProjectSaveError
)

logger = logging.getLogger(__name__)

PROJECT_VERSION = "1.0"
PROJECT_FILE = "project.json"
COMMITS_FILE = "commits.json"


class ProjectManager:
    """Manages ROM editing projects with version control"""

    def __init__(self):
        self.current_project: Optional[Project] = None
        self.commits: List[Commit] = []

    def create_project(
        self,
        project_path: str,
        project_name: str,
        source_rom_path: str,
        rom_definition: RomDefinition,
        description: str = ""
    ) -> Project:
        """
        Create a new project from a ROM file

        Args:
            project_path: Path where project folder will be created
            project_name: Name for the project
            source_rom_path: Path to source ROM file
            rom_definition: ROM definition metadata
            description: Optional project description

        Returns:
            Created Project object

        Raises:
            ProjectError: If project creation fails
        """
        project_dir = Path(project_path)

        try:
            # Create project directory (flat structure, no subdirectories)
            project_dir.mkdir(parents=True, exist_ok=True)

            source_path = Path(source_rom_path)
            rom_id = rom_definition.romid.internalidstring

            # v0 = pristine backup (never modified)
            v0_filename = f"v0_{rom_id}_original.bin"
            v0_path = project_dir / v0_filename
            shutil.copy2(source_path, v0_path)

            # v1 = working ROM (editable copy)
            working_filename = f"v1_{rom_id}_working.bin"
            working_path = project_dir / working_filename
            shutil.copy2(source_path, working_path)

            # Calculate checksum from pristine copy
            with open(v0_path, 'rb') as f:
                checksum = hashlib.sha256(f.read()).hexdigest()

            # Create original ROM info
            original_info = OriginalRomInfo(
                filename=v0_filename,
                size=v0_path.stat().st_size,
                checksum_sha256=checksum,
                rom_id=rom_id,
                definition_xmlid=rom_definition.romid.xmlid,
                make=rom_definition.romid.make,
                model=rom_definition.romid.model
            )

            # Create project
            now = datetime.now()
            project = Project(
                version=PROJECT_VERSION,
                name=project_name,
                description=description,
                created_at=now,
                updated_at=now,
                original_rom=original_info,
                working_rom=working_filename,
                head_commit_id=None,
                project_path=str(project_dir),
                head_version=0,
                last_suffix="original",
                settings={"auto_snapshot_interval": 10}
            )

            # Save project file
            self._save_project_file(project)

            # Initialize commit history with v0 (pristine backup)
            initial_commit = Commit.create(
                message="Original ROM",
                changes=[],
                version=0,
                parent_id=None,
                snapshot_filename=v0_filename
            )
            self.commits = [initial_commit]
            self._save_commits(self.commits, project)

            # Update project head
            project.head_commit_id = initial_commit.id
            project.head_version = 0
            self._save_project_file(project)

            self.current_project = project
            logger.info(f"Created project: {project_name} at {project_path}")

            return project

        except Exception as e:
            logger.error(f"Failed to create project: {e}")
            raise ProjectError(f"Failed to create project: {e}")

    def open_project(self, project_path: str) -> Project:
        """
        Open an existing project

        Args:
            project_path: Path to project folder

        Returns:
            Project object

        Raises:
            ProjectNotFoundError: If project folder or file doesn't exist
            ProjectCorruptError: If project data is invalid
        """
        project_dir = Path(project_path)
        project_file = project_dir / PROJECT_FILE

        if not project_dir.exists():
            raise ProjectNotFoundError(f"Project folder not found: {project_path}")

        if not project_file.exists():
            raise ProjectNotFoundError(f"No project.json found in {project_path}")

        try:
            # Load project metadata
            with open(project_file, 'r') as f:
                data = json.load(f)

            project = Project.from_dict(data, str(project_dir))

            # Verify working ROM exists
            if not Path(project.working_rom_path).exists():
                raise ProjectCorruptError(f"Working ROM not found: {project.working_rom_path}")

            # Load commit history
            self.commits = self._load_commits(project_dir)

            self.current_project = project

            logger.info(f"Opened project: {project.name}")

            return project

        except json.JSONDecodeError as e:
            raise ProjectCorruptError(f"Invalid project.json: {e}")
        except KeyError as e:
            raise ProjectCorruptError(f"Missing field in project.json: {e}")

    def save_project(self):
        """Save current project metadata"""
        if not self.current_project:
            raise ProjectError("No project is currently open")

        self.current_project.updated_at = datetime.now()
        self._save_project_file(self.current_project)
        logger.info("Project metadata saved")

    def commit_changes(
        self,
        message: str,
        changes: List[TableChanges],
        create_snapshot: bool = False,
        snapshot_suffix: str = ""
    ) -> Commit:
        """
        Create a new commit with pending changes

        Args:
            message: Commit message
            changes: List of table changes
            create_snapshot: Whether to create a full ROM snapshot
            snapshot_suffix: User-provided suffix for snapshot filename

        Returns:
            Created Commit object

        Raises:
            ProjectError: If no project is open or commit fails
        """
        if not self.current_project:
            raise ProjectError("No project is currently open")

        try:
            # Calculate next version
            next_version = self.get_next_version()
            rom_id = self.current_project.original_rom.rom_id

            # Generate snapshot filename if creating snapshot
            snapshot_filename = None
            if create_snapshot and snapshot_suffix:
                snapshot_filename = f"v{next_version}_{rom_id}_{snapshot_suffix}.bin"

            # Create commit with version
            commit = Commit.create(
                message=message,
                changes=changes,
                version=next_version,
                parent_id=self.current_project.head_commit_id,
                snapshot_filename=snapshot_filename
            )

            # Optionally create snapshot at project root
            if create_snapshot and snapshot_filename:
                project_dir = Path(self.current_project.project_path)
                working_path = project_dir / self.current_project.working_rom
                snapshot_path = project_dir / snapshot_filename
                shutil.copy2(working_path, snapshot_path)
                logger.debug(f"Created snapshot: {snapshot_filename}")

            # Add to history
            self.commits.append(commit)
            self._save_commits(self.commits, self.current_project)

            # Update project head and version
            self.current_project.head_commit_id = commit.id
            self.current_project.head_version = next_version
            if snapshot_suffix:
                self.current_project.last_suffix = snapshot_suffix
            self.save_project()

            tables_str = ", ".join(commit.tables_modified[:3])
            if len(commit.tables_modified) > 3:
                tables_str += f" (+{len(commit.tables_modified) - 3} more)"

            logger.info(f"Committed v{next_version}: {message[:50]}... ({len(changes)} tables: {tables_str})")

            return commit

        except Exception as e:
            logger.error(f"Failed to commit changes: {e}")
            raise ProjectSaveError(f"Failed to commit changes: {e}")

    def get_commit_history(self) -> List[Commit]:
        """Get all commits in chronological order"""
        return list(self.commits)

    def get_recent_commits(self, limit: int = 20) -> List[Commit]:
        """Get most recent commits (newest first)"""
        return list(reversed(self.commits[-limit:]))

    def get_commit(self, commit_id: str) -> Optional[Commit]:
        """Get a specific commit by ID"""
        for commit in self.commits:
            if commit.id == commit_id:
                return commit
        return None

    def get_table_history(self, table_name: str) -> List[Commit]:
        """Get all commits that modified a specific table"""
        return [
            c for c in self.commits
            if table_name in c.tables_modified
        ]

    def get_next_version(self) -> int:
        """Get the next version number for a new commit"""
        if not self.current_project:
            return 0
        return self.current_project.head_version + 1

    def get_commit_by_version(self, version: int) -> Optional[Commit]:
        """Get a commit by its version number"""
        for commit in self.commits:
            if commit.version == version:
                return commit
        return None

    def get_snapshot_path(self, version: int) -> Optional[Path]:
        """
        Find snapshot file path by version number

        Args:
            version: Version number to find

        Returns:
            Path to snapshot file, or None if not found
        """
        if not self.current_project:
            return None

        project_dir = Path(self.current_project.project_path)

        # For version 0, look up snapshot_filename from the commit
        if version == 0:
            commit = self.get_commit_by_version(0)
            if commit and commit.snapshot_filename:
                v0_path = project_dir / commit.snapshot_filename
                if v0_path.exists():
                    return v0_path
            # Fallback for old projects: original.bin
            original_path = project_dir / "original.bin"
            if original_path.exists():
                return original_path

        # Try v{version}_*.bin at project root (new flat structure)
        for f in project_dir.glob(f"v{version}_*.bin"):
            return f

        # Fallback for old projects: history/snapshots/
        snapshots_dir = project_dir / "history" / "snapshots"
        if snapshots_dir.exists():
            for f in snapshots_dir.glob(f"v{version}_*.bin"):
                return f
            # Fall back to old naming pattern: commit_{uuid}.bin
            commit = self.get_commit_by_version(version)
            if commit:
                old_path = snapshots_dir / f"commit_{commit.id}.bin"
                if old_path.exists():
                    return old_path

        return None

    def load_version_data(self, version: int) -> Optional[bytes]:
        """
        Load ROM data from a specific version

        Args:
            version: Version number to load

        Returns:
            ROM data as bytes, or None if not found
        """
        snapshot_path = self.get_snapshot_path(version)
        if snapshot_path and snapshot_path.exists():
            with open(snapshot_path, 'rb') as f:
                return f.read()
        return None

    def close_project(self):
        """Close the current project"""
        if self.current_project:
            logger.info(f"Closing project: {self.current_project.name}")
        self.current_project = None
        self.commits = []

    def is_project_open(self) -> bool:
        """Check if a project is currently open"""
        return self.current_project is not None

    def _save_project_file(self, project: Project):
        """Save project.json (atomic write)"""
        project_file = Path(project.project_path) / PROJECT_FILE
        tmp_path = str(project_file) + '.tmp'
        try:
            with open(tmp_path, 'w') as f:
                json.dump(project.to_dict(), f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(project_file))
        except Exception as e:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            raise ProjectSaveError(f"Failed to save project file: {e}")

    def _save_commits(self, commits: List[Commit], project: Project):
        """Save commits.json (atomic write)"""
        commits_file = Path(project.project_path) / COMMITS_FILE
        commits_file.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "version": "1.0",
            "commits": [c.to_dict() for c in commits]
        }
        tmp_path = str(commits_file) + '.tmp'
        try:
            with open(tmp_path, 'w') as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(commits_file))
        except Exception as e:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            raise ProjectSaveError(f"Failed to save commits file: {e}")

    def _load_commits(self, project_dir: Path) -> List[Commit]:
        """Load commits from history file with backward compatibility"""
        commits_file = project_dir / COMMITS_FILE
        # Fallback for old projects that stored commits in history/
        if not commits_file.exists():
            legacy_file = project_dir / "history" / "commits.json"
            if legacy_file.exists():
                commits_file = legacy_file
            else:
                return []
        try:
            with open(commits_file, 'r') as f:
                data = json.load(f)

            commits = []
            for i, c in enumerate(data.get("commits", [])):
                # Pass fallback_version for backward compatibility
                # Old commits without version field get sequential numbers
                commit = Commit.from_dict(c, fallback_version=i)
                commits.append(commit)

            return commits
        except Exception as e:
            logger.warning(f"Failed to load commits: {e}")
            return []

    @staticmethod
    def is_project_folder(path: str) -> bool:
        """Check if a path is a valid project folder"""
        project_file = Path(path) / PROJECT_FILE
        return project_file.exists()
