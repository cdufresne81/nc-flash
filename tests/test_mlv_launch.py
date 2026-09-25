"""MegaLogViewerHD launch (#109): header grouping, launch args, offer gating.

``QProcess.startDetached`` and the modal prompt are patched — nothing is
actually launched and no dialog blocks the test.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.ui import mlv_launch


def _csv(path: Path, header: str, rows: int = 2) -> Path:
    path.write_text(header + "\n" + "1,2\n" * rows, encoding="utf-8")
    return path


@pytest.fixture
def launches(monkeypatch):
    calls = []

    def fake_start(program, args, workdir):
        calls.append((program, list(args), workdir))
        return (True, 1234)

    monkeypatch.setattr(mlv_launch.QProcess, "startDetached", fake_start)
    return calls


@pytest.fixture
def exe(tmp_path):
    path = tmp_path / "MLV" / "MegaLogViewerHD.exe"
    path.parent.mkdir()
    path.write_bytes(b"")
    return path


def test_same_header_logs_open_in_one_launch_in_given_order(tmp_path, exe, launches):
    old = _csv(tmp_path / "old.csv", "time,rpm")
    new = _csv(tmp_path / "new.csv", "time,rpm")
    assert mlv_launch.open_logs([old, new], exe) is True
    assert len(launches) == 1
    program, args, workdir = launches[0]
    assert program == str(exe)
    assert args == [str(old.resolve()), str(new.resolve())]  # plain paths
    assert workdir == str(exe.parent)


def test_different_headers_never_concatenate(tmp_path, exe, launches):
    # MLV maps later files by column POSITION: mixing headers would misalign
    # every channel, so each header set gets its own window.
    a = _csv(tmp_path / "a.csv", "time,rpm")
    b = _csv(tmp_path / "b.csv", "time,rpm,afr")
    c = _csv(tmp_path / "c.csv", "time,rpm")
    assert mlv_launch.open_logs([a, b, c], exe) is True
    assert [[Path(p).name for p in args] for _, args, _ in launches] == [
        ["a.csv", "c.csv"],
        ["b.csv"],
    ]


def test_unreadable_file_is_launched_alone(tmp_path):
    a = _csv(tmp_path / "a.csv", "time,rpm")
    missing = tmp_path / "gone.csv"
    groups = mlv_launch.group_by_header([a, missing])
    assert groups == [[a], [missing]]


def test_failed_launch_reports_false(tmp_path, exe, monkeypatch):
    monkeypatch.setattr(
        mlv_launch.QProcess, "startDetached", lambda *a: (False, 0)
    )  # PySide6 tuple form: a truthy tuple must not read as success
    assert mlv_launch.open_logs([_csv(tmp_path / "a.csv", "t")], exe) is False


def test_nothing_to_open_or_not_installed(tmp_path, launches, monkeypatch):
    monkeypatch.setattr(mlv_launch, "find_mlv", lambda: None)
    assert mlv_launch.open_logs([]) is False
    assert mlv_launch.open_logs([_csv(tmp_path / "a.csv", "t")]) is False
    assert launches == []


# --- offer_to_open ------------------------------------------------------------


def _settings(offer=True):
    s = MagicMock()
    s.get_offer_open_logs_in_mlv.return_value = offer
    return s


def test_offer_yes_opens(tmp_path, exe, launches, monkeypatch):
    monkeypatch.setattr(mlv_launch, "_ask", lambda parent, n: (True, False))
    log = _csv(tmp_path / "a.csv", "t")
    assert mlv_launch.offer_to_open(None, [log], _settings(), exe) is True
    assert len(launches) == 1


def test_offer_no_does_not_open(tmp_path, exe, launches, monkeypatch):
    monkeypatch.setattr(mlv_launch, "_ask", lambda parent, n: (False, False))
    settings = _settings()
    log = _csv(tmp_path / "a.csv", "t")
    assert mlv_launch.offer_to_open(None, [log], settings, exe) is False
    assert launches == []
    settings.set_offer_open_logs_in_mlv.assert_not_called()


def test_dont_ask_again_turns_the_setting_off(tmp_path, exe, launches, monkeypatch):
    monkeypatch.setattr(mlv_launch, "_ask", lambda parent, n: (False, True))
    settings = _settings()
    mlv_launch.offer_to_open(None, [_csv(tmp_path / "a.csv", "t")], settings, exe)
    settings.set_offer_open_logs_in_mlv.assert_called_once_with(False)


@pytest.mark.parametrize("case", ["nothing-downloaded", "offer-off", "no-mlv"])
def test_offer_is_silent_when_it_does_not_apply(tmp_path, monkeypatch, case):
    ask = MagicMock()
    monkeypatch.setattr(mlv_launch, "_ask", ask)
    monkeypatch.setattr(mlv_launch, "find_mlv", lambda: None)
    paths = [] if case == "nothing-downloaded" else [tmp_path / "a.csv"]
    settings = _settings(offer=case != "offer-off")
    assert mlv_launch.offer_to_open(None, paths, settings) is False
    ask.assert_not_called()  # no dialog at all
