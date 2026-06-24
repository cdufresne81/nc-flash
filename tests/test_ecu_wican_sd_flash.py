"""SD-staged WiCAN flash orchestration (Option B Phase 3 skeleton).

Proves :class:`WiCANSdFlasher` packages → uploads → *refuses to trigger* on
fast-read-only firmware (the rev-gate), reuses the :class:`WiCANFlasher`
safeguards (battery/link gate + read-back verify) by composition, and never
reaches the ECU until a fastwrite-capable build (NCFRv5+) is present. The
orchestration tests patch ``build_flash_package`` to stay fast and
``_secure``-independent; one end-to-end test exercises the real packager.
"""

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


def _make_flasher(transport, uploader=None):
    f = WiCANSdFlasher(transport, uploader=uploader or MagicMock())
    # Stub the link/battery gate, ECU auth, and read-back so no real I/O happens.
    f._safeguards._gate = MagicMock(name="gate")
    f._verify_readback = MagicMock(name="verify")
    f._authenticate_ecu = MagicMock(name="auth")
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
        with patch(
            "src.ecu.wican_sd_flash.build_flash_package", return_value=_fake_package()
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
        with patch(
            "src.ecu.wican_sd_flash.build_flash_package", return_value=_fake_package()
        ):
            flasher.flash_rom(b"\x00" * 16, verify=True)
        assert transport.fast_write_calls == [("ID_20260623-1745.bin", "L")]
        flasher._verify_readback.assert_called_once()


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
