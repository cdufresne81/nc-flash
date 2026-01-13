"""
Tests for the Test Script Runner

Tests command parsing, script parsing, cleanup logic, and initialization.
"""

import pytest
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
import tempfile
import os

# Import the TestRunner class
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from tools.test_runner import TestRunner


class TestCommandParsing:
    """Tests for command string parsing"""

    def test_parse_simple_command(self):
        """Parse a simple command without arguments"""
        runner = TestRunner(quiet=True)
        result = runner._parse_command("start")
        assert result == ["start"]

    def test_parse_command_with_args(self):
        """Parse command with space-separated arguments"""
        runner = TestRunner(quiet=True)
        result = runner._parse_command("select 1 2 3 4")
        assert result == ["select", "1", "2", "3", "4"]

    def test_parse_command_with_quoted_string(self):
        """Parse command with double-quoted argument"""
        runner = TestRunner(quiet=True)
        result = runner._parse_command('open_table "APP to TP Desired"')
        assert result == ["open_table", "APP to TP Desired"]

    def test_parse_command_with_single_quotes(self):
        """Parse command with single-quoted argument"""
        runner = TestRunner(quiet=True)
        result = runner._parse_command("open_table 'Some Table Name'")
        assert result == ["open_table", "Some Table Name"]

    def test_parse_command_with_mixed_quotes(self):
        """Parse command with quotes inside other quotes"""
        runner = TestRunner(quiet=True)
        result = runner._parse_command('''load_rom "path/to/rom's file.bin"''')
        assert result == ["load_rom", "path/to/rom's file.bin"]

    def test_parse_command_with_path(self):
        """Parse command with file path argument"""
        runner = TestRunner(quiet=True)
        result = runner._parse_command("load_rom /path/to/file.bin")
        assert result == ["load_rom", "/path/to/file.bin"]

    def test_parse_empty_command(self):
        """Parse empty command returns empty list"""
        runner = TestRunner(quiet=True)
        result = runner._parse_command("")
        assert result == []

    def test_parse_command_extra_whitespace(self):
        """Parse command with extra whitespace"""
        runner = TestRunner(quiet=True)
        result = runner._parse_command("  select   1   2  ")
        assert result == ["select", "1", "2"]


class TestScriptExecution:
    """Tests for script line processing"""

    def test_skip_empty_lines(self):
        """Script should skip empty lines"""
        runner = TestRunner(quiet=True)

        # Create a temp script file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("\n\n\n")
            script_path = f.name

        try:
            # Mock start_app to avoid Qt initialization
            with patch.object(runner, 'start_app', return_value=True):
                with patch.object(runner, '_execute_command', return_value=True) as mock_exec:
                    runner.run_script(script_path)
                    # Should not have executed any commands
                    mock_exec.assert_not_called()
        finally:
            os.unlink(script_path)

    def test_skip_comment_lines(self):
        """Script should skip comment lines starting with #"""
        runner = TestRunner(quiet=True)

        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("# This is a comment\n")
            f.write("# Another comment\n")
            script_path = f.name

        try:
            with patch.object(runner, 'start_app', return_value=True):
                with patch.object(runner, '_execute_command', return_value=True) as mock_exec:
                    runner.run_script(script_path)
                    mock_exec.assert_not_called()
        finally:
            os.unlink(script_path)

    def test_execute_valid_commands(self):
        """Script should execute non-empty, non-comment lines"""
        runner = TestRunner(quiet=True)

        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("# Comment\n")
            f.write("\n")
            f.write("command1\n")
            f.write("command2 arg1\n")
            script_path = f.name

        try:
            with patch.object(runner, 'start_app', return_value=True):
                with patch.object(runner, '_execute_command', return_value=True) as mock_exec:
                    runner.run_script(script_path)
                    assert mock_exec.call_count == 2
                    mock_exec.assert_any_call("command1")
                    mock_exec.assert_any_call("command2 arg1")
        finally:
            os.unlink(script_path)

    def test_script_not_found(self):
        """Should return False for non-existent script"""
        runner = TestRunner(quiet=True)
        result = runner.run_script("/nonexistent/script.txt")
        assert result is False


class TestScreenshotCleanup:
    """Tests for screenshot cleanup functionality"""

    def test_cleanup_all_auto_generated(self):
        """Cleanup should delete auto-generated screenshots"""
        with tempfile.TemporaryDirectory() as tmpdir:
            screenshots_dir = Path(tmpdir)

            # Create test files
            (screenshots_dir / "demo_01.png").touch()
            (screenshots_dir / "dm_test.png").touch()
            (screenshots_dir / "test_result.png").touch()
            (screenshots_dir / "screenshot_123.png").touch()
            (screenshots_dir / "keep_me.png").touch()  # Should not be deleted

            runner = TestRunner(quiet=True)
            runner.screenshots_dir = screenshots_dir

            deleted = runner.cleanup_screenshots()

            assert deleted == 4
            assert not (screenshots_dir / "demo_01.png").exists()
            assert not (screenshots_dir / "dm_test.png").exists()
            assert not (screenshots_dir / "test_result.png").exists()
            assert not (screenshots_dir / "screenshot_123.png").exists()
            assert (screenshots_dir / "keep_me.png").exists()

    def test_cleanup_with_pattern(self):
        """Cleanup should respect pattern filter"""
        with tempfile.TemporaryDirectory() as tmpdir:
            screenshots_dir = Path(tmpdir)

            # Create test files
            (screenshots_dir / "demo_01.png").touch()
            (screenshots_dir / "demo_02.png").touch()
            (screenshots_dir / "test_result.png").touch()

            runner = TestRunner(quiet=True)
            runner.screenshots_dir = screenshots_dir

            deleted = runner.cleanup_screenshots(pattern="demo_*")

            assert deleted == 2
            assert not (screenshots_dir / "demo_01.png").exists()
            assert not (screenshots_dir / "demo_02.png").exists()
            assert (screenshots_dir / "test_result.png").exists()

    def test_cleanup_with_age_filter(self):
        """Cleanup should respect age filter"""
        with tempfile.TemporaryDirectory() as tmpdir:
            screenshots_dir = Path(tmpdir)

            # Create test files
            old_file = screenshots_dir / "demo_old.png"
            new_file = screenshots_dir / "demo_new.png"

            old_file.touch()
            new_file.touch()

            # Make one file appear old (modify mtime)
            old_time = (datetime.now() - timedelta(hours=48)).timestamp()
            os.utime(old_file, (old_time, old_time))

            runner = TestRunner(quiet=True)
            runner.screenshots_dir = screenshots_dir

            # Only delete files older than 24 hours
            deleted = runner.cleanup_screenshots(pattern="demo_*", max_age_hours=24)

            assert deleted == 1
            assert not old_file.exists()
            assert new_file.exists()

    def test_cleanup_empty_directory(self):
        """Cleanup on empty directory should return 0"""
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = TestRunner(quiet=True)
            runner.screenshots_dir = Path(tmpdir)

            deleted = runner.cleanup_screenshots()
            assert deleted == 0


class TestListScreenshots:
    """Tests for listing screenshots"""

    def test_list_empty_directory(self):
        """List screenshots in empty directory"""
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = TestRunner(quiet=True)
            runner.screenshots_dir = Path(tmpdir)

            result = runner.list_screenshots()
            assert result == []

    def test_list_screenshots(self):
        """List screenshots returns filenames"""
        with tempfile.TemporaryDirectory() as tmpdir:
            screenshots_dir = Path(tmpdir)

            # Create test files
            (screenshots_dir / "screenshot1.png").touch()
            (screenshots_dir / "screenshot2.png").touch()
            (screenshots_dir / "not_a_screenshot.txt").touch()  # Should be ignored

            runner = TestRunner(quiet=True)
            runner.screenshots_dir = screenshots_dir

            result = runner.list_screenshots()

            assert len(result) == 2
            assert "screenshot1.png" in result
            assert "screenshot2.png" in result
            assert "not_a_screenshot.txt" not in result


class TestInitialization:
    """Tests for TestRunner initialization"""

    def test_default_metadata_dir(self):
        """Default metadata directory should be project's metadata/"""
        runner = TestRunner(quiet=True)
        assert runner.metadata_dir.name == "metadata"

    def test_custom_metadata_dir(self):
        """Custom metadata directory should be used"""
        runner = TestRunner(metadata_dir="/custom/path", quiet=True)
        assert str(runner.metadata_dir) == "/custom/path"

    def test_quiet_mode(self):
        """Quiet mode should suppress logging"""
        runner = TestRunner(quiet=True)
        assert runner.quiet is True

    def test_initial_state(self):
        """Initial state should have no loaded ROM or table"""
        runner = TestRunner(quiet=True)
        assert runner.app is None
        assert runner.main_window is None
        assert runner.current_table_window is None
        assert runner.rom_reader is None
        assert runner.rom_definition is None


class TestCommandExecution:
    """Tests for command execution logic (without Qt)"""

    def test_unknown_command(self):
        """Unknown command should return False"""
        runner = TestRunner(quiet=True)
        result = runner._execute_command("unknown_command")
        assert result is False

    def test_list_tables_no_rom(self):
        """list_tables without ROM should return empty list"""
        runner = TestRunner(quiet=True)
        result = runner.list_tables()
        assert result == []

    def test_execute_wait_command(self):
        """wait command should execute successfully"""
        runner = TestRunner(quiet=True)
        runner.app = MagicMock()  # Mock Qt app

        # Short wait should succeed
        result = runner._execute_command("wait 10")
        assert result is True

    def test_execute_cleanup_command(self):
        """cleanup command should execute successfully"""
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = TestRunner(quiet=True)
            runner.screenshots_dir = Path(tmpdir)

            result = runner._execute_command("cleanup")
            assert result is True

    def test_execute_list_screenshots_command(self):
        """list_screenshots command should execute successfully"""
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = TestRunner(quiet=True)
            runner.screenshots_dir = Path(tmpdir)

            result = runner._execute_command("list_screenshots")
            assert result is True
