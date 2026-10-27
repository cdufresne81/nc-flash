"""
Tests for the built-in OpenPort 2.0 driver choice in src/ecu/j2534.py.

With the default driver setting, J2534Device.open() tries NC Flash's bundled
driver (WinUSB + libusb) first and falls back to Tactrix's driver when it finds
no WinUSB-bound OpenPort. A custom driver path is always used as set.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

from src.ecu import j2534
from src.ecu.j2534 import (
    J2534Device,
    find_builtin_openport_dll,
    is_automatic_driver,
)
from src.ecu.constants import DEFAULT_J2534_DLL
from src.ecu.exceptions import J2534DeviceNotFound, J2534DLLNotFound

BUILTIN = r"C:\NCFlash\_internal\openport\j2534.dll"
ERR_DEVICE_NOT_CONNECTED = 0x08
ERR_DEVICE_IN_USE = 0x0E


@pytest.fixture(autouse=True)
def builtin_driver_free():
    """Each test starts with the process-wide built-in driver guard free."""
    j2534._builtin_in_use = False
    yield
    j2534._builtin_in_use = False


def _device(dll_path=DEFAULT_J2534_DLL):
    """A J2534Device without __init__ (no bridge auto-build)."""
    dev = J2534Device.__new__(J2534Device)
    dev._dll_path = dll_path
    dev._automatic = is_automatic_driver(dll_path)
    dev._holds_builtin = False
    dev._dll = None
    dev._bridge = None
    dev._device_id = None
    dev._funcs = {}
    return dev


def _passthru_open(result, device_id=7):
    """A PassThruOpen stand-in that fills in the device ID like the DLL does."""

    def call(_name, ref):
        if result == 0:
            ref._obj.value = device_id
        return result

    return MagicMock(side_effect=call)


def _builtin_dll(open_result):
    dll = MagicMock(name="builtin j2534.dll")
    dll.PassThruOpen = _passthru_open(open_result)
    dll.PassThruGetLastError = None
    return dll


def _official_ensure_dll(open_result=0):
    """Replacement for _ensure_dll that loads a fake Tactrix DLL."""

    def ensure(self):
        self._dll = MagicMock(name="op20pt32.dll")
        self._funcs = {
            "PassThruOpen": _passthru_open(open_result, device_id=3),
            "PassThruGetLastError": None,
        }

    return ensure


@pytest.fixture
def builtin_found():
    with patch.object(j2534, "find_builtin_openport_dll", return_value=BUILTIN):
        yield


class TestIsAutomaticDriver:
    @pytest.mark.parametrize("path", ["", DEFAULT_J2534_DLL, "OP20PT32.DLL"])
    def test_default_setting_is_automatic(self, path):
        assert is_automatic_driver(path)

    def test_custom_path_is_not_automatic(self):
        assert not is_automatic_driver(r"C:\Drivers\other_pt.dll")


class TestBuiltinChoice:
    def test_winusb_openport_opens_on_builtin(self, builtin_found):
        dev = _device()
        dll = _builtin_dll(open_result=0)
        with patch.object(j2534.ctypes, "WinDLL", return_value=dll, create=True):
            with patch.object(J2534Device, "_ensure_dll") as ensure:
                dev.open()
        ensure.assert_not_called()
        assert dev._device_id == 7
        assert dev.driver_path == BUILTIN
        assert dev._funcs["PassThruOpen"] is dll.PassThruOpen

    def test_no_winusb_openport_falls_back_to_tactrix(self, builtin_found):
        dev = _device()
        dll = _builtin_dll(open_result=ERR_DEVICE_NOT_CONNECTED)
        with patch.object(j2534.ctypes, "WinDLL", return_value=dll, create=True):
            with patch.object(J2534Device, "_ensure_dll", _official_ensure_dll()):
                dev.open()
        assert dev._device_id == 3
        assert dev.driver_path == DEFAULT_J2534_DLL
        assert dev._funcs["PassThruOpen"] is not dll.PassThruOpen

    def test_builtin_load_failure_falls_back(self, builtin_found):
        dev = _device()
        with patch.object(
            j2534.ctypes, "WinDLL", side_effect=OSError("bad image"), create=True
        ):
            with patch.object(J2534Device, "_ensure_dll", _official_ensure_dll()):
                dev.open()
        assert dev._device_id == 3
        assert dev.driver_path == DEFAULT_J2534_DLL

    def test_builtin_missing_uses_tactrix(self):
        dev = _device()
        with patch.object(j2534, "find_builtin_openport_dll", return_value=None):
            with patch.object(j2534.ctypes, "WinDLL", create=True) as windll:
                with patch.object(J2534Device, "_ensure_dll", _official_ensure_dll()):
                    dev.open()
        windll.assert_not_called()
        assert dev._device_id == 3

    def test_custom_driver_never_tries_builtin(self, builtin_found):
        dev = _device(r"C:\Drivers\other_pt.dll")
        with patch.object(j2534.ctypes, "WinDLL", create=True) as windll:
            with patch.object(J2534Device, "_ensure_dll", _official_ensure_dll()):
                dev.open()
        windll.assert_not_called()
        assert dev.driver_path == r"C:\Drivers\other_pt.dll"


class TestOneBuiltinConnection:
    """The built-in driver keeps one global connection per process: a second
    PassThruOpen while one is live must never reach the DLL."""

    def _open_builtin(self, dll):
        dev = _device()
        with patch.object(j2534.ctypes, "WinDLL", return_value=dll, create=True):
            dev.open()
        return dev

    def test_second_open_is_refused_before_the_dll(self, builtin_found):
        dll = _builtin_dll(open_result=0)
        self._open_builtin(dll)
        second = _device()
        with patch.object(j2534.ctypes, "WinDLL", return_value=dll, create=True):
            with patch.object(J2534Device, "_ensure_dll") as ensure:
                with pytest.raises(J2534DeviceNotFound, match="already in use"):
                    second.open()
        assert dll.PassThruOpen.call_count == 1
        ensure.assert_not_called()  # no fallback to Tactrix's driver either

    def test_close_frees_it_for_the_next_connection(self, builtin_found):
        dll = _builtin_dll(open_result=0)
        dll.PassThruClose = MagicMock(return_value=0)
        first = self._open_builtin(dll)
        first.close()
        second = self._open_builtin(dll)
        assert second.driver_path == BUILTIN
        assert dll.PassThruOpen.call_count == 2

    def test_failed_builtin_open_does_not_hold_it(self, builtin_found):
        dll = _builtin_dll(open_result=ERR_DEVICE_NOT_CONNECTED)
        dev = _device()
        with patch.object(j2534.ctypes, "WinDLL", return_value=dll, create=True):
            with patch.object(J2534Device, "_ensure_dll", _official_ensure_dll()):
                dev.open()
        assert not j2534._builtin_in_use

    def test_driver_reporting_in_use_is_an_error_not_a_fallback(self, builtin_found):
        dll = _builtin_dll(open_result=ERR_DEVICE_IN_USE)
        dev = _device()
        with patch.object(j2534.ctypes, "WinDLL", return_value=dll, create=True):
            with patch.object(J2534Device, "_ensure_dll") as ensure:
                with pytest.raises(J2534DeviceNotFound, match="already in use"):
                    dev.open()
        ensure.assert_not_called()
        assert not j2534._builtin_in_use


class TestNotFoundHint:
    def _open_failing(self, dll_path):
        dev = _device(dll_path)
        with patch.object(j2534, "find_builtin_openport_dll", return_value=None):
            with patch.object(
                J2534Device,
                "_ensure_dll",
                _official_ensure_dll(open_result=ERR_DEVICE_NOT_CONNECTED),
            ):
                with pytest.raises(J2534DeviceNotFound) as exc:
                    dev.open()
        return str(exc.value)

    def test_automatic_driver_points_at_the_winusb_fix(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        message = self._open_failing(DEFAULT_J2534_DLL)
        assert "Code 39" in message and "WinUSB" in message

    def test_custom_driver_gets_no_hint(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        message = self._open_failing(r"C:\Drivers\other_pt.dll")
        assert "WinUSB" not in message

    def test_tactrix_driver_not_installed_gets_the_hint(self, monkeypatch):
        """WinUSB user, cable unplugged, no Tactrix software: still pointed home."""
        monkeypatch.setattr(sys, "platform", "win32")
        dev = _device()

        def missing(_self):
            raise J2534DLLNotFound("Cannot load J2534 DLL 'op20pt32.dll'")

        with patch.object(j2534, "find_builtin_openport_dll", return_value=None):
            with patch.object(J2534Device, "_ensure_dll", missing):
                with pytest.raises(J2534DLLNotFound) as exc:
                    dev.open()
        assert j2534.OPENPORT_WINDOWS11_HELP in str(exc.value)


class TestFindBuiltinDll:
    def test_none_off_windows(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert find_builtin_openport_dll() is None

    def test_found_in_frozen_bundle(self, monkeypatch, tmp_path):
        dll = tmp_path / "openport" / "j2534.dll"
        dll.parent.mkdir()
        dll.write_bytes(b"MZ")
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
        assert find_builtin_openport_dll() == str(dll)
