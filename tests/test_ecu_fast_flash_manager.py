"""FlashManager.fast_flash: the app's fast flash, end to end on a simulated ECU.

The simulated ECU answers the application's session services (programming
session, security, memory read, flash counter), then becomes the boot loader
that takes the kernel, then the v3 kernel. Image prep is replaced by the
identity (the shared prep has its own tests), so the ECU is compared with the
example ROM as is.
"""

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from src.ecu import fast_flash as ff
from src.ecu import fast_flash_kernel as fk
from src.ecu.constants import BLOCK_SIZE, GEN_DETECT_OFFSET, GEN_NC1, ROM_SIZE
from src.ecu.exceptions import (
    ECUGenerationMismatchError,
    ECUStillProgrammingError,
    FastFlashUnavailable,
    FlashError,
    ROMValidationError,
)
from src.ecu.flash_manager import FlashManager, FlashState, _TRANSITIONS
from src.ecu.protocol import UDSConnection
from tests.test_ecu_fast_flash import SimEcuV3, make_kernel, same_outside_counter

EXAMPLE_ROM = Path(__file__).parent.parent / "examples" / "lf9veb.bin"
NC1_SIG = bytes.fromhex("00000aacffffbfa0")


@pytest.fixture(scope="module")
def rom():
    return EXAMPLE_ROM.read_bytes()


def changed(rom, addresses):
    out = bytearray(rom)
    for a in addresses:
        out[a] ^= 0x5A
    return bytes(out)


class AppEcu(SimEcuV3):
    """The running application until the kernel is uploaded."""

    def __init__(self, flash, *, counter_nrc=None, download_nrc=None, **kw):
        kw.setdefault("chip_class", 1)
        super().__init__(flash, **kw)
        self.counter_nrc = counter_nrc
        self.download_nrc = download_nrc

    def _handle(self, p):
        if self.kernel_running:
            return super()._handle(p)
        sid = p[0]
        if sid == 0x3E:
            return [b"\x7e"]
        if sid == 0x01:
            return [b"\x41\x0c\x00\x00"]  # engine off
        if sid == 0x10:
            return [bytes([0x50, p[1]])]
        if sid == 0x27 and p[1] == 0x01:
            return [b"\x67\x01\x12\x34\x56"]
        if sid == 0x27 and p[1] == 0x02:
            return [b"\x67\x02"]
        if sid == 0x23:
            a, n = int.from_bytes(p[1:5], "big"), int.from_bytes(p[5:7], "big")
            return [b"\x63" + bytes(self.flash[a : a + n])]
        if sid == 0xB1:  # the flash counter routine
            if self.counter_nrc:
                return self._neg(0xB1, self.counter_nrc)
            return [b"\xf1" + p[1:] + b"\x00"]
        if sid == 0x34 and self.download_nrc:
            return self._neg(0x34, self.download_nrc)
        return super()._handle(p)


class KernelLeftRunning(SimEcuV3):
    """A fast flash was cut short after its first erase: the kernel still runs."""

    def __init__(self, flash, kernel, **kw):
        kw.setdefault("chip_class", 1)
        super().__init__(flash, **kw)
        self.kernel_running = True
        self.upload = bytearray(kernel)
        start, size = ff.block_range(15)
        self.flash[start : start + size] = b"\xff" * size


def _kernel(pinned=True):
    data = make_kernel()
    pin = (
        fk.KernelPin(len(data), hashlib.sha256(data).hexdigest(), 3) if pinned else None
    )
    return fk.LoadedKernel(data, "test kernel", pin)


def _identity_prep(rom_data, _start):
    return bytes(rom_data), b"\x00" * 0x1800, "NC2", []


@pytest.fixture(autouse=True)
def app_environment(monkeypatch):
    monkeypatch.setattr("src.ecu.flash_manager.SECURE_MODULE_AVAILABLE", True)
    monkeypatch.setattr(
        "src.ecu.flash_manager.compute_security_key", lambda seed: b"\x01\x02\x03"
    )
    monkeypatch.setattr("src.ecu.flash_manager.prepare_flash_image", _identity_prep)
    monkeypatch.setattr(ff.time, "sleep", lambda s: None)


def _run(ecu, rom_data, archive=None, **kw):
    fm = FlashManager()
    fm.use_uds(UDSConnection(ecu))
    states = []
    kw.setdefault("kernel", _kernel())
    try:
        return fm.fast_flash(
            rom_data,
            archive,
            progress_cb=lambda p: states.append(p.state),
            **kw,
        )
    finally:
        # The ROM is checked before the state machine leaves IDLE.
        _run.states = [s for s in dict.fromkeys(states) if s != FlashState.IDLE]
        _run.manager = fm


def _sids(ecu):
    return [q[0] for q in ecu.requests]


def test_flashes_only_the_changed_blocks_and_saves_the_archive(rom, tmp_path):
    want = changed(rom, [0x20010, 0xC1234])
    ecu = AppEcu(rom)
    archive = tmp_path / "ecu.rda"
    result = _run(ecu, want, str(archive))
    assert result.rewritten == [9, 14, 15]
    assert same_outside_counter(bytes(ecu.flash), want)
    assert archive.read_bytes() == want
    assert ecu.requests[-1] == b"\x11\x01"
    assert _run.states == [
        FlashState.CONNECTING,
        FlashState.AUTHENTICATING,
        FlashState.UPLOADING_KERNEL,
        FlashState.COMPARING,
        FlashState.PROGRAMMING,
        FlashState.VERIFYING,
        FlashState.COMMITTING,
        FlashState.RESETTING,
        FlashState.COMPLETE,
    ]
    assert _run.manager.state == FlashState.COMPLETE


def test_generation_is_read_before_the_flash_counter_and_the_upload(rom):
    ecu = AppEcu(rom)
    _run(ecu, changed(rom, [0x20010]))
    sids = _sids(ecu)
    read = sids.index(0x23)
    counter = sids.index(0xB1)
    upload = sids.index(0x34)
    assert sids.index(0x10) < sids.index(0x27) < read < counter < upload


def test_identical_rom_writes_nothing_resets_and_reports_identical(rom, tmp_path):
    ecu = AppEcu(rom)
    archive = tmp_path / "ecu.rda"
    with pytest.raises(ROMValidationError, match="identical"):
        _run(ecu, rom, str(archive))
    assert [o for o in ecu.ops if o[0] == "erase"] == []
    assert ecu.requests[-1] == b"\x11\x01"
    assert archive.read_bytes() == rom  # the ECU holds exactly this image
    assert _run.manager.state == FlashState.ERROR


def test_unproven_chip_class_falls_back_with_nothing_changed(rom, tmp_path):
    ecu = AppEcu(rom, chip_class=0)
    archive = tmp_path / "ecu.rda"
    with pytest.raises(FastFlashUnavailable, match="Nothing was changed"):
        _run(ecu, changed(rom, [0x20010]), str(archive))
    assert ecu.ops == [] and bytes(ecu.flash) == rom
    assert ecu.requests[-1] == b"\x11\x01"
    assert not archive.exists()


def test_nc1_rom_is_refused_before_any_contact(rom):
    want = bytearray(rom)
    want[GEN_DETECT_OFFSET] = GEN_NC1
    ecu = AppEcu(rom)
    with pytest.raises(FastFlashUnavailable, match="NC1"):
        _run(ecu, bytes(want))
    assert ecu.requests == []


def test_stuck_normal_flash_loader_offers_the_reset(rom):
    ecu = AppEcu(rom, counter_nrc=0x22)
    with pytest.raises(ECUStillProgrammingError):
        _run(ecu, changed(rom, [0x20010]))
    assert 0x34 not in _sids(ecu)


def test_kernel_upload_refused_with_0x22_offers_the_reset(rom):
    ecu = AppEcu(rom, download_nrc=0x22)
    with pytest.raises(ECUStillProgrammingError):
        _run(ecu, changed(rom, [0x20010]))
    assert ecu.ops == []


def test_kernel_upload_refused_otherwise_says_nothing_was_written(rom):
    ecu = AppEcu(rom, download_nrc=0x31)
    with pytest.raises(FlashError, match="Nothing was erased"):
        _run(ecu, changed(rom, [0x20010]))
    assert ecu.ops == []


def test_resume_finishes_an_interrupted_fast_flash(rom, tmp_path):
    ecu = KernelLeftRunning(rom, make_kernel())
    archive = tmp_path / "ecu.rda"
    result = _run(ecu, rom, str(archive), resume=True)
    assert result.rewritten == list(range(2, 16))  # every block
    assert same_outside_counter(bytes(ecu.flash), rom)
    assert archive.read_bytes() == rom
    # No session or security request: the kernel refuses them.
    assert 0x10 not in _sids(ecu) and 0x27 not in _sids(ecu)
    # The kernel was recognised by its refusals before identify was sent.
    first_identify = next(i for i, q in enumerate(ecu.requests) if q[:2] == b"\x31\x00")
    assert _sids(ecu)[:3] == [0x3E, 0x01, 0x21]
    assert _sids(ecu)[3] == 0x22 and first_identify == 4
    assert _run.states[:2] == [FlashState.CONNECTING, FlashState.COMPARING]


def test_resume_refused_when_the_ecu_runs_its_application(rom):
    ecu = AppEcu(rom)
    with pytest.raises(FlashError, match="no longer waiting"):
        _run(ecu, changed(rom, [0x20010]), resume=True)
    assert not any(q[:2] == b"\x31\x00" for q in ecu.requests)
    assert ecu.ops == []


def test_resume_checks_the_generation_by_the_boot_code(rom):
    flash = bytearray(rom)
    flash[0:8] = NC1_SIG
    ecu = KernelLeftRunning(bytes(flash), make_kernel())
    with pytest.raises(ECUGenerationMismatchError):
        _run(ecu, rom, resume=True)
    assert ecu.ops == [] and ecu.requests[-1] == b"\x11\x01"


def test_fast_flash_is_never_aborted(rom):
    ecu = AppEcu(rom)
    fm = FlashManager()
    fm.use_uds(UDSConnection(ecu))

    def abort_at_programming(p):
        if p.state == FlashState.PROGRAMMING:
            fm.abort()

    fm.fast_flash(changed(rom, [0x20010]), None, abort_at_programming, kernel=_kernel())
    assert fm.state == FlashState.COMPLETE


def test_compare_only_erases_nothing(rom):
    ecu = AppEcu(rom)
    fm = FlashManager()
    fm.use_uds(UDSConnection(ecu))
    result = fm.fast_compare(changed(rom, [0x20010]), kernel=_kernel())
    assert result.changed == [9]
    assert ecu.ops == [] and ecu.requests[-1] == b"\x11\x01"
    assert fm.state == FlashState.COMPLETE


def test_fast_states_reach_error():
    for state in (
        FlashState.UPLOADING_KERNEL,
        FlashState.COMPARING,
        FlashState.PROGRAMMING,
        FlashState.VERIFYING,
        FlashState.COMMITTING,
    ):
        assert FlashState.ERROR in _TRANSITIONS[state]
    # Once programming starts there is no way to ABORTED.
    for state in (
        FlashState.PROGRAMMING,
        FlashState.VERIFYING,
        FlashState.COMMITTING,
    ):
        assert FlashState.ABORTED not in _TRANSITIONS[state]


# --- The kernel-aware reset ---


def test_reset_of_a_running_kernel_is_a_bare_ecu_reset(rom):
    ecu = KernelLeftRunning(rom, make_kernel())
    FlashManager().reset_programming_ecu(UDSConnection(ecu), fast_flash_kernel=True)
    assert ecu.requests[-1] == b"\x11\x01"
    assert 0x10 not in _sids(ecu) and 0x27 not in _sids(ecu)


def test_kernel_reset_refused_when_the_kernel_is_gone(rom):
    ecu = AppEcu(rom)
    with pytest.raises(FlashError, match="no longer answers"):
        FlashManager().reset_programming_ecu(UDSConnection(ecu), fast_flash_kernel=True)
    assert 0x11 not in _sids(ecu)
    assert not any(q[:2] == b"\x31\x00" for q in ecu.requests)


def test_example_rom_is_a_full_image(rom):
    assert len(rom) == ROM_SIZE and len(rom) % BLOCK_SIZE == 0


@patch("src.ecu.flash_manager.SECURE_MODULE_AVAILABLE", False)
def test_needs_the_security_module(rom):
    from src.ecu.exceptions import SecureModuleNotAvailable

    with pytest.raises(SecureModuleNotAvailable):
        FlashManager().fast_flash(rom, kernel=_kernel())


def test_the_pinned_v3_kernel_runs_through_the_app_path(rom):
    """The real kernel image (where present) passes the app's checks on the
    simulated ECU: pin, upload marker, self-check, policy. Skipped in CI."""
    from src.utils.paths import get_app_root

    path = get_app_root() / fk.KERNEL_FILE
    if not path.is_file():
        pytest.skip("no fast_flash/kernel.bin on this machine")
    data = path.read_bytes()
    kernel = fk.LoadedKernel(data, str(path), fk.find_pin(data))
    assert kernel.pin is not None
    ecu = AppEcu(rom)
    result = _run(ecu, changed(rom, [0x20010]), kernel=kernel)
    assert result.rewritten == [9, 15]
    assert bytes(ecu.upload) == data


# --- Independent review fixes (2026-11-15) ---


class SilentAfterErase(AppEcu):
    """Stops answering right after its first erase (the PC lost the link)."""

    def send_message(self, payload, timeout_ms):
        if any(o[0] == "erase" for o in self.ops):
            self.requests.append(bytes(payload))
            return
        super().send_message(payload, timeout_ms)


class DropsResets(AppEcu):
    """Loses the first ``drop`` ECUReset requests (no 51 comes back)."""

    def __init__(self, flash, drop, **kw):
        super().__init__(flash, **kw)
        self.drop = drop

    def send_message(self, payload, timeout_ms):
        if self.kernel_running and bytes(payload[:2]) == b"\x11\x01" and self.drop:
            self.drop -= 1
            self.requests.append(bytes(payload))
            return
        super().send_message(payload, timeout_ms)


@pytest.fixture
def short_waits(monkeypatch):
    for name in ("TIMEOUT_ERASE_MS", "TIMEOUT_RESET_MS", "TIMEOUT_CRC_MS"):
        monkeypatch.setattr(ff, name, 30)


def _archive(tmp_path, content=b"old archive"):
    path = tmp_path / "ecu.rda"
    path.write_bytes(content)
    return path


def _aside(path):
    from src.ecu.flash_manager import ARCHIVE_ASIDE_SUFFIX

    return Path(str(path) + ARCHIVE_ASIDE_SUFFIX)


def test_unfinished_flash_leaves_no_archive_to_go_stale(rom, tmp_path, short_waits):
    """Finding 1: once blocks were erased, the old archive no longer describes
    the ECU. It is set aside (never deleted) and not put back."""
    archive = _archive(tmp_path)
    ecu = SilentAfterErase(rom)
    with pytest.raises(FlashError, match="waiting"):
        _run(ecu, changed(rom, [0x20010]), str(archive))
    assert not archive.exists()
    assert _aside(archive).read_bytes() == b"old archive"


def test_without_an_archive_the_archive_based_flash_does_not_run(rom, tmp_path):
    """...so the next archive-based Dynamic Flash cannot compute a partial flash
    from a stale copy: it refuses before any contact (the window then picks a
    full flash, see test_ecu_window_fast_flash)."""
    ecu = AppEcu(rom)
    fm = FlashManager()
    fm.use_uds(UDSConnection(ecu))
    with pytest.raises(ROMValidationError, match="No ECU archive"):
        fm.dynamic_flash(rom, str(tmp_path / "ecu.rda"))
    assert ecu.requests == []


def test_archive_put_back_when_nothing_was_erased(rom, tmp_path):
    archive = _archive(tmp_path)
    ecu = AppEcu(rom, chip_class=0)  # refused before the first erase
    with pytest.raises(FastFlashUnavailable):
        _run(ecu, changed(rom, [0x20010]), str(archive))
    assert archive.read_bytes() == b"old archive"
    assert not _aside(archive).exists()


def test_archive_replaced_only_after_a_verified_flash(rom, tmp_path):
    archive = _archive(tmp_path)
    want = changed(rom, [0x20010])
    _run(AppEcu(rom), want, str(archive))
    assert archive.read_bytes() == want
    assert _aside(archive).read_bytes() == b"old archive"  # kept, not deleted


def test_rom_without_start_address_is_refused_before_contact(rom, tmp_path):
    """Finding 6: checked before connecting, not after the programming session."""
    bad = bytearray(rom)
    bad[ff.ENTRY_POINTER : ff.ENTRY_POINTER + 4] = b"\xff" * 4
    archive = _archive(tmp_path)
    ecu = AppEcu(rom)
    with pytest.raises(FlashError, match="start address"):
        _run(ecu, bytes(bad), str(archive))
    assert ecu.requests == []
    assert archive.read_bytes() == b"old archive"


@pytest.mark.parametrize("drop, confirmed", [(1, True), (2, False)])
def test_restart_request_is_sent_again_when_unanswered(
    rom, short_waits, drop, confirmed
):
    """Finding 3: one lost 11 01 is resent; two mean the user must cycle the
    ignition (the result says so)."""
    ecu = DropsResets(rom, drop)
    result = _run(ecu, changed(rom, [0x20010]))
    resets = [q for q in ecu.requests if q[:2] == b"\x11\x01"]
    assert len(resets) == 2
    assert result.restart_confirmed is confirmed
    assert _run.manager.state == FlashState.COMPLETE


def test_resume_refusal_does_not_claim_nothing_changed(rom):
    """Finding 7: in a resumed session blocks may already be erased."""
    ecu = KernelLeftRunning(rom, make_kernel(), chip_class=0)
    with pytest.raises(FastFlashUnavailable) as e:
        _run(ecu, rom, resume=True)
    assert "Nothing was changed" not in str(e.value)
    assert "recovery mode" in str(e.value)
    assert ecu.requests[-1] == b"\x11\x01"


def test_failed_resume_never_restores_the_archive(rom, tmp_path):
    """Re-review R4: the interrupted session may have erased; the ECU no
    longer matches the archive."""
    archive = _archive(tmp_path)
    ecu = KernelLeftRunning(rom, make_kernel(), chip_class=0)  # refused
    with pytest.raises(FastFlashUnavailable):
        _run(ecu, rom, str(archive), resume=True)
    assert not archive.exists()
    assert _aside(archive).read_bytes() == b"old archive"
