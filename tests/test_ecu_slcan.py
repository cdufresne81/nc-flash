"""
Tests for src/ecu/slcan.py — the LAWICEL/SLCAN ASCII codec.

This codec frames every CAN message that reaches the ECU over a WiCAN/
TCP transport. A mis-encoded ID or a dropped data byte would corrupt a
flash write, so the round-trip (encode -> decode), the malformed-input
rejection, and the TCP stream re-assembly (coalesced/split chunks) are
all pinned here. The module is pure/headless — no I/O is exercised.
"""

import pytest

from src.ecu.slcan import (
    BEL,
    BITRATE_500K,
    BITRATE_CODES,
    CLOSE,
    CR,
    EXT_ID_MAX,
    LISTEN,
    MAX_DLC,
    OPEN,
    SERIAL,
    STD_ID_MAX,
    VERSION,
    SlcanError,
    SlcanFrameStream,
    bitrate_command,
    decode_frame,
    encode_data_frame,
    is_error_ack,
)
from src.ecu.constants import CAN_REQUEST_ID, CAN_RESPONSE_ID
from src.ecu.exceptions import ECUError

# ---------------------------------------------------------------------------
# Encoding — exact byte layout
# ---------------------------------------------------------------------------


class TestEncodeDataFrame:
    def test_standard_frame_byte_for_byte(self):
        # ID 0x123, DLC 4, payload DE AD BE EF
        line = encode_data_frame(0x123, b"\xde\xad\xbe\xef")
        assert line == b"t1234DEADBEEF\r"

    def test_id_is_three_uppercase_hex_digits_zero_padded(self):
        line = encode_data_frame(0x7, b"")
        assert line == b"t0070\r"  # 't' + '007' (ID) + '0' (DLC) + '\r'

    def test_can_request_id_empty_payload(self):
        line = encode_data_frame(CAN_REQUEST_ID, b"")
        assert line == b"t7E00\r"

    def test_can_response_id_with_payload(self):
        line = encode_data_frame(CAN_RESPONSE_ID, b"\x50\x02")
        assert line == b"t7E825002\r"

    def test_data_hex_is_uppercase(self):
        line = encode_data_frame(0x100, b"\xab\xcd")
        assert line == b"t10025002".replace(b"5002", b"ABCD") + b"\r"
        assert b"abcd" not in line

    def test_trailing_carriage_return(self):
        line = encode_data_frame(0x7E0, b"\x10\x02")
        assert line.endswith(b"\r")

    def test_max_standard_id(self):
        line = encode_data_frame(STD_ID_MAX, b"")
        assert line == b"t7FF0\r"

    def test_extended_frame_uses_eight_id_digits(self):
        line = encode_data_frame(0x18DAF110, b"\x01\x02", extended=True)
        assert line == b"T18DAF11020102\r"

    def test_extended_id_zero_padded_to_eight(self):
        line = encode_data_frame(0x1, b"", extended=True)
        assert line == b"T000000010\r"

    @pytest.mark.parametrize("n", range(0, MAX_DLC + 1))
    def test_all_dlc_lengths(self, n):
        payload = bytes(range(n))
        line = encode_data_frame(0x200, payload)
        # 't' + 3 id + 1 dlc + 2*n data + '\r'
        assert len(line) == 1 + 3 + 1 + 2 * n + 1
        assert line[4:5] == f"{n:X}".encode()

    def test_payload_too_long_raises(self):
        with pytest.raises(SlcanError):
            encode_data_frame(0x100, bytes(9))

    def test_standard_id_overflow_raises(self):
        with pytest.raises(SlcanError):
            encode_data_frame(0x800, b"")

    def test_extended_id_overflow_raises(self):
        with pytest.raises(SlcanError):
            encode_data_frame(EXT_ID_MAX + 1, b"", extended=True)

    def test_negative_id_raises(self):
        with pytest.raises(SlcanError):
            encode_data_frame(-1, b"")


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


class TestDecodeFrame:
    def test_standard_frame(self):
        assert decode_frame(b"t1234DEADBEEF\r") == (0x123, b"\xde\xad\xbe\xef")

    def test_standard_frame_without_trailing_cr(self):
        assert decode_frame(b"t1234DEADBEEF") == (0x123, b"\xde\xad\xbe\xef")

    def test_empty_payload(self):
        assert decode_frame(b"t7E00\r") == (0x7E0, b"")

    def test_str_input_accepted(self):
        assert decode_frame("t7E825002\r") == (0x7E8, b"\x50\x02")

    def test_extended_frame(self):
        assert decode_frame(b"T18DAF11020102\r") == (0x18DAF110, b"\x01\x02")

    def test_lowercase_hex_payload_accepted(self):
        # Adapters emit uppercase, but be liberal in what we accept.
        assert decode_frame(b"t1002dead\r") == (0x100, b"\xde\xad")

    def test_bare_cr_ack_returns_none(self):
        assert decode_frame(CR) is None

    def test_empty_line_returns_none(self):
        assert decode_frame(b"") is None

    def test_bare_bel_returns_none(self):
        assert decode_frame(BEL) is None

    def test_dlc_length_mismatch_raises(self):
        # DLC says 4 bytes but only 2 provided.
        with pytest.raises(SlcanError):
            decode_frame(b"t1004DEAD\r")

    def test_dlc_too_large_raises(self):
        with pytest.raises(SlcanError):
            decode_frame(b"t1009" + b"00" * 9 + b"\r")

    def test_non_hex_id_raises(self):
        with pytest.raises(SlcanError):
            decode_frame(b"tXYZ0\r")

    def test_non_hex_payload_raises(self):
        with pytest.raises(SlcanError):
            decode_frame(b"t1002ZZ00\r")

    def test_truncated_frame_raises(self):
        with pytest.raises(SlcanError):
            decode_frame(b"t10\r")

    def test_unsupported_frame_type_raises(self):
        # 'r' (RTR) is not a data frame and is not handled by this codec.
        with pytest.raises(SlcanError):
            decode_frame(b"r1230\r")

    def test_non_ascii_raises(self):
        with pytest.raises(SlcanError):
            decode_frame(b"t10\xff2dead\r")


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    @pytest.mark.parametrize(
        "can_id",
        [0x000, 0x001, CAN_REQUEST_ID, CAN_RESPONSE_ID, 0x100, STD_ID_MAX],
    )
    @pytest.mark.parametrize("length", range(0, MAX_DLC + 1))
    def test_standard_roundtrip(self, can_id, length):
        payload = bytes((i * 7 + 1) & 0xFF for i in range(length))
        encoded = encode_data_frame(can_id, payload)
        assert decode_frame(encoded) == (can_id, payload)

    @pytest.mark.parametrize("can_id", [0x0, 0x18DAF110, 0x1FFFFFFF, EXT_ID_MAX])
    @pytest.mark.parametrize("length", [0, 1, 8])
    def test_extended_roundtrip(self, can_id, length):
        payload = bytes(range(length))
        encoded = encode_data_frame(can_id, payload, extended=True)
        assert decode_frame(encoded) == (can_id, payload)

    def test_uds_request_roundtrip(self):
        # A typical tester-present request from 0x7E0.
        encoded = encode_data_frame(CAN_REQUEST_ID, b"\x02\x3e\x00")
        assert decode_frame(encoded) == (CAN_REQUEST_ID, b"\x02\x3e\x00")


# ---------------------------------------------------------------------------
# Control / bitrate commands
# ---------------------------------------------------------------------------


class TestControlCommands:
    def test_open_close_bytes(self):
        assert OPEN == b"O\r"
        assert CLOSE == b"C\r"

    def test_version_serial_listen_bytes(self):
        assert VERSION == b"V\r"
        assert SERIAL == b"N\r"
        assert LISTEN == b"L\r"

    def test_bitrate_500k_constant(self):
        assert BITRATE_500K == b"S6\r"

    def test_bitrate_500k_maps_to_500000(self):
        assert BITRATE_CODES[6] == 500_000

    @pytest.mark.parametrize("code", range(0, 9))
    def test_bitrate_command_all_codes(self, code):
        assert bitrate_command(code) == f"S{code}\r".encode()

    def test_bitrate_command_500k_matches_constant(self):
        assert bitrate_command(6) == BITRATE_500K

    def test_bitrate_command_invalid_raises(self):
        with pytest.raises(SlcanError):
            bitrate_command(9)
        with pytest.raises(SlcanError):
            bitrate_command(-1)


# ---------------------------------------------------------------------------
# Ack handling
# ---------------------------------------------------------------------------


class TestAckHandling:
    def test_error_ack_int(self):
        assert is_error_ack(0x07) is True
        assert is_error_ack(0x0D) is False

    def test_error_ack_bytes(self):
        assert is_error_ack(BEL) is True
        assert is_error_ack(CR) is False

    def test_error_ack_trailing_bel(self):
        assert is_error_ack(b"t1230\x07") is True

    def test_error_ack_trailing_cr(self):
        assert is_error_ack(b"V0117\r") is False


# ---------------------------------------------------------------------------
# Streaming parser
# ---------------------------------------------------------------------------


class TestSlcanFrameStream:
    def test_single_complete_frame(self):
        stream = SlcanFrameStream()
        frames = stream.feed(b"t7E825002\r")
        assert frames == [(0x7E8, b"\x50\x02")]
        assert stream.pending == b""

    def test_coalesced_frames_in_one_chunk(self):
        stream = SlcanFrameStream()
        chunk = b"t7E00\r" + b"t7E825002\r" + b"t100100\r"
        frames = stream.feed(chunk)
        assert frames == [
            (0x7E0, b""),
            (0x7E8, b"\x50\x02"),
            (0x100, b"\x00"),
        ]

    def test_frame_split_across_two_chunks(self):
        stream = SlcanFrameStream()
        assert stream.feed(b"t7E82") == []
        assert stream.pending == b"t7E82"
        frames = stream.feed(b"5002\r")
        assert frames == [(0x7E8, b"\x50\x02")]
        assert stream.pending == b""

    def test_frame_split_across_many_chunks_byte_by_byte(self):
        stream = SlcanFrameStream()
        line = b"t7E825002\r"
        out: list = []
        for i in range(len(line)):
            out += stream.feed(line[i : i + 1])
        assert out == [(0x7E8, b"\x50\x02")]

    def test_trailing_partial_after_complete_frame(self):
        stream = SlcanFrameStream()
        frames = stream.feed(b"t7E00\rt7E8")
        assert frames == [(0x7E0, b"")]
        assert stream.pending == b"t7E8"
        frames = stream.feed(b"25002\r")
        assert frames == [(0x7E8, b"\x50\x02")]

    def test_ack_bytes_skipped_in_stream(self):
        stream = SlcanFrameStream()
        # An 'open ok' CR ack followed by a data frame.
        frames = stream.feed(b"\rt7E00\r")
        assert frames == [(0x7E0, b"")]

    def test_bel_ack_skipped_in_stream(self):
        stream = SlcanFrameStream()
        frames = stream.feed(BEL + b"\r" + b"t1230\r")
        assert frames == [(0x123, b"")]

    def test_malformed_line_in_stream_raises(self):
        stream = SlcanFrameStream()
        with pytest.raises(SlcanError):
            stream.feed(b"t1004DEAD\r")  # DLC/length mismatch

    def test_good_frames_before_malformed_line_are_discarded(self):
        # Pins the documented all-or-nothing contract: a good frame decoded
        # earlier in the SAME feed() call is NOT returned when a later line in
        # the same chunk is malformed; the whole feed() raises. The caller must
        # reset() and cannot rely on having received the leading good frame.
        stream = SlcanFrameStream()
        good = b"t7E00\r"  # valid empty-payload frame
        bad = b"t1004DEAD\r"  # DLC says 4 bytes, only 2 present -> malformed
        with pytest.raises(SlcanError):
            stream.feed(good + bad)
        # Per contract the caller resets after a malformed line; once reset the
        # stream is usable again and the previously-decoded good frame is gone.
        stream.reset()
        assert stream.pending == b""
        assert stream.feed(b"t1230\r") == [(0x123, b"")]

    def test_reset_clears_pending(self):
        stream = SlcanFrameStream()
        stream.feed(b"t7E82")
        assert stream.pending != b""
        stream.reset()
        assert stream.pending == b""

    def test_feed_iter_yields_incrementally(self):
        stream = SlcanFrameStream()
        gen = stream.feed_iter(b"t7E00\rt1230\r")
        assert next(gen) == (0x7E0, b"")
        assert next(gen) == (0x123, b"")
        with pytest.raises(StopIteration):
            next(gen)

    def test_no_cr_yields_nothing_but_buffers(self):
        stream = SlcanFrameStream()
        assert stream.feed(b"t7E0250") == []
        assert stream.pending == b"t7E0250"


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


def test_slcan_error_is_ecu_error():
    assert issubclass(SlcanError, ECUError)
