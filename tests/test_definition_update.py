"""Delivering new and corrected bundled definitions to existing installs (#121).

Each test is one rule of src/core/definition_update.py: what happens to the
user's copy of a definition when NC Flash ships a newer one.
"""

import os
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from lxml import etree

from src.core import definition_update as du
from src.core.definition_parser import load_definition

ROMID = (
    "<romid><xmlid>{rid}</xmlid><internalidaddress>b8046</internalidaddress>"
    "<internalidstring>{rid}</internalidstring><ecuid>{rid}</ecuid>"
    "<make>Mazda</make><model>MX5</model><flashmethod>Romdrop</flashmethod>"
    "<memmodel>SH7058</memmodel><checksummodule>21053000</checksummodule></romid>"
)


def scaling(name, mn="0", mx="100", storage="float", fmt="%0.2f"):
    return (
        f'<scaling name="{name}" units="" toexpr="x" frexpr="x" format="{fmt}" '
        f'min="{mn}" max="{mx}" inc="1" storagetype="{storage}" endian="big"/>'
    )


def table(name, address, sc="s1", elements="1", category="Cat", axes=""):
    kind = "2D" if axes else "1D"
    body = f">{axes}</table>" if axes else "/>"
    return (
        f'<table level="1" type="{kind}" category="{category}" swapxy="true" '
        f'name="{name}" address="{address}" elements="{elements}" scaling="{sc}"{body}'
    )


def axis(name, address, elements="4", sc="s1"):
    return (
        f'<table name="{name}" address="{address}" elements="{elements}" '
        f'scaling="{sc}" type="Y Axis"/>'
    )


def doc(*items, rid="TEST01"):
    inner = "\n    ".join(items)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<roms>\n  <rom>\n    '
        + ROMID.format(rid=rid)
        + "\n    "
        + inner
        + "\n  </rom>\n</roms>\n"
    ).encode("utf-8")


def tables_of(data):
    """address -> list of tables there."""
    rom = etree.fromstring(data).find("rom")
    out = {}
    for t in rom:
        if isinstance(t.tag, str) and t.tag == "table":
            out.setdefault(t.get("address"), []).append(t)
    return out


def scalings_of(data):
    rom = etree.fromstring(data).find("rom")
    return {s.get("name"): s for s in rom.findall("scaling")}


def release_hashes(*versions):
    return {du.file_hash(v) for v in versions}


@pytest.fixture
def ws(tmp_path):
    """A workspace with a bundled dir, a metadata dir and a run helper."""
    bundled = tmp_path / "bundled"
    metadata = tmp_path / "workspace" / "metadata"
    bundled.mkdir()
    metadata.mkdir(parents=True)

    class WS:
        root = tmp_path / "workspace"
        meta = metadata

        def ship(self, name, data):
            (bundled / name).write_bytes(data)

        def user(self, name, data=None):
            if data is not None:
                (metadata / name).write_bytes(data)
            return (metadata / name).read_bytes()

        def base(self, name, data):
            p = self.root / du.BASE_DIR
            p.mkdir(parents=True, exist_ok=True)
            (p / name).write_bytes(data)

        def run(self, history=None, now=datetime(2026, 10, 8, 12, 0, 0)):
            return du.update_definitions(bundled, metadata, self.root, history, now)

        def outcome(self, report, name):
            return next(f for f in report.files if f.name == name)

    return WS()


V1 = doc(scaling("s1"), table("Old Name", "c000"), table("Gone", "c010"))
V2 = doc(scaling("s1"), table("New Name", "c000"), table("Added", "c020"))


# ---------------------------------------------------------------- files


def test_missing_file_is_added_and_remembered(ws):
    ws.ship("test01.xml", V2)
    report = ws.run()
    assert ws.outcome(report, "test01.xml").action == "added"
    assert ws.user("test01.xml") == V2
    assert (ws.root / du.BASE_DIR / "test01.xml").read_bytes() == V2


def test_file_the_user_deleted_is_not_brought_back(ws):
    ws.ship("test01.xml", V2)
    ws.base("test01.xml", V1)
    report = ws.run()
    assert ws.outcome(report, "test01.xml").action == "kept-deleted"
    assert not (ws.meta / "test01.xml").exists()


def test_rom_defined_in_users_own_file_gets_no_second_definition(ws):
    ws.ship("test01.xml", V2)
    # Another xmlid, same internal ID string and address: ROM detection matches it
    own = V1.replace(b"<xmlid>TEST01</xmlid>", b"<xmlid>MY_TUNE</xmlid>")
    ws.user("my_tune.xml", own)
    report = ws.run()
    out = ws.outcome(report, "test01.xml")
    assert out.action == "skipped" and "my_tune.xml" in out.details[0]
    assert not (ws.meta / "test01.xml").exists()


def test_copy_that_identifies_another_rom_is_left_alone(ws):
    ws.ship("test01.xml", V2)
    ws.base("test01.xml", V1)
    other = V1.replace(b"<internalidstring>TEST01", b"<internalidstring>TEST99")
    ws.user("test01.xml", other)
    report = ws.run()
    assert ws.outcome(report, "test01.xml").action == "skipped"
    assert ws.user("test01.xml") == other


def test_unreadable_user_file_is_left_alone(ws):
    ws.ship("test01.xml", V2)
    ws.user("test01.xml", b"<roms><rom>broken")
    report = ws.run()
    assert ws.outcome(report, "test01.xml").action == "skipped"
    assert ws.user("test01.xml") == b"<roms><rom>broken"


def test_identical_content_in_another_layout_is_unchanged(ws):
    ws.ship("test01.xml", V2)
    one_line = etree.tostring(
        etree.fromstring(V2, etree.XMLParser(remove_blank_text=True)),
        xml_declaration=True,
        encoding="UTF-8",
    )
    ws.user("test01.xml", one_line)
    report = ws.run()
    assert ws.outcome(report, "test01.xml").action == "unchanged"
    assert ws.user("test01.xml") == one_line
    assert report.backup_dir is None


# ---------------------------------------------------------------- no base copy


def test_released_version_is_replaced_with_backup(ws):
    ws.ship("test01.xml", V2)
    ws.user("test01.xml", V1)  # no base copy: an install from an older version
    report = ws.run({"test01.xml": release_hashes(V1)})
    assert ws.outcome(report, "test01.xml").action == "replaced"
    assert ws.user("test01.xml") == V2
    assert (report.backup_dir / "test01.xml").read_bytes() == V1


def test_edited_file_without_base_is_left_alone(ws):
    ws.ship("test01.xml", V2)
    edited = V1.replace(b'min="0"', b'min="-5"')
    ws.user("test01.xml", edited)
    report = ws.run({"test01.xml": release_hashes(V1)})
    out = ws.outcome(report, "test01.xml")
    assert out.action == "left-alone"
    assert ws.user("test01.xml") == edited
    # No base written: a later release still sees it as unknown, not as "edits"
    assert not (ws.root / du.BASE_DIR / "test01.xml").exists()
    assert "were not updated" in report.summary_text()


def test_released_version_of_another_file_does_not_count(ws):
    # History is per file: a copy matching another file's release is unknown
    ws.ship("test01.xml", V2)
    ws.user("test01.xml", V1)
    report = ws.run({"other.xml": release_hashes(V1)})
    assert ws.outcome(report, "test01.xml").action == "left-alone"


# ---------------------------------------------------------------- three-way


def test_display_edit_is_kept_and_bundled_changes_still_arrive(ws):
    ws.ship("test01.xml", V2)
    ws.base("test01.xml", V1)
    ws.user("test01.xml", V1.replace(b'min="0"', b'min="-5"'))
    report = ws.run()
    out = ws.outcome(report, "test01.xml")
    assert out.action == "merged"
    data = ws.user("test01.xml")
    assert scalings_of(data)["s1"].get("min") == "-5"
    tables = tables_of(data)
    assert tables["c000"][0].get("name") == "New Name"  # rename delivered
    assert "c020" in tables  # new table delivered
    assert "c010" not in tables  # table no longer shipped: removed


def test_users_layout_fix_survives_when_bundled_did_not_change_it(ws):
    # The user fixed KR to one byte; the bundled file changed something else
    base = doc(scaling("kr", storage="float"), table("KR", "c000", sc="kr"))
    bundled = doc(
        scaling("kr", storage="float"),
        table("KR", "c000", sc="kr"),
        table("New", "c010", sc="kr"),
    )
    ws.ship("test01.xml", bundled)
    ws.base("test01.xml", base)
    ws.user("test01.xml", base.replace(b'storagetype="float"', b'storagetype="uint8"'))
    report = ws.run()
    assert scalings_of(ws.user("test01.xml"))["kr"].get("storagetype") == "uint8"
    assert not any("corrected" in d for d in ws.outcome(report, "test01.xml").details)


def test_layout_conflict_takes_the_bundled_correction_and_reports_it(ws):
    base = doc(
        scaling("kr", storage="float", fmt="%0.2f"), table("KR", "c000", sc="kr")
    )
    bundled = doc(
        scaling("kr", storage="uint8", fmt="%d"), table("KR", "c000", sc="kr")
    )
    user = doc(
        scaling("kr", storage="uint16", fmt="%0.1f"), table("KR", "c000", sc="kr")
    )
    ws.ship("test01.xml", bundled)
    ws.base("test01.xml", base)
    ws.user("test01.xml", user)
    report = ws.run()
    kr = scalings_of(ws.user("test01.xml"))["kr"]
    assert kr.get("storagetype") == "uint8"  # bundled correction
    assert kr.get("format") == "%0.1f"  # display: user's
    details = ws.outcome(report, "test01.xml").details
    assert (
        "NC Flash's corrected value replaced your edit: scaling kr: storagetype"
        in details
    )


def test_three_way_per_attribute(ws):
    base = doc(scaling("s1", mn="0", mx="100"), table("T", "c000"))
    bundled = doc(scaling("s1", mn="0", mx="200"), table("T", "c000"))
    user = doc(scaling("s1", mn="-5", mx="100"), table("T", "c000"))
    ws.ship("test01.xml", bundled)
    ws.base("test01.xml", base)
    ws.user("test01.xml", user)
    ws.run()
    s1 = scalings_of(ws.user("test01.xml"))["s1"]
    assert (s1.get("min"), s1.get("max")) == ("-5", "200")


def test_axes(ws):
    base = doc(
        scaling("s1"), table("T", "c000", elements="4", axes=axis("rpm", "c100"))
    )
    ws.ship("test01.xml", base)
    ws.base("test01.xml", base)
    ws.user("test01.xml", base.replace(b'name="rpm"', b'name="My RPM"'))
    # Bundled moves the axis and the user renamed it: both arrive
    moved = base.replace(b'address="c100"', b'address="c200"')
    ws.ship("test01.xml", moved)
    ws.run()
    t = tables_of(ws.user("test01.xml"))["c000"][0]
    assert (t[0].get("name"), t[0].get("address")) == ("My RPM", "c200")


def test_moved_table_is_not_kept_at_its_old_address(ws):
    v1 = doc(scaling("s1"), table("KR", "c000"))
    v2 = doc(scaling("s1"), table("KR", "c004"))  # address corrected
    ws.ship("test01.xml", v2)
    ws.base("test01.xml", v1)
    ws.user("test01.xml", v1.replace(b'name="KR"', b'name="KR mine"'))
    report = ws.run()
    tables = tables_of(ws.user("test01.xml"))
    assert "c000" not in tables and "c004" in tables
    details = ws.outcome(report, "test01.xml").details
    assert any("removed" in d and "KR mine" in d for d in details)


def test_users_own_table_is_kept_with_its_scaling(ws):
    ws.ship("test01.xml", V2)
    ws.base("test01.xml", V1)
    extra = scaling("mine") + table("Mine", "d000", sc="mine")
    ws.user("test01.xml", V1.replace(b"</rom>", extra.encode() + b"\n  </rom>"))
    report = ws.run()
    data = ws.user("test01.xml")
    assert tables_of(data)["d000"][0].get("name") == "Mine"
    assert "mine" in scalings_of(data)
    assert "kept 1 table(s) you added" in ws.outcome(report, "test01.xml").details


def test_users_second_table_at_a_bundled_address_does_not_block_corrections(ws):
    v1 = doc(scaling("s1"), table("T", "c000", elements="1"))
    v2 = doc(scaling("s1"), table("T", "c000", elements="2"))
    ws.ship("test01.xml", v2)
    ws.base("test01.xml", v1)
    ws.user(
        "test01.xml", v1.replace(b"</rom>", table("T pct", "c000").encode() + b"</rom>")
    )
    ws.run()
    at = {
        t.get("name"): t.get("elements")
        for t in tables_of(ws.user("test01.xml"))["c000"]
    }
    assert at == {"T": "2", "T pct": "1"}


def test_old_scaling_still_used_by_users_table_is_not_dropped(ws):
    v1 = doc(scaling("s1"), scaling("old"), table("T", "c000"))
    v2 = doc(scaling("s1"), table("T", "c000"))
    ws.ship("test01.xml", v2)
    ws.base("test01.xml", v1)
    ws.user(
        "test01.xml",
        v1.replace(b"</rom>", table("Mine", "d000", sc="old").encode() + b"</rom>"),
    )
    ws.run()
    data = ws.user("test01.xml")
    assert "old" in scalings_of(data)
    assert tables_of(data)["d000"][0].get("scaling") == "old"


def test_entry_the_user_removed_stays_removed(ws):
    v1 = doc(scaling("s1"), table("A", "c000"), table("B", "c010"))
    v2 = doc(scaling("s1"), table("A", "c000"), table("B", "c010"), table("C", "c020"))
    ws.ship("test01.xml", v2)
    ws.base("test01.xml", v1)
    ws.user("test01.xml", doc(scaling("s1"), table("A", "c000")))
    ws.run()
    tables = tables_of(ws.user("test01.xml"))
    assert "c010" not in tables and "c020" in tables


def test_duplicate_scaling_names(ws):
    v = doc(
        scaling("dup", mx="1"), scaling("dup", mx="2"), table("T", "c000", sc="dup")
    )
    v2 = v.replace(b"</rom>", table("New", "c010", sc="dup").encode() + b"</rom>")
    ws.ship("test01.xml", v2)
    ws.base("test01.xml", v)
    ws.user("test01.xml", v.replace(b'max="2"', b'max="3"'))
    ws.run()
    data = ws.user("test01.xml")
    assert [s.get("max") for s in etree.fromstring(data).iter("scaling")] == ["1", "3"]
    assert "c010" in tables_of(data)


def test_second_run_changes_nothing(ws):
    ws.ship("test01.xml", V2)
    ws.base("test01.xml", V1)
    ws.user("test01.xml", V1.replace(b'min="0"', b'min="-5"'))
    ws.run()
    merged = ws.user("test01.xml")
    report = ws.run(now=datetime(2026, 10, 8, 12, 0, 1))
    assert ws.outcome(report, "test01.xml").action == "unchanged"
    assert ws.user("test01.xml") == merged
    assert not report.worth_telling


def test_merged_file_loads_in_the_definition_parser(ws):
    ws.ship("test01.xml", V2)
    ws.base("test01.xml", V1)
    ws.user("test01.xml", V1.replace(b'min="0"', b'min="-5"'))
    ws.run()
    definition = load_definition(str(ws.meta / "test01.xml"))
    assert {t.name for t in definition.tables} >= {"New Name", "Added"}


# ---------------------------------------------------------------- safety


def test_backups_are_never_overwritten(ws):
    ws.ship("test01.xml", V2)
    ws.base("test01.xml", V1)
    original = V1.replace(b'min="0"', b'min="-5"')
    ws.user("test01.xml", original)
    first = ws.run()
    ws.ship("test01.xml", V2.replace(b"New Name", b"Newer Name"))
    second = ws.run()  # same timestamp
    assert first.backup_dir != second.backup_dir
    assert (first.backup_dir / "test01.xml").read_bytes() == original


def test_unreadable_base_counts_as_no_base(ws):
    ws.ship("test01.xml", V2)
    ws.base("test01.xml", b"<roms><rom>trunc")
    ws.user("test01.xml", V1)
    report = ws.run({"test01.xml": release_hashes(V1)})
    assert ws.outcome(report, "test01.xml").action == "replaced"


def test_write_failure_is_reported_and_retried(ws, monkeypatch):
    ws.ship("test01.xml", V2)
    ws.base("test01.xml", V1)
    ws.user("test01.xml", V1)
    real = du._write_atomic

    def locked(path, data):
        if path.name == "test01.xml" and path.parent == ws.meta:
            raise PermissionError("file in use")
        real(path, data)

    monkeypatch.setattr(du, "_write_atomic", locked)
    report = ws.run()
    out = ws.outcome(report, "test01.xml")
    assert out.action == "skipped" and out.failed_io
    assert report.failed_io
    assert ws.user("test01.xml") == V1


# ---------------------------------------------------------------- startup


def _settings(workspace: Path, metadata: Path, stored=""):
    store = {du.VERSION_KEY: stored}
    s = MagicMock()
    s.get_workspace_directory.return_value = str(workspace)
    s.get_metadata_directory.return_value = str(metadata)
    s.settings.value.side_effect = lambda k, d=None: store.get(k, d)
    s.settings.setValue.side_effect = lambda k, v: store.__setitem__(k, v)
    return s, store


@pytest.fixture
def app(tmp_path, monkeypatch):
    root = tmp_path / "app"
    (root / "examples" / "metadata").mkdir(parents=True)
    (root / "examples" / "metadata" / "test01.xml").write_bytes(V2)
    monkeypatch.delenv(du.FORCE_ENV, raising=False)
    with patch("src.utils.paths.get_app_root", return_value=root):
        yield root


def test_runs_once_per_newer_release(app, tmp_path):
    ws = tmp_path / "ws"
    settings, store = _settings(ws, ws / "metadata", stored="2.21.0")
    with patch("src.utils.constants.APP_VERSION", "2.22.0"):
        first = du.update_workspace_definitions(settings)
        again = du.update_workspace_definitions(settings)
    assert first.by_action("added") and again is None
    assert store[du.VERSION_KEY] == "2.22.0"


def test_never_runs_on_a_downgrade_or_from_source(app, tmp_path):
    ws = tmp_path / "ws"
    settings, _ = _settings(ws, ws / "metadata", stored="2.22.0")
    with patch("src.utils.constants.APP_VERSION", "2.21.0"):
        assert du.update_workspace_definitions(settings) is None
    with patch("src.utils.constants.APP_VERSION", "dev"):
        assert du.update_workspace_definitions(settings) is None
    assert not (ws / "metadata").exists()


def test_environment_forces_a_run_from_source(app, tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    settings, _ = _settings(ws, ws / "metadata")
    monkeypatch.setenv(du.FORCE_ENV, "1")
    with patch("src.utils.constants.APP_VERSION", "dev"):
        assert du.update_workspace_definitions(settings).by_action("added")


def test_custom_definitions_folder_is_never_written(app, tmp_path):
    custom = tmp_path / "my_repo"
    custom.mkdir()
    settings, _ = _settings(tmp_path / "ws", custom)
    with patch("src.utils.constants.APP_VERSION", "2.22.0"):
        report = du.update_workspace_definitions(settings)
        assert str(custom) in report.skipped_reason
        assert du.update_workspace_definitions(settings) is None  # told once
    assert list(custom.iterdir()) == []


def test_a_second_instance_does_not_run_concurrently(app, tmp_path):
    ws = tmp_path / "ws"
    lock = ws / du.LOCK_FILE
    lock.parent.mkdir(parents=True)
    lock.write_text("")
    settings, store = _settings(ws, ws / "metadata")
    with patch("src.utils.constants.APP_VERSION", "2.22.0"):
        assert du.update_workspace_definitions(settings) is None
    assert store[du.VERSION_KEY] == ""  # tried again next start
    old = lock.stat().st_mtime - du.LOCK_STALE_SECONDS - 5
    os.utime(lock, (old, old))  # a crashed run's lock goes stale
    with patch("src.utils.constants.APP_VERSION", "2.22.0"):
        assert du.update_workspace_definitions(settings).by_action("added")


def test_io_failure_keeps_the_version_unmarked(app, tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    settings, store = _settings(ws, ws / "metadata")
    monkeypatch.setattr(
        du, "_write_atomic", MagicMock(side_effect=PermissionError("in use"))
    )
    with patch("src.utils.constants.APP_VERSION", "2.22.0"):
        report = du.update_workspace_definitions(settings)
    assert report.failed_io and store[du.VERSION_KEY] == ""


# ---------------------------------------------------------------- shipped data


def test_bundled_definitions_merge_with_themselves():
    # A sample (every 10th file, plus the TCM ones): all 105 take ~20 s
    root = Path(__file__).resolve().parent.parent / "examples"
    files = sorted((root / "metadata").glob("*.xml"))
    for p in files[::10] + [f for f in files if "_v02" in f.name]:
        data = p.read_bytes()
        result = du.merge(data, data, data)
        assert du.file_hash(result.data) == du.file_hash(data), p.name


def test_release_history_lists_every_bundled_file():
    root = Path(__file__).resolve().parent.parent / "examples"
    history = du.history_hashes((root / du.HISTORY_FILE).read_bytes())
    released = {p.name.lower() for p in (root / "metadata").glob("*.xml")}
    # Files added since the last release are not in it yet; most are
    assert len(released & set(history)) >= len(released) - 5


def test_progress_is_reported_per_file(ws):
    ws.ship("a.xml", V2.replace(b"TEST01", b"TESTA1"))
    ws.ship("b.xml", V2.replace(b"TEST01", b"TESTB1"))
    seen = []
    du.update_definitions(
        ws.root.parent / "bundled",
        ws.meta,
        ws.root,
        progress=lambda d, t: seen.append((d, t)),
    )
    assert seen == [(0, 2), (1, 2)]


def test_summary_and_details_name_the_files(ws):
    ws.ship("test01.xml", V2)
    ws.base("test01.xml", V1)
    ws.user("test01.xml", V1.replace(b'min="0"', b'min="-5"'))
    report = ws.run()
    assert "Updated 1 ROM definition(s)" in report.summary_text()
    assert str(report.backup_dir) in report.summary_text()
    assert "test01.xml:" in report.details_text()
    assert "kept your edit: scaling s1" in report.details_text()


# ---------------------------------------------------------------- review round 2


def test_ambiguous_tables_at_one_address_leave_the_file_alone(ws):
    # Base T is corrected to 2 elements; the user renamed T and added "Mine"
    # at the same address: guessing which is T could correct the wrong table
    v1 = doc(scaling("s1"), table("T", "c000", elements="1"))
    v2 = doc(scaling("s1"), table("T", "c000", elements="2"))
    user = doc(
        scaling("s1"),
        table("Mine", "c000", elements="1"),
        table("T renamed", "c000", elements="1", category="X"),
    )
    ws.ship("test01.xml", v2)
    ws.base("test01.xml", v1)
    ws.user("test01.xml", user)
    report = ws.run()
    out = ws.outcome(report, "test01.xml")
    assert out.action == "skipped" and "can't tell" in out.details[0]
    assert ws.user("test01.xml") == user


def test_left_alone_file_lists_byte_affecting_differences(ws):
    # The KR case on an install from before this feature
    old = doc(scaling("kr", storage="float"), table("KR", "c000", sc="kr"))
    new = doc(scaling("kr", storage="uint8"), table("KR", "c000", sc="kr"))
    ws.ship("test01.xml", new)
    ws.user("test01.xml", old.replace(b'max="100"', b'max="50"'))
    report = ws.run({"test01.xml": release_hashes(old)})
    out = ws.outcome(report, "test01.xml")
    assert out.action == "left-alone"
    assert du.LAYOUT_DIFF + "scaling kr storagetype: yours float, NC Flash's uint8" in (
        out.details
    )
    assert "(see Show Details): test01.xml." in report.summary_text()


def test_identity_correction_reaches_an_unedited_copy(ws):
    v1 = V1
    v2 = V2.replace(b"<internalidaddress>b8046", b"<internalidaddress>c0046")
    ws.ship("test01.xml", v2)
    ws.base("test01.xml", v1)
    ws.user("test01.xml", v1)
    report = ws.run()
    assert ws.outcome(report, "test01.xml").action == "replaced"
    assert ws.user("test01.xml") == v2


def test_summary_counts_what_was_not_kept(ws):
    v1 = doc(scaling("kr", storage="float"), table("KR", "c000", sc="kr"))
    v2 = doc(scaling("kr", storage="uint8"), table("KR", "c004", sc="kr"))
    user = doc(scaling("kr", storage="uint16"), table("KR mine", "c000", sc="kr"))
    ws.ship("test01.xml", v2)
    ws.base("test01.xml", v1)
    ws.user("test01.xml", user)
    text = ws.run().summary_text()
    assert "1 value(s) you had changed were replaced" in text
    assert "1 table(s) you had edited were removed" in text


def test_a_file_that_keeps_failing_stops_being_retried(app, tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    settings, store = _settings(ws, ws / "metadata")
    monkeypatch.setattr(
        du, "_write_atomic", MagicMock(side_effect=PermissionError("read-only"))
    )
    with patch("src.utils.constants.APP_VERSION", "2.22.0"):
        for attempt in range(du.MAX_ATTEMPTS):
            assert du.update_workspace_definitions(settings).failed_io
        assert store[du.VERSION_KEY] == "2.22.0"
        assert du.update_workspace_definitions(settings) is None
