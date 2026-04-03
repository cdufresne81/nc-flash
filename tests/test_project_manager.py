"""
Tests for ProjectManager: tuning log, soft delete, revert, commit flow.
"""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from src.core.project_manager import ProjectManager
from src.core.version_models import (
    Project,
    OriginalRomInfo,
    Commit,
    TableChanges,
    CellChange,
)
from src.core.exceptions import ProjectError, ProjectSaveError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def rom_definition():
    """Mock ROM definition with romid attributes"""
    defn = MagicMock()
    defn.romid.internalidstring = "LF9VEB"
    defn.romid.xmlid = "LF9VEB_XML"
    defn.romid.make = "Subaru"
    defn.romid.model = "WRX"
    return defn


@pytest.fixture
def source_rom(tmp_path):
    """Create a fake source ROM file"""
    rom_file = tmp_path / "source" / "stock.bin"
    rom_file.parent.mkdir()
    rom_file.write_bytes(b"\x00" * 1024)
    return rom_file


@pytest.fixture
def project_dir(tmp_path):
    """Return a clean project directory path"""
    return tmp_path / "my_tune"


@pytest.fixture
def pm(project_dir, source_rom, rom_definition):
    """Create a ProjectManager with a fresh project"""
    mgr = ProjectManager()
    mgr.create_project(
        project_path=str(project_dir),
        project_name="Test Tune",
        source_rom_path=str(source_rom),
        rom_definition=rom_definition,
    )
    return mgr


def _make_changes(table_name="Fuel Map", n_cells=3):
    """Create a list of TableChanges for testing"""
    cells = [
        CellChange(
            table_name=table_name,
            table_address="0x1000",
            row=i,
            col=0,
            old_value=10.0 + i,
            new_value=12.0 + i,
            old_raw=10.0 + i,
            new_raw=12.0 + i,
        )
        for i in range(n_cells)
    ]
    return [
        TableChanges(table_name=table_name, table_address="0x1000", cell_changes=cells)
    ]


# ===========================================================================
# create_project
# ===========================================================================


class TestCreateProject:
    def test_working_rom_naming(self, pm, project_dir):
        """Working ROM should be {ROMID}.bin, not v1_*_working.bin"""
        assert pm.current_project.working_rom == "LF9VEB.bin"
        assert (project_dir / "LF9VEB.bin").exists()

    def test_v0_original_created(self, pm, project_dir):
        """v0 original ROM should exist"""
        assert (project_dir / "v0_LF9VEB_original.bin").exists()

    def test_tuning_log_created(self, pm, project_dir):
        """TUNING_LOG.md should be created with header"""
        log_path = project_dir / "TUNING_LOG.md"
        assert log_path.exists()
        content = log_path.read_text()
        assert "# Tuning Log" in content
        assert "LF9VEB" in content
        assert "Subaru" in content
        assert "WRX" in content

    def test_project_json_no_last_suffix(self, pm, project_dir):
        """project.json should not have last_suffix or settings fields"""
        data = json.loads((project_dir / "project.json").read_text())
        assert "last_suffix" not in data
        assert "settings" not in data

    def test_initial_commit_is_v0(self, pm):
        """Initial commit should be version 0"""
        assert len(pm.commits) == 1
        assert pm.commits[0].version == 0
        assert pm.commits[0].snapshot_filename == "v0_LF9VEB_original.bin"


# ===========================================================================
# commit_changes
# ===========================================================================


class TestCommitChanges:
    def test_commit_creates_snapshot(self, pm, project_dir):
        """Commit should always create a snapshot file"""
        changes = _make_changes()
        commit = pm.commit_changes(
            message="Test commit",
            changes=changes,
            version_name="test_v1",
        )
        assert commit.version == 1
        assert commit.snapshot_filename == "v1_LF9VEB_test_v1.bin"
        assert (project_dir / "v1_LF9VEB_test_v1.bin").exists()

    def test_commit_appends_tuning_log(self, pm, project_dir):
        """Commit should append to TUNING_LOG.md"""
        changes = _make_changes()
        pm.commit_changes(
            message="Added fuel enrichment",
            changes=changes,
            version_name="fuel_tune",
        )
        log = (project_dir / "TUNING_LOG.md").read_text(encoding="utf-8")
        assert "## v1" in log
        assert "fuel_tune" in log
        assert "Added fuel enrichment" in log
        assert "Fuel Map" in log

    def test_tuning_log_direction_increase(self, pm, project_dir):
        """Log should show increase direction for all-increasing changes"""
        cells = [
            CellChange(
                table_name="Timing",
                table_address="0x2000",
                row=0,
                col=0,
                old_value=10.0,
                new_value=15.0,
                old_raw=10.0,
                new_raw=15.0,
            ),
        ]
        changes = [
            TableChanges(
                table_name="Timing", table_address="0x2000", cell_changes=cells
            )
        ]
        pm.commit_changes(message="More timing", changes=changes, version_name="timing")
        log = (project_dir / "TUNING_LOG.md").read_text(encoding="utf-8")
        assert "\u2191" in log  # up arrow

    def test_tuning_log_based_on_previous(self, pm, project_dir):
        """Second commit's log should reference first commit's snapshot"""
        pm.commit_changes(message="v1", changes=_make_changes(), version_name="first")
        pm.commit_changes(message="v2", changes=_make_changes(), version_name="second")
        log = (project_dir / "TUNING_LOG.md").read_text(encoding="utf-8")
        assert "v1_LF9VEB_first.bin" in log

    def test_sequential_versions(self, pm):
        """Multiple commits should increment version numbers"""
        pm.commit_changes(message="v1", changes=_make_changes(), version_name="a")
        pm.commit_changes(message="v2", changes=_make_changes(), version_name="b")
        pm.commit_changes(message="v3", changes=_make_changes(), version_name="c")

        assert pm.commits[-1].version == 3
        assert pm.commits[-2].version == 2
        assert pm.commits[-3].version == 1

    def test_commit_requires_project(self):
        """Commit should fail if no project is open"""
        mgr = ProjectManager()
        with pytest.raises(ProjectError):
            mgr.commit_changes(message="x", changes=[], version_name="y")

    def test_has_snapshot_always_true(self, pm):
        """Every commit should have has_snapshot=True"""
        commit = pm.commit_changes(
            message="test", changes=_make_changes(), version_name="snap"
        )
        assert commit.has_snapshot is True


# ===========================================================================
# soft_delete_version
# ===========================================================================


class TestSoftDelete:
    def test_soft_delete_moves_to_trash(self, pm, project_dir):
        """Soft delete should move snapshot to _trash/"""
        pm.commit_changes(
            message="v1", changes=_make_changes(), version_name="deleteme"
        )
        pm.soft_delete_version(1)

        assert not (project_dir / "v1_LF9VEB_deleteme.bin").exists()
        assert (project_dir / "_trash" / "v1_LF9VEB_deleteme.bin").exists()

    def test_soft_delete_marks_commit(self, pm):
        """Soft delete should set deleted=True on the commit"""
        pm.commit_changes(message="v1", changes=_make_changes(), version_name="d")
        pm.soft_delete_version(1)

        commit = pm.get_commit_by_version(1)
        assert commit.deleted is True

    def test_soft_delete_persists_to_json(self, pm, project_dir):
        """Deleted flag should be saved to commits.json"""
        pm.commit_changes(message="v1", changes=_make_changes(), version_name="d")
        pm.soft_delete_version(1)

        data = json.loads((project_dir / "commits.json").read_text())
        v1_commit = [c for c in data["commits"] if c.get("version") == 1][0]
        assert v1_commit["deleted"] is True

    def test_cannot_delete_v0(self, pm):
        """Cannot delete the original ROM (v0)"""
        with pytest.raises(ProjectError, match="Cannot delete"):
            pm.soft_delete_version(0)

    def test_cannot_double_delete(self, pm):
        """Cannot delete an already-deleted version"""
        pm.commit_changes(message="v1", changes=_make_changes(), version_name="d")
        pm.soft_delete_version(1)
        with pytest.raises(ProjectError, match="already deleted"):
            pm.soft_delete_version(1)

    def test_delete_nonexistent_version(self, pm):
        """Deleting a nonexistent version should raise"""
        with pytest.raises(ProjectError, match="not found"):
            pm.soft_delete_version(99)


# ===========================================================================
# revert_to_version
# ===========================================================================


class TestRevert:
    def test_revert_overwrites_working_rom(self, pm, project_dir):
        """Revert should overwrite the working ROM with snapshot data"""
        # Modify working ROM to make it different
        working = project_dir / "LF9VEB.bin"
        original_data = working.read_bytes()

        pm.commit_changes(message="v1", changes=_make_changes(), version_name="base")

        # Corrupt the working file to simulate edits
        working.write_bytes(b"\xff" * len(original_data))

        pm.revert_to_version(1)

        # Working ROM should now match the v1 snapshot
        assert working.read_bytes() == original_data

    def test_revert_deletes_newer_versions(self, pm, project_dir):
        """Revert should soft-delete versions newer than target"""
        pm.commit_changes(message="v1", changes=_make_changes(), version_name="a")
        pm.commit_changes(message="v2", changes=_make_changes(), version_name="b")
        pm.commit_changes(message="v3", changes=_make_changes(), version_name="c")

        pm.revert_to_version(1)

        assert pm.get_commit_by_version(1).deleted is False
        assert pm.get_commit_by_version(2).deleted is True
        assert pm.get_commit_by_version(3).deleted is True

    def test_revert_appends_to_log(self, pm, project_dir):
        """Revert should append an entry to TUNING_LOG.md"""
        pm.commit_changes(message="v1", changes=_make_changes(), version_name="base")
        pm.revert_to_version(1)

        log = (project_dir / "TUNING_LOG.md").read_text(encoding="utf-8")
        assert "Reverted to v1" in log

    def test_revert_to_v0(self, pm, project_dir, source_rom):
        """Revert to v0 should restore original ROM"""
        pm.commit_changes(message="v1", changes=_make_changes(), version_name="a")

        # Modify working file
        working = project_dir / "LF9VEB.bin"
        working.write_bytes(b"\xff" * 1024)

        pm.revert_to_version(0)

        assert working.read_bytes() == source_rom.read_bytes()

    def test_version_numbers_stay_monotonic(self, pm):
        """After revert, next commit version should be max+1"""
        pm.commit_changes(message="v1", changes=_make_changes(), version_name="a")
        pm.commit_changes(message="v2", changes=_make_changes(), version_name="b")
        pm.commit_changes(message="v3", changes=_make_changes(), version_name="c")

        pm.revert_to_version(1)

        # Next version should be 4 (max=3, +1)
        assert pm.get_next_version() == 4

    def test_revert_nonexistent_version(self, pm):
        """Revert to nonexistent version should raise"""
        with pytest.raises(ProjectError, match="not found"):
            pm.revert_to_version(99)

    def test_revert_deleted_version(self, pm):
        """Revert to deleted version should raise"""
        pm.commit_changes(message="v1", changes=_make_changes(), version_name="d")
        pm.soft_delete_version(1)
        with pytest.raises(ProjectError, match="deleted"):
            pm.revert_to_version(1)


# ===========================================================================
# Commit model — deleted field
# ===========================================================================


class TestCommitDeletedField:
    def test_deleted_default_false(self):
        """Commit.deleted should default to False"""
        commit = Commit.create(message="test", changes=[], version=1)
        assert commit.deleted is False

    def test_deleted_serialization(self):
        """deleted field should serialize/deserialize"""
        commit = Commit.create(message="test", changes=[], version=1)
        commit.deleted = True
        data = commit.to_dict()
        assert data["deleted"] is True

        restored = Commit.from_dict(data)
        assert restored.deleted is True

    def test_backward_compat_missing_deleted(self):
        """Old commits without deleted field should default to False"""
        data = {
            "id": "abc",
            "version": 1,
            "parent_id": None,
            "message": "test",
            "timestamp": "2026-01-01T00:00:00",
            "author": "User",
            "tables_modified": [],
            "changes": [],
        }
        commit = Commit.from_dict(data)
        assert commit.deleted is False


# ===========================================================================
# Project model — removed fields
# ===========================================================================


class TestProjectModelCleanup:
    def test_project_no_last_suffix(self):
        """Project should not have last_suffix attribute"""
        assert (
            not hasattr(Project, "last_suffix")
            or "last_suffix" not in Project.__dataclass_fields__
        )

    def test_project_no_settings(self):
        """Project should not have settings attribute"""
        assert (
            not hasattr(Project, "settings")
            or "settings" not in Project.__dataclass_fields__
        )

    def test_backward_compat_old_project_json(self, tmp_path):
        """Opening an old project.json with last_suffix/settings should still work"""
        data = {
            "version": "1.0",
            "name": "Old Project",
            "description": "",
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
            "original_rom": {
                "filename": "v0_LF9VEB_original.bin",
                "size": 1024,
                "checksum_sha256": "abc123",
                "rom_id": "LF9VEB",
                "definition_xmlid": "LF9VEB_XML",
                "make": "Subaru",
                "model": "WRX",
            },
            "working_rom": "v1_LF9VEB_working.bin",
            "head_commit_id": None,
            "head_version": 0,
            "last_suffix": "original",
            "settings": {"auto_snapshot_interval": 10},
        }
        # from_dict should just ignore unknown fields (via .get() defaults)
        project = Project.from_dict(data, str(tmp_path))
        assert project.name == "Old Project"
        assert project.working_rom == "v1_LF9VEB_working.bin"


# ===========================================================================
# Backward compatibility — old project open
# ===========================================================================


class TestBackwardCompat:
    def test_old_working_rom_pattern(self, tmp_path, rom_definition):
        """Old projects with v1_*_working.bin should still open"""
        proj_dir = tmp_path / "old_project"
        proj_dir.mkdir()

        # Create old-style project files
        rom_data = b"\x00" * 1024

        (proj_dir / "v0_LF9VEB_original.bin").write_bytes(rom_data)
        (proj_dir / "v1_LF9VEB_working.bin").write_bytes(rom_data)

        project_data = {
            "version": "1.0",
            "name": "Old Project",
            "description": "",
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
            "original_rom": {
                "filename": "v0_LF9VEB_original.bin",
                "size": 1024,
                "checksum_sha256": "abc",
                "rom_id": "LF9VEB",
                "definition_xmlid": "LF9VEB_XML",
                "make": "Subaru",
                "model": "WRX",
            },
            "working_rom": "v1_LF9VEB_working.bin",
            "head_commit_id": None,
            "head_version": 0,
            "last_suffix": "original",
            "settings": {},
        }
        (proj_dir / "project.json").write_text(json.dumps(project_data))

        commits_data = {"version": "1.0", "commits": []}
        (proj_dir / "commits.json").write_text(json.dumps(commits_data))

        mgr = ProjectManager()
        project = mgr.open_project(str(proj_dir))
        assert project.working_rom == "v1_LF9VEB_working.bin"
        assert Path(project.working_rom_path).exists()


# ===========================================================================
# CommitDialog sanitization
# ===========================================================================


class TestCommitDialogSanitization:
    def test_sanitize_basic(self):
        from src.ui.commit_dialog import CommitDialog

        assert CommitDialog._sanitize_name("EGR Delete") == "egr_delete"

    def test_sanitize_special_chars(self):
        from src.ui.commit_dialog import CommitDialog

        assert CommitDialog._sanitize_name("stage-1 (WOT)") == "stage1_wot"

    def test_sanitize_already_clean(self):
        from src.ui.commit_dialog import CommitDialog

        assert CommitDialog._sanitize_name("fuel_tune") == "fuel_tune"

    def test_sanitize_empty(self):
        from src.ui.commit_dialog import CommitDialog

        assert CommitDialog._sanitize_name("   ") == ""

    def test_sanitize_numbers(self):
        from src.ui.commit_dialog import CommitDialog

        assert CommitDialog._sanitize_name("v2 timing") == "v2_timing"


# ===========================================================================
# Atomic writes (#61)
# ===========================================================================


class TestAtomicWrites:
    def test_snapshot_size_matches_source(self, pm, project_dir):
        """Snapshot file should have same size as working ROM after commit."""
        changes = _make_changes()
        commit = pm.commit_changes(message="test", changes=changes, version_name="a")
        snapshot_path = project_dir / commit.snapshot_filename
        working_path = project_dir / pm.current_project.working_rom
        assert snapshot_path.stat().st_size == working_path.stat().st_size

    def test_revert_restores_snapshot_content(self, pm, project_dir):
        """After revert, working ROM should match the target snapshot."""
        changes = _make_changes()
        commit_v1 = pm.commit_changes(message="v1", changes=changes, version_name="a")
        # Modify the working ROM
        working_path = project_dir / pm.current_project.working_rom
        working_path.write_bytes(b"\xFF" * 1024)
        # Commit v2 with modified data
        commit_v2 = pm.commit_changes(message="v2", changes=changes, version_name="b")
        # Read v1 snapshot content
        v1_snapshot = (project_dir / commit_v1.snapshot_filename).read_bytes()
        # Revert to v1
        pm.revert_to_version(commit_v1.version)
        # Working ROM should now match v1 snapshot
        assert working_path.read_bytes() == v1_snapshot

    def test_atomic_copy_cleans_up_on_failure(self, pm, project_dir, monkeypatch):
        """If os.replace fails, .tmp file should be cleaned up."""
        import os

        original_replace = os.replace

        def fail_replace(src, dst):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(os, "replace", fail_replace)
        source = project_dir / pm.current_project.working_rom
        dest = project_dir / "test_copy.bin"
        with pytest.raises(ProjectSaveError):
            pm._atomic_copy(source, dest)
        # No .tmp file should remain
        assert not (project_dir / "test_copy.bin.tmp").exists()
