"""MLV trail launcher: exe discovery, the properties-file contract, launch.

The properties file IS the integration contract with MegaLogViewerHD (its
documented automation hook): ``fileName`` with forward slashes (backslash is
the Java properties escape character), ``trailFile=true`` for Trail Live File
mode, ``startPlayback=true`` to play from the most recent sample. A regression
in any of these silently opens MLV in the wrong mode — or on the wrong file.
"""

from pathlib import Path

from src.ui import mlv_trail


class _FakeQProcess:
    """Stands in for QtCore.QProcess: records startDetached calls.

    Mirrors PySide6's real return shape — a ``(success, pid)`` tuple, not a
    bool. A truthiness regression in launch_trail (a failed ``(False, 0)`` is
    a truthy tuple) shipped precisely because an earlier fake returned a bool.
    """

    calls = []
    result = True

    @staticmethod
    def startDetached(program, arguments):
        _FakeQProcess.calls.append((program, list(arguments)))
        return (_FakeQProcess.result, 4242 if _FakeQProcess.result else 0)


class TestFindMlv:
    def test_not_installed_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mlv_trail, "_CANDIDATES", (tmp_path / "nope.exe",))
        assert mlv_trail.find_mlv() is None

    def test_first_existing_candidate_wins(self, monkeypatch, tmp_path):
        first = tmp_path / "a" / "MegaLogViewerHD.exe"
        second = tmp_path / "b" / "MegaLogViewerHD.exe"
        for exe in (first, second):
            exe.parent.mkdir()
            exe.touch()
        monkeypatch.setattr(mlv_trail, "_CANDIDATES", (first, second))
        assert mlv_trail.find_mlv() == first


class TestTrailProperties:
    def test_contract_keys_and_forward_slashes(self, tmp_path):
        csv = tmp_path / "live" / "live_20260711_130000.csv"
        csv.parent.mkdir()
        csv.write_text("time,rpm\n", encoding="utf-8")

        props = mlv_trail.write_trail_properties(csv)

        assert props == csv.with_suffix(".mlv.properties")
        lines = props.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "fileName=" + str(csv).replace("\\", "/")
        assert "trailFile=true" in lines
        assert "startPlayback=true" in lines
        # Backslashes are escape chars in Java .properties — none may survive.
        assert "\\" not in props.read_text(encoding="utf-8")


class TestLaunchTrail:
    def setup_method(self):
        _FakeQProcess.calls = []
        _FakeQProcess.result = True

    def test_launches_exe_with_properties_argument(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mlv_trail, "QProcess", _FakeQProcess)
        exe = tmp_path / "MegaLogViewerHD.exe"
        exe.touch()
        csv = tmp_path / "live_x.csv"
        csv.write_text("h\n", encoding="utf-8")

        assert mlv_trail.launch_trail(csv, exe) is True

        ((program, arguments),) = _FakeQProcess.calls
        assert program == str(exe)
        assert arguments == [str(csv.with_suffix(".mlv.properties"))]

    def test_not_installed_is_quiet_false(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mlv_trail, "QProcess", _FakeQProcess)
        monkeypatch.setattr(mlv_trail, "_CANDIDATES", (tmp_path / "nope.exe",))
        csv = tmp_path / "live_x.csv"
        csv.write_text("h\n", encoding="utf-8")

        assert mlv_trail.launch_trail(csv) is False
        assert _FakeQProcess.calls == []  # nothing launched…
        assert not csv.with_suffix(".mlv.properties").exists()  # …nothing written

    def test_failed_detach_returns_false(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mlv_trail, "QProcess", _FakeQProcess)
        _FakeQProcess.result = False
        exe = tmp_path / "MegaLogViewerHD.exe"
        exe.touch()
        csv = tmp_path / "live_x.csv"
        csv.write_text("h\n", encoding="utf-8")

        assert mlv_trail.launch_trail(csv, exe) is False
