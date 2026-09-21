"""
Tests for src/ecu/wican_config.py — read-only device HTTP + datalog coordination.

The module under test no longer writes device config at all (issue #99: the
firmware has one mode, so there is nothing to switch and nothing that could
strand a device). What remains, and what these tests cover:

  * ``read_config_raw`` — returns the device blob VERBATIM, with no json
    round-trip that could reorder keys or retype values. The mock's config
    deliberately carries a secret-looking ``sta_pass``, so a change that starts
    reserialising or echoing the body shows up here.
  * ``host_caps`` — the firmware's host contract, and its soft degradation to
    ``None`` on a missing, garbage, or unreachable endpoint.
  * ``WiCANDatalogClient`` — bus reservation, leases, keepalive and the
    crash-recovery breadcrumb. These are brick-safety invariants: they decide
    whether the user's datalogger is running while the host owns the CAN bus.

Everything runs headless over 127.0.0.1 — no hardware, no PySide6.
"""

import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from src.ecu.wican_config import WiCANConfigurator

# A realistic, mostly-flat config blob. Critically it includes:
#   * a top-level "protocol" (the ONLY field we may change),
#   * the home_/drive_/batt_alert_ sibling protocols that must NOT be touched,
#   * a secret-looking "sta_pass" that must survive byte-for-byte.
# It is intentionally hand-written raw text (not json.dumps) to mirror the
# device's own ordering/spacing, which our targeted regex edit must preserve.
REALISTIC_CONFIG = (
    '{"protocol": "poll_log", "home_protocol": "auto_pid", '
    '"drive_protocol": "realdash", "batt_alert_protocol": "mqtt", '
    '"sta_ssid": "MyNetwork", "sta_pass": "hunter2", '
    '"mqtt_user": "caruser", "mqtt_pass": "s3cr3t!", '
    '"can_bitrate": "500", "ble_status": "1"}'
)


# ---------------------------------------------------------------------------
# Mock WiCAN HTTP server: GET /load_config, POST /store_config (+ reboot sim).
# ---------------------------------------------------------------------------


class _MockWiCANServer:
    """In-process HTTP server emulating the WiCAN's READ endpoints.

    GET-only, deliberately: the host has no way to write device config any more
    (issue #99), so there is no ``/store_config`` handler here to make that look
    possible. ``config`` is the raw text served at ``/load_config``.
    """

    def __init__(self, config: str):
        self.config = config
        #: Body served at /host_caps. None => 404, i.e. every build in the field
        #: today, which predates the endpoint.
        self.host_caps_body = None

        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence test output
                pass

            def _send_text(self, code, text):
                payload = text.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                if self.path == "/host_caps":
                    if server.host_caps_body is None:
                        self._send_text(404, "not found")
                    else:
                        self._send_text(200, server.host_caps_body)
                    return
                if self.path != "/load_config":
                    self._send_text(404, "not found")
                    return
                self._send_text(200, server.config)

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def _make_configurator(server, **kwargs):
    """Build a WiCANConfigurator pointed at the mock, with a short timeout."""
    kwargs.setdefault("timeout_s", 2.0)
    return WiCANConfigurator("127.0.0.1", http_port=server.port, **kwargs)


# ---------------------------------------------------------------------------
# Crash-recovery sidecar + slcan_session() context manager.
#
# These NEVER touch the real OS temp dir: ``_recovery_in_tmp`` monkeypatches
# tempfile.gettempdir() to a per-test temp dir so a stray sidecar can't survive
# a test run or collide with a real bench run.
# ---------------------------------------------------------------------------


@pytest.fixture
def _recovery_in_tmp(tmp_path, monkeypatch):
    """Point the recovery sidecar at an isolated temp dir for the test."""
    import src.ecu.wican_config as mod

    # The sidecars now live under ~/.nc-flash (a temp clean must not destroy the
    # only record of a stranded device). Redirect BOTH the new home and the
    # legacy OS-temp location so a test can never touch either real directory.
    legacy = tmp_path / "legacy_temp"
    legacy.mkdir(exist_ok=True)
    monkeypatch.setattr(mod, "_sidecar_dir", lambda: str(tmp_path))
    monkeypatch.setattr(mod.tempfile, "gettempdir", lambda: str(legacy))
    return tmp_path


# ---------------------------------------------------------------------------
# WiCANDatalogClient — no-reboot coexistence REST /datalog (firmware #36.C).
#
# The contract is failure-tolerant: a port-only build (404), a timeout, or an
# unreachable device must degrade to None and NEVER raise into the flash path
# (the firmware FLASH_ACTIVE_BIT interlock is the real brick guard). Tests stand
# up an in-process loopback /datalog server (never a real device).
# ---------------------------------------------------------------------------


class _MockDatalogServer:
    """In-process HTTP server emulating the firmware ``/datalog`` endpoint.

    ``parked`` and ``flash_active`` model the device's reported state; POSTing
    pause/resume flips ``parked``. ``mode`` selects degraded behaviours:
    ``"ok"`` (200 JSON), ``"404"`` (no /datalog — a port-only build),
    ``"garbage"`` (200 non-JSON), or ``"500"``.
    """

    def __init__(self, mode: str = "ok", flash_active: bool = False):
        self.mode = mode
        self.parked = False
        self.flash_active = flash_active
        self.claimed = False
        self.park_token = None
        self.claim_token = None
        self._next_token = 1000
        self.keepalive_count = 0
        self.requests: list[tuple[str, str]] = []  # (method, path)

        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _state_json(self):
                return json.dumps(
                    {
                        "ok": True,
                        "flash_active": server.flash_active,
                        "datalog_parked": server.parked,
                        "host_bus_claimed": server.claimed,
                        "manual_mode": "off" if server.parked else "auto",
                        "park_token": server.park_token,
                        "claim_token": server.claim_token,
                        "lease_ttl_ms": 12000,
                        "claim_ttl_ms": 75000,
                        "bus_idle_ms": 0,
                        "stuck_flash_alarm": False,
                    }
                )

            def _send(self, code, body):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _reply(self, code=200):
                if server.mode == "404":
                    self._send(404, b"not found")
                elif server.mode == "500":
                    self._send(500, b"err")
                elif server.mode == "garbage":
                    self._send(200, b"not json at all")
                else:
                    self._send(code, self._state_json().encode("utf-8"))

            def do_GET(self):
                server.requests.append(("GET", self.path))
                self._reply()

            def do_POST(self):
                server.requests.append(("POST", self.path))
                if server.mode != "ok":
                    self._reply()
                    return
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                op = qs.get("op", [""])[0]
                token = qs.get("token", [None])[0]
                code = 200
                if op == "pause":
                    server.parked = True
                    server._next_token += 1
                    server.park_token = server._next_token
                elif op == "resume":
                    # Token-matched: a stale token (the reaper already reset it) -> 409.
                    if token is not None and str(server.park_token) != token:
                        code = 409
                    else:
                        server.parked = False
                        server.park_token = None
                elif op == "bus_claim":
                    server.claimed = True
                    server._next_token += 1
                    server.claim_token = server._next_token
                elif op == "bus_release":
                    if token is not None and str(server.claim_token) != token:
                        code = 409
                    else:
                        server.claimed = False
                        server.claim_token = None
                elif op == "keepalive":
                    server.keepalive_count += 1
                self._reply(code)

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def _datalog_client(server, **kwargs):
    from src.ecu.wican_config import WiCANDatalogClient

    kwargs.setdefault("timeout_s", 2.0)
    # Long keepalive interval by default so the background daemon never ticks mid-test;
    # the keepalive lifecycle tests override it with a short interval explicitly.
    kwargs.setdefault("keepalive_interval_s", 60.0)
    return WiCANDatalogClient("127.0.0.1", http_port=server.port, **kwargs)


def test_datalog_pause_posts_and_returns_state_and_writes_breadcrumb(_recovery_in_tmp):
    with _MockDatalogServer("ok") as server:
        client = _datalog_client(server)
        state = client.pause()
        assert state is not None
        assert state["datalog_parked"] is True and state["flash_active"] is False
        assert ("POST", "/datalog?op=pause") in server.requests
        # Breadcrumb is written for crash recovery.
        assert os.path.exists(client.recovery_path)
        client.close()  # stop the keepalive daemon pause() started


def test_datalog_resume_clears_breadcrumb_on_success(_recovery_in_tmp):
    with _MockDatalogServer("ok") as server:
        client = _datalog_client(server)
        client.pause()
        assert os.path.exists(client.recovery_path)
        state = client.resume()
        assert state is not None and state["datalog_parked"] is False
        # Resume is now token-matched: the path carries the firmware-issued park token.
        assert any(
            m == "POST" and p.startswith("/datalog?op=resume")
            for m, p in server.requests
        )
        assert not os.path.exists(client.recovery_path)


def test_datalog_get_state(_recovery_in_tmp):
    with _MockDatalogServer("ok", flash_active=True) as server:
        client = _datalog_client(server)
        state = client.get_state()
        assert state is not None and state["flash_active"] is True
        assert ("GET", "/datalog") in server.requests


def _count_ops(server):
    """Counter of /datalog ``op`` values seen over POST."""
    import urllib.parse
    from collections import Counter

    ops = Counter()
    for method, path in server.requests:
        if method == "POST":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
            ops[q.get("op", [""])[0]] += 1
    return ops


def test_reserved_refcounts_so_nesting_never_double_claims(_recovery_in_tmp):
    """The whole-session reservation and the flash fence share ONE client, so
    reserved() MUST claim/pause exactly once (0->1) and release/resume exactly once
    (1->0). A second bus_claim on the single-owner firmware lease would brick a flash
    by stranding the reaper on a stale token, so this is a brick-safety invariant."""
    with _MockDatalogServer("ok") as server:
        client = _datalog_client(server)
        try:
            with client.reserved():  # outer == the whole-session reservation
                with client.reserved():  # inner == the flash _datalog_fence
                    assert server.claimed is True and server.parked is True
                # Inner exit must NOT release — the outer still holds it.
                assert server.claimed is True and server.parked is True
            # Outer exit releases the single reservation.
            assert server.claimed is False and server.parked is False
        finally:
            client.close()
        ops = _count_ops(server)
        assert ops["bus_claim"] == 1 and ops["pause"] == 1
        assert ops["bus_release"] == 1 and ops["resume"] == 1


def test_reserved_releases_on_exception(_recovery_in_tmp):
    """An exception inside the reservation still releases the bus (the reaper must
    never be left fenced on a host error)."""
    with _MockDatalogServer("ok") as server:
        client = _datalog_client(server)
        try:
            with pytest.raises(ValueError):
                with client.reserved():
                    assert server.claimed is True and server.parked is True
                    raise ValueError("boom")
            assert server.claimed is False and server.parked is False
        finally:
            client.close()


def test_release_bus_without_acquire_is_a_noop(_recovery_in_tmp):
    """A stray release (depth already 0) must not underflow or hit the wire."""
    with _MockDatalogServer("ok") as server:
        client = _datalog_client(server)
        client.release_bus()  # never acquired
        assert server.requests == []
        assert client._reserve_depth == 0


def test_datalog_404_soft_degrades_to_none_and_never_raises(_recovery_in_tmp):
    """A port-only NCFRv6 build (no /datalog) must degrade, not abort a flash."""
    with _MockDatalogServer("404") as server:
        client = _datalog_client(server)
        assert client.pause() is None
        assert client.resume() is None
        assert client.get_state() is None
        # pause still wrote the breadcrumb (before the request); a failed resume
        # must NOT clear it (nothing was confirmed).
        assert os.path.exists(client.recovery_path)


def test_datalog_500_and_garbage_degrade_to_none(_recovery_in_tmp):
    with _MockDatalogServer("500") as server:
        assert _datalog_client(server).get_state() is None
    with _MockDatalogServer("garbage") as server:
        assert _datalog_client(server).get_state() is None


def test_datalog_unreachable_host_degrades_to_none(_recovery_in_tmp):
    from src.ecu.wican_config import WiCANDatalogClient

    # Port 1 is not listening; the client must swallow the connection error.
    client = WiCANDatalogClient("127.0.0.1", http_port=1, timeout_s=0.3)
    assert client.pause() is None
    assert client.resume() is None
    assert client.get_state() is None


def test_datalog_reconcile_noop_without_breadcrumb(_recovery_in_tmp):
    with _MockDatalogServer("ok") as server:
        client = _datalog_client(server)
        client.reconcile()
        # No breadcrumb -> no HTTP traffic at all.
        assert server.requests == []


def test_datalog_reconcile_resumes_when_no_active_flash(_recovery_in_tmp):
    with _MockDatalogServer("ok", flash_active=False) as server:
        client = _datalog_client(server)
        client._mark_stopped()  # simulate a prior run that paused then died
        server.parked = True
        client.reconcile()
        assert ("POST", "/datalog?op=resume") in server.requests
        assert server.parked is False
        assert not os.path.exists(client.recovery_path)


def test_datalog_reconcile_skips_resume_when_flash_active(_recovery_in_tmp):
    """Two-instance guard: never resume while another instance is mid-flash."""
    with _MockDatalogServer("ok", flash_active=True) as server:
        client = _datalog_client(server)
        client._mark_stopped()
        server.parked = True
        client.reconcile()
        # GET happened (the guard check) but NO resume POST.
        assert ("GET", "/datalog") in server.requests
        assert all(r != ("POST", "/datalog?op=resume") for r in server.requests)
        # Breadcrumb is left for the owning instance to clear.
        assert os.path.exists(client.recovery_path)


def test_datalog_pause_writes_breadcrumb_before_request(_recovery_in_tmp):
    """Even if the pause request fails, the breadcrumb must already be on disk so a
    crash mid-flash is reconciled next connect."""
    with _MockDatalogServer("500") as server:
        client = _datalog_client(server)
        assert client.pause() is None  # request failed
        assert os.path.exists(client.recovery_path)  # but breadcrumb persisted


# --- Dead-man's-switch: bus-claim, leases, keepalive (docs/WICAN_DEADMAN_AUTORESUME) ---


def test_datalog_bus_claim_and_release_roundtrip(_recovery_in_tmp):
    """bus_claim raises the host claim (the auth-window brick fence); bus_release drops it."""
    with _MockDatalogServer("ok") as server:
        client = _datalog_client(server)
        state = client.bus_claim()
        assert state is not None and state["host_bus_claimed"] is True
        assert server.claimed is True and server.claim_token is not None
        assert client._claim_token == server.claim_token
        client.bus_release()
        assert server.claimed is False
        assert client._claim_token is None
        client.close()


def test_datalog_bus_claim_404_soft_degrades(_recovery_in_tmp):
    """A pre-deadman build (no bus_claim op) must degrade, not abort a flash."""
    with _MockDatalogServer("404") as server:
        client = _datalog_client(server)
        assert client.bus_claim() is None
        assert client._claim_token is None
        assert client.bus_release() is None  # also harmless


def test_datalog_resume_409_already_reaped_is_success(_recovery_in_tmp):
    """If the firmware reaper already auto-resumed (host had vanished), the host's
    later resume gets a 409 — that is SUCCESS: clear the breadcrumb, never raise."""
    with _MockDatalogServer("ok") as server:
        client = _datalog_client(server)
        client.pause()  # issues park_token
        assert os.path.exists(client.recovery_path)
        # Simulate the reaper having reset the lease after we 'vanished'.
        server.parked = False
        server.park_token = None
        state = client.resume()  # sends our now-stale token -> 409
        assert state is None  # 409 carries no usable body
        assert not os.path.exists(client.recovery_path)  # treated as done
        assert any(m == "POST" and "op=resume" in p for m, p in server.requests)
        client.close()


def test_datalog_reconcile_skips_when_host_bus_claimed(_recovery_in_tmp):
    """The auth window (host_bus_claimed, FLASH_ACTIVE_BIT still clear) is exactly the
    danger zone the old reconcile wrongly thought safe — it must NOT resume into it."""
    with _MockDatalogServer("ok") as server:
        server.claimed = True  # a live programming session owns the bus
        client = _datalog_client(server)
        client._mark_stopped()
        server.parked = True
        client.reconcile()
        assert ("GET", "/datalog") in server.requests
        assert all("op=resume" not in p for _m, p in server.requests)
        assert os.path.exists(client.recovery_path)  # left for the owner / reaper


def test_datalog_keepalive_renews_both_leases_real_thread(_recovery_in_tmp):
    """REAL (not mocked) keepalive daemon renews BOTH leases while held, and STOPS once
    both are released (per feedback_qthread_real_test: exercise the live thread)."""
    with _MockDatalogServer("ok") as server:
        client = _datalog_client(server, keepalive_interval_s=0.05)
        client.bus_claim()
        client.pause()
        time.sleep(0.3)  # let the live daemon tick several times
        assert server.keepalive_count >= 1
        ka_paths = [p for m, p in server.requests if "op=keepalive" in p]
        # At least one tick (after both calls landed) renewed BOTH leases at once.
        assert any("park_token=" in p and "claim_token=" in p for p in ka_paths)
        # Release both -> the daemon must stop ticking and the thread must exit.
        client.bus_release()
        client.resume()
        time.sleep(0.05)
        settled = server.keepalive_count
        time.sleep(0.2)
        assert server.keepalive_count == settled  # no ticks after release
        assert client._ka_thread is None


def test_datalog_keepalive_stops_on_close(_recovery_in_tmp):
    """close() (session teardown / window close) must stop the keepalive daemon so a
    disconnected flasher never leaves a host thread renewing a lease."""
    with _MockDatalogServer("ok") as server:
        client = _datalog_client(server, keepalive_interval_s=0.05)
        client.pause()
        time.sleep(0.12)
        assert client._ka_thread is not None and client._ka_thread.is_alive()
        client.close()
        assert client._ka_thread is None
        assert client._park_token is None and client._claim_token is None


# ---------------------------------------------------------------------------
# /host_caps: the small, secret-free capability reply (#92).
#
# It exists so a host tool can settle "does this device support no-reboot
# flashing?" by reading an integer, instead of inferring it from which socket
# error arrived first on a marginal link -- an inference that was measured wrong
# on Windows. Absent on every build in the field today, so "no endpoint" must
# never be read as evidence about the firmware.
# ---------------------------------------------------------------------------


def test_read_config_raw_returns_the_device_text_verbatim():
    """No json round-trip: the blob is returned byte-for-byte as the device sent
    it, so nothing can silently reorder keys or retype values."""
    with _MockWiCANServer(REALISTIC_CONFIG) as server:
        assert _make_configurator(server).read_config_raw() == REALISTIC_CONFIG


def test_read_config_raw_raises_on_unreachable_host():
    from src.ecu.wican_config import WiCANConfigError

    cfg = WiCANConfigurator("127.0.0.1", http_port=9, timeout_s=0.4)
    with pytest.raises(WiCANConfigError):
        cfg.read_config_raw()


def test_wican_config_error_is_an_ecu_error():
    """It must be caught by the app's unified ECU error handlers."""
    from src.ecu.exceptions import ECUError
    from src.ecu.wican_config import WiCANConfigError

    assert issubclass(WiCANConfigError, ECUError)


def test_host_caps_parses_a_good_reply():
    with _MockWiCANServer(REALISTIC_CONFIG) as server:
        server.host_caps_body = '{"ncfr_rev": 6, "protocol": "poll_log"}'
        caps = _make_configurator(server).host_caps()
    assert caps == {"ncfr_rev": 6, "protocol": "poll_log"}


def test_host_caps_returns_none_when_absent():
    # Today's firmware: 404. Must be None, NOT an exception and NOT a verdict.
    with _MockWiCANServer(REALISTIC_CONFIG) as server:
        assert server.host_caps_body is None
        assert _make_configurator(server).host_caps() is None


def test_host_caps_returns_none_on_garbage():
    with _MockWiCANServer(REALISTIC_CONFIG) as server:
        server.host_caps_body = "<html>not json</html>"
        assert _make_configurator(server).host_caps() is None


def test_host_caps_returns_none_on_a_json_non_object():
    with _MockWiCANServer(REALISTIC_CONFIG) as server:
        server.host_caps_body = "[1, 2, 3]"
        assert _make_configurator(server).host_caps() is None


def test_host_caps_never_raises_when_the_device_is_gone():
    cfg = WiCANConfigurator("127.0.0.1", http_port=9, timeout_s=0.4)
    assert cfg.host_caps() is None


# ---------------------------------------------------------------------------
# Upgrading from a pre-#92 build: sidecars used to live in the OS temp dir.
# An upgrade must adopt them, or a device the OLD build stranded could never be
# recovered automatically.
# ---------------------------------------------------------------------------
