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
            return 200, status
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
    device.refuse = (409, {"ok": False, "error": "inactive", "msg": "poll_log off"})
    with pytest.raises(WiCANAutoPidError) as inactive:
        client.begin(ROWS)
    assert not isinstance(inactive.value, (WiCANSessionBusy, WiCANSessionStale))
    assert "logger is not running" in str(inactive.value)
    device.refuse = (400, {"ok": False, "error": "bad_request", "msg": "no pids"})
    with pytest.raises(WiCANAutoPidError, match="no pids"):
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


def test_wait_applied_until_the_temporary_list_is_polled(device):
    device.statuses = [
        # reload_ok / pids_unpollable are stale while a reload is pending.
        {"session_list": False, "reload_pending": True, "reload_ok": False},
        {"session_list": True, "reload_pending": True, "pids_unpollable": 3},
        # The reload of the saved list finished earlier; ours is not loaded yet.
        {"session_list": False, "reload_pending": False, "reload_ok": True},
        {"session_list": True, "reload_pending": True},
        {"session_list": True, "reload_pending": False, "reload_ok": True},
    ]
    assert _client(device).wait_applied()["session_list"] is True
    assert device.statuses == device.statuses[-1:]


@pytest.mark.parametrize(
    "status,text",
    [
        ({"session_list": False, "reload_pending": False, "reload_ok": False}, "load"),
        (
            {
                "session_list": True,
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
        _client(device).wait_applied()


def test_wait_applied_gives_up_and_can_be_stopped(device):
    device.statuses = [{"session_list": False, "reload_pending": True}]
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    client = WiCANSessionList(
        "127.0.0.1", http_port=device.port, sleep=sleep, clock=lambda: now[0]
    )
    with pytest.raises(WiCANAutoPidError, match="right now"):
        client.wait_applied(timeout_s=2)
    # A stop asked meanwhile ends the wait without an error.
    assert client.wait_applied(should_stop=lambda: True)["reload_pending"] is True


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
