"""SD-staged WiCAN flash orchestration (Option B Phase 3 skeleton).

Proves :class:`WiCANSdFlasher` packages → uploads → *refuses to trigger* on
fast-read-only firmware (the rev-gate), reuses the :class:`WiCANFlasher`
safeguards (battery/link gate + read-back verify) by composition, and never
reaches the ECU until a fastwrite-capable build (NCFRv5+) is present. The
orchestration tests patch ``build_flash_package`` to stay fast and
``_secure``-independent; one end-to-end test exercises the real packager.
"""

import os
import time
import urllib.parse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.ecu.checksum import crc32
from src.ecu.wican_sd_flash import (
    FASTWRITE_MIN_FW_REV,
    WiCANSdFlasher,
    _parse_fw_rev,
)
from src.ecu.constants import (
    FLASH_COUNTER_OFFSET,
    FLASH_COUNTER_SIZE,
    ROM_FLASH_START_MIN,
    ROM_SIZE,
)
from src.ecu.exceptions import FlashError
from src.ecu.wican_sd_package import SECURE_MODULE_AVAILABLE, FlashPackage
from src.ecu.wican_transport import WiCANError

_ROM_PATH = Path(__file__).resolve().parent.parent / "examples" / "lf9veb.bin"

requires_secure = pytest.mark.skipif(
    not SECURE_MODULE_AVAILABLE, reason="_secure module (SBL IP) not installed"
)


class _FakeTransport:
    """Minimal transport: a host, a version-ping marker, and a fast_write spy."""

    def __init__(self, host="10.0.0.5", marker=b"NCFRv4"):
        self.host = host
        self._marker = marker
        self.fast_write_calls = []

    def version_ping(self, *a, **k):
        return self._marker

    def fast_write(self, staged_name, *, mode="L", progress_cb=None, **k):
        self.fast_write_calls.append((staged_name, mode))
        if progress_cb:
            progress_cb(1022, 1022)  # simulate a completed flash


def _fake_package(name="ID_20260623-1745.bin", image=b"img-bytes"):
    return FlashPackage(
        image=image,
        manifest={
            "image_len": len(image),
            "image_crc32": crc32(image),
            "staged_filename": name,
            "flash_start_index": 0x2000,
            "flash_type": "full",
        },
    )


def _make_flasher(transport, uploader=None, datalog=None):
    # Inject a mock datalog client by default so the no-reboot /datalog pause/resume
    # makes no real HTTP from unit tests (it would soft-degrade, but slowly).
    f = WiCANSdFlasher(
        transport, uploader=uploader or MagicMock(), datalog=datalog or MagicMock()
    )
    # Stub the link/battery gate, ECU auth, read-back, and the best-effort post-abort
    # UDS teardown so no real I/O happens.
    f._safeguards._gate = MagicMock(name="gate")
    f._verify_readback = MagicMock(name="verify")
    f._authenticate_ecu = MagicMock(name="auth")
    f._safe_uds_teardown = MagicMock(name="uds_teardown")
    return f


class TestParseFwRev:
    @pytest.mark.parametrize(
        "marker,expected",
        [
            (b"NCFRv4", 4),
            (b"NCFRv5", 5),
            (b"NCFRv12", 12),
            (b"junkNCFRv7tail", 7),
            (b"NCFRvX", None),
            (b"NCFR", None),
            (b"", None),
            (None, None),
        ],
    )
    def test_parse(self, marker, expected):
        assert _parse_fw_rev(marker) == expected


class TestRevGate:
    def test_fast_read_only_firmware_refuses_trigger(self):
        flasher = _make_flasher(_FakeTransport(marker=b"NCFRv4"))
        with patch(
            "src.ecu.wican_sd_flash.build_flash_package", return_value=_fake_package()
        ) as bp:
            with pytest.raises(WiCANError, match=f"NCFRv{FASTWRITE_MIN_FW_REV}"):
                flasher.flash_rom(b"\x00" * 16, verify=True)

        # Gate ran, package built, both artifacts uploaded — but the ECU was never
        # touched: the rev-gate refused BEFORE auth, and verify never ran.
        flasher._safeguards._gate.assert_called_once()
        bp.assert_called_once()
        flasher._uploader.upload_package.assert_called_once()
        flasher._uploader.upload_manifest.assert_called_once()
        flasher._authenticate_ecu.assert_not_called()
        flasher._verify_readback.assert_not_called()

    def test_unknown_firmware_refuses_trigger(self):
        flasher = _make_flasher(_FakeTransport(marker=b""))
        with patch(
            "src.ecu.wican_sd_flash.build_flash_package", return_value=_fake_package()
        ):
            with pytest.raises(WiCANError, match="fast-read-only|unknown"):
                flasher.flash_rom(b"\x00" * 16)

    def test_capable_firmware_triggers_fast_write(self):
        # NCFRv5 clears the rev-gate -> the trigger drives the firmware fast_write
        # (mode 'L') with the staged filename and the flash completes on firmware
        # confirmation. The inline read-back verify does NOT run by default — the
        # NC ECU is in its bootloader post-reset until a physical ignition cycle.
        transport = _FakeTransport(marker=b"NCFRv5")
        flasher = _make_flasher(transport)
        with (
            patch(
                "src.ecu.wican_sd_flash.build_flash_package",
                return_value=_fake_package(),
            ),
            patch("src.ecu.wican_sd_flash.time.sleep"),
        ):
            flasher.flash_rom(b"\x00" * 16)
        # Auth happened (after the rev-gate), then the firmware flash. No verify.
        flasher._authenticate_ecu.assert_called_once()
        assert transport.fast_write_calls == [("ID_20260623-1745.bin", "L")]
        flasher._verify_readback.assert_not_called()

    def test_verify_opt_in_runs_readback(self):
        # When the operator has cycled the ignition and opts in (verify=True), the
        # read-back compare runs after the firmware flash.
        transport = _FakeTransport(marker=b"NCFRv5")
        flasher = _make_flasher(transport)
        with (
            patch(
                "src.ecu.wican_sd_flash.build_flash_package",
                return_value=_fake_package(),
            ),
            patch("src.ecu.wican_sd_flash.time.sleep"),
        ):
            flasher.flash_rom(b"\x00" * 16, verify=True)
        assert transport.fast_write_calls == [("ID_20260623-1745.bin", "L")]
        flasher._verify_readback.assert_called_once()

    def test_settles_before_authenticating(self):
        """The host-side pre-session settle (brick-safety margin so an in-flight
        datalogger poll frame can't corrupt the UDS auth) must run BEFORE the ECU
        is authenticated / put into the programming session."""
        from src.ecu.wican_sd_flash import PRE_SESSION_SETTLE_S

        transport = _FakeTransport(marker=b"NCFRv5")
        flasher = _make_flasher(transport)
        order = []
        flasher._authenticate_ecu.side_effect = lambda: order.append("auth")
        with (
            patch(
                "src.ecu.wican_sd_flash.build_flash_package",
                return_value=_fake_package(),
            ),
            patch(
                "src.ecu.wican_sd_flash.time.sleep",
                side_effect=lambda s: order.append(("sleep", s)),
            ),
        ):
            flasher.flash_rom(b"\x00" * 16)
        assert ("sleep", PRE_SESSION_SETTLE_S) in order
        assert order.index(("sleep", PRE_SESSION_SETTLE_S)) < order.index("auth")


class TestDatalogCoexistence:
    """No-reboot coexistence (#36.C): the datalogger is REST-paused before the flash
    and resumed after — on EVERY exit path — and the flash is never aborted by a
    /datalog failure."""

    def _spy_flasher(self):
        from src.ecu.wican_config import WiCANDatalogClient

        transport = _FakeTransport(marker=b"NCFRv5")
        order = []
        # A REAL client so the fence exercises the real refcounted reserved()/
        # acquire_bus()/release_bus(); only the four lease ops are spied for order.
        datalog = WiCANDatalogClient("127.0.0.1")
        datalog.bus_claim = lambda: order.append("bus_claim")
        datalog.pause = lambda: order.append("pause")
        datalog.bus_release = lambda: order.append("bus_release")
        datalog.resume = lambda: order.append("resume")
        flasher = _make_flasher(transport, datalog=datalog)
        flasher._authenticate_ecu.side_effect = lambda: order.append("auth")
        transport.fast_write = lambda *a, **k: order.append("flash")
        return flasher, datalog, order

    def test_pause_before_flash_resume_after(self):
        flasher, datalog, order = self._spy_flasher()
        with patch(
            "src.ecu.wican_sd_flash.build_flash_package", return_value=_fake_package()
        ):
            flasher.flash_rom(b"\x00" * 16)
        # claim+pause fence the WHOLE window; auth -> flash; then release+resume.
        # The single ordered appearance of each op proves it ran exactly once.
        assert order == ["bus_claim", "pause", "auth", "flash", "bus_release", "resume"]

    def test_preflight_gate_runs_inside_the_datalog_fence(self):
        """Regression (HW-9 bench hang): the link/battery preflight gate pings the ECU
        over the transport, so on the no-reboot coexist port it MUST run with the datalog
        fence already raised — otherwise the datalogger still owns the single CAN bus and
        the gate's Tester-Present round-trips never return (the bench flash hung here).
        Assert bus_claim + pause precede the gate, and release happens after the flash.
        """
        flasher, datalog, order = self._spy_flasher()
        flasher._safeguards._gate.side_effect = lambda: order.append("gate")
        with patch(
            "src.ecu.wican_sd_flash.build_flash_package", return_value=_fake_package()
        ):
            flasher.flash_rom(b"\x00" * 16)
        assert order == [
            "bus_claim",
            "pause",
            "gate",
            "auth",
            "flash",
            "bus_release",
            "resume",
        ]

    def test_resume_runs_even_when_flash_raises(self):
        """The datalogger must be restored (claim released + resumed) even if the flash
        fails mid-transfer."""
        flasher, datalog, order = self._spy_flasher()

        def boom(*a, **k):
            order.append("flash")
            raise WiCANError("FWERR mid-transfer")

        flasher._transport.fast_write = boom
        with patch(
            "src.ecu.wican_sd_flash.build_flash_package", return_value=_fake_package()
        ):
            with pytest.raises(WiCANError, match="FWERR"):
                flasher.flash_rom(b"\x00" * 16)
        # On a FAILED flash the best-effort UDS teardown also fires (mocked here).
        flasher._safe_uds_teardown.assert_called_once()
        # bus_release + resume still ran (the reservation's finally) even on failure.
        assert order == ["bus_claim", "pause", "auth", "flash", "bus_release", "resume"]

    def test_datalog_pause_failure_does_not_abort_flash(self):
        """A /datalog error (port-only build, timeout) must NEVER abort a flash —
        the client already soft-degrades (returns None), so a real failure surfaces
        as None here and the flash proceeds."""
        flasher, datalog, order = self._spy_flasher()
        datalog.pause = lambda: (order.append("pause"), None)[
            1
        ]  # soft-degrades to None
        with patch(
            "src.ecu.wican_sd_flash.build_flash_package", return_value=_fake_package()
        ):
            flasher.flash_rom(b"\x00" * 16)  # must not raise
        assert "flash" in order


@pytest.fixture
def _recovery_in_tmp(tmp_path, monkeypatch):
    """Isolate the datalog crash-recovery breadcrumb in a per-test temp dir."""
    import src.ecu.wican_config as mod

    monkeypatch.setattr(mod.tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path


def _post_ops(server):
    """Ordered list of /datalog ``op`` values the mock server received over POST."""
    ops = []
    for method, path in server.requests:
        if method != "POST":
            continue
        q = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
        ops.append(q.get("op", [""])[0])
    return ops


class TestDeadmanUiPathIntegration:
    """End-to-end UI flash path against a firmware-faithful ``/datalog`` mock.

    The other tests spy on a MagicMock datalog client. These drive the REAL
    :class:`WiCANDatalogClient` through ``WiCANSdFlasher._trigger_firmware_flash`` against
    the SAME ``_MockDatalogServer`` the dead-man's-switch FIRMWARE is verified against —
    proving the UI flash path emits exactly the wire handshake the firmware implements
    (bus_claim → pause → keepalive(both leases) → bus_release → resume), issues + clears
    both leases, manages the crash breadcrumb, and tears the keepalive daemon down. This is
    the host↔firmware contract closing the loop on the UI path.
    """

    def _real_client(self, server, **kw):
        from src.ecu.wican_config import WiCANDatalogClient

        kw.setdefault("timeout_s", 2.0)
        kw.setdefault(
            "keepalive_interval_s", 0.05
        )  # ticks during the PRE_SESSION_SETTLE window
        return WiCANDatalogClient("127.0.0.1", http_port=server.port, **kw)

    def test_flash_drives_full_deadman_handshake(self, _recovery_in_tmp):
        from tests.test_ecu_wican_config import _MockDatalogServer

        with _MockDatalogServer("ok") as server:
            client = self._real_client(server)
            flasher = _make_flasher(
                _FakeTransport(marker=b"NCFRv5", host="127.0.0.1"), datalog=client
            )
            try:
                with patch(
                    "src.ecu.wican_sd_flash.build_flash_package",
                    return_value=_fake_package(),
                ):
                    flasher.flash_rom(b"\x00" * 16)

                ops = _post_ops(server)
                # The brick fence + park bracket the WHOLE window, released after the flash.
                lifecycle = [
                    o
                    for o in ops
                    if o in ("bus_claim", "pause", "bus_release", "resume")
                ]
                assert lifecycle == ["bus_claim", "pause", "bus_release", "resume"]
                # The keepalive daemon renewed BOTH leases at once while the flash was held.
                ka_paths = [
                    p for m, p in server.requests if m == "POST" and "op=keepalive" in p
                ]
                assert ka_paths, "keepalive daemon never ticked during the flash window"
                assert any("park_token=" in p and "claim_token=" in p for p in ka_paths)
                # Firmware-side: both leases released, neither bit left set.
                assert server.claimed is False and server.parked is False
                assert server.claim_token is None and server.park_token is None
                # Host-side: lease tokens dropped, breadcrumb cleared, daemon stopped.
                assert client._claim_token is None and client._park_token is None
                assert not os.path.exists(client.recovery_path)
                assert client._ka_thread is None
            finally:
                client.close()

    def test_flash_failure_still_runs_full_deadman_teardown(self, _recovery_in_tmp):
        """A flash that raises mid-transfer must STILL release the claim + resume over the
        real wire (the worker ``finally``), so the firmware never stays fenced/parked.
        """
        from tests.test_ecu_wican_config import _MockDatalogServer

        with _MockDatalogServer("ok") as server:
            client = self._real_client(server)
            transport = _FakeTransport(marker=b"NCFRv5", host="127.0.0.1")

            def boom(*a, **k):
                raise WiCANError("FWERR mid-transfer")

            transport.fast_write = boom
            flasher = _make_flasher(transport, datalog=client)
            try:
                with patch(
                    "src.ecu.wican_sd_flash.build_flash_package",
                    return_value=_fake_package(),
                ):
                    with pytest.raises(WiCANError, match="FWERR"):
                        flasher.flash_rom(b"\x00" * 16)

                lifecycle = [
                    o
                    for o in _post_ops(server)
                    if o in ("bus_claim", "pause", "bus_release", "resume")
                ]
                assert lifecycle == ["bus_claim", "pause", "bus_release", "resume"]
                # Even on failure the device is fully restored over the real wire.
                assert server.claimed is False and server.parked is False
                assert client._ka_thread is None
            finally:
                client.close()

    def test_flash_against_port_only_build_soft_degrades(self, _recovery_in_tmp):
        """A pre-deadman / port-only build (no /datalog → 404) must not abort the flash:
        every claim/pause/keepalive/resume call returns None and is swallowed."""
        from tests.test_ecu_wican_config import _MockDatalogServer

        with _MockDatalogServer("404") as server:
            client = self._real_client(server)
            flasher = _make_flasher(
                _FakeTransport(marker=b"NCFRv5", host="127.0.0.1"), datalog=client
            )
            try:
                with patch(
                    "src.ecu.wican_sd_flash.build_flash_package",
                    return_value=_fake_package(),
                ):
                    flasher.flash_rom(b"\x00" * 16)  # must NOT raise
                # No tokens were ever issued; nothing to clean up; flash still ran.
                assert client._claim_token is None and client._park_token is None
                assert transport_fast_write_ran(flasher)
            finally:
                client.close()


def transport_fast_write_ran(flasher):
    """True if the flasher's transport recorded a fast_write (the flash was triggered)."""
    return bool(getattr(flasher._transport, "fast_write_calls", None))


class TestDynamicFlash:
    def test_dynamic_reads_archive_and_packages_dynamic(self, tmp_path):
        archive = tmp_path / "ncflash.rda"
        archive.write_bytes(b"\xab" * 64)
        flasher = _make_flasher(_FakeTransport(marker=b"NCFRv4"))

        with patch(
            "src.ecu.wican_sd_flash.build_flash_package", return_value=_fake_package()
        ) as bp:
            with pytest.raises(WiCANError):
                flasher.dynamic_flash(b"\x00" * 16, str(archive))

        _, kwargs = bp.call_args
        assert kwargs["flash_type"] == "dynamic"
        assert kwargs["archive_data"] == b"\xab" * 64


class TestSourceName:
    def test_source_name_threads_to_packager(self):
        # The display filename given to the flasher reaches build_flash_package,
        # which names the staged SD image after it.
        transport = _FakeTransport(marker=b"NCFRv4")
        flasher = WiCANSdFlasher(
            transport, uploader=MagicMock(), source_name="My Tune éà.bin"
        )
        flasher._safeguards._gate = MagicMock()
        with patch(
            "src.ecu.wican_sd_flash.build_flash_package", return_value=_fake_package()
        ) as bp:
            with pytest.raises(WiCANError):  # NCFRv4 rev-gate stops before the ECU
                flasher.flash_rom(b"\x00" * 16)
        _, kwargs = bp.call_args
        assert kwargs["source_name"] == "My Tune éà.bin"


class TestReadbackVerify:
    def _flasher(self, written):
        t = _FakeTransport(marker=b"NCFRv5")
        t.fast_read = MagicMock(return_value=bytes(written))
        f = WiCANSdFlasher(t, uploader=MagicMock())
        f._authenticate_ecu = MagicMock()  # ECU "comes back" immediately
        return f

    def test_pass_ignores_flash_counter(self):
        src = bytes(ROM_SIZE)
        written = bytearray(ROM_SIZE)
        for i in range(FLASH_COUNTER_OFFSET, FLASH_COUNTER_OFFSET + FLASH_COUNTER_SIZE):
            written[i] = 0x5A  # ECU-stamped flash counter — must be tolerated
        f = self._flasher(written)
        with (
            patch("src.ecu.wican_sd_flash.correct_rom_checksums"),
            patch("src.ecu.wican_sd_flash.time.sleep"),
        ):
            f._verify_readback(src)  # must NOT raise
        f._authenticate_ecu.assert_called()  # re-auth before reading back

    def test_mismatch_outside_counter_raises(self):
        src = bytes(ROM_SIZE)
        written = bytearray(ROM_SIZE)
        written[ROM_FLASH_START_MIN + 50] = 0xFF  # real diff in the flashed region
        f = self._flasher(written)
        with (
            patch("src.ecu.wican_sd_flash.correct_rom_checksums"),
            patch("src.ecu.wican_sd_flash.time.sleep"),
        ):
            with pytest.raises(FlashError, match="verify FAILED"):
                f._verify_readback(src)

    def test_ecu_not_readable_raises_with_ignition_hint(self):
        # If the ECU still refuses reads (e.g. operator hasn't cycled the key yet),
        # the error must guide them to cycle the ignition, not fail cryptically.
        f = self._flasher(bytes(ROM_SIZE))
        f._authenticate_ecu = MagicMock(side_effect=WiCANError("no response"))
        with patch("src.ecu.wican_sd_flash.time.sleep"):
            with pytest.raises(FlashError, match="[Cc]ycle the ignition"):
                f._verify_readback(bytes(ROM_SIZE))


class TestConstruction:
    def test_requires_host_when_no_uploader(self):
        class _NoHost:
            def version_ping(self):
                return b"NCFRv4"

        with pytest.raises(WiCANError, match="host"):
            WiCANSdFlasher(_NoHost())

    def test_preflight_delegates_to_safeguards(self):
        flasher = _make_flasher(_FakeTransport())
        flasher._safeguards.preflight = MagicMock(return_value="LQ")
        assert flasher.preflight() == "LQ"
        flasher._safeguards.preflight.assert_called_once()


@requires_secure
class TestEndToEndPackaging:
    def test_real_package_flows_to_upload_then_rev_gate(self):
        rom = _ROM_PATH.read_bytes()
        uploader = MagicMock()
        flasher = _make_flasher(_FakeTransport(marker=b"NCFRv4"), uploader=uploader)

        with pytest.raises(WiCANError, match="NCFRv5"):
            flasher.flash_rom(rom, verify=True)

        # The real packager produced a package; the uploader received the staged
        # image whose first megabyte is the checksum-corrected ROM.
        pkg = uploader.upload_package.call_args.args[0]
        assert pkg.manifest["flash_type"] == "full"
        assert pkg.manifest["generation"] == "NC2"
        assert len(pkg.image) == pkg.manifest["image_len"]
        from src.ecu.checksum import correct_rom_checksums

        corrected = bytearray(rom)
        correct_rom_checksums(corrected)
        assert pkg.corrected_rom == bytes(corrected)
