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
    assert replace_pids(ORIGINAL, [])["pids"] == []
    assert ORIGINAL["pids"]  # the original is not modified
    assert orphaned_calculated(ORIGINAL, [{"Name": "MAP"}, {"Name": "BARO"}]) == ["AFR"]
