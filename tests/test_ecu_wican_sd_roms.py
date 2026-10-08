"""Staged-ROM retention on the WiCAN SD card (#139).

:func:`trim_staged_roms` deletes files from the folder the flash path reads
from, so these pin what it may touch: only NC Flash's own staged pairs, never
the one just flashed, never folders / locked entries / foreign files, and a
failed delete stops the pass instead of raising.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.ecu.wican_http import WiCANHttpError
from src.ecu.wican_sd_roms import SD_ROMS_DIR, staged_group, trim_staged_roms


class _FakeSdFiles:
    """In-memory ``roms/`` folder with the file manager's list/delete surface."""

    def __init__(self, entries):
        self.entries = dict(entries)
        self.deletes = []
        self.fail = {}  # name -> list of exceptions to raise, in order

    def list_dir(self, rel_dir):
        assert rel_dir == SD_ROMS_DIR
        return {n: dict(e) for n, e in self.entries.items()}

    def delete_file(self, rel_dir, name):
        assert rel_dir == SD_ROMS_DIR
        self.deletes.append(name)
        queued = self.fail.get(name)
        if queued:
            raise queued.pop(0)
        del self.entries[name]


def _file(**extra):
    return {"type": "file", "size": 1054720, "mtime": 0, **extra}


def _pairs(*stamps, stem="ROM"):
    entries = {}
    for ts in stamps:
        entries[f"{stem}_{ts}.bin"] = _file()
        entries[f"{stem}_{ts}.json"] = _file(size=300)
    return entries


class TestStagedGroup:
    @pytest.mark.parametrize(
        "name,group",
        [
            ("LF9VEB_20261007_1200.bin", "LF9VEB_20261007_1200"),
            ("LF9VEB_20261007_1200.json", "LF9VEB_20261007_1200"),
            ("LF9VEB_20261007_1200.bin.part", "LF9VEB_20261007_1200"),
            ("AFR_12.5-tune_20261007_1200.json.part", "AFR_12.5-tune_20261007_1200"),
            ("x_20250101_0000_20261007_1200.bin", "x_20250101_0000_20261007_1200"),
            ("LF9VEB.bin", None),
            ("LF9VEB_20261007-1200.bin", None),
            ("_20261007_1200.bin", None),
            ("ROM_20261007_1200.txt", None),
            ("my rom_20261007_1200.bin", None),
            ("ROM_20261007_1200.bin.bak", None),
        ],
    )
    def test_only_names_nc_flash_stages_are_recognised(self, name, group):
        assert staged_group(name) == group


class TestTrim:
    def test_keeps_newest_pairs_and_deletes_older_ones_oldest_first(self):
        fs = _FakeSdFiles(
            _pairs("20261001_0900", "20261003_0900", "20261005_0900", "20261007_1200")
        )
        result = trim_staged_roms(fs, 2, protect="ROM_20261007_1200.bin")
        assert fs.deletes == [
            "ROM_20261001_0900.bin",
            "ROM_20261001_0900.json",
            "ROM_20261003_0900.bin",
            "ROM_20261003_0900.json",
        ]
        assert sorted(fs.entries) == sorted(_pairs("20261005_0900", "20261007_1200"))
        assert result.kept == ["ROM_20261007_1200", "ROM_20261005_0900"]
        assert result.failed is None

    def test_sorts_by_name_timestamp_across_different_rom_names(self):
        entries = {
            **_pairs("20261006_0800", stem="zzz"),
            **_pairs("20261002_0800", stem="aaa"),
            **_pairs("20261007_1200", stem="mid"),
        }
        fs = _FakeSdFiles(entries)
        trim_staged_roms(fs, 2, protect="mid_20261007_1200.bin")
        assert fs.deletes == ["aaa_20261002_0800.bin", "aaa_20261002_0800.json"]

    def test_device_mtime_is_ignored(self):
        # A clockless WiCAN stamps nonsense mtimes; only the name decides.
        fs = _FakeSdFiles(_pairs("20261001_0900", "20261007_1200"))
        fs.entries["ROM_20261001_0900.bin"]["mtime"] = 2_000_000_000
        trim_staged_roms(fs, 1, protect="ROM_20261007_1200.bin")
        assert "ROM_20261007_1200.bin" in fs.entries
        assert "ROM_20261001_0900.bin" not in fs.entries

    def test_just_flashed_pair_is_kept_even_when_older_names_sort_newer(self):
        # A PC clock once set ahead staged a "future" image; the pair just
        # flashed must survive anyway and count toward keep.
        fs = _FakeSdFiles(_pairs("20301231_2359", "20261007_1200", "20261001_0900"))
        result = trim_staged_roms(fs, 1, protect="ROM_20261007_1200.bin")
        assert "ROM_20261007_1200.bin" in fs.entries
        assert "ROM_20261007_1200.json" in fs.entries
        assert sorted(fs.deletes) == sorted(_pairs("20301231_2359", "20261001_0900"))
        assert result.kept == ["ROM_20261007_1200"]

    def test_protect_counts_even_when_not_listed(self):
        # Keep 2 = the just-flashed pair + the single newest other one.
        fs = _FakeSdFiles(_pairs("20261001_0900", "20261003_0900"))
        trim_staged_roms(fs, 2, protect="ROM_20261007_1200.bin")
        assert fs.deletes == ["ROM_20261001_0900.bin", "ROM_20261001_0900.json"]

    def test_nothing_to_do_under_the_limit(self):
        fs = _FakeSdFiles(_pairs("20261005_0900", "20261007_1200"))
        result = trim_staged_roms(fs, 5, protect="ROM_20261007_1200.bin")
        assert fs.deletes == [] and result.deleted == []

    def test_foreign_files_folders_and_locked_entries_are_never_touched(self):
        entries = {
            **_pairs("20261001_0900", "20261007_1200"),
            "notes.txt": _file(),
            "stock.bin": _file(),
            "my rom_20261001_0900.bin": _file(),
            "backup_20261001_0900.bin": {"type": "dir"},
            "locked_20261001_0900.bin": _file(locked=True),
            "active_20261001_0900.bin": _file(active=True),
            "typeless_20261001_0900.bin": {"size": 5},
        }
        fs = _FakeSdFiles(entries)
        trim_staged_roms(fs, 1, protect="ROM_20261007_1200.bin")
        assert fs.deletes == ["ROM_20261001_0900.bin", "ROM_20261001_0900.json"]

    def test_orphan_manifest_and_upload_leftovers_go_with_their_pair(self):
        fs = _FakeSdFiles(
            {
                **_pairs("20261007_1200"),
                "ROM_20261001_0900.json": _file(),
                "ROM_20261002_0900.bin.part": _file(),
                "ROM_20261002_0900.json": _file(),
            }
        )
        trim_staged_roms(fs, 1, protect="ROM_20261007_1200.bin")
        assert fs.deletes == [
            "ROM_20261001_0900.json",
            "ROM_20261002_0900.json",
            "ROM_20261002_0900.bin.part",
        ]
        assert sorted(fs.entries) == sorted(_pairs("20261007_1200"))

    def test_fence_409_is_retried_once_after_a_wait(self):
        fs = _FakeSdFiles(_pairs("20261001_0900", "20261007_1200"))
        fs.fail["ROM_20261001_0900.bin"] = [
            WiCANHttpError("POST http://x/files failed: HTTP 409 flashing")
        ]
        with patch("src.ecu.wican_sd_roms.time.sleep") as sleep:
            result = trim_staged_roms(fs, 1, protect="ROM_20261007_1200.bin")
        sleep.assert_called_once()
        assert result.deleted == ["ROM_20261001_0900.bin", "ROM_20261001_0900.json"]
        assert result.failed is None

    def test_second_409_stops_the_pass_without_raising(self):
        fs = _FakeSdFiles(_pairs("20261001_0900", "20261002_0900", "20261007_1200"))
        err = WiCANHttpError("POST http://x/files failed: HTTP 409 flashing")
        fs.fail["ROM_20261001_0900.bin"] = [err, err]
        with patch("src.ecu.wican_sd_roms.time.sleep"):
            result = trim_staged_roms(fs, 1, protect="ROM_20261007_1200.bin")
        assert result.deleted == []
        assert result.failed[0] == "ROM_20261001_0900.bin"
        assert fs.deletes == ["ROM_20261001_0900.bin", "ROM_20261001_0900.bin"]

    def test_other_failure_stops_the_pass_at_once(self):
        fs = _FakeSdFiles(_pairs("20261001_0900", "20261002_0900", "20261007_1200"))
        fs.fail["ROM_20261001_0900.json"] = [WiCANHttpError("timed out")]
        with patch("src.ecu.wican_sd_roms.time.sleep") as sleep:
            result = trim_staged_roms(fs, 1, protect="ROM_20261007_1200.bin")
        sleep.assert_not_called()
        assert result.deleted == ["ROM_20261001_0900.bin"]
        assert result.failed[0] == "ROM_20261001_0900.json"
        assert "ROM_20261002_0900.bin" in fs.entries

    @pytest.mark.parametrize("keep", [0, -1])
    def test_keep_below_one_is_rejected(self, keep):
        fs = _FakeSdFiles(_pairs("20261001_0900"))
        with pytest.raises(ValueError):
            trim_staged_roms(fs, keep, protect="ROM_20261007_1200.bin")
        assert fs.deletes == []

    def test_unrecognised_protect_name_deletes_nothing(self):
        fs = MagicMock()
        with pytest.raises(ValueError):
            trim_staged_roms(fs, 1, protect="whatever.bin")
        fs.list_dir.assert_not_called()
        fs.delete_file.assert_not_called()
