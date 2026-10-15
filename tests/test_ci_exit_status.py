"""The status file CI reads to tell a failed run from a crash after a passing one (#149).

CI treats a non-zero process exit as a pass only when this file says 0, so a
wrong value here would hide real test failures.
"""

import os
from types import SimpleNamespace

import pytest

from tests import conftest


def _unconfigure(monkeypatch, tmp_path, exitstatus):
    status_file = tmp_path / "status.txt"
    monkeypatch.setenv("NCFLASH_PYTEST_STATUS_FILE", str(status_file))
    # never end this test process: no CI hard exit, and fail loudly if reached
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(os, "_exit", lambda code: pytest.fail(f"os._exit({code})"))
    config = SimpleNamespace()
    conftest.pytest_sessionfinish(SimpleNamespace(config=config), exitstatus=0)
    config._ncflash_session.exitstatus = exitstatus
    conftest.pytest_unconfigure(config)
    return status_file


def test_writes_zero_for_a_passing_run(monkeypatch, tmp_path):
    status_file = _unconfigure(monkeypatch, tmp_path, pytest.ExitCode.OK)
    assert status_file.read_bytes() == b"0"


@pytest.mark.parametrize(
    "exitstatus",
    [pytest.ExitCode.TESTS_FAILED, pytest.ExitCode.INTERNAL_ERROR, 5],
)
def test_writes_the_failing_status(monkeypatch, tmp_path, exitstatus):
    status_file = _unconfigure(monkeypatch, tmp_path, exitstatus)
    assert status_file.read_bytes() == str(int(exitstatus)).encode()


def test_uses_the_final_status_not_sessionfinish_argument(monkeypatch, tmp_path):
    # a plugin can still change session.exitstatus after sessionfinish's argument
    # was taken; pytest returns the final value, so the file must hold it too
    status_file = _unconfigure(monkeypatch, tmp_path, pytest.ExitCode.TESTS_FAILED)
    assert status_file.read_bytes() == b"1"


def test_no_file_without_the_env_var(monkeypatch, tmp_path):
    monkeypatch.delenv("NCFLASH_PYTEST_STATUS_FILE", raising=False)
    monkeypatch.delenv("CI", raising=False)
    config = SimpleNamespace()
    session = SimpleNamespace(config=config, exitstatus=pytest.ExitCode.OK)
    conftest.pytest_sessionfinish(session, exitstatus=0)
    conftest.pytest_unconfigure(config)
    assert list(tmp_path.iterdir()) == []


def test_no_file_when_the_session_never_finished(monkeypatch, tmp_path):
    # e.g. a usage error: pytest_unconfigure runs without pytest_sessionfinish
    status_file = tmp_path / "status.txt"
    monkeypatch.setenv("NCFLASH_PYTEST_STATUS_FILE", str(status_file))
    conftest.pytest_unconfigure(SimpleNamespace())
    assert not status_file.exists()
