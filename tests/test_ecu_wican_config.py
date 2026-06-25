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
