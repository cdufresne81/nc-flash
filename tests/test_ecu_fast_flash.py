"""Tests for src/ecu/fast_flash.py against a simulated boot loader + fast-flash kernel."""

import lzma
import zlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.ecu import fast_flash as ff
from src.ecu.constants import FLASH_COUNTER_OFFSET, ROM_SIZE
from src.ecu.exceptions import (
    ECUGenerationMismatchError,
    FastFlashUnavailable,
    FlashAbortedError,
    FlashError,
    J2534Error,
    NegativeResponseError,
    UDSTimeoutError,
)
from src.ecu.obdx import ObdxError
from src.ecu.protocol import UDSConnection
from src.ecu.transport import EcuTransport

ENTRY_POINTER = 0xFFFFC


def make_kernel(size=0xC00, marker_at_end=True):
    body = bytearray(b"\x00" * (size - 0x400) + b"\x09" * 0x300 + b"\xff" * 0x100)
    if marker_at_end:
        body[size - 0x100 - 12 : size - 0x100] = b"NF01*SBL_END"
    return bytes(body)


def make_image(seed=1):
    """A 1 MB image with data in every block, blank areas, and a valid entry pointer."""
    img = bytearray(b"\xff" * ROM_SIZE)
    for blk in range(16):
        start, size = ff.block_range(blk)
        for off in range(0, min(size, 0x1800), 0x400):
            img[start + off : start + off + 0x400] = bytes(
                (seed * 7 + blk * 13 + off // 0x400 + i) & 0xFF or 1
                for i in range(0x400)
            )
    img[FLASH_COUNTER_OFFSET : FLASH_COUNTER_OFFSET + 128] = b"\xff" * 128
    img[ENTRY_POINTER : ENTRY_POINTER + 4] = (0x2000).to_bytes(4, "big")
    return bytes(img)


class SimEcu(EcuTransport):
    """Boot loader until a kernel is uploaded, then the stage-1 kernel protocol."""

    def __init__(self, flash, flash_ready=True, corrupt_block=None):
        self.flash = bytearray(flash)
        self.flash_ready = flash_ready
        self.corrupt_block = corrupt_block
        self.kernel_running = False
        self.upload_left = 0
        self.upload = bytearray()
        self.ops = []  # ("erase", blk) / ("program", addr, len)
        self.requests = []
        self._out = []
        self.ptr = self.left = 0
        self.open_range = False
        self.last_erased = False
        self.corrupt_commit = False

    # -- EcuTransport --
    def open(self):
        pass

    def close(self):
        pass

    @property
    def description(self):
        return "SimEcu"

    def send_message(self, payload, timeout_ms):
        self.requests.append(bytes(payload))
        self._out.extend(self._handle(bytes(payload)))

    def receive_message(self, timeout_ms):
        return self._out.pop(0) if self._out else None

    # -- behaviour --
    def _neg(self, sid, nrc):
        return [bytes([0x7F, sid, nrc])]

    def _app_invalid(self):
        return (
            self.last_erased
            and self.flash[ENTRY_POINTER : ENTRY_POINTER + 4] == b"\xff\xff\xff\xff"
        )

    def _handle(self, p):
        sid = p[0]
        if not self.kernel_running:
            if sid == 0x34:
                size = int.from_bytes(p[5:9], "big") - ff.LOADER_SIZE_BASE
                if size <= 0 or size % 0x400 or size > 0x3000:
                    return self._neg(0x34, 0x31)
                self.upload_left = size
                self.upload = bytearray()
                return [bytes([0x74, 0x04, 0x01])]
            if sid == 0x36 and self.upload_left and len(p) == 0x401:
                early = b"SBL_END" in self.upload
                self.upload += p[1:]
                self.upload_left -= 0x400
                if self.upload_left:
                    return [bytes([0x76])]
                if early or b"SBL_END" not in p[1:]:
                    return self._neg(0x36, 0x72)
                self.kernel_running = True
                return [bytes([0x7F, 0x36, 0x78]), bytes([0x76])]
            return self._neg(sid, 0x11)
        if sid == 0x31 and p[1] == 0x00:
            return [bytes([0x71, 0x00]) + b"NF" + bytes([1, 0, int(self.flash_ready)])]
        if sid == 0x31 and p[1] == 0x01 and len(p) == 8:
            a, n = int.from_bytes(p[2:5], "big"), int.from_bytes(p[5:8], "big")
            if n == 0 or a + n > ROM_SIZE:
                return self._neg(0x31, 0x31)
            return [
                bytes([0x71, 0x01])
                + zlib.crc32(self.flash[a : a + n]).to_bytes(4, "big")
            ]
        if sid == 0x31 and p[1] == 0x02 and len(p) == 3:
            blk = p[2]
            if not 2 <= blk <= 15:
                return self._neg(0x31, 0x31)
            if not self.flash_ready or (blk != 15 and not self._app_invalid()):
                return self._neg(0x31, 0x22)
            start, size = ff.block_range(blk)
            self.flash[start : start + size] = b"\xff" * size
            if blk == 15:
                self.flash[FLASH_COUNTER_OFFSET : FLASH_COUNTER_OFFSET + 8] = (
                    b"\x00\x01\xff\xfe_WDS"
                )
            if blk == 15:
                self.last_erased = True
            self.ops.append(("erase", blk))
            return [bytes([0x7F, 0x31, 0x78]), bytes([0x71, 0x02, blk])]
        if sid == 0x34 and len(p) == 9:
            a, n = int.from_bytes(p[1:5], "big"), int.from_bytes(p[5:9], "big")
            self.open_range = False
            if a < 0x2000 or n == 0 or a + n > ROM_SIZE or a % 128 or n % 128:
                return self._neg(0x34, 0x31)
            if a + n > ff.COMMIT_PAGE and not (a == ff.COMMIT_PAGE and n == 128):
                return self._neg(0x34, 0x31)
            if a < 0xE0000 and not self._app_invalid():
                return self._neg(0x34, 0x22)
            self.ptr, self.left, self.open_range = a, n, True
            return [bytes([0x74])]
        if sid == 0x36:
            data = p[1:]
            if not self.open_range:
                return self._neg(0x36, 0x24)
            if not data or len(data) % 128 or len(data) > self.left:
                return self._neg(0x36, 0x31)
            for off in range(0, len(data), 128):
                a = self.ptr + off
                page = data[off : off + 128]
                if a == FLASH_COUNTER_OFFSET:
                    continue
                if self.flash[a : a + 128] != b"\xff" * 128:
                    return self._neg(0x36, 0x72)
                self.flash[a : a + 128] = page
                if a == ff.COMMIT_PAGE:
                    self.last_erased = False
                    if self.corrupt_commit:
                        self.flash[a + 127] ^= 0x01
            if self.corrupt_block is not None:
                start, size = ff.block_range(self.corrupt_block)
                if start <= self.ptr < start + size:
                    self.flash[self.ptr] ^= 0x01
            self.ops.append(("program", self.ptr, len(data)))
            self.ptr += len(data)
            self.left -= len(data)
            return [bytes([0x76])]
        if sid == 0x37:
            if not self.open_range or self.left:
                return self._neg(0x37, 0x24)
            self.open_range = False
            return [bytes([0x77])]
        if sid == 0x11:
            return [bytes([0x51])]
        return self._neg(sid, 0x11)


def same_outside_counter(flash, image):
    a, b = FLASH_COUNTER_OFFSET, FLASH_COUNTER_OFFSET + 128
    return flash[0x2000:a] == image[0x2000:a] and flash[b:] == image[b:]


def modified(image, addresses):
    out = bytearray(image)
    for a in addresses:
        out[a] ^= 0x5A
    return bytes(out)


class TestKernelValidation:
    def test_accepts_marker_in_last_block(self):
        ff.validate_kernel(make_kernel())

    @pytest.mark.parametrize("size", [0, 0xBFF, 0x3400])
    def test_rejects_bad_size(self, size):
        with pytest.raises(FlashError):
            ff.validate_kernel(b"\x00" * size)

    def test_rejects_missing_marker(self):
        with pytest.raises(FlashError):
            ff.validate_kernel(make_kernel(marker_at_end=False))

    def test_rejects_marker_in_earlier_block(self):
        k = bytearray(make_kernel())
        k[0x100:0x108] = b"*SBL_END"
        with pytest.raises(FlashError):
            ff.validate_kernel(bytes(k))


class TestCompareRanges:
    def test_last_block_leaves_out_the_counter_page(self):
        assert ff.compare_ranges(15) == [(0xE0000, 0x1FB00), (0xFFB80, 0x480)]
        assert ff.compare_ranges(15, with_commit=False)[1] == (0xFFB80, 0x400)

    def test_blocks_cover_the_rom(self):
        assert sum(ff.block_range(b)[1] for b in range(16)) == ROM_SIZE


class TestCompareOnly:
    def test_reports_changed_blocks_and_changes_nothing(self):
        ecu_img = make_image()
        want = modified(ecu_img, [0x20010, 0xC0400, 0xE0020])
        ecu = SimEcu(ecu_img)
        r = ff.compare_only(UDSConnection(ecu), make_kernel(), want)
        assert r.changed == [9, 14, 15]
        assert r.kernel.flash_ready and r.kernel.version == 1
        assert bytes(ecu.flash) == ecu_img and ecu.ops == []

    def test_counter_page_is_ignored(self):
        ecu_img = make_image()
        want = modified(ecu_img, [FLASH_COUNTER_OFFSET + 3])
        r = ff.compare_only(UDSConnection(SimEcu(ecu_img)), make_kernel(), want)
        assert r.changed == []

    def test_unknown_program_is_refused(self):
        ecu = SimEcu(make_image())
        ecu._handle_orig = ecu._handle
        ecu._handle = lambda p: (
            [bytes([0x71, 0x00]) + b"XX\x01\x00\x01"]
            if p[:2] == b"\x31\x00"
            else ecu._handle_orig(p)
        )
        with pytest.raises(FlashError):
            ff.compare_only(UDSConnection(ecu), make_kernel(), make_image())


class TestFastFlash:
    def test_rewrites_only_changed_blocks_last_block_first_and_last(self):
        ecu_img = make_image()
        want = modified(ecu_img, [0x8010, 0xA0400])
        ecu = SimEcu(ecu_img)
        r = ff.fast_flash(UDSConnection(ecu), make_kernel(), want)
        assert r.rewritten == [8, 13, 15]
        assert same_outside_counter(bytes(ecu.flash), want)
        erases = [op[1] for op in ecu.ops if op[0] == "erase"]
        assert erases == [15, 8, 13]
        assert ecu.ops[0] == ("erase", 15)
        assert ecu.ops[-1] == ("program", ff.COMMIT_PAGE, 128)
        assert bytes(ecu.flash[:0x2000]) == ecu_img[:0x2000]

    def test_identical_image_touches_nothing(self):
        img = make_image()
        ecu = SimEcu(img)
        r = ff.fast_flash(UDSConnection(ecu), make_kernel(), img)
        assert r.rewritten == [] and ecu.ops == [] and bytes(ecu.flash) == img

    def test_blank_pieces_are_not_sent(self):
        ecu_img = make_image()
        want = modified(ecu_img, [0xE0010])
        ecu = SimEcu(ecu_img)
        r = ff.fast_flash(UDSConnection(ecu), make_kernel(), want)
        assert r.bytes_sent < 0x20000 // 4
        assert same_outside_counter(bytes(ecu.flash), want)

    def test_recovers_an_ecu_left_without_an_application(self):
        want = make_image()
        broken = bytearray(want)
        broken[0xE0000:] = b"\xff" * 0x20000
        broken[0x40000:0x60000] = b"\xff" * 0x20000
        ecu = SimEcu(bytes(broken))
        r = ff.fast_flash(UDSConnection(ecu), make_kernel(), want)
        assert r.interrupted_before
        assert r.rewritten == list(range(2, 16))
        assert same_outside_counter(bytes(ecu.flash), want)

    def test_flash_not_ready_stops_before_any_erase(self):
        ecu_img = make_image()
        ecu = SimEcu(ecu_img, flash_ready=False)
        with pytest.raises(FlashError):
            ff.fast_flash(
                UDSConnection(ecu), make_kernel(), modified(ecu_img, [0xE0010])
            )
        assert ecu.ops == [] and bytes(ecu.flash) == ecu_img

    def test_bad_write_is_caught_by_the_final_compare(self):
        ecu_img = make_image()
        ecu = SimEcu(ecu_img, corrupt_block=9)
        with pytest.raises(FlashError, match="verification FAILED"):
            ff.fast_flash(
                UDSConnection(ecu), make_kernel(), modified(ecu_img, [0x20010])
            )

    def test_bad_block_leaves_the_ecu_in_its_boot_loader(self):
        ecu_img = make_image()
        ecu = SimEcu(ecu_img, corrupt_block=9)
        with pytest.raises(FlashError):
            ff.fast_flash(
                UDSConnection(ecu), make_kernel(), modified(ecu_img, [0x20010])
            )
        assert bytes(ecu.flash[ENTRY_POINTER : ENTRY_POINTER + 4]) == b"\xff" * 4

    def test_failed_commit_erases_the_last_block_again(self):
        ecu_img = make_image()
        ecu = SimEcu(ecu_img)
        ecu.corrupt_commit = True
        with pytest.raises(FlashError, match="final page"):
            ff.fast_flash(
                UDSConnection(ecu), make_kernel(), modified(ecu_img, [0xE0010])
            )
        assert ecu.ops[-1] == ("erase", 15)
        assert bytes(ecu.flash[ENTRY_POINTER : ENTRY_POINTER + 4]) == b"\xff" * 4

    def test_image_without_entry_pointer_is_refused_before_any_contact(self):
        img = bytearray(make_image())
        img[ENTRY_POINTER : ENTRY_POINTER + 4] = b"\xff" * 4
        ecu = SimEcu(make_image())
        with pytest.raises(FlashError):
            ff.fast_flash(UDSConnection(ecu), make_kernel(), bytes(img))
        assert ecu.requests == []

    def test_foreign_boot_block_is_reported_not_written(self):
        ecu_img = make_image()
        want = modified(ecu_img, [0x0FFB, 0xE0010])
        ecu = SimEcu(ecu_img)
        r = ff.fast_flash(UDSConnection(ecu), make_kernel(), want)
        assert not r.boot_block_same
        assert bytes(ecu.flash[:0x2000]) == ecu_img[:0x2000]

    def test_abort_before_the_first_erase_changes_nothing(self):
        ecu_img = make_image()
        ecu = SimEcu(ecu_img)
        with pytest.raises(FlashAbortedError):
            ff.fast_flash(
                UDSConnection(ecu),
                make_kernel(),
                modified(ecu_img, [0xE0010]),
                abort=lambda: True,
            )
        assert ecu.ops == []

    def test_boot_blocks_are_never_requested(self):
        ecu_img = make_image()
        want = modified(ecu_img, [0x0010, 0x1010, 0x2010])
        ecu = SimEcu(ecu_img)
        r = ff.fast_flash(UDSConnection(ecu), make_kernel(), want)
        assert r.rewritten == [2, 15]
        assert bytes(ecu.flash[:0x2000]) == ecu_img[:0x2000]
        for req in ecu.requests:
            if (
                ecu.kernel_running
                and req[0] == 0x34
                and len(req) == 9
                and req[1:5] != b"\x00\x00\x80\x00"
            ):
                assert int.from_bytes(req[1:5], "big") >= 0x2000

    def test_erase_helper_refuses_boot_blocks_without_asking_the_ecu(self):
        ecu = SimEcu(make_image())
        for blk in (0, 1, 16):
            with pytest.raises(FlashError):
                ff.erase_block(UDSConnection(ecu), blk)
        assert ecu.requests == []


class LossyEcu(SimEcu):
    """Loses one write: the request (nothing programmed) or the answer (programmed)."""

    def __init__(self, flash, lose_at, lose_answer):
        super().__init__(flash)
        self.lose_at, self.lose_answer, self.writes = lose_at, lose_answer, 0

    def send_message(self, payload, timeout_ms):
        if self.kernel_running and payload[0] == 0x36:
            self.writes += 1
            if self.writes == self.lose_at:
                self.requests.append(bytes(payload))
                if self.lose_answer:
                    self._handle(bytes(payload))  # programmed, answer lost
                return
        super().send_message(payload, timeout_ms)


class TestLossyLink:
    @pytest.mark.parametrize("lose_answer", [False, True])
    def test_lost_write_is_recovered_without_misplaced_data(
        self, monkeypatch, lose_answer
    ):
        monkeypatch.setattr(ff.time, "sleep", lambda s: None)
        ecu_img = make_image()
        want = modified(ecu_img, [0x20010, 0x20410])
        ecu = LossyEcu(ecu_img, lose_at=3, lose_answer=lose_answer)
        r = ff.fast_flash(UDSConnection(ecu), make_kernel(), want, chunk=256)
        assert r.rewritten == [9, 15]
        assert same_outside_counter(bytes(ecu.flash), want)


class LossyUploadEcu(SimEcu):
    """Boot loader that takes one upload block but loses its answer, then
    refuses the resend with NRC 0x22 (seen on the bench, 2026-11-15)."""

    def __init__(self, flash, lose_block, accept_restart=True):
        super().__init__(flash)
        self.lose_block, self.accept_restart = lose_block, accept_restart
        self.blocks = 0
        self.out_of_step = False
        self.downloads = 0

    def send_message(self, payload, timeout_ms):
        if not self.kernel_running:
            if payload[0] == 0x34:
                self.downloads += 1
                if self.downloads > 1 and not self.accept_restart:
                    self.requests.append(bytes(payload))
                    self._out.extend(self._neg(0x34, 0x22))
                    return
                self.out_of_step = False
            elif payload[0] == 0x36:
                if self.out_of_step:
                    self.requests.append(bytes(payload))
                    self._out.extend(self._neg(0x36, 0x22))
                    return
                self.blocks += 1
                if self.blocks == self.lose_block:
                    self.requests.append(bytes(payload))
                    self._handle(bytes(payload))  # taken, answer lost
                    self.out_of_step = not self.kernel_running
                    return
        super().send_message(payload, timeout_ms)


class TestUploadLostAnswer:
    def test_refused_resend_restarts_the_upload(self, monkeypatch):
        monkeypatch.setattr(ff.time, "sleep", lambda s: None)
        ecu = LossyUploadEcu(make_image(), lose_block=1)
        ff.upload_kernel(UDSConnection(ecu), make_kernel())
        assert ecu.kernel_running and ecu.downloads == 2
        assert bytes(ecu.upload) == make_kernel()

    def test_lost_answer_of_the_last_block_means_the_kernel_runs(self, monkeypatch):
        monkeypatch.setattr(ff.time, "sleep", lambda s: None)
        n = len(make_kernel()) // 0x400
        ecu = LossyUploadEcu(make_image(), lose_block=n)
        ff.upload_kernel(UDSConnection(ecu), make_kernel())
        assert ecu.kernel_running and ecu.downloads == 1

    def test_restart_refused_fails_cleanly_with_nothing_written(self, monkeypatch):
        monkeypatch.setattr(ff.time, "sleep", lambda s: None)
        ecu = LossyUploadEcu(make_image(), lose_block=1, accept_restart=False)
        with pytest.raises(FlashError, match="Nothing was erased"):
            ff.upload_kernel(UDSConnection(ecu), make_kernel())
        assert not ecu.kernel_running and ecu.ops == []


class TestUpload:
    def test_kernel_announced_with_loader_size_base(self):
        ecu = SimEcu(make_image())
        ff.upload_kernel(UDSConnection(ecu), make_kernel())
        assert ecu.kernel_running
        assert ecu.requests[0] == b"\x34\x00\x00\x80\x00\x00\x0f\xec\x00"
        assert bytes(ecu.upload) == make_kernel()


class SimEcuV2(SimEcu):
    """Kernel v2: also takes LZMA-compressed ranges (34 10 lp pb addr len, 36 seq stream)."""

    def __init__(self, flash, lose_answer_at=None):
        super().__init__(flash)
        self.lz = None
        self.pieces = 0
        self.lose_answer_at = lose_answer_at

    def send_message(self, payload, timeout_ms):
        if self.kernel_running and payload[0] == 0x36 and self.lz is not None:
            self.pieces += 1
            if self.pieces == self.lose_answer_at:
                self.requests.append(bytes(payload))
                self._handle(bytes(payload))  # used, answer lost
                return
        super().send_message(payload, timeout_ms)

    def _handle(self, p):
        if self.kernel_running and p[:2] == b"\x31\x00":
            return [bytes([0x71, 0x00]) + b"NF" + bytes([2, 0, 1])]
        if self.kernel_running and p[0] == 0x34:
            self.lz = None
            if len(p) == 12 and p[1] == 0x10:
                a, n = int.from_bytes(p[4:8], "big"), int.from_bytes(p[8:12], "big")
                if a <= FLASH_COUNTER_OFFSET < a + n or a == ff.COMMIT_PAGE:
                    return self._neg(0x34, 0x31)
                if a < 0xE0000 and not self._app_invalid():
                    return self._neg(0x34, 0x22)
                self.lz = {
                    "a": a,
                    "n": n,
                    "lp": p[2],
                    "pb": p[3],
                    "seq": 0,
                    "buf": b"",
                    "done": False,
                }
                return [bytes([0x74])]
        if self.kernel_running and p[0] == 0x36 and self.lz is not None:
            z = self.lz
            if z["seq"] and p[1] == z["seq"]:
                return [bytes([0x76, p[1]])]
            if p[1] != (z["seq"] + 1) & 0xFF:
                self.lz = None
                return self._neg(0x36, 0x24)
            z["seq"] = p[1]
            if not z["done"]:
                z["buf"] += p[2:]
                f = [
                    {
                        "id": lzma.FILTER_LZMA1,
                        "lc": 0,
                        "lp": z["lp"],
                        "pb": z["pb"],
                        "dict_size": 1 << 20,
                    }
                ]
                d = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=f)
                out = d.decompress(z["buf"], max_length=z["n"])
                if len(out) == z["n"]:
                    a = z["a"]
                    if self.flash[a : a + z["n"]] != b"\xff" * z["n"]:
                        self.lz = None
                        return self._neg(0x36, 0x72)
                    self.flash[a : a + z["n"]] = out
                    self.ops.append(("program", a, z["n"]))
                    z["done"] = True
            return [bytes([0x76, p[1]])]
        if self.kernel_running and p[0] == 0x37 and self.lz is not None:
            ok = self.lz["done"]
            self.lz = None
            return [bytes([0x77])] if ok else self._neg(0x37, 0x24)
        return super()._handle(p)


class TestCompressed:
    def test_compressed_flash_writes_the_image_and_sends_less(self):
        ecu_img = make_image()
        want = modified(ecu_img, [0x20010, 0xC1234])
        ecu = SimEcuV2(ecu_img)
        r = ff.fast_flash(UDSConnection(ecu), make_kernel(), want)
        assert r.kernel.version == 2
        assert r.rewritten == [9, 14, 15]
        assert same_outside_counter(bytes(ecu.flash), want)
        assert any(q[:2] == b"\x34\x10" for q in ecu.requests)

    def test_no_compress_uses_plain_writes(self):
        ecu_img = make_image()
        want = modified(ecu_img, [0x20010])
        ecu = SimEcuV2(ecu_img)
        ff.fast_flash(UDSConnection(ecu), make_kernel(), want, compress=False)
        assert same_outside_counter(bytes(ecu.flash), want)
        assert not any(q[:2] == b"\x34\x10" for q in ecu.requests)

    def test_lost_piece_answer_is_resent_with_the_same_number(self, monkeypatch):
        monkeypatch.setattr(ff.time, "sleep", lambda s: None)
        ecu_img = make_image()
        want = modified(ecu_img, [0x20010])
        ecu = SimEcuV2(ecu_img, lose_answer_at=1)
        ff.fast_flash(UDSConnection(ecu), make_kernel(), want)
        assert same_outside_counter(bytes(ecu.flash), want)
        pieces = [q for q in ecu.requests if q[0] == 0x36 and len(q) > 2 and q[1] == 1]
        assert len(pieces) >= 2 and pieces[0] == pieces[1]

    def test_counter_and_commit_pages_never_sent_compressed(self):
        for blk in (14, 15):
            for a, n in ff._lz_ranges(blk):
                assert not a <= FLASH_COUNTER_OFFSET < a + n
                assert a + n <= ff.COMMIT_PAGE


class SimEcuV3(SimEcuV2):
    """Kernel v3: reports the CRC of its own image, wants block 15 erased in
    this session before ANY range, and can show the faults the review named."""

    def __init__(
        self,
        flash,
        corrupt_kernel=False,
        noise_at_piece=None,
        lose_commit_exit=False,
        refuse_piece_once=None,
        chip_class=0,
    ):
        super().__init__(flash)
        self.chip_class = chip_class
        self.corrupt_kernel = corrupt_kernel
        self.noise_at_piece = noise_at_piece
        self.lose_commit_exit = lose_commit_exit
        self.refuse_piece_once = refuse_piece_once
        self.commit_open = False

    def send_message(self, payload, timeout_ms):
        if self.kernel_running and self.lose_commit_exit:
            if payload[0] == 0x34 and len(payload) == 9:
                a = int.from_bytes(payload[1:5], "big")
                self.commit_open = a == ff.COMMIT_PAGE
            elif payload[0] == 0x37 and self.commit_open:
                self.lose_commit_exit = False
                self.requests.append(bytes(payload))
                self._handle(bytes(payload))  # range closed, answer lost
                return
        super().send_message(payload, timeout_ms)

    def _handle(self, p):
        if not self.kernel_running:
            return super()._handle(p)
        if p[0] == 0x3E:
            return [b"\x7e"]
        if p[:2] == b"\x31\x00":
            return [
                bytes([0x71, 0x00])
                + b"NF"
                + bytes([3, self.chip_class, int(self.flash_ready)])
            ]
        if p[0] == 0x31 and p[1] == 0x03 and len(p) == 4:
            n = int.from_bytes(p[2:4], "big")
            if n == 0 or n > 0x3000:
                return self._neg(0x31, 0x31)
            c = zlib.crc32(bytes(self.upload[:n])) ^ (1 if self.corrupt_kernel else 0)
            return [bytes([0x71, 0x03]) + c.to_bytes(4, "big")]
        if p[0] == 0x34 and not self._app_invalid():
            self.lz = None
            self.open_range = False
            return self._neg(0x34, 0x22)
        if p[0] == 0x36 and self.lz is not None:
            n = self.pieces  # counted by SimEcuV2.send_message
            if n == self.refuse_piece_once:
                self.refuse_piece_once = None
                self.lz = None
                return self._neg(0x36, 0x72)
            if n == self.noise_at_piece:
                # another tester's request was refused by the kernel just
                # before our piece was answered
                return [bytes([0x7F, 0x22, 0x11])] + super()._handle(p)
        return super()._handle(p)


class TestKernelV3:
    def test_self_check_runs_before_the_first_erase(self):
        ecu_img = make_image()
        want = modified(ecu_img, [0x20010])
        ecu = SimEcuV3(ecu_img)
        r = ff.fast_flash(UDSConnection(ecu), make_kernel(), want)
        assert r.rewritten == [9, 15]
        assert same_outside_counter(bytes(ecu.flash), want)
        check = next(i for i, q in enumerate(ecu.requests) if q[:2] == b"\x31\x03")
        erase = next(i for i, q in enumerate(ecu.requests) if q[:2] == b"\x31\x02")
        assert check < erase
        assert ecu.requests[check][2:] == len(make_kernel()).to_bytes(2, "big")

    def test_damaged_kernel_is_never_given_an_erase(self):
        ecu_img = make_image()
        ecu = SimEcuV3(ecu_img, corrupt_kernel=True)
        with pytest.raises(FlashError, match="does not match"):
            ff.fast_flash(
                UDSConnection(ecu), make_kernel(), modified(ecu_img, [0x20010])
            )
        assert ecu.ops == [] and bytes(ecu.flash) == ecu_img
        assert ecu.requests[-1] == b"\x11\x01"  # the kernel is told to stop

    def test_old_kernel_without_self_check_still_flashes(self):
        ecu_img = make_image()
        want = modified(ecu_img, [0x20010])
        ecu = SimEcuV2(ecu_img)
        ff.fast_flash(UDSConnection(ecu), make_kernel(), want)
        assert same_outside_counter(bytes(ecu.flash), want)
        assert not any(q[:2] == b"\x31\x03" for q in ecu.requests)

    def test_answer_to_another_tester_is_not_taken_as_a_refusal(self):
        ecu_img = make_image()
        want = modified(ecu_img, [0x20010])
        ecu = SimEcuV3(ecu_img, noise_at_piece=1)
        r = ff.fast_flash(UDSConnection(ecu), make_kernel(), want)
        assert r.rewritten == [9, 15]
        assert same_outside_counter(bytes(ecu.flash), want)
        assert [o for o in ecu.ops if o[0] == "erase"] == [("erase", 15), ("erase", 9)]

    def test_refused_piece_erases_the_block_again_and_resends(self):
        ecu_img = make_image()
        want = modified(ecu_img, [0x20010])
        ecu = SimEcuV3(ecu_img, refuse_piece_once=1)
        r = ff.fast_flash(UDSConnection(ecu), make_kernel(), want)
        assert r.rewritten == [9, 15]
        assert same_outside_counter(bytes(ecu.flash), want)
        assert [o for o in ecu.ops if o[0] == "erase"] == [
            ("erase", 15),
            ("erase", 9),
            ("erase", 9),
        ]

    def test_lost_answer_at_the_commit_does_not_erase_a_good_flash(self, monkeypatch):
        monkeypatch.setattr(ff.time, "sleep", lambda s: None)
        monkeypatch.setattr(ff, "TIMEOUT_PROGRAM_MS", 50)
        ecu_img = make_image()
        want = modified(ecu_img, [0x20010])
        ecu = SimEcuV3(ecu_img, lose_commit_exit=True)
        r = ff.fast_flash(UDSConnection(ecu), make_kernel(), want)
        assert r.rewritten == [9, 15]
        assert same_outside_counter(bytes(ecu.flash), want)
        assert [o for o in ecu.ops if o[0] == "erase"].count(("erase", 15)) == 1
        ep = ff.ENTRY_POINTER
        assert bytes(ecu.flash[ep : ep + 4]) == want[ep : ep + 4]

    def test_range_on_block_15_needs_the_erase_in_this_session(self):
        ecu = SimEcuV3(make_image())
        uds = UDSConnection(ecu)
        ff.upload_kernel(uds, make_kernel())
        with pytest.raises(Exception) as e:
            ff._open_range(uds, ff.COMMIT_PAGE, 128)
        assert getattr(e.value, "nrc", None) == 0x22


# --- Hardening for the app (policy, adapter faults, outages, stages) ---

NC2_SIG = bytes.fromhex("00000c0cffffbfa0")
NC1_SIG = bytes.fromhex("00000aacffffbfa0")


def nc2_image(seed=1):
    img = bytearray(make_image(seed))
    img[0:8] = NC2_SIG
    return bytes(img)


APP_POLICY = ff.KernelPolicy(
    chip_classes=frozenset({1}),
    versions=frozenset({3}),
    require_self_check=True,
    generation="NC2",
)


def kernel_ops(ecu):
    return [o for o in ecu.ops if o[0] == "erase"]


class TestKernelPolicy:
    def test_accepted_kernel_flashes(self):
        ecu_img = nc2_image()
        want = modified(ecu_img, [0x20010])
        ecu = SimEcuV3(ecu_img, chip_class=1)
        stages = []
        r = ff.fast_flash(
            UDSConnection(ecu),
            make_kernel(),
            want,
            policy=APP_POLICY,
            on_stage=stages.append,
        )
        assert r.rewritten == [9, 15]
        assert same_outside_counter(bytes(ecu.flash), want)
        assert stages == ["upload", "compare", "program", "verify", "commit"]

    def test_unproven_chip_class_stops_before_any_erase(self):
        ecu_img = nc2_image()
        ecu = SimEcuV3(ecu_img, chip_class=0)
        with pytest.raises(FastFlashUnavailable, match="chip class 0"):
            ff.fast_flash(
                UDSConnection(ecu),
                make_kernel(),
                modified(ecu_img, [0x20010]),
                policy=APP_POLICY,
            )
        assert ecu.ops == [] and bytes(ecu.flash) == ecu_img
        assert ecu.requests[-1] == b"\x11\x01"  # the kernel is told to stop
        assert not any(q[:2] == b"\x31\x02" for q in ecu.requests)

    def test_other_kernel_version_is_refused(self):
        ecu = SimEcuV3(nc2_image(), chip_class=1)
        policy = ff.KernelPolicy(versions=frozenset({4}))
        with pytest.raises(FastFlashUnavailable, match="version 3"):
            ff.fast_flash(
                UDSConnection(ecu),
                make_kernel(),
                modified(nc2_image(), [0x20010]),
                policy=policy,
            )
        assert ecu.ops == [] and ecu.requests[-1] == b"\x11\x01"

    def test_kernel_without_self_check_is_refused_by_the_app(self):
        ecu = SimEcuV2(nc2_image())  # v2: no self CRC
        with pytest.raises(FastFlashUnavailable, match="cannot check its own"):
            ff.fast_flash(
                UDSConnection(ecu),
                make_kernel(),
                modified(nc2_image(), [0x20010]),
                policy=ff.KernelPolicy(require_self_check=True),
            )
        assert ecu.ops == []

    def test_wrong_generation_by_boot_code_crc_is_refused(self):
        img = bytearray(nc2_image())
        img[0:8] = NC1_SIG
        ecu = SimEcuV3(bytes(img), chip_class=1)
        with pytest.raises(ECUGenerationMismatchError):
            ff.fast_flash(
                UDSConnection(ecu),
                make_kernel(),
                modified(bytes(img), [0x20010]),
                policy=APP_POLICY,
            )
        assert ecu.ops == [] and ecu.requests[-1] == b"\x11\x01"

    def test_unknown_boot_code_is_not_available(self):
        img = bytearray(nc2_image())
        img[0:8] = b"\x12" * 8
        ecu = SimEcuV3(bytes(img), chip_class=1)
        with pytest.raises(FastFlashUnavailable, match="boot code"):
            ff.fast_flash(
                UDSConnection(ecu),
                make_kernel(),
                modified(bytes(img), [0x20010]),
                policy=APP_POLICY,
            )
        assert ecu.ops == []

    def test_flash_routines_not_ready_stops_the_kernel(self):
        ecu = SimEcuV3(nc2_image(), chip_class=1)
        ecu.flash_ready = False
        with pytest.raises(FastFlashUnavailable, match="flash routines"):
            ff.fast_flash(
                UDSConnection(ecu), make_kernel(), modified(nc2_image(), [0x20010])
            )
        assert ecu.ops == [] and ecu.requests[-1] == b"\x11\x01"

    def test_unidentified_program_is_stopped_and_unavailable(self):
        ecu = SimEcuV3(nc2_image(), chip_class=1)
        handle = ecu._handle
        ecu._handle = lambda p: (
            [bytes([0x71, 0x00]) + b"NF\x09\x01\x01"]
            if ecu.kernel_running and p[:2] == b"\x31\x00"
            else handle(p)
        )
        with pytest.raises(FastFlashUnavailable, match="identify"):
            ff.fast_flash(
                UDSConnection(ecu), make_kernel(), modified(nc2_image(), [0x20010])
            )
        assert ecu.ops == [] and ecu.requests[-1] == b"\x11\x01"

    def test_boot_generation_by_crc(self):
        ecu = SimEcuV3(nc2_image(), chip_class=1)
        uds = UDSConnection(ecu)
        ff.upload_kernel(uds, make_kernel())
        assert ff.ecu_boot_generation(uds) == "NC2"
        ecu.flash[0:8] = NC1_SIG
        assert ff.ecu_boot_generation(uds) == "NC1"


class TestIdentifyParsing:
    def test_parse_identify(self):
        info = ff.parse_identify(b"\x00NF\x03\x01\x01")
        assert info == ff.KernelInfo(version=3, chip_class=1, flash_ready=True)
        assert ff.parse_identify(b"\x00XX\x03\x01\x01") is None
        assert ff.parse_identify(b"\x00NF\x03") is None

    def test_probe_kernel_sends_one_request_and_never_raises(self):
        uds = MagicMock()
        uds.send_request.side_effect = UDSTimeoutError("silent")
        assert ff.probe_kernel(uds) is None
        assert uds.send_request.call_count == 1


class _FaultyLink(EcuTransport):
    """Wraps an ECU; the Nth CRC request raises an adapter or link error."""

    def __init__(self, ecu, fail_at, error, times=1):
        self.ecu, self.fail_at, self.error, self.times = ecu, fail_at, error, times
        self.crcs = 0
        self.flushes = 0

    def open(self):
        pass

    def close(self):
        pass

    @property
    def description(self):
        return "faulty"

    def flush(self):
        self.flushes += 1

    def send_message(self, payload, timeout_ms):
        if self.ecu.kernel_running and payload[:2] == b"\x31\x01":
            self.crcs += 1
            if self.fail_at <= self.crcs < self.fail_at + self.times:
                self.ecu.requests.append(bytes(payload))
                if isinstance(self.error, Exception):
                    raise self.error
                return  # silent: the request is lost
        self.ecu.send_message(payload, timeout_ms)

    def receive_message(self, timeout_ms):
        return self.ecu.receive_message(timeout_ms)


def _fast_timeouts(monkeypatch):
    monkeypatch.setattr(ff.time, "sleep", lambda s: None)
    monkeypatch.setattr(ff, "TIMEOUT_CRC_MS", 30)


class TestAdapterFaults:
    @pytest.mark.parametrize(
        "error",
        [
            J2534Error("J2534 bridge process died unexpectedly."),
            ObdxError("OBDX refused the CAN write (status 0x01)"),
        ],
        ids=["dead_bridge", "obdx_refused_write"],
    )
    def test_adapter_fault_is_fatal_and_never_resent(self, monkeypatch, error):
        _fast_timeouts(monkeypatch)
        ecu = SimEcuV3(nc2_image(), chip_class=1)
        link = _FaultyLink(ecu, fail_at=3, error=error)
        with pytest.raises(type(error)):
            ff.compare_only(UDSConnection(link), make_kernel(), nc2_image())
        assert link.crcs == 3  # no retry after the fault
        assert ecu.ops == []

    def test_adapter_fault_after_the_first_erase_says_the_ecu_waits(self, monkeypatch):
        _fast_timeouts(monkeypatch)
        ecu_img = nc2_image()
        ecu = SimEcuV3(ecu_img, chip_class=1)
        # CRC requests before the erase: blocks 2-14 (13) + block 15 (2 ranges)
        # + boot block + entry pointer = 17; the 18th is the first verify CRC.
        link = _FaultyLink(ecu, fail_at=18, error=J2534Error("bridge died"))
        with pytest.raises(FlashError, match="waiting in its") as e:
            ff.fast_flash(
                UDSConnection(link), make_kernel(), modified(ecu_img, [0x20010])
            )
        assert isinstance(e.value.__cause__, J2534Error)
        assert link.crcs == 18
        ep = ff.ENTRY_POINTER
        assert bytes(ecu.flash[ep : ep + 4]) == b"\xff" * 4  # not committed

    def test_link_error_wrapping_an_adapter_fault_is_fatal(self):
        try:
            try:
                raise ObdxError("OBDX serial read failed (USB unplugged?)")
            except ObdxError:
                raise UDSTimeoutError("No response from ECU")
        except UDSTimeoutError as e:
            wrapped = e
        assert ff.is_adapter_fault(wrapped)
        assert not ff._is_link_error(wrapped)
        assert ff._is_link_error(UDSTimeoutError("silent"))


class TestRetryPolicy:
    def test_lost_request_is_retried_after_a_flush(self, monkeypatch):
        _fast_timeouts(monkeypatch)
        ecu = SimEcuV3(nc2_image(), chip_class=1)
        link = _FaultyLink(ecu, fail_at=2, error=UDSTimeoutError("lost"), times=2)
        r = ff.compare_only(UDSConnection(link), make_kernel(), nc2_image())
        assert r.changed == []
        assert link.flushes >= 2  # one flush before each retry

    def test_silent_link_fails_after_the_quick_retries_by_default(self, monkeypatch):
        _fast_timeouts(monkeypatch)
        waits = []
        monkeypatch.setattr(ff.time, "sleep", waits.append)
        ecu = SimEcuV3(nc2_image(), chip_class=1)
        link = _FaultyLink(ecu, fail_at=1, error=UDSTimeoutError("lost"), times=99)
        with pytest.raises(UDSTimeoutError):
            ff.compare_only(UDSConnection(link), make_kernel(), nc2_image())
        assert link.crcs == ff.WRITE_RETRIES + 1
        assert ff.LINK_OUTAGE_WAIT_S not in waits

    def test_outage_is_waited_out_only_when_asked(self, monkeypatch):
        _fast_timeouts(monkeypatch)
        waits = []
        monkeypatch.setattr(ff.time, "sleep", waits.append)
        ecu = SimEcuV3(nc2_image(), chip_class=1)
        lost = ff.WRITE_RETRIES + 3
        link = _FaultyLink(ecu, fail_at=1, error=UDSTimeoutError("lost"), times=lost)
        r = ff.compare_only(
            UDSConnection(link), make_kernel(), nc2_image(), wait_out_outages=True
        )
        assert r.changed == []
        assert waits.count(ff.LINK_OUTAGE_WAIT_S) == lost - ff.WRITE_RETRIES

    def test_outage_policy_does_not_leak_to_the_next_call(self):
        with ff._outage_policy(True):
            assert ff._outage_wait_s.get() == ff.LINK_OUTAGE_S
        assert ff._outage_wait_s.get() == 0.0


class TestObdxTransport:
    """The fast flash over the OBDX Pro: the adapter segments each 1024-byte
    request itself (long write), and the kernel's answers come back as single
    frames. Driven through the OBDX transport's fake serial port."""

    def test_compressed_piece_goes_out_as_one_long_write(self):
        import random

        from tests.test_ecu_obdx import FakeAdapter, _sf, _transport

        ecu_img = nc2_image()
        want = bytearray(ecu_img)
        rnd = random.Random(5)
        want[0x20000:0x21000] = bytes(rnd.randrange(256) for _ in range(0x1000))
        want = bytes(want)
        ecu = SimEcuV3(ecu_img, chip_class=1)

        def respond(payload):
            ecu.requests.append(bytes(payload))
            return [_sf(0x7E8, r) for r in ecu._handle(bytes(payload))]

        adapter = FakeAdapter(responder=respond)
        t = _transport(adapter)
        t.open()
        r = ff.fast_flash(UDSConnection(t), make_kernel(), want, policy=APP_POLICY)
        assert r.rewritten == [9, 15]
        assert same_outside_counter(bytes(ecu.flash), want)
        long_pieces = [
            w
            for w in adapter.writes
            if w[0] == 0x11 and w[7] == 0x36 and len(w) == 3 + 4 + 1024 + 1
        ]
        assert long_pieces, "no full 1024-byte compressed piece was written"
        assert long_pieces[0][8] == 1  # sequence number of the first piece


# --- Independent review fixes (2026-11-15) ---


class TestReviewFixes:
    def test_refusal_from_the_kernel_before_any_erase_stops_it(self):
        """Finding 8: not only a FlashError; a refused self-check (NRC) too."""
        ecu = SimEcuV3(nc2_image(), chip_class=1)
        handle = ecu._handle
        ecu._handle = lambda p: (
            [bytes([0x7F, 0x31, 0x22])]
            if ecu.kernel_running and p[:2] == b"\x31\x03"
            else handle(p)
        )
        with pytest.raises(NegativeResponseError):
            ff.fast_flash(
                UDSConnection(ecu), make_kernel(), modified(nc2_image(), [0x20010])
            )
        assert ecu.ops == [] and ecu.requests[-1] == b"\x11\x01"

    def test_late_crc_answer_is_drained_before_the_next_crc(self):
        """Finding 4: a stale 71 01 waiting in the queue must not be taken as
        the answer to the next CRC (it carries no address)."""
        ecu = SimEcuV3(nc2_image(), chip_class=1)
        uds = UDSConnection(ecu)
        ff.upload_kernel(uds, make_kernel())
        ecu._out.append(b"\x71\x01\xde\xad\xbe\xef")  # a late duplicate
        assert ff.compare_blocks(uds, nc2_image()) == ff.compare_blocks(
            uds, nc2_image()
        )
        assert all(b.same for b in ff.compare_blocks(uds, nc2_image()))

    def test_commit_refused_and_check_lost_says_it_cannot_tell(self, monkeypatch):
        """Finding 7: not the raw timeout, and not 'waiting in its boot loader'."""
        monkeypatch.setattr(ff.time, "sleep", lambda s: None)

        def send(sid, data=b"", **kw):
            if sid == 0x34:
                raise NegativeResponseError(0x22, service_id=0x34)
            raise UDSTimeoutError("lost")

        uds = MagicMock()
        uds.send_request.side_effect = send
        uds._transport.receive_message.return_value = None
        with pytest.raises(FlashError, match="cannot tell"):
            ff.commit(uds, nc2_image())

    def test_reset_is_sent_twice_at_most(self, monkeypatch):
        monkeypatch.setattr(ff.time, "sleep", lambda s: None)
        uds = MagicMock()
        uds.send_request.side_effect = UDSTimeoutError("no 51")
        uds._transport.receive_message.return_value = None
        assert ff.reset_ecu(uds) is False
        assert uds.send_request.call_count == 2


class _RecordingTransport:
    """receive_message stand-in: records timeouts, replays scripted results.
    ``("instant", None)`` returns None at once (an indication frame);
    ``("wait", None)`` sleeps the full wait (nothing queued)."""

    def __init__(self, script):
        self.script = list(script)
        self.timeouts = []

    def receive_message(self, timeout_ms):
        import time as _t

        self.timeouts.append(timeout_ms)
        kind, value = self.script.pop(0) if self.script else ("wait", None)
        if kind == "wait":
            _t.sleep(timeout_ms / 1000.0)
        return value


class TestDrain:
    """Bench 2026-11-15: receive_message(0) blocked forever on the OpenPort
    clone DLL (PassThruReadMsgs, Timeout 0), so the drain must never pass 0."""

    def test_never_reads_with_a_zero_timeout_and_at_most_16_reads(self):
        t = _RecordingTransport([("data", b"\x71\x01late")] * 40)
        ff._drain(SimpleNamespace(_transport=t))
        assert t.timeouts and 0 not in t.timeouts
        assert all(ms == ff.DRAIN_READ_MS > 0 for ms in t.timeouts)
        assert len(t.timeouts) == ff.DRAIN_MAX_READS == 16

    def test_reads_past_an_indication_frame(self):
        """Re-review R5: a None that came back at once (a J2534 indication
        without UDS payload) must not end the drain."""
        t = _RecordingTransport(
            [("instant", None), ("data", b"\x71\x01late"), ("wait", None)]
        )
        ff._drain(SimpleNamespace(_transport=t))
        assert len(t.timeouts) == 3 and t.script == []

    def test_stops_when_nothing_is_waiting(self):
        t = _RecordingTransport([("wait", None), ("data", b"never read")])
        ff._drain(SimpleNamespace(_transport=t))
        assert len(t.timeouts) == 1


class TestResumeLinkError:
    def test_link_error_in_a_resume_leaves_the_kernel_running(self, monkeypatch):
        """Re-review R3: a second resume must still be possible."""
        _fast_timeouts(monkeypatch)
        ecu = SimEcuV3(nc2_image(), chip_class=1)
        uds = UDSConnection(ecu)
        ff.upload_kernel(uds, make_kernel())
        link = _FaultyLink(ecu, fail_at=1, error=UDSTimeoutError("lost"), times=99)
        with pytest.raises(UDSTimeoutError):
            ff.fast_flash(
                UDSConnection(link),
                make_kernel(),
                modified(nc2_image(), [0x20010]),
                resume=True,
            )
        assert not any(q[:2] == b"\x11\x01" for q in ecu.requests)

    def test_link_error_in_a_new_session_stops_the_kernel(self, monkeypatch):
        _fast_timeouts(monkeypatch)
        ecu = SimEcuV3(nc2_image(), chip_class=1)
        link = _FaultyLink(ecu, fail_at=1, error=UDSTimeoutError("lost"), times=99)
        with pytest.raises(UDSTimeoutError):
            ff.compare_only(UDSConnection(link), make_kernel(), nc2_image())
        assert ecu.requests[-1] == b"\x11\x01"
