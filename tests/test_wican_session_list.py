"""The WiCAN's temporary (per-run) channel list client, against a small real
HTTP server that speaks the firmware contract."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from src.ecu import wican_http
from src.ecu.wican_autopid import (
    WiCANAutoPidError,
    WiCANSessionBusy,
    WiCANSessionList,
    WiCANSessionStale,
    WiCANSessionUnsupported,
    session_document,
)

ROWS = [{"Name": "RPM", "enabled": True}, {"Name": "MAP", "enabled": False}]


class Device:
    """A WiCAN with the temporary list; ``old`` makes it firmware without."""

    def __init__(self):
        self.old = False
        self.caps = {"ncfr_rev": 7, "session_list": 1}
        self.refuse = None  # (status, body) for the next begin
        self.refuse_token_op = None  # (status, body) for keepalive / end
        self.token = 0
        self.next_token = 41
        self.begun = []  # (query, body) of every begin
        self.requests = []  # "METHOD path?query"
        self.statuses = [
            {"session_list": True, "reload_pending": False, "reload_ok": True}
        ]

    def get(self, path):
        if path == "/host_caps":
            return (404, "") if self.old and self.caps is None else (200, self.caps)
        if path == "/poll_status":
            status = self.statuses[0]
            if len(self.statuses) > 1:
                self.statuses.pop(0)
            # A (status, body) pair is an HTTP error reply.
            return status if isinstance(status, tuple) else (200, status)
        return 404, ""

    def post(self, path, query, body):
        if path != "/datalog_session" or self.old:
            return 404, "Nothing matches the given URI"
        op = query.get("op", [""])[0]
        if op == "begin":
            if self.refuse:
                return self.refuse
            self.begun.append((query, json.loads(body)))
            self.next_token += 1
            self.token = self.next_token
            ttl = int(query.get("ttl_s", ["30"])[0])
            return 200, {"ok": True, "active": True, "token": self.token, "ttl_s": ttl}
        if self.refuse_token_op:
            return self.refuse_token_op
        if not self.token or query.get("token", [""])[0] != str(self.token):
            return 409, {"ok": False, "error": "stale"}
        if op == "keepalive":
            return 200, {"ok": True, "active": True, "remaining_ms": 29000}
        if op == "end":
            self.token = 0
            return 200, {"ok": True, "active": False}
        return 400, {"ok": False, "error": "bad_request", "msg": "unknown op"}


@pytest.fixture
def device():
    dev = Device()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self, status, body):
            data = (body if isinstance(body, str) else json.dumps(body)).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            dev.requests.append(f"GET {self.path}")
            self._reply(*dev.get(urlparse(self.path).path))

        def do_POST(self):
            dev.requests.append(f"POST {self.path}")
            url = urlparse(self.path)
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self._reply(*dev.post(url.path, parse_qs(url.query), body))

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    dev.port = server.server_address[1]
    yield dev
    server.shutdown()
    server.server_close()


def _client(device, **kw):
    return WiCANSessionList(
        "127.0.0.1", http_port=device.port, sleep=lambda s: None, **kw
    )


def test_session_document_holds_the_rows_and_nothing_else():
    assert session_document(ROWS) == {
        "format": "nc-flash-1",
        "pids": ROWS,
        "can_filters": [],
        "calculated": [],
    }


def test_begin_keepalive_end(device):
    client = _client(device)
    assert client.supported() is True
    token = client.begin(ROWS)
    assert token == device.token != 0
    query, body = device.begun[0]
    assert query["ttl_s"] == ["30"] and body == session_document(ROWS)
    assert client.keepalive(token) == 29000
    assert client.end(token) is True
    assert client.end(token) is False  # already ended: not an error
    # The saved list is never read or written.
    assert not [r for r in device.requests if "auto" in r]


def test_old_firmware_is_unsupported(device):
    device.old = True
    device.caps = {"ncfr_rev": 6}  # /host_caps without the capability
    client = _client(device)
    assert client.supported() is False
    device.caps = None  # no /host_caps at all (404)
    assert client.supported() is False
    with pytest.raises(WiCANSessionUnsupported):
        client.begin(ROWS)


def test_busy_stale_and_other_refusals_are_told_apart(device):
    client = _client(device)
    device.refuse = (409, {"ok": False, "error": "busy", "msg": "host bus claim"})
    with pytest.raises(WiCANSessionBusy):
        client.begin(ROWS)
    hint = "Add one channel on the WiCAN's Logger page"
    device.refuse = (409, {"ok": False, "error": "inactive", "msg": hint})
    with pytest.raises(WiCANAutoPidError) as inactive:
        client.begin(ROWS)
    assert not isinstance(inactive.value, (WiCANSessionBusy, WiCANSessionStale))
    assert str(inactive.value) == hint  # the WiCAN's own words
    # 0 rows, over 128 rows, or a row that is not service 01/22/23.
    refused = {"ok": False, "error": "bad_request", "msg": "more than 128 channels"}
    device.refuse = (400, refused)
    with pytest.raises(WiCANAutoPidError, match="more than 128 channels"):
        client.begin(ROWS)
    # Out of memory on the WiCAN: a failed start with its reason, never
    # mistaken for old firmware or a network failure.
    device.refuse = (500, {"ok": False, "error": "internal", "msg": "no memory"})
    with pytest.raises(WiCANAutoPidError, match="no memory") as internal:
        client.begin(ROWS)
    assert not isinstance(internal.value, WiCANSessionUnsupported)
    device.refuse = None
    with pytest.raises(WiCANSessionStale):
        client.keepalive(12345)  # not our token (lease expired, or a reboot)


def test_network_failure_is_its_own_error(device):
    client = WiCANSessionList("127.0.0.1", http_port=1, timeout_s=0.5)
    for call in (client.supported, lambda: client.begin(ROWS)):
        with pytest.raises(wican_http.WiCANHttpError) as failure:
            call()
        assert failure.value.status is None
        assert not isinstance(failure.value, WiCANAutoPidError)


def test_a_list_too_large_is_refused_before_sending(device):
    rows = [{"Name": f"C{i}", "Expression": "x" * 100} for i in range(1000)]
    with pytest.raises(WiCANAutoPidError, match="too large"):
        _client(device).begin(rows)
    assert device.requests == []


def test_wait_applied_until_our_list_is_the_one_polled(device):
    device.statuses = [
        # reload_ok / pids_unpollable are stale while a reload is pending.
        {"session_token": 0, "reload_pending": True, "reload_ok": False},
        {"session_token": 42, "reload_pending": True, "pids_unpollable": 3},
        {"session_token": 42, "reload_pending": False, "reload_ok": True},
    ]
    assert _client(device).wait_applied(42)["session_token"] == 42
    assert device.statuses == device.statuses[-1:]


def test_wait_applied_without_a_session_token_uses_session_list(device):
    device.statuses = [
        {"session_list": False, "reload_pending": False, "reload_ok": True},
        {"session_list": True, "reload_pending": True},
        {"session_list": True, "reload_pending": False, "reload_ok": True},
    ]
    assert _client(device).wait_applied(42)["session_list"] is True
    assert device.statuses == device.statuses[-1:]


@pytest.mark.parametrize(
    "status,text",
    [
        ({"session_token": 42, "reload_pending": False, "reload_ok": False}, "load"),
        (
            {
                "session_token": 42,
                "reload_pending": False,
                "reload_ok": True,
                "pids_unpollable": 2,
            },
            "can't poll 2",
        ),
    ],
)
def test_wait_applied_reports_a_refused_list(device, status, text):
    device.statuses = [status]
    with pytest.raises(WiCANAutoPidError, match=text):
        _client(device).wait_applied(42)


def test_unreadable_unpollable_count_is_not_an_error(device):
    status = {"session_token": 42, "reload_pending": False, "pids_unpollable": "n/a"}
    device.statuses = [status]
    assert _client(device).wait_applied(42) == status


@pytest.mark.parametrize(
    "status,error,text",
    [
        # It parsed our list, could not use it, and is back on its own.
        (
            {"session_token": 0, "reload_pending": False, "reload_ok": False},
            WiCANAutoPidError,
            "could not load",
        ),
        # Nothing pending and the table is not ours: it dropped the list.
        (
            {"session_token": 0, "reload_pending": False, "reload_ok": True},
            WiCANSessionStale,
            "dropped the channel list",
        ),
        # Another NC Flash began its own session meanwhile.
        (
            {"session_token": 7, "session_list": True, "reload_pending": False},
            WiCANSessionStale,
            "another NC Flash",
        ),
    ],
)
def test_wait_applied_stops_at_once_when_the_list_is_not_ours(
    device, status, error, text
):
    device.statuses = [status]
    with pytest.raises(error, match=text) as failure:
        _client(device).wait_applied(42)
    assert type(failure.value) is error
    assert device.requests == ["GET /poll_status"]  # no 25 s wait


def _timed_client(device):
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    return WiCANSessionList(
        "127.0.0.1", http_port=device.port, sleep=sleep, clock=lambda: now[0]
    )


def test_wait_applied_gives_up_and_can_be_stopped(device):
    # The WiCAN can't close its open trip: the reload stays pending.
    device.statuses = [{"session_token": 0, "reload_pending": True}]
    client = _timed_client(device)
    with pytest.raises(WiCANAutoPidError, match="right now"):
        client.wait_applied(42, timeout_s=2)
    # A stop asked meanwhile ends the wait without an error.
    status = client.wait_applied(42, should_stop=lambda: True)
    assert status["reload_pending"] is True


def test_wait_applied_renews_the_lease_while_it_waits(device):
    # 21 s to close the open trip, on a 30 s lease: without renewals the
    # lease would lapse 9 s after the run started.
    client = _timed_client(device)
    token = client.begin(ROWS)
    device.statuses = [{"session_token": 0, "reload_pending": True}] * 42 + [
        {"session_token": token, "reload_pending": False, "reload_ok": True}
    ]
    client.wait_applied(token)
    renewals = [r for r in device.requests if "op=keepalive" in r]
    assert renewals == [f"POST /datalog_session?op=keepalive&token={token}"] * 2


def test_wait_applied_stops_when_the_lease_is_gone(device):
    client = _timed_client(device)
    token = client.begin(ROWS)
    device.statuses = [{"session_token": 0, "reload_pending": True}]
    device.token = 0  # the WiCAN rebooted
    with pytest.raises(WiCANSessionStale):
        client.wait_applied(token)


def test_wait_applied_survives_a_failed_status_call(device):
    device.statuses = [
        {"session_token": 0, "reload_pending": True},
        (503, {"err": "busy closing trip"}),
        {"session_token": 42, "reload_pending": False, "reload_ok": True},
    ]
    assert _client(device).wait_applied(42)["session_token"] == 42
    # ...but a WiCAN that never answers is a network failure, not a refusal.
    device.statuses = [(503, "")]
    with pytest.raises(wican_http.WiCANHttpError):
        _timed_client(device).wait_applied(42, timeout_s=2)


def test_a_list_that_never_loads_is_reported_as_refused(device):
    # Firmware without session_token: only the timeout can tell.
    device.statuses = [
        {"session_list": False, "reload_pending": False, "reload_ok": False}
    ]
    with pytest.raises(WiCANAutoPidError, match="could not load"):
        _timed_client(device).wait_applied(42, timeout_s=2)


def test_any_409_on_a_token_that_is_not_busy_means_stale(device):
    client = _client(device)
    token = client.begin(ROWS)
    for reply in ((409, "Conflict"), (409, {"ok": False}), (409, {"error": "gone"})):
        device.refuse_token_op = reply
        with pytest.raises(WiCANSessionStale):
            client.keepalive(token)
        assert client.end(token) is False
    device.refuse_token_op = (409, {"ok": False, "error": "busy", "msg": "sleeping"})
    with pytest.raises(WiCANSessionBusy):
        client.keepalive(token)


def test_http_errors_carry_the_status_and_reply(device):
    with pytest.raises(wican_http.WiCANHttpError) as failure:
        wican_http.post_json(f"http://127.0.0.1:{device.port}/nope", {})
    assert failure.value.status == 404
    assert "Nothing matches" in failure.value.body
    # The message existing callers show is unchanged.
    assert str(failure.value).endswith("HTTP 404 Nothing matches the given URI")
    with pytest.raises(wican_http.WiCANHttpError) as failure:
        wican_http.get_json(f"http://127.0.0.1:{device.port}/nope")
    assert failure.value.status == 404
