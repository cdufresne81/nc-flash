"""
Tests for src/ecu/openport_setup.py (reading the Tactrix OpenPort's Windows
driver state, building the elevated WinUSB-switch command) and for the
PowerShell script it runs (parsed and its pure functions run; nothing installed).
"""

import base64
import re
import subprocess
import sys
from pathlib import Path

import pytest

from src.ecu import openport_setup
from src.ecu.openport_setup import (
    OpenPortDriver,
    OpenPortState,
    classify,
    detect_openport,
    elevated_setup_command,
    find_setup_script,
    new_log_name,
    powershell_path,
    read_setup_result,
)

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell")


def _script_text():
    return Path(find_setup_script()).read_text(encoding="utf-8")


def _run_ps(command):
    return subprocess.run(
        [powershell_path(), "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=60,
    )


class TestClassify:
    @pytest.mark.parametrize(
        "service, problem, expected",
        [
            ("WinUSB", 0, OpenPortDriver.WINUSB),
            ("winusb", 0, OpenPortDriver.WINUSB),
            ("openport", 0, OpenPortDriver.TACTRIX),
            ("openport", 39, OpenPortDriver.BLOCKED),  # Windows 11 cross-cert block
            ("openport", 48, OpenPortDriver.BLOCKED),  # driver blocked
            ("WinUSB", 52, OpenPortDriver.BLOCKED),  # unsigned driver
            (None, 28, OpenPortDriver.NO_DRIVER),
            (None, 0, OpenPortDriver.NO_DRIVER),
            ("WinUSB", 10, OpenPortDriver.OTHER),  # bound but failed to start
            ("ftdibus", 0, OpenPortDriver.OTHER),
        ],
    )
    def test_states(self, service, problem, expected):
        assert classify(service, problem) is expected

    def test_only_blocked_or_driverless_needs_winusb(self):
        needs = {d for d in OpenPortDriver if OpenPortState(d).needs_winusb}
        assert needs == {OpenPortDriver.BLOCKED, OpenPortDriver.NO_DRIVER}


class TestDetect:
    def _patch(self, monkeypatch, instances, devnodes):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(
            openport_setup, "_find_present_instances", lambda: instances
        )
        monkeypatch.setattr(openport_setup, "_read_devnode", lambda i: devnodes[i])

    def test_not_found_off_windows(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert detect_openport().driver is OpenPortDriver.NOT_FOUND

    def test_unplugged_cable(self, monkeypatch):
        self._patch(monkeypatch, [], {})
        assert detect_openport().driver is OpenPortDriver.NOT_FOUND

    def test_blocked_cable(self, monkeypatch):
        self._patch(monkeypatch, ["A"], {"A": ("openport", 39)})
        state = detect_openport()
        assert state.driver is OpenPortDriver.BLOCKED and state.problem == 39

    def test_of_two_cables_the_one_needing_the_switch_is_reported(self, monkeypatch):
        self._patch(
            monkeypatch, ["A", "B"], {"A": ("WinUSB", 0), "B": ("openport", 39)}
        )
        assert detect_openport().driver is OpenPortDriver.BLOCKED

    @pytest.mark.parametrize(
        "instance, ours",
        [
            (r"USB\VID_0403&PID_CC4D\TAHJALXT", True),  # no microSD card
            (r"usb\vid_0403&pid_cc4d\tahjalxt", True),
            (r"USB\VID_0403&PID_CC4C&MI_00\6&1FEF8493&1&0000", True),  # with a card
            (r"USB\VID_0403&PID_CC4C&MI_02\6&1FEF8493&1&0002", False),  # the card
            (r"USB\VID_0403&PID_CC4C\TAHJALXT", False),  # composite parent
            (r"USB\VID_0403&PID_CC4B\TAHJALXT", False),  # bootloader
            (r"USB\VID_0403&PID_CC4DX\1", False),
        ],
    )
    def test_only_the_j2534_part_of_the_cable_is_matched(self, instance, ours):
        assert openport_setup.is_openport_instance(instance) is ours

    def test_windows_api_error_reads_as_not_found(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")

        def fail():
            raise OSError("cfgmgr32 unavailable")

        monkeypatch.setattr(openport_setup, "_find_present_instances", fail)
        assert detect_openport().driver is OpenPortDriver.NOT_FOUND


class TestElevatedCommand:
    def _inner(self, cmd):
        encoded = re.search(r"'-EncodedCommand','([A-Za-z0-9+/=]+)'", cmd[4]).group(1)
        return base64.b64decode(encoded).decode("utf-16-le")

    def test_runs_the_script_text_elevated_with_full_paths(self):
        cmd = elevated_setup_command("Install", _script_text(), "setup_abc.log")
        assert cmd[0] == powershell_path() and cmd[0].lower().endswith(
            r"system32\windowspowershell\v1.0\powershell.exe"
        )
        outer = cmd[4]
        assert "-Verb RunAs -Wait -PassThru" in outer
        assert "exit $p.ExitCode" in outer
        # Only a cancelled UAC prompt (1223) is "declined"; other failures differ.
        assert "NativeErrorCode -eq 1223) { exit 2 } else { exit 4 }" in outer
        assert ".ps1" not in outer  # the text itself, never a path to it
        inner = self._inner(cmd)
        assert inner.endswith("} -Action 'Install' -LogName 'setup_abc.log'")
        assert "New-OpenPortInf" in inner and "\n#" not in inner

    def test_here_strings_are_kept_exactly(self):
        script = (
            "# comment\n"
            "    $x = 1\n"
            "Add-Type -MemberDefinition @'\n"
            "    # a C# line that starts with a hash\n"
            '    [DllImport("newdev.dll")]\n'
            "'@\n"
        )
        inner = self._inner(elevated_setup_command("Install", script, "setup_x.log"))
        assert "    # a C# line that starts with a hash\n" in inner
        assert '    [DllImport("newdev.dll")]\n\'@' in inner
        assert "# comment" not in inner and "\n$x = 1\n" in inner

    def test_real_script_keeps_its_native_call(self):
        inner = self._inner(
            elevated_setup_command("Install", _script_text(), "setup_x.log")
        )
        assert "public static extern bool UpdateDriverForPlugAndPlayDevicesW(" in inner

    def test_fits_on_a_command_line(self):
        cmd = elevated_setup_command("Restore", _script_text(), new_log_name())
        assert len(cmd[4]) < 30000

    @pytest.mark.parametrize(
        "name", ["a b.log", "..\\x.log", "x.txt", ".log", "x';.log"]
    )
    def test_bad_log_names_are_refused(self, name):
        with pytest.raises(ValueError):
            elevated_setup_command("Install", "", name)

    def test_unknown_action_is_refused(self):
        with pytest.raises(ValueError):
            elevated_setup_command("Format", "", "setup_x.log")

    @windows_only
    def test_decoded_command_parses(self, tmp_path):
        cmd = elevated_setup_command("Install", _script_text(), "setup_x.log")
        path = tmp_path / "inner.ps1"
        path.write_text(self._inner(cmd), encoding="utf-8")
        r = _run_ps(
            "$e=$null; [void][System.Management.Automation.Language.Parser]::"
            f"ParseFile('{path}', [ref]$null, [ref]$e); $e.Count"
        )
        assert r.stdout.strip() == "0", r.stdout + r.stderr


class TestSetupResult:
    @pytest.mark.parametrize(
        "text, result",
        [
            ("Cable before: x\nRESULT: OK\n", "ok"),
            ("RESULT: RESTART\n", "restart"),
            ("RESULT: FAILED: pnputil failed\n", "failed"),
            ("Cable before: x\n", None),
        ],
    )
    def test_results(self, tmp_path, text, result):
        log = tmp_path / "l.log"
        log.write_text(text, encoding="utf-8")
        assert read_setup_result(str(log)) == (result, text)

    def test_missing_log(self, tmp_path):
        assert read_setup_result(str(tmp_path / "none.log")) == (None, "")


def test_setup_script_found_in_repo():
    path = find_setup_script()
    assert path is not None and Path(path).name == "openport_winusb.ps1"


@windows_only
class TestScript:
    """Loads the script's functions without running an action."""

    def _call(self, body):
        return _run_ps(f". '{find_setup_script()}'; {body}")

    def test_parses(self):
        r = _run_ps(
            "$e=$null; [void][System.Management.Automation.Language.Parser]::"
            f"ParseFile('{find_setup_script()}', [ref]$null, [ref]$e); $e.Count"
        )
        assert r.stdout.strip() == "0", r.stdout + r.stderr

    @pytest.mark.parametrize("culture", ["de-DE", "fr-CA", "hu-HU", "en-US"])
    def test_driver_date_is_us_format_in_every_region(self, culture):
        r = self._call(
            f"[Threading.Thread]::CurrentThread.CurrentCulture = '{culture}'; "
            "New-OpenPortInf 'm' ([datetime]'2026-10-26')"
        )
        assert "DriverVer   = 10/26/2026,1.0.0.0" in r.stdout, r.stdout + r.stderr

    def test_inf_targets_only_the_openport(self):
        r = self._call("New-OpenPortInf 'marker-1' (Get-Date)")
        inf = r.stdout
        assert "marker-1" in inf
        assert "Include = winusb.inf" in inf and "CoInstallers" not in inf
        ids = re.findall(r"USB_Install, (\S+)", inf)
        # every architecture: the cable without and with a microSD card, never
        # the card's own storage function
        assert ids == ["USB\\VID_0403&PID_CC4D", "USB\\VID_0403&PID_CC4C&MI_00"] * 3

    @pytest.mark.parametrize(
        "devices, expected",
        [
            ([], "not plugged in"),
            (["USB\\VID_0403&PID_CC4C\\TAHJALXT"], "not plugged in"),  # parent
            (["USB\\VID_0403&PID_CC4C&MI_02\\6&1&0002"], "not plugged in"),  # card
            (
                ["USB\\VID_0403&PID_CC4DX\\1", "USB\\VID_0403&PID_CC4C&MI_001\\x"],
                "not plugged in",
            ),
            (["USB\\VID_0403&PID_CC4D\\TAHJALXT"], "CC4D present"),
            (
                [
                    "USB\\VID_0403&PID_CC4C\\TAHJALXT",
                    "USB\\VID_0403&PID_CC4C&MI_02\\6&1&0002",
                    "usb\\vid_0403&pid_cc4c&mi_00\\6&1&0000",
                ],
                "CC4C&MI_00 present",
            ),
            (
                ["USB\\VID_0403&PID_CC4D\\A", "USB\\VID_0403&PID_CC4C&MI_00\\B"],
                "CC4D present,CC4C&MI_00 present",
            ),
        ],
    )
    def test_cable_state_reads_only_the_j2534_part(self, devices, expected):
        # Stub Windows' device list; the service name echoes the instance found.
        listed = ",".join("'" + d.replace("'", "''") + "'" for d in devices)
        r = self._call(
            f"$devs = @({listed}); "
            "function Get-PnpDevice { param([switch]$PresentOnly, $ErrorAction) "
            "$devs | ForEach-Object { [pscustomobject]@{ InstanceId = $_ } } }; "
            "function Get-PnpDeviceProperty { param($InstanceId, $KeyName) "
            "if ($KeyName -eq 'DEVPKEY_Device_Service') "
            "{ [pscustomobject]@{ Data = $InstanceId } } "
            "else { [pscustomobject]@{ Data = 0 } } }; "
            "$found = @($HardwareIds | Where-Object "
            "{ (Get-CableState @($_)) -ne 'not plugged in' }); "
            "if ($found.Count) { ($found | ForEach-Object "
            "{ ($_ -replace '^USB\\\\VID_0403&PID_', '') + ' present' }) -join ',' } "
            "else { Get-CableState }"
        )
        assert r.stdout.strip() == expected, r.stdout + r.stderr

    @pytest.mark.parametrize(
        "text, ours",
        [
            ('USB\\VID_0403&PID_CC4D\nInclude = winusb.inf\nProvider = "libwdi"', True),
            (
                "USB\\VID_0403&PID_CC4D\nInclude = winusb.inf\n[Strings]\n"
                'Provider   = "NC Flash"',
                True,
            ),
            # Tactrix's own setup file: never removed
            ('USB\\VID_0403&PID_CC4D\nProvider = "Tactrix Inc."\nopenport.sys', False),
            # someone else's WinUSB setup for the same cable
            ('USB\\VID_0403&PID_CC4D\nInclude = winusb.inf\nProvider = "Acme"', False),
            # Zadig's setup for the cable with a microSD card
            (
                'USB\\VID_0403&PID_CC4C&MI_00\nInclude = winusb.inf\nProvider = "libwdi"',
                True,
            ),
            # a WinUSB setup for the card itself is not ours
            (
                'USB\\VID_0403&PID_CC4C&MI_02\nInclude = winusb.inf\nProvider = "libwdi"',
                False,
            ),
        ],
    )
    def test_restore_only_removes_ncflash_or_zadig_setups(self, text, ours):
        escaped = text.replace("'", "''")
        r = self._call(f"Test-OurPackage '{escaped}'")
        assert r.stdout.strip() == str(ours), r.stdout + r.stderr
