"""Where the fast-flash kernel comes from and when the app may use it.

A kernel is used only when its size + CRC are pinned (or, on the bench, with
NCFLASH_FAST_FLASH_UNPINNED=1); only on the Tactrix / OBDX adapters; only for
the hardware generation and chip classes proven on the bench.
"""

import hashlib
import sys
from pathlib import Path

import pytest

from src.ecu import fast_flash_kernel as fk
from src.ecu._secure_stub import get_fast_flash_kernel as stub_kernel
from src.ecu.constants import GEN_DETECT_OFFSET, GEN_NC1
from src.ecu.exceptions import SecureModuleNotAvailable
from tests.test_ecu_fast_flash import make_kernel

EXAMPLE_ROM = Path(__file__).parent.parent / "examples" / "lf9veb.bin"


@pytest.fixture
def no_sources(monkeypatch, tmp_path):
    """No kernel in the security module, the app root in an empty folder."""
    monkeypatch.setattr(fk, "_from_secure_module", lambda: None)
    monkeypatch.setattr("src.utils.paths.get_app_root", lambda: tmp_path)
    monkeypatch.delenv(fk.UNPINNED_ENV, raising=False)
    return tmp_path


def _write_kernel(root: Path, data: bytes) -> Path:
    path = root / fk.KERNEL_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _pin(monkeypatch, data: bytes, version=3):
    monkeypatch.setattr(
        fk,
        "FAST_FLASH_KERNELS",
        (fk.KernelPin(len(data), hashlib.sha256(data).hexdigest(), version),),
    )


def test_ships_with_the_v3_kernel_pinned():
    """The CI-built v3 kernel (bench-proven 2026-11-15) is the only one."""
    assert fk.FAST_FLASH_KERNELS == (
        fk.KernelPin(
            size=0x1800,
            sha256="8f21aa273495cc3d2cb1fd5927c5928f1867b4cacb814ede25e841e8eb63a62c",
            version=3,
        ),
    )


def test_stub_has_no_kernel():
    with pytest.raises(SecureModuleNotAvailable):
        stub_kernel()


def test_nothing_installed_means_no_kernel(no_sources):
    assert fk.load_kernel() is None


def test_unpinned_file_is_ignored(no_sources):
    _write_kernel(no_sources, make_kernel())
    assert fk.load_kernel() is None


def test_unpinned_file_is_used_on_the_bench_only_with_the_env_var(
    no_sources, monkeypatch
):
    _write_kernel(no_sources, make_kernel())
    monkeypatch.setenv(fk.UNPINNED_ENV, "1")
    kernel = fk.load_kernel()
    assert kernel is not None and kernel.pin is None
    assert kernel.data == make_kernel()


def test_pinned_file_is_used(no_sources, monkeypatch):
    path = _write_kernel(no_sources, make_kernel())
    _pin(monkeypatch, make_kernel())
    kernel = fk.load_kernel()
    assert kernel.pin.version == 3 and kernel.source == str(path)


def test_invalid_kernel_is_never_used(no_sources, monkeypatch):
    bad = b"\x00" * 0xC00  # no end marker
    _write_kernel(no_sources, bad)
    _pin(monkeypatch, bad)
    monkeypatch.setenv(fk.UNPINNED_ENV, "1")
    assert fk.load_kernel() is None


def test_security_module_comes_first(no_sources, monkeypatch):
    other = bytearray(make_kernel())
    other[0] = 0x55
    other = bytes(other)
    _write_kernel(no_sources, make_kernel())
    monkeypatch.setattr(fk, "_from_secure_module", lambda: other)
    monkeypatch.setattr(
        fk,
        "FAST_FLASH_KERNELS",
        (
            fk.KernelPin(len(other), hashlib.sha256(other).hexdigest(), 3),
            fk.KernelPin(
                len(make_kernel()), hashlib.sha256(make_kernel()).hexdigest(), 3
            ),
        ),
    )
    kernel = fk.load_kernel()
    assert kernel.data == other and kernel.source == "security module"


def test_unpinned_security_kernel_falls_back_to_a_pinned_file(no_sources, monkeypatch):
    monkeypatch.setattr(fk, "_from_secure_module", lambda: b"\x01" * 0xC00)
    _write_kernel(no_sources, make_kernel())
    _pin(monkeypatch, make_kernel())
    assert fk.load_kernel().data == make_kernel()


def test_pin_line_is_ready_to_paste():
    line = fk.pin_line(make_kernel(), 3)
    digest = hashlib.sha256(make_kernel()).hexdigest()
    assert line == f'KernelPin(size=0xC00, sha256="{digest}", version=3),'


def test_same_crc32_different_bytes_is_not_accepted(no_sources, monkeypatch):
    """The pin is a SHA-256: a kernel forged to the pinned size and CRC-32 is
    refused (finding 2)."""
    good = make_kernel()
    _pin(monkeypatch, good)
    forged = bytearray(good)
    forged[0] ^= 0xFF
    assert fk.find_pin(bytes(forged)) is None
    assert fk.find_pin(good) is not None


def test_release_build_never_reads_the_kernel_file(no_sources, monkeypatch):
    _write_kernel(no_sources, make_kernel())
    _pin(monkeypatch, make_kernel())
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert fk.load_kernel() is None


# --- When the fast flash may run ---


@pytest.fixture(scope="module")
def nc2_rom():
    return EXAMPLE_ROM.read_bytes()


def _kernel():
    return fk.LoadedKernel(make_kernel(), "test", fk.KernelPin(0xC00, "", 3))


@pytest.mark.parametrize("adapter", ["j2534", "obdx"])
def test_wired_adapters_with_a_kernel_and_an_nc2_rom(nc2_rom, adapter):
    assert fk.unavailable_reason(adapter, nc2_rom, _kernel()) is None


@pytest.mark.parametrize("adapter", ["wican", None])
def test_never_over_the_wican(nc2_rom, adapter):
    assert "Tactrix" in fk.unavailable_reason(adapter, nc2_rom, _kernel())


def test_no_kernel(nc2_rom):
    assert "program" in fk.unavailable_reason("j2534", nc2_rom, None)


def test_nc1_rom_is_not_tested_yet(nc2_rom):
    rom = bytearray(nc2_rom)
    rom[GEN_DETECT_OFFSET] = GEN_NC1
    assert "NC1" in fk.unavailable_reason("j2534", bytes(rom), _kernel())


def test_unreadable_rom(nc2_rom):
    assert fk.unavailable_reason("j2534", b"\x00" * 16, _kernel()) is not None


def test_app_policy():
    policy = fk.kernel_policy(_kernel(), "NC2")
    assert policy.chip_classes == frozenset({1})
    assert policy.versions == frozenset({3})
    assert policy.require_self_check and policy.generation == "NC2"
    unpinned = fk.kernel_policy(fk.LoadedKernel(make_kernel(), "f", None), "NC2")
    # Only v3 is ever accepted by the app, pinned or not.
    assert unpinned.versions == frozenset({3}) and unpinned.require_self_check
    old = fk.LoadedKernel(make_kernel(), "f", fk.KernelPin(0xC00, "", 2))
    assert fk.kernel_policy(old, "NC2").versions == frozenset()


def test_bench_kernel_file_is_accepted_when_present(monkeypatch):
    """The real v3 kernel.bin (git-ignored, bench/dev machines only) matches
    the pin. Skipped where the file is absent (CI)."""
    from src.utils.paths import get_app_root

    if not (get_app_root() / fk.KERNEL_FILE).is_file():
        pytest.skip("no fast_flash/kernel.bin on this machine")
    monkeypatch.setattr(fk, "_from_secure_module", lambda: None)
    monkeypatch.delenv(fk.UNPINNED_ENV, raising=False)
    kernel = fk.load_kernel()
    assert kernel is not None and kernel.pin.version == 3
    assert kernel.data.rstrip(b"\xff").endswith(b"NF03*SBL_END")
