"""The Tag Release workflow's request check (tools/release_tag_check.py).

A wrong answer here tags the wrong branch: a stable tag on ``dev`` would offer
every unfinished experiment to every user.
"""

import subprocess

import pytest

from tools import release_tag_check as rtc


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _commit(repo, name, text):
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", name)


def _sha(repo, ref):
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", ref],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A clone whose origin has master (with a v2.23.1 section) and dev."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "master")
    _git(origin, "config", "user.email", "t@example.com")
    _git(origin, "config", "user.name", "t")
    _commit(origin, "CHANGELOG.md", "## [Unreleased]\n\n## [v2.23.1] - x\n")
    _git(origin, "tag", "v2.23.1")
    _git(origin, "checkout", "-b", "dev")
    _commit(origin, "feature.txt", "beta work\n")
    _git(origin, "checkout", "master")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(origin), str(clone))
    monkeypatch.chdir(clone)
    monkeypatch.setattr(
        rtc, "_ci_runs", lambda sha: [{"status": "completed", "conclusion": "success"}]
    )
    return clone


@pytest.mark.parametrize(
    "version, branch",
    [
        ("v2.24.0-beta.16", "dev"),
        ("v2.24.0-beta.1", "dev"),
        ("v2.24.0", "master"),
        ("v10.0.12", "master"),
    ],
)
def test_version_picks_the_branch(version, branch):
    assert rtc.target_branch(version) == branch


@pytest.mark.parametrize(
    "version",
    [
        "",
        "2.24.0",
        "v2.24",
        "v2.24.0-rc1",
        "v2.24.0-beta1",
        "v2.24.0-beta.",
        "v2.24.0-alpha.1",
        "v2.24.0-beta.1 ",
        "v2.24.0; rm -rf /",
        "v2.24.0\nv2.25.0",
        "v2.24.0\n",
    ],
)
def test_other_version_forms_are_refused(version):
    with pytest.raises(rtc.TagRefused):
        rtc.target_branch(version)


def test_beta_tags_the_head_of_dev(repo):
    assert rtc.check("v2.24.0-beta.1") == _sha(repo, "origin/dev")


def test_an_existing_tag_is_refused(repo):
    with pytest.raises(rtc.TagRefused, match="already exists"):
        rtc.check("v2.23.1")


def test_stable_needs_its_changelog_section(repo):
    with pytest.raises(rtc.TagRefused, match="CHANGELOG"):
        rtc.check("v2.24.0")


def test_stable_with_a_changelog_section_tags_master(repo, tmp_path):
    origin = tmp_path / "origin"
    _commit(origin, "CHANGELOG.md", "## [Unreleased]\n\n## [v2.24.0] - x\n")
    _git(repo, "fetch", "origin")
    sha = rtc.check("v2.24.0")
    assert sha == _sha(repo, "origin/master")
    assert sha != _sha(repo, "origin/dev")


def test_beta_of_a_released_version_is_refused(repo):
    with pytest.raises(rtc.TagRefused, match="already released"):
        rtc.check("v2.23.1-beta.4")


def test_commit_must_match_the_branch_head(repo):
    dev = _sha(repo, "origin/dev")
    assert rtc.check("v2.24.0-beta.1", dev) == dev
    assert rtc.check("v2.24.0-beta.1", dev[:8].upper()) == dev
    with pytest.raises(rtc.TagRefused, match="moved"):
        rtc.check("v2.24.0-beta.1", _sha(repo, "origin/master"))
    with pytest.raises(rtc.TagRefused, match="moved"):
        rtc.check("v2.24.0-beta.1", dev[:3])


@pytest.mark.parametrize(
    "runs",
    [
        [],
        [{"status": "in_progress", "conclusion": None}],
        [{"status": "completed", "conclusion": "failure"}],
        [{"status": "completed", "conclusion": "cancelled"}],
    ],
)
def test_ci_must_have_passed(repo, monkeypatch, runs):
    monkeypatch.setattr(rtc, "_ci_runs", lambda sha: runs)
    with pytest.raises(rtc.TagRefused, match="CI has not passed"):
        rtc.check("v2.24.0-beta.1")


def test_one_green_run_is_enough_after_a_rerun():
    assert rtc.ci_passed(
        [
            {"status": "completed", "conclusion": "failure"},
            {"status": "completed", "conclusion": "success"},
        ]
    )


def test_main_writes_the_commit_for_the_next_step(repo, monkeypatch, tmp_path, capsys):
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr("sys.argv", ["release_tag_check.py", "v2.24.0-beta.1"])
    assert rtc.main() == 0
    assert out.read_text(encoding="utf-8") == f"sha={_sha(repo, 'origin/dev')}\n"


def test_main_reports_a_refusal(repo, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["release_tag_check.py", "v2.24.0-rc1"])
    assert rtc.main() == 1
    assert "::error::" in capsys.readouterr().out
