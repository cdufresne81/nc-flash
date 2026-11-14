"""Tests for src/ecu/fast_flash.py against a simulated boot loader + fast-flash kernel."""

import zlib

import pytest

from src.ecu import fast_flash as ff
from src.ecu.constants import FLASH_COUNTER_OFFSET, ROM_SIZE
from src.ecu.exceptions import FlashAbortedError, FlashError
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


class TestUpload:
    def test_kernel_announced_with_loader_size_base(self):
        ecu = SimEcu(make_image())
        ff.upload_kernel(UDSConnection(ecu), make_kernel())
        assert ecu.kernel_running
        assert ecu.requests[0] == b"\x34\x00\x00\x80\x00\x00\x0f\xec\x00"
        assert bytes(ecu.upload) == make_kernel()
