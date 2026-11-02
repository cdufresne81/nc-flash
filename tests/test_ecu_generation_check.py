"""Pre-flash ECU generation check (NC1 vs NC2).

``verify_ecu_generation`` runs on the unlocked session, before the flash counter
and RequestDownload, in both the J2534 and the WiCAN SD flash paths. It must:
judge the ECU by its boot block (never written by a flash), not by the program it
holds; refuse a ROM for the other generation with no override; let any
same-generation ROM through (NC2 cal IDs are often cross-compatible); and when
the ECU can't be read, ask which car it is from rather than block a recovery
flash, keeping the session alive while the operator answers.
"""

import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.ecu import flash_manager
from src.ecu.constants import (
    BLOCK_SIZE,
    BOOT_SIGNATURE_NC1,
    BOOT_SIGNATURE_NC2,
    GEN_DETECT_OFFSET,
    GEN_NC1,
    GEN_NC2_A,
    GEN_NC2_B,
    ROM_FLASH_START_MIN,
    ROM_SIZE,
    SBL_SIZE,
    SID_DIAGNOSTIC_SESSION,
    SID_READ_MEM_BY_ADDR,
    SID_REQUEST_DOWNLOAD,
    SID_ROUTINE_CONTROL,
    SID_SECURITY_ACCESS,
    SID_TESTER_PRESENT,
)
from src.ecu.exceptions import (
    ECUGenerationMismatchError,
    ECUGenerationUnverifiedError,
    FlashError,
    NegativeResponseError,
    UDSTimeoutError,
)
from src.ecu.flash_manager import (
    GENERATION_READ_ATTEMPTS,
    FlashManager,
    security_unlock,
    verify_ecu_generation,
)
from src.ecu.rom_utils import (
    detect_vehicle_generation,
    generation_from_boot_block,
    generation_from_byte,
)

from ecu_test_helpers import build_negative_response, build_positive_response
from test_ecu_flash_integration import _apply_secure_patches

_ROM_PATH = Path(__file__).resolve().parent.parent / "examples" / "lf9veb.bin"
_BOOT = {"NC1": BOOT_SIGNATURE_NC1, "NC2": BOOT_SIGNATURE_NC2}
_PROGRAM = {"NC1": GEN_NC1, "NC2": GEN_NC2_B}


def _refused():
    return NegativeResponseError(0x11, service_id=SID_READ_MEM_BY_ADDR)


def _memory(boot="NC2", program=None, cal=b"LF9KEC"):
    """ECU memory as 1 KB blocks: boot signature at 0, program generation byte at
    0x2030, cal ID at 0xC0046. ``boot`` may be an exception to raise instead."""
    program = program or (boot if isinstance(boot, str) else "NC2")
    blocks = {}
    if isinstance(boot, str):
        blocks[0x0] = _BOOT[boot] + bytes(BLOCK_SIZE - len(_BOOT[boot]))
    block = bytearray(BLOCK_SIZE)
    block[GEN_DETECT_OFFSET % BLOCK_SIZE] = _PROGRAM[program]
    blocks[GEN_DETECT_OFFSET - GEN_DETECT_OFFSET % BLOCK_SIZE] = bytes(block)
    block = bytearray(BLOCK_SIZE)
    block[0x46 : 0x46 + len(cal)] = cal
    blocks[0xC0000] = bytes(block)
    return blocks


def _uds(boot="NC2", program=None, cal=b"LF9KEC"):
    """Mock UDS answering memory reads from :func:`_memory`."""
    blocks = _memory(boot, program, cal)
    uds = MagicMock(name="uds")

    def rmba(address, size, **_kw):
        if address == 0x0 and isinstance(boot, Exception):
            raise boot
        return blocks.get(address, bytes(size))

    uds.read_memory_by_address.side_effect = rmba
    uds.security_access_request_seed.return_value = b"\x00\x00\x00"
    return uds


@pytest.fixture(autouse=True)
def _no_backoff():
    with patch("src.ecu.flash_manager.time.sleep"):
        yield


class TestBootSignature:
    def test_mapping(self):
        assert generation_from_boot_block(BOOT_SIGNATURE_NC1 + b"\x00") == "NC1"
        assert generation_from_boot_block(BOOT_SIGNATURE_NC2) == "NC2"
        assert generation_from_boot_block(bytes(8)) is None

    def test_example_rom_boot_and_program_agree(self):
        # The file side (0x2030, which picks the SBL) and the boot block must
        # name the same generation on a real ROM.
        rom = _ROM_PATH.read_bytes()
        assert generation_from_boot_block(rom) == detect_vehicle_generation(rom)

    @pytest.mark.parametrize(
        "byte,gen",
        [(GEN_NC1, "NC1"), (GEN_NC2_A, "NC2"), (GEN_NC2_B, "NC2"), (0x00, None)],
    )
    def test_program_byte_mapping(self, byte, gen):
        assert generation_from_byte(byte) == gen


class TestVerifyEcuGeneration:
    def test_same_generation_passes_and_reads_the_boot_block(self):
        uds = _uds("NC2")
        prompt = MagicMock()
        verify_ecu_generation(uds, "NC2", "LF9VEB", confirm_unverified=prompt)
        prompt.assert_not_called()
        first = uds.read_memory_by_address.call_args_list[0]
        assert first.args == (0x0, BLOCK_SIZE)

    def test_other_nc2_cal_id_is_allowed(self, caplog):
        # LF9KED onto an LF9KEC ECU: same generation, proven cross-compatible.
        with caplog.at_level("INFO"):
            verify_ecu_generation(_uds("NC2", cal=b"LF9KEC"), "NC2", "LF9KED")
        assert "file LF9KED, ECU LF9KEC" in caplog.text

    @pytest.mark.parametrize("rom_gen,ecu_gen", [("NC1", "NC2"), ("NC2", "NC1")])
    def test_other_generation_is_refused_without_asking(self, rom_gen, ecu_gen):
        prompt = MagicMock(return_value=rom_gen)  # even agreeing must not help
        with pytest.raises(ECUGenerationMismatchError) as info:
            verify_ecu_generation(_uds(ecu_gen), rom_gen, confirm_unverified=prompt)
        prompt.assert_not_called()
        assert info.value.rom_generation == rom_gen
        assert info.value.ecu_generation == ecu_gen
        assert "Nothing was written" in str(info.value)

    def test_after_a_wrong_flash_the_boot_block_wins(self, caplog):
        # An NC2 ECU still holding an NC1 program (the field incident): the
        # correct NC2 file is allowed, and the disagreement is logged.
        uds = _uds(boot="NC2", program="NC1")
        with caplog.at_level("WARNING"):
            verify_ecu_generation(uds, "NC2")
        assert "boot code is NC2 but the program it holds is NC1" in caplog.text

    def test_after_a_wrong_flash_the_wrong_file_stays_refused(self):
        with pytest.raises(ECUGenerationMismatchError):
            verify_ecu_generation(_uds(boot="NC2", program="NC1"), "NC1")

    def test_unreadable_program_blocks_do_not_fail_the_check(self):
        uds = _uds("NC2")
        boot = uds.read_memory_by_address.side_effect

        def rmba(address, size, **kw):
            if address:
                raise UDSTimeoutError("gone")
            return boot(address, size, **kw)

        uds.read_memory_by_address.side_effect = rmba
        verify_ecu_generation(uds, "NC2")

    def test_refusal_is_not_retried(self):
        uds = _uds(boot=_refused())
        with pytest.raises(ECUGenerationUnverifiedError, match="refused"):
            verify_ecu_generation(uds, "NC2")
        assert uds.read_memory_by_address.call_count == 1

    def test_lost_replies_are_retried_briefly(self):
        uds = _uds(boot=UDSTimeoutError("no reply"))
        with pytest.raises(ECUGenerationUnverifiedError, match="did not answer"):
            verify_ecu_generation(uds, "NC2")
        assert uds.read_memory_by_address.call_count == GENERATION_READ_ATTEMPTS
        # The tight per-block budget, not the 60 s response-pending default.
        kwargs = uds.read_memory_by_address.call_args.kwargs
        assert kwargs["timeout"] == flash_manager.READ_BLOCK_TIMEOUT_MS
        assert kwargs["pending_max"] == flash_manager.READ_BLOCK_PENDING_MAX_MS

    def test_stray_refusal_of_another_service_is_retried(self):
        uds = _uds("NC2")
        real = uds.read_memory_by_address.side_effect
        calls = []

        def rmba(address, size, **kw):
            calls.append(address)
            if len(calls) == 1:  # a late "7F 01 11" from someone else's request
                raise NegativeResponseError(0x11, service_id=0x01)
            return real(address, size, **kw)

        uds.read_memory_by_address.side_effect = rmba
        verify_ecu_generation(uds, "NC2")
        assert calls[:2] == [0x0, 0x0]

    def test_unknown_boot_code_is_unverified(self):
        uds = _uds("NC2")
        uds.read_memory_by_address.side_effect = lambda a, s, **k: bytes(s)
        with pytest.raises(ECUGenerationUnverifiedError, match="not one NC Flash"):
            verify_ecu_generation(uds, "NC2")


class TestUnverifiedPrompt:
    def test_matching_answer_reunlocks_then_continues(self):
        uds = _uds(boot=_refused())
        prompt = MagicMock(return_value="NC2")
        verify_ecu_generation(uds, "NC2", "LF9VEB", confirm_unverified=prompt)
        rom_gen, cal, reason = prompt.call_args.args
        assert (rom_gen, cal) == ("NC2", "LF9VEB")
        assert "refused" in reason
        # The answer may have taken a while: re-enter the session and unlock.
        uds.diagnostic_session.assert_called_once()
        uds.security_access_request_seed.assert_called_once()

    def test_other_generation_answer_is_refused(self):
        with pytest.raises(ECUGenerationMismatchError) as info:
            verify_ecu_generation(
                _uds(boot=_refused()),
                "NC2",
                confirm_unverified=MagicMock(return_value="NC1"),
            )
        assert info.value.from_operator
        assert "you said this car is an NC1" in str(info.value)

    def test_cancel_refuses(self):
        uds = _uds(boot=_refused())
        with pytest.raises(ECUGenerationUnverifiedError, match="cancelled"):
            verify_ecu_generation(
                uds, "NC2", confirm_unverified=MagicMock(return_value=None)
            )
        uds.diagnostic_session.assert_not_called()

    def test_no_prompt_refuses(self):
        with pytest.raises(ECUGenerationUnverifiedError):
            verify_ecu_generation(_uds(boot=_refused()), "NC2")

    def test_ecu_gone_after_the_answer_fails_cleanly(self):
        uds = _uds(boot=_refused())
        uds.diagnostic_session.side_effect = UDSTimeoutError("gone")
        with pytest.raises(FlashError, match="stopped answering"):
            verify_ecu_generation(
                uds, "NC2", confirm_unverified=MagicMock(return_value="NC2")
            )

    def test_session_is_kept_alive_while_the_operator_answers(self):
        uds = _uds(boot=_refused())
        sent_during = []

        def slow_answer(*_args):
            threading.Event().wait(0.3)  # time.sleep is patched here
            sent_during.append(uds.tester_present.call_count)
            return "NC2"

        with patch.object(flash_manager, "PROMPT_KEEPALIVE_S", 0.05):
            verify_ecu_generation(uds, "NC2", confirm_unverified=slow_answer)
        assert sent_during[0] >= 2
        # The keepalive thread is gone before the flash goes on.
        assert not any(t.name == "gen-prompt-keepalive" for t in threading.enumerate())


class TestSecurityUnlock:
    def test_zero_seed_sends_no_key(self):
        uds = MagicMock()
        uds.security_access_request_seed.return_value = b"\x00\x00\x00"
        security_unlock(uds)
        uds.security_access_send_key.assert_not_called()

    def test_seed_gets_its_key(self):
        uds = MagicMock()
        uds.security_access_request_seed.return_value = b"\x01\x02\x03"
        with patch("src.ecu.flash_manager.compute_security_key", return_value=b"\xaa"):
            security_unlock(uds)
        uds.security_access_send_key.assert_called_once_with(b"\xaa")


# --- J2534 flash path: the check sits between the unlock and the flash counter ---


def _sent_sids(mock_dev) -> list[int]:
    """Service IDs the flash wrote, in order (J2534 msgs carry a 4-byte CAN ID)."""
    sids = []
    for call in mock_dev.write_msgs.call_args_list:
        msgs = call.args[1] if len(call.args) > 1 else call.kwargs["msgs"]
        for msg in msgs:
            sids.append(msg.Data[4])
    return sids


def _auth_responses():
    return [
        [build_positive_response(SID_TESTER_PRESENT, b"\x01")],
        [build_positive_response(SID_DIAGNOSTIC_SESSION, b"\x85")],
        [build_positive_response(SID_SECURITY_ACCESS, b"\x01\xaa\xbb\xcc")],
        [build_positive_response(SID_SECURITY_ACCESS, b"\x02")],
    ]


def _check_responses(boot="NC1"):
    """The check's reads: boot block, program block, cal-ID block."""
    blocks = _memory(boot, cal=b"LFDJEA")
    return [
        [build_positive_response(SID_READ_MEM_BY_ADDR, blocks[0x0])],
        [build_positive_response(SID_READ_MEM_BY_ADDR, blocks[0x2000])],
        [build_positive_response(SID_READ_MEM_BY_ADDR, blocks[0xC0000])],
    ]


def _nc1_rom() -> bytearray:
    rom = bytearray(ROM_SIZE)
    rom[GEN_DETECT_OFFSET] = GEN_NC1
    return rom


class TestJ2534FlashPath:
    @_apply_secure_patches
    @patch("src.ecu.j2534.J2534Device")
    @patch("src.ecu.j2534.setup_isotp_flow_control", return_value=100)
    def test_mismatch_never_reaches_counter_or_erase(self, _fc, MockDevice):
        mock_dev = MockDevice.return_value
        mock_dev.connect.return_value = 1
        # The ECU's boot code says NC2; the file is NC1.
        mock_dev.read_msgs.side_effect = _auth_responses() + _check_responses("NC2")
        with pytest.raises(ECUGenerationMismatchError):
            FlashManager().flash_rom(_nc1_rom())
        sids = _sent_sids(mock_dev)
        assert SID_READ_MEM_BY_ADDR in sids
        assert SID_ROUTINE_CONTROL not in sids
        assert SID_REQUEST_DOWNLOAD not in sids

    @_apply_secure_patches
    @patch("src.ecu.j2534.J2534Device")
    @patch("src.ecu.j2534.setup_isotp_flow_control", return_value=100)
    def test_bootloader_without_prompt_refuses_before_erase(self, _fc, MockDevice):
        mock_dev = MockDevice.return_value
        mock_dev.connect.return_value = 1
        mock_dev.read_msgs.side_effect = _auth_responses() + [
            [build_negative_response(SID_READ_MEM_BY_ADDR, 0x11)],
        ]
        with pytest.raises(ECUGenerationUnverifiedError):
            FlashManager().flash_rom(_nc1_rom())
        assert SID_REQUEST_DOWNLOAD not in _sent_sids(mock_dev)

    @_apply_secure_patches
    @patch("src.ecu.j2534.J2534Device")
    @patch("src.ecu.j2534.setup_isotp_flow_control", return_value=100)
    def test_bootloader_with_cancel_stops_before_erase(self, _fc, MockDevice):
        mock_dev = MockDevice.return_value
        mock_dev.connect.return_value = 1
        mock_dev.read_msgs.side_effect = _auth_responses() + [
            [build_negative_response(SID_READ_MEM_BY_ADDR, 0x11)],
        ]
        fm = FlashManager()
        prompt = MagicMock(return_value=None)
        fm.set_unverified_generation_prompt(prompt)
        with pytest.raises(ECUGenerationUnverifiedError):
            fm.flash_rom(_nc1_rom())
        prompt.assert_called_once()
        assert SID_REQUEST_DOWNLOAD not in _sent_sids(mock_dev)

    @_apply_secure_patches
    @patch("src.ecu.j2534.J2534Device")
    @patch("src.ecu.j2534.setup_isotp_flow_control", return_value=100)
    def test_match_goes_on_to_counter_then_erase(self, _fc, MockDevice):
        from test_ecu_flash_integration import _build_flash_responses

        mock_dev = MockDevice.return_value
        mock_dev.connect.return_value = 1
        num_sbl = SBL_SIZE // BLOCK_SIZE
        num_program = (ROM_SIZE - ROM_FLASH_START_MIN + BLOCK_SIZE - 1) // BLOCK_SIZE
        mock_dev.read_msgs.side_effect = _build_flash_responses(num_sbl, num_program)
        FlashManager().flash_rom(_nc1_rom())
        sids = _sent_sids(mock_dev)
        # The check's reads come BEFORE the flash counter (after it, the ECU
        # refuses memory reads), and the erase comes last.
        assert sids.index(SID_READ_MEM_BY_ADDR) < sids.index(SID_ROUTINE_CONTROL)
        assert sids.index(SID_ROUTINE_CONTROL) < sids.index(SID_REQUEST_DOWNLOAD)
