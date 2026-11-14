"""Sending the Datalogger's channel list to the WiCAN (HTTP mocked)."""

import json

import pytest

from src.ecu import wican_autopid as mod
from src.ecu.wican_autopid import (
    WiCANAutoPid,
    WiCANAutoPidError,
    orphaned_calculated,
    replace_pids,
)

ORIGINAL = {
    "format": "nc-flash-1",
    "initialisation": "ATSP6",
    "pid_polling_min_voltage": "12.8",
    "std_pids": [{"Name": "X"}],
    "standard_pids": "disable",
    "can_filters": [{"frame_id": 513, "parameters": [{"name": "VSS"}]}],
    "calculated": [
        {"name": "BOOST", "expression": "(MAP-BARO)*0.145038", "enabled": True},
        {"name": "AFR", "expression": "EQ_RATIO_ACT*14.71", "enabled": True},
        {"name": "SPD", "expression": "VSS*1", "enabled": True},
        {"name": "OFF", "expression": "NOPE*1", "enabled": False},
    ],
    "pids": [{"Name": "MAP"}, {"Name": "BARO"}],
}
NEW_ROWS = [{"Name": "RPM", "enabled": True}, {"Name": "EQ_RATIO_ACT", "enabled": True}]


class FakeHttp:
    """Stands in for wican_http; records every call."""

    def __init__(self, monkeypatch, raw=None, store_reply=None, statuses=None):
        self.raw = json.dumps(ORIGINAL) if raw is None else raw
        self.store_reply = store_reply or {"applied": "live"}
        self.statuses = list(statuses or [{"reload_pending": False, "reload_ok": True}])
        self.calls = []
        self.stored = []
        monkeypatch.setattr(mod.wican_http, "fetch_head", self.fetch_head)
        monkeypatch.setattr(mod.wican_http, "post_json", self.post_json)
        monkeypatch.setattr(mod.wican_http, "get_json", self.get_json)

    def fetch_head(self, url, max_bytes, timeout_s=None):
        self.calls.append(("GET", url))
        return self.raw.encode()

    def get_json(self, url, timeout_s=None):
        self.calls.append(("GET", url))
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def post_json(self, url, body, timeout_s=None):
        self.calls.append(("POST", url))
        if url.endswith("/store_auto_data"):
            self.stored.append(body)
            return self.store_reply
        return {}


def _client():
    return WiCANAutoPid("wican.local", sleep=lambda s: None)


def test_push_backs_up_then_replaces_only_pids(monkeypatch, tmp_path):
    http = FakeHttp(monkeypatch)
    result = _client().push(NEW_ROWS, tmp_path / "backups")
    stored = http.stored[0]
    assert stored["pids"] == NEW_ROWS
    for key in ORIGINAL:
        if key != "pids":
            assert stored[key] == ORIGINAL[key]  # everything else untouched
    assert result.applied == "live"
    backup = json.loads(open(result.backup, encoding="utf-8").read())
    assert backup == ORIGINAL  # the file exactly as read
    # The backup is written before the device is changed.
    assert http.calls.index(("POST", "http://wican.local:80/store_auto_data")) > 0
    assert result.orphaned_calculated == ["BOOST"]  # MAP/BARO gone; VSS is broadcast


def test_no_file_on_the_device_is_never_replaced(monkeypatch, tmp_path):
    http = FakeHttp(monkeypatch, raw="NONE")
    with pytest.raises(WiCANAutoPidError):
        _client().push(NEW_ROWS, tmp_path)
    assert http.stored == []


def test_unreadable_or_unknown_format_is_never_replaced(monkeypatch, tmp_path):
    http = FakeHttp(monkeypatch, raw="{not json")
    with pytest.raises(WiCANAutoPidError):
        _client().push(NEW_ROWS, tmp_path)
    http = FakeHttp(monkeypatch, raw=json.dumps({"pids": []}))
    with pytest.raises(WiCANAutoPidError):
        _client().push(NEW_ROWS, tmp_path)
    assert http.stored == []


def test_wait_applied_until_reload_finishes(monkeypatch):
    FakeHttp(
        monkeypatch,
        statuses=[
            {"reload_pending": True},
            {"reload_pending": True},
            {"reload_pending": False, "reload_ok": True, "pids_unpollable": 0},
        ],
    )
    assert _client().wait_applied()["reload_ok"] is True


@pytest.mark.parametrize(
    "status",
    [
        {"reload_pending": False, "reload_ok": False},
        {"reload_pending": False, "reload_ok": True, "pids_unpollable": 2},
    ],
)
def test_wait_applied_reports_a_refused_table(monkeypatch, status):
    FakeHttp(monkeypatch, statuses=[status])
    with pytest.raises(WiCANAutoPidError):
        _client().wait_applied()


def test_wait_applied_times_out(monkeypatch):
    FakeHttp(monkeypatch, statuses=[{"reload_pending": True}])
    now = [0.0]

    def sleep(s):
        now[0] += s

    client = WiCANAutoPid("h", sleep=sleep, clock=lambda: now[0])
    # Still pending: a trip opened in between, so it applies after that trip.
    assert client.wait_applied(timeout_s=2)["reload_pending"] is True


def test_cancel_stops_waiting(monkeypatch):
    FakeHttp(monkeypatch, statuses=[{"reload_pending": True}])
    client = _client()
    client.cancel()
    assert client.wait_applied()["reload_pending"] is True


def test_older_firmware_without_reload_report(monkeypatch):
    FakeHttp(monkeypatch, statuses=[{"active": True}])
    assert _client().wait_applied() == {"active": True}


def test_preview_reports_what_would_change_without_writing(monkeypatch):
    http = FakeHttp(monkeypatch)
    preview = _client().preview([{"Name": "MAP", "enabled": True}])
    assert preview == {
        "removed": ["BARO"],
        "added": [],
        "orphaned": ["BOOST", "AFR"],
        "rpm": False,
        "count": 1,
        "clashes": [],
    }
    assert http.stored == [] and all(m == "GET" for m, _ in http.calls)


def test_trip_logger_is_never_forced_on_or_off(monkeypatch, tmp_path):
    # op=start leaves the released firmware's logger forced ON for good.
    http = FakeHttp(monkeypatch, store_reply={"applied": "deferred"})
    client = _client()
    client.push(NEW_ROWS, tmp_path)
    client.wait_applied()
    assert not [u for m, u in http.calls if "csv_logger" in u]


def test_backup_path_is_known_when_the_store_fails(monkeypatch, tmp_path):
    http = FakeHttp(monkeypatch)

    def fail(url, body, timeout_s=None):
        raise mod.wican_http.WiCANHttpError("POST failed: timed out")

    monkeypatch.setattr(mod.wican_http, "post_json", fail)
    client = _client()
    with pytest.raises(mod.wican_http.WiCANHttpError):
        client.push(NEW_ROWS, tmp_path)
    assert client.last_backup and json.loads(
        open(client.last_backup, encoding="utf-8").read()
    ) == json.loads(http.raw)


def test_restore_puts_the_backup_back(monkeypatch, tmp_path):
    http = FakeHttp(monkeypatch)
    result = _client().push(NEW_ROWS, tmp_path)
    _client().restore(result.backup)
    assert http.stored[-1] == ORIGINAL


def test_restore_refuses_a_file_that_is_not_a_channel_file(monkeypatch, tmp_path):
    http = FakeHttp(monkeypatch)
    bad = tmp_path / "x.json"
    bad.write_text('{"pids": []}', encoding="utf-8")
    with pytest.raises(WiCANAutoPidError):
        _client().restore(bad)
    assert http.stored == []


def test_replace_pids_and_orphans_helpers():
    rpm_only = [{"Name": "RPM", "enabled": True}]
    assert replace_pids(ORIGINAL, rpm_only)["pids"] == rpm_only
    assert ORIGINAL["pids"]  # the original is not modified
    assert orphaned_calculated(ORIGINAL, [{"Name": "MAP"}, {"Name": "BARO"}]) == ["AFR"]


def test_a_file_saved_before_the_format_marker_is_ours(monkeypatch, tmp_path):
    """Real case (bench WiCAN, 2026-11-10): a file the firmware's Logger page
    saved before "format" existed has "calculated" but no "format"."""
    old = {k: v for k, v in ORIGINAL.items() if k != "format"}
    old.setdefault("calculated", [])
    http = FakeHttp(monkeypatch, raw=json.dumps(old))
    _client().push(NEW_ROWS, tmp_path)
    stored = http.stored[-1]
    assert stored["format"] == "nc-flash-1"  # marked on the way out
    assert stored["pids"] == NEW_ROWS
    assert stored["calculated"] == old["calculated"]
    # Its backup (no "format") can be put back too.
    backup = next(tmp_path.rglob("*.json"))
    assert "format" not in json.loads(backup.read_text(encoding="utf-8"))
    _client().restore(backup)
    assert http.stored[-1] == old


@pytest.mark.parametrize(
    "doc",
    [
        {"pids": []},  # official MeatPi firmware: neither key
        {"pids": [], "calculated": [], "format": "someone-else-2"},
        {"pids": [], "calculated": {}},  # wrong shape
    ],
)
def test_files_that_are_not_ours_are_never_replaced(monkeypatch, tmp_path, doc):
    http = FakeHttp(monkeypatch, raw=json.dumps(doc))
    with pytest.raises(WiCANAutoPidError):
        _client().push(NEW_ROWS, tmp_path)
    assert http.stored == []


def test_polled_rows_named_like_a_broadcast_channel_are_turned_off(
    monkeypatch, tmp_path
):
    """Real case (bench WiCAN, 2026-11-10): the firmware's list reads ECT from
    the CAN bus (on) and RPM by polling (its broadcast RPM is off). NC Flash's
    list polls both. Only ECT clashes; its polled copy is kept but turned off.
    Dropping the polled RPM instead made the WiCAN judge the engine by
    battery voltage and open a trip on the bench."""
    doc = dict(ORIGINAL)
    doc["can_filters"] = [
        {
            "frame_id": 576,
            "parameters": [
                {"name": "ECT", "expression": "B1-40", "enabled": True},
                {"name": "RPM", "expression": "B1", "enabled": False},
            ],
        }
    ]
    http = FakeHttp(monkeypatch, raw=json.dumps(doc))
    rows = [{"Name": "RPM", "enabled": True}, {"Name": "ECT", "enabled": True}]
    preview = _client().preview(rows)
    assert preview["clashes"] == ["ECT"] and "ECT" not in preview["added"]
    assert preview["rpm"] is True  # the polled RPM stays on
    _client().push(rows, tmp_path)
    stored = {r["Name"]: r["enabled"] for r in http.stored[-1]["pids"]}
    assert stored == {"RPM": True, "ECT": False}
    assert http.stored[-1]["can_filters"] == doc["can_filters"]


def test_refusal_says_why_in_plain_words(monkeypatch, tmp_path):
    FakeHttp(monkeypatch, raw=json.dumps({"pids": []}))
    with pytest.raises(WiCANAutoPidError) as err:
        _client().push(NEW_ROWS, tmp_path)
    assert "official MeatPi firmware" in str(err.value)
    assert "Logger" in str(err.value) and "None" not in str(err.value)


# --- Put Back safety (second review) -----------------------------------------


def _bench_like(**over):
    doc = {k: v for k, v in ORIGINAL.items() if k != "format"}
    doc["calculated"] = []
    doc["can_filters"] = [
        {
            "frame_id": 513,
            "parameters": [
                {"name": "VSS", "expression": "B1", "enabled": True},
                {"name": "RPM", "expression": "B1", "enabled": False},
            ],
        },
        {"frame_id": 576, "parameters": [{"name": "ECT", "expression": "B1-40"}]},
    ]
    doc["pids"] = [{"Name": "RPM", "enabled": True}, {"Name": "MAP", "enabled": True}]
    doc.update(over)
    return doc


def test_restore_backs_up_the_current_list_first(monkeypatch, tmp_path):
    from pathlib import Path

    current = _bench_like()
    http = FakeHttp(monkeypatch, raw=json.dumps(current))
    old = tmp_path / "old.json"
    old.write_text(json.dumps(_bench_like(pids=[{"Name": "RPM"}])), encoding="utf-8")
    result = _client().restore(old, tmp_path / "b")
    saved = Path(result["backup"])
    assert json.loads(saved.read_text(encoding="utf-8")) == current
    assert http.stored[-1]["pids"] == [{"Name": "RPM"}]


def test_restore_refuses_a_list_that_changed_unless_forced(monkeypatch, tmp_path):
    from src.ecu.wican_autopid import WiCANListChanged, file_sha

    raw = json.dumps(_bench_like())
    http = FakeHttp(monkeypatch, raw=raw)
    old = tmp_path / "old.json"
    old.write_text(json.dumps(_bench_like(pids=[{"Name": "RPM"}])), encoding="utf-8")
    with pytest.raises(WiCANListChanged):
        _client().restore(old, tmp_path / "b", expect_sha="not-this")
    assert http.stored == []  # nothing written
    _client().restore(old, tmp_path / "b", expect_sha=file_sha(raw))  # matches
    _client().restore(old, tmp_path / "b", expect_sha="not-this", force=True)
    assert len(http.stored) == 2


@pytest.mark.parametrize(
    "reply,pending", [({"applied": "deferred"}, True), ({"applied": "reboot"}, True)]
)
def test_restore_reports_a_switch_that_has_not_happened(
    monkeypatch, tmp_path, reply, pending
):
    http = FakeHttp(monkeypatch, raw=json.dumps(_bench_like()))
    http.store_reply = reply
    old = tmp_path / "old.json"
    old.write_text(json.dumps(_bench_like()), encoding="utf-8")
    result = _client().restore(old, tmp_path / "b")
    assert result["applied"] == reply["applied"] and result["reload_pending"] is pending


def test_rpm_is_never_turned_off_and_calculated_names_never_clash(
    monkeypatch, tmp_path
):
    doc = _bench_like()
    doc["can_filters"][0]["parameters"][1]["enabled"] = True  # broadcast RPM on
    doc["calculated"] = [{"name": "MAP", "expression": "BARO*1"}]
    http = FakeHttp(monkeypatch, raw=json.dumps(doc))
    rows = [
        {"Name": "RPM", "enabled": True},
        {"Name": "MAP", "enabled": True},
        {"Name": "ect", "enabled": True},  # case differs: still the broadcast ECT
    ]
    preview = _client().preview(rows)
    assert preview["clashes"] == ["ect"] and preview["rpm"] is True
    _client().push(rows, tmp_path)
    stored = {r["Name"]: r["enabled"] for r in http.stored[-1]["pids"]}
    assert stored == {"RPM": True, "MAP": True, "ect": False}


def test_a_list_without_any_rpm_is_refused(monkeypatch, tmp_path):
    http = FakeHttp(monkeypatch, raw=json.dumps(_bench_like()))  # broadcast RPM off
    rows = [{"Name": "MAP", "enabled": True}, {"Name": "RPM", "enabled": False}]
    assert _client().preview(rows)["rpm"] is False
    with pytest.raises(WiCANAutoPidError) as err:
        _client().push(rows, tmp_path)
    assert "RPM" in str(err.value) and http.stored == []


def test_broadcast_rpm_counts_for_the_engine_gate(monkeypatch):
    from src.ecu.wican_autopid import has_engine_rpm

    on = [{"parameters": [{"Name": "rpm", "Enabled": True}]}]
    off = [{"parameters": [{"name": "RPM", "enabled": False}]}]
    assert has_engine_rpm([], on)
    assert not has_engine_rpm([], off)
    assert has_engine_rpm([{"name": "Rpm", "enabled": "yes"}], off)  # non-bool = on


def test_an_unknown_fingerprint_always_warns(monkeypatch, tmp_path):
    """A push that failed half way stores no fingerprint: Put Back must ask."""
    from src.ecu.wican_autopid import WiCANListChanged

    http = FakeHttp(monkeypatch, raw=json.dumps(_bench_like()))
    old = tmp_path / "old.json"
    old.write_text(json.dumps(_bench_like()), encoding="utf-8")
    with pytest.raises(WiCANListChanged) as err:
        _client().restore(old, tmp_path / "b", expect_sha="")
    assert "did not finish" in str(err.value) and http.stored == []
    _client().restore(old, tmp_path / "b", expect_sha="", force=True)
    assert len(http.stored) == 1


def test_a_failed_reload_after_put_back_names_the_saved_list(monkeypatch, tmp_path):
    FakeHttp(
        monkeypatch,
        raw=json.dumps(_bench_like()),
        statuses=[{"reload_pending": False, "reload_ok": False}],
    )
    old = tmp_path / "old.json"
    old.write_text(json.dumps(_bench_like()), encoding="utf-8")
    with pytest.raises(WiCANAutoPidError) as err:
        _client().restore(old, tmp_path / "b")
    assert "saved in" in str(err.value)
