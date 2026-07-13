"""
Tests for src/ecu/wican_config.py — the WiCAN HTTP config auto-switch.

This module REWRITES the user's entire device config (which holds their WiFi /
MQTT passwords in plaintext) and reboots the device, so the cardinal rule is:
change ONLY the top-level ``protocol`` token and preserve every other byte. The
tests below stand up a realistic in-process mock HTTP server on loopback (never
a real device) whose config includes the ``home_/drive_/batt_alert_protocol``
sibling keys and a secret-looking ``sta_pass`` field, then assert byte-level
that those survive a switch.

Everything runs headless over 127.0.0.1 — no hardware, no PySide6.
"""

import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from src.ecu.exceptions import ECUError
from src.ecu.wican_config import (
    WiCANConfigError,
    WiCANConfigurator,
    get_top_level_protocol,
    set_top_level_protocol,
)

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
    """In-process HTTP server emulating the WiCAN /load_config + /store_config.

    ``config`` holds the current raw config text. POSTing stores the body
    verbatim (mirroring the firmware, which does NOT reserialize). The optional
    ``fail_gets_after_post`` knob makes the first N GETs after a POST return 503
    to emulate the device being unreachable mid-reboot.
    """

    def __init__(self, config: str):
        self.config = config
        self.posts: list[str] = []
        self.fail_gets_after_post = 0
        self._pending_get_failures = 0
        self.always_fail_get = False

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
                if self.path != "/load_config":
                    self._send_text(404, "not found")
                    return
                if server.always_fail_get or server._pending_get_failures > 0:
                    if server._pending_get_failures > 0:
                        server._pending_get_failures -= 1
                    self._send_text(503, "rebooting")
                    return
                self._send_text(200, server.config)

            def do_POST(self):
                if self.path != "/store_config":
                    self._send_text(404, "not found")
                    return
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                server.posts.append(body)
                server.config = body  # store verbatim, like the firmware
                server._pending_get_failures = server.fail_gets_after_post
                self._send_text(200, "Configuration saved! Rebooting...")

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
    """Build a WiCANConfigurator pointed at the mock, with fast polling."""
    kwargs.setdefault("timeout_s", 2.0)
    kwargs.setdefault("reboot_timeout_s", 5.0)
    kwargs.setdefault("poll_interval_s", 0.05)
    return WiCANConfigurator("127.0.0.1", http_port=server.port, **kwargs)


# ---------------------------------------------------------------------------
# set_top_level_protocol / get_top_level_protocol — pure text unit tests.
# ---------------------------------------------------------------------------


def test_set_top_level_protocol_exactly_one_match():
    out = set_top_level_protocol(REALISTIC_CONFIG, "slcan")
    assert '"protocol": "slcan"' in out
    assert '"protocol": "poll_log"' not in out


def test_set_top_level_protocol_does_not_touch_siblings():
    out = set_top_level_protocol(REALISTIC_CONFIG, "slcan")
    # The sibling protocol keys and their values must be byte-identical.
    assert '"home_protocol": "auto_pid"' in out
    assert '"drive_protocol": "realdash"' in out
    assert '"batt_alert_protocol": "mqtt"' in out


def test_set_top_level_protocol_preserves_secrets_and_surrounding_text():
    out = set_top_level_protocol(REALISTIC_CONFIG, "slcan")
    # Only the protocol token changed: every other field is byte-identical.
    expected = REALISTIC_CONFIG.replace('"protocol": "poll_log"', '"protocol": "slcan"')
    assert out == expected
    assert '"sta_pass": "hunter2"' in out
    assert '"mqtt_pass": "s3cr3t!"' in out


def test_set_top_level_protocol_raises_when_no_top_level_protocol():
    # A body with ONLY sibling protocol keys must not match.
    body = (
        '{"home_protocol": "auto_pid", "drive_protocol": "realdash", '
        '"batt_alert_protocol": "mqtt"}'
    )
    with pytest.raises(WiCANConfigError):
        set_top_level_protocol(body, "slcan")
    with pytest.raises(WiCANConfigError):
        get_top_level_protocol(body)


def test_set_top_level_protocol_raises_on_multiple_top_level_matches():
    body = '{"protocol": "a", "x": 1, "protocol": "b"}'
    with pytest.raises(WiCANConfigError):
        set_top_level_protocol(body, "slcan")


def test_get_top_level_protocol_ignores_siblings():
    assert get_top_level_protocol(REALISTIC_CONFIG) == "poll_log"


def test_set_top_level_protocol_value_with_special_chars_is_literal():
    # A replacement value containing a backslash/group-ref must be inserted
    # literally (no regex backreference expansion).
    out = set_top_level_protocol('{"protocol": "x"}', r"a\1b")
    assert get_top_level_protocol(out) == r"a\1b"


# ---------------------------------------------------------------------------
# switch_to_slcan / restore — full round trips against the mock server.
# ---------------------------------------------------------------------------


def test_switch_to_slcan_returns_previous_and_preserves_all_fields():
    with _MockWiCANServer(REALISTIC_CONFIG) as server:
        cfg = _make_configurator(server)

        previous = cfg.switch_to_slcan()

        assert previous == "poll_log"
        assert cfg.current_protocol() == "slcan"

        # Exactly one POST happened, and it changed ONLY the protocol token.
        assert len(server.posts) == 1
        stored = server.posts[0]
        expected = REALISTIC_CONFIG.replace(
            '"protocol": "poll_log"', '"protocol": "slcan"'
        )
        assert stored == expected

        # Byte-level: every non-protocol field survived unchanged.
        stored_obj = json.loads(stored)
        assert stored_obj["sta_pass"] == "hunter2"
        assert stored_obj["mqtt_pass"] == "s3cr3t!"
        assert stored_obj["home_protocol"] == "auto_pid"
        assert stored_obj["drive_protocol"] == "realdash"
        assert stored_obj["batt_alert_protocol"] == "mqtt"


def test_switch_to_slcan_idempotent_when_already_slcan():
    already = REALISTIC_CONFIG.replace('"protocol": "poll_log"', '"protocol": "slcan"')
    with _MockWiCANServer(already) as server:
        cfg = _make_configurator(server)

        previous = cfg.switch_to_slcan()

        assert previous == "slcan"
        assert cfg.is_slcan() is True
        # No write should ever happen when already in slcan mode.
        assert server.posts == []


def test_restore_round_trips_back_to_previous():
    with _MockWiCANServer(REALISTIC_CONFIG) as server:
        cfg = _make_configurator(server)

        previous = cfg.switch_to_slcan()
        assert cfg.current_protocol() == "slcan"

        cfg.restore(previous)

        assert cfg.current_protocol() == "poll_log"
        assert len(server.posts) == 2
        # The restored config is byte-identical to the original.
        assert server.config == REALISTIC_CONFIG


def test_restore_is_noop_when_already_target():
    with _MockWiCANServer(REALISTIC_CONFIG) as server:
        cfg = _make_configurator(server)

        cfg.restore("poll_log")  # device is already poll_log

        assert server.posts == []
        assert cfg.current_protocol() == "poll_log"


def test_read_config_returns_parsed_dict():
    with _MockWiCANServer(REALISTIC_CONFIG) as server:
        cfg = _make_configurator(server)
        parsed = cfg.read_config()
        assert parsed["protocol"] == "poll_log"
        assert parsed["sta_pass"] == "hunter2"


# ---------------------------------------------------------------------------
# Reboot-window tolerance and timeout.
# ---------------------------------------------------------------------------


def test_wait_for_protocol_tolerates_transient_503_after_post():
    with _MockWiCANServer(REALISTIC_CONFIG) as server:
        # First two GETs after the POST return 503 (device rebooting), then OK.
        server.fail_gets_after_post = 2
        cfg = _make_configurator(server)

        previous = cfg.switch_to_slcan()

        assert previous == "poll_log"
        assert cfg.current_protocol() == "slcan"


def test_wait_for_protocol_times_out_when_never_ready():
    with _MockWiCANServer(REALISTIC_CONFIG) as server:
        cfg = _make_configurator(server, reboot_timeout_s=0.3, poll_interval_s=0.05)
        # Device accepts the POST but every subsequent GET fails (never reboots
        # back). The verify wait must raise rather than hang or lie.
        server.always_fail_get = True

        with pytest.raises(WiCANConfigError):
            cfg.switch_to_slcan()


# ---------------------------------------------------------------------------
# Defensive write guards.
# ---------------------------------------------------------------------------


def test_set_protocol_rejects_edit_that_breaks_json_before_posting(monkeypatch):
    with _MockWiCANServer(REALISTIC_CONFIG) as server:
        cfg = _make_configurator(server)

        # Force the targeted edit helper to return non-JSON text. set_protocol
        # must catch this in its defensive parse-check and refuse to POST.
        import src.ecu.wican_config as mod

        monkeypatch.setattr(
            mod, "set_top_level_protocol", lambda raw, value: '{"protocol": '
        )

        with pytest.raises(WiCANConfigError):
            cfg.set_protocol("slcan")

        # Nothing was written to the device.
        assert server.posts == []


def test_read_config_raw_raises_on_unreachable_host():
    # Port 1 on loopback is not listening; the read must raise WiCANConfigError.
    cfg = WiCANConfigurator("127.0.0.1", http_port=1, timeout_s=1.0)
    with pytest.raises(WiCANConfigError):
        cfg.read_config_raw()


def test_wican_config_error_is_ecu_error():
    assert issubclass(WiCANConfigError, ECUError)


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

    monkeypatch.setattr(mod.tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path


def _slcan_config(base=REALISTIC_CONFIG):
    return base.replace('"protocol": "poll_log"', '"protocol": "slcan"')


def test_recovery_path_is_host_keyed_and_sanitized(_recovery_in_tmp):
    cfg = WiCANConfigurator("192.168.0.10", http_port=80)
    # The host is sanitized (dots -> underscores) and lives in our temp dir.
    assert cfg.recovery_path == os.path.join(
        str(_recovery_in_tmp), "wican_recovery_192_168_0_10.json"
    )


def test_read_recovery_tolerates_missing_file(_recovery_in_tmp):
    cfg = WiCANConfigurator("127.0.0.1", http_port=80)
    assert not os.path.exists(cfg.recovery_path)
    assert cfg.read_recovery() is None


def test_read_recovery_tolerates_corrupt_file(_recovery_in_tmp):
    cfg = WiCANConfigurator("127.0.0.1", http_port=80)
    with open(cfg.recovery_path, "w", encoding="utf-8") as fh:
        fh.write("{ this is not valid json")
    assert cfg.read_recovery() is None


def test_read_recovery_ignores_other_host_sidecar(_recovery_in_tmp):
    cfg = WiCANConfigurator("127.0.0.1", http_port=80)
    # A sidecar whose recorded host differs from ours must be ignored.
    with open(cfg.recovery_path, "w", encoding="utf-8") as fh:
        json.dump({"host": "10.0.0.9", "previous_protocol": "poll_log"}, fh)
    assert cfg.read_recovery() is None


def test_write_then_read_recovery_round_trips(_recovery_in_tmp):
    cfg = WiCANConfigurator("127.0.0.1", http_port=80)
    cfg._write_recovery("poll_log")
    assert cfg.read_recovery() == "poll_log"
    cfg.clear_recovery()
    assert cfg.read_recovery() is None
    # clear is idempotent / best-effort on a missing file.
    cfg.clear_recovery()


def test_slcan_session_normal_path(_recovery_in_tmp):
    """poll_log device: inside block it is slcan with a recovery file; after,
    it is poll_log again and the recovery file is gone."""
    with _MockWiCANServer(REALISTIC_CONFIG) as server:
        cfg = _make_configurator(server)
        assert cfg.read_recovery() is None

        with cfg.slcan_session() as prev:
            assert prev == "poll_log"
            # Inside the block the device is slcan ...
            assert cfg.current_protocol() == "slcan"
            # ... and a recovery sidecar records the TRUE original.
            assert os.path.exists(cfg.recovery_path)
            assert cfg.read_recovery() == "poll_log"

        # After the block the device is restored and the sidecar is gone.
        assert cfg.current_protocol() == "poll_log"
        assert not os.path.exists(cfg.recovery_path)
        assert cfg.read_recovery() is None


def test_slcan_session_recovers_true_original_after_crash(_recovery_in_tmp):
    """Crash sim: a recovery file (poll_log) pre-exists AND the device is already
    slcan. The session must restore poll_log from the RECORDED value (not the
    current slcan) and clear the file."""
    with _MockWiCANServer(_slcan_config()) as server:
        cfg = _make_configurator(server)
        # Pre-write the breadcrumb a crashed prior run would have left.
        cfg._write_recovery("poll_log")
        assert cfg.current_protocol() == "slcan"

        with cfg.slcan_session() as prev:
            # The TRUE original came from the sidecar, NOT the current slcan.
            assert prev == "poll_log"
            assert cfg.current_protocol() == "slcan"
            # No needless re-switch: device was already slcan.
            assert server.posts == []

        # Restored to the recorded original and the sidecar is cleared.
        assert cfg.current_protocol() == "poll_log"
        assert len(server.posts) == 1
        assert not os.path.exists(cfg.recovery_path)


def test_slcan_session_persists_breadcrumb_before_switch(_recovery_in_tmp, monkeypatch):
    """The recovery sidecar must be written BEFORE the device is switched, so a
    hard kill during the multi-second switch/reboot still leaves a breadcrumb
    the next run can use to restore the true original."""
    with _MockWiCANServer(REALISTIC_CONFIG) as server:
        cfg = _make_configurator(server)

        def boom(protocol):
            # At switch time the breadcrumb must already be on disk.
            assert os.path.exists(cfg.recovery_path)
            assert cfg.read_recovery() == "poll_log"
            raise RuntimeError("simulated crash during switch")

        monkeypatch.setattr(cfg, "set_protocol", boom)

        with pytest.raises(RuntimeError):
            with cfg.slcan_session():
                pass

        # The switch never committed (no POST) and the breadcrumb survives, so
        # the next run can still restore poll_log.
        assert server.posts == []
        assert cfg.read_recovery() == "poll_log"


def test_slcan_session_intentional_slcan_does_nothing(_recovery_in_tmp):
    """Device already slcan, NO recovery file: the session writes NO recovery
    file and performs NO restore (device stays slcan)."""
    with _MockWiCANServer(_slcan_config()) as server:
        cfg = _make_configurator(server)
        assert cfg.read_recovery() is None

        with cfg.slcan_session() as prev:
            assert prev == "slcan"
            assert cfg.current_protocol() == "slcan"
            # No sidecar was ever written.
            assert not os.path.exists(cfg.recovery_path)
            assert server.posts == []

        # Still slcan, no restore POST, and still no sidecar.
        assert cfg.current_protocol() == "slcan"
        assert server.posts == []
        assert not os.path.exists(cfg.recovery_path)


def test_slcan_session_restores_on_exception(_recovery_in_tmp):
    """An exception inside the block still restores the original and clears the
    sidecar (the finally must run)."""
    with _MockWiCANServer(REALISTIC_CONFIG) as server:
        cfg = _make_configurator(server)

        class _Boom(Exception):
            pass

        with pytest.raises(_Boom):
            with cfg.slcan_session() as prev:
                assert prev == "poll_log"
                assert cfg.current_protocol() == "slcan"
                raise _Boom()

        assert cfg.current_protocol() == "poll_log"
        assert not os.path.exists(cfg.recovery_path)


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
        # /csv_logger model (live-trip lifecycle): manual mode, the firmware
        # dead-man lease, and how many rotate=1 new-trip requests arrived.
        # mode "no_csv_auto" emulates pre-op=auto firmware (op=auto -> 400).
        self.manual_mode = "auto"
        self.lease_armed = False
        self.rotations = 0
        self.csv_ops: list[str] = []  # raw /csv_logger paths, in order

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
                if server.mode not in ("ok", "no_csv_auto", "no_csv_renew"):
                    self._reply()
                    return
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                op = qs.get("op", [""])[0]
                if urllib.parse.urlparse(self.path).path == "/csv_logger":
                    server.csv_ops.append(self.path)
                    if op == "start":
                        server.manual_mode = "on"
                        # Mirrors the firmware: an UNLEASED start disarms the lease.
                        server.lease_armed = "lease_ms" in qs
                        if qs.get("rotate", ["0"])[0] == "1":
                            server.rotations += 1
                    elif op == "stop":
                        server.manual_mode = "off"
                        server.lease_armed = False
                    elif op == "auto" and server.mode != "no_csv_auto":
                        server.manual_mode = "auto"
                        server.lease_armed = False
                    elif op == "renew" and server.mode != "no_csv_renew":
                        # Mirrors the firmware: a heartbeat that can NEVER restart a
                        # stopped trip — 409 once the trip is no longer manual-ON.
                        if server.manual_mode == "on":
                            server.lease_armed = True
                        else:
                            self._reply(409)
                            return
                    else:  # unknown op, or an op this firmware vintage predates
                        self._send(400, b"op must be start, stop or mark")
                        return
                    self._reply()
                    return
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


def test_live_trip_lifts_and_restores_the_session_reservation(_recovery_in_tmp):
    """A live trip needs the bus UN-parked (the poller produces the rows), but the
    logical session reservation must survive: begin lifts the physical leases without
    touching the refcount; end restores AUTO on the device and re-arms the park for
    the still-connected ECU session."""
    with _MockDatalogServer("ok") as server:
        client = _datalog_client(server)
        try:
            client.acquire_bus()  # the ECU session's whole-session reservation
            assert server.parked is True and server.claimed is True
            client.begin_live_trip()
            assert server.parked is False and server.claimed is False
            assert server.manual_mode == "on" and server.lease_armed is True
            assert server.rotations == 1  # every press = a NEW trip file
            client.end_live_trip()
            assert server.manual_mode == "auto" and server.lease_armed is False
            assert server.parked is True and server.claimed is True  # session re-parked
            client.release_bus()
            assert server.parked is False and server.claimed is False
        finally:
            client.close()


def test_session_disconnect_mid_trip_keeps_streaming(_recovery_in_tmp):
    """Dropping the LAST session ref while the physical leases are suspended for a
    live trip must neither POST a phantom resume (nothing is armed — the firmware
    would restore a stale pre-pause mode) nor re-park: the trip keeps running on its
    own csv-lease dead-man."""
    with _MockDatalogServer("ok") as server:
        client = _datalog_client(server)
        try:
            client.acquire_bus()
            client.begin_live_trip()
            resumes_before = _count_ops(server)["resume"]
            client.release_bus()  # ECU session disconnects mid-stream
            assert _count_ops(server)["resume"] == resumes_before
            assert server.manual_mode == "on"  # trip still running
            client.end_live_trip()
            assert server.manual_mode == "auto"
            assert server.parked is False  # no refs left: device fully free
        finally:
            client.close()


def test_stop_choreography_parks_the_device_silent(_recovery_in_tmp):
    """User stop: hold_silent FIRST (park while the manual trip still owns the mode —
    no instant of un-parked AUTO that could open a stub trip), then end_live_trip
    (op=auto fixes mode + restore snapshot). The device ends parked and stays silent
    until release_trip_hold. Both hold ops are idempotent."""
    with _MockDatalogServer("ok") as server:
        client = _datalog_client(server)
        try:
            client.begin_live_trip()
            client.hold_silent()
            client.end_live_trip()
            assert server.parked is True and server.claimed is True
            assert server.manual_mode == "auto" and server.lease_armed is False
            client.hold_silent()  # one-shot: no double-acquire
            client.release_trip_hold()
            assert server.parked is False and server.claimed is False
            client.release_trip_hold()  # idempotent
        finally:
            client.close()
        assert _count_ops(server)["pause"] == 1  # the hold armed exactly once


def test_keepalive_renews_the_trip_lease(_recovery_in_tmp):
    """While a trip runs, every keepalive tick POSTs op=renew&lease_ms — the
    heartbeat form that can NEVER restart a stopped trip. Re-POSTing op=start
    here is the field regression (web Stop lost to every 4 s tick); no tick may
    rotate the trip file either."""
    with _MockDatalogServer("ok") as server:
        client = _datalog_client(server, keepalive_interval_s=0.05)
        try:
            client.begin_live_trip()
            renewals = []
            deadline = time.time() + 5.0
            while time.time() < deadline:
                renewals = [
                    p for p in server.csv_ops if "op=renew" in p and "lease_ms" in p
                ]
                if len(renewals) >= 2:
                    break
                time.sleep(0.05)
            assert len(renewals) >= 2
            assert all("rotate" not in p for p in renewals)
            # The begin itself is the ONLY leased start; ticks never re-start.
            starts = [p for p in server.csv_ops if "op=start" in p]
            assert len(starts) == 1
        finally:
            client.end_live_trip()
            client.close()


def test_web_stop_mid_trip_wins_over_the_keepalive(_recovery_in_tmp):
    """Web-UI Stop Trip during a host live trip: the next heartbeat 409s, the
    one-shot callback fires, and NOTHING the host does afterwards may touch the
    mode — no re-start (the old fight) and no op=auto from end_live_trip (that
    would flip the operator's explicit OFF back to follow-ignition)."""
    with _MockDatalogServer("ok") as server:
        client = _datalog_client(server, keepalive_interval_s=0.05)
        stopped = threading.Event()
        try:
            client.begin_live_trip(on_external_stop=stopped.set)
            # The device operator presses Stop Trip (another client's op=stop).
            server.manual_mode = "off"
            server.lease_armed = False
            ops_at_stop = len(server.csv_ops)
            assert stopped.wait(5.0)
            client.end_live_trip()
            after = server.csv_ops[ops_at_stop:]
            assert all("op=start" not in p for p in after)  # the old fight
            assert all("op=auto" not in p for p in after)  # mode left alone
            assert server.manual_mode == "off"  # the operator's Stop stands
        finally:
            client.close()


def test_renew_legacy_firmware_falls_back_to_leased_start(_recovery_in_tmp):
    """Pre-renew firmware answers op=renew with 400: degrade to the old leased
    re-start heartbeat (keeping the dead-man) instead of losing renewal."""
    with _MockDatalogServer("no_csv_renew") as server:
        client = _datalog_client(server, keepalive_interval_s=0.05)
        try:
            client.begin_live_trip()
            deadline = time.time() + 5.0
            fallback_starts = []
            while time.time() < deadline:
                fallback_starts = [
                    p
                    for p in server.csv_ops
                    if "op=start" in p and "lease_ms" in p and "rotate" not in p
                ]
                if len(fallback_starts) >= 2:
                    break
                time.sleep(0.05)
            assert len(fallback_starts) >= 2
            # Feature detection is remembered: exactly one 400'd renew probe.
            assert len([p for p in server.csv_ops if "op=renew" in p]) == 1
            assert server.manual_mode == "on"
        finally:
            client.end_live_trip()
            client.close()


def test_end_live_trip_falls_back_to_stop_on_pre_auto_firmware(_recovery_in_tmp):
    """Pre-op=auto firmware answers 400: best effort is op=stop — a sticky OFF beats
    an orphaned FORCE_ON filling the SD until reboot."""
    with _MockDatalogServer("no_csv_auto") as server:
        client = _datalog_client(server)
        try:
            client.begin_live_trip()
            client.end_live_trip()
            assert server.manual_mode == "off"
        finally:
            client.close()


def test_get_datalog_client_shares_one_instance_per_device(_recovery_in_tmp):
    """The firmware issues a fresh lease token on every arm, so ALL lease holders
    (session reservation, flash fence, live trip) must share one client per device
    or their tokens clobber each other."""
    from src.ecu.wican_config import _datalog_clients, get_datalog_client

    _datalog_clients.clear()
    try:
        a = get_datalog_client("10.0.0.9")
        b = get_datalog_client("10.0.0.9")
        c = get_datalog_client("10.0.0.9", http_port=8080)
        assert a is b
        assert a is not c
    finally:
        _datalog_clients.clear()


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


def test_keepalive_failure_logs_quiet_during_bulk_transfer(_recovery_in_tmp, caplog):
    """During a bulk transfer window (trip-log download) keepalive-tick failures
    log at DEBUG instead of INFO — during a large download against the device's
    single-task httpd every tick times out by construction, and the repeated
    INFO line reads as an incident (field report 2026-07-12). Logging-only: the
    tick still fires either way, and the level is restored after the window."""
    import logging as _logging

    from src.ecu.wican_config import WiCANDatalogClient

    # Port 1 is not listening -> every tick fails like a busy/absent httpd.
    client = WiCANDatalogClient("127.0.0.1", http_port=1, timeout_s=0.3)
    client._park_token = 7  # as if a pause() lease were held

    def _tick_levels():
        caplog.clear()
        client._send_keepalive()  # direct tick; no daemon needed
        return [
            r.levelno
            for r in caplog.records
            if "relying on firmware interlock" in r.getMessage()
        ]

    try:
        with caplog.at_level(_logging.DEBUG, logger="src.ecu.wican_config"):
            levels = _tick_levels()
            assert levels and set(levels) == {_logging.INFO}

            client.set_bulk_transfer(True)
            levels = _tick_levels()
            assert levels and set(levels) == {_logging.DEBUG}

            client.set_bulk_transfer(False)
            levels = _tick_levels()
            assert levels and set(levels) == {_logging.INFO}
    finally:
        client.close()


def test_peek_datalog_client_never_creates(_recovery_in_tmp):
    """peek returns the existing shared client or None — it must never create
    one (a sync with no session around has no keepalives to quiet)."""
    from src.ecu.wican_config import get_datalog_client, peek_datalog_client

    host = "peek-test-host.invalid"
    assert peek_datalog_client(host) is None
    client = get_datalog_client(host)
    assert peek_datalog_client(host) is client
    client.close()


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
