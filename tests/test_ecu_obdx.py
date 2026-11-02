"""Tests for the OBDX Pro USB transport (DVI binary protocol over serial).

Byte strings marked "bench" were captured from an OBDX Pro VX talking to the
bench NC ECU on 2026-10-17.
"""

import pytest

from src.ecu.obdx import (
    DviFrameParser,
    ObdxError,
    ObdxTransport,
    build_frame,
    build_network_write,
    checksum,
    decode_network_frame,
)
from src.ecu.protocol import UDSConnection
from src.ecu.transport import FakeTransport, create_ecu_transport

H = bytes.fromhex

# bench: TesterPresent reply, Single Frame "01 7E 00" padded to 8 bytes
SF_REPLY = H("08 0c 00 00 07 e8 01 7e 00 00 00 00 00 00 7d")
# bench: ROM ID reply, reassembled First Frame "10 1B" + 27 payload bytes
FF_REPLY = H(
    "08 21 00 00 07 e8 10 1b 62 e6 11 53 57 2d 4c 46 44 4a 45 41 30 30 30 2e"
    " 48 45 58 00 00 00 00 00 00 00 00 43"
)


class TestFraming:
    def test_checksum_matches_bench_frames(self):
        assert build_frame(0x22, 1, 3) == H("22 01 03 d9")
        assert build_frame(0x3A, 2, 0, 0xFE) == H("3a 02 00 fe c5")
        assert checksum(SF_REPLY[:-1]) == SF_REPLY[-1]

    def test_short_network_write_matches_bench(self):
        assert build_network_write(0x7E0, b"\x3e\x01") == H(
            "10 06 00 00 07 e0 3e 01 c3"
        )

    def test_long_network_write_uses_two_byte_length(self):
        frame = build_network_write(0x7E0, bytes(300))
        assert frame[0] == 0x11
        assert (frame[1] << 8) | frame[2] == 304
        assert len(frame) == 3 + 304 + 1
        assert checksum(frame[:-1]) == frame[-1]

    def test_decode_single_frame(self):
        # The NC ECU answers TesterPresent with a bare 0x7E (length nibble 1).
        assert decode_network_frame(SF_REPLY) == (0x7E8, b"\x7e")

    def test_decode_first_frame_reassembled(self):
        can_id, payload = decode_network_frame(FF_REPLY)
        assert can_id == 0x7E8
        assert len(payload) == 0x1B
        assert payload[3:19] == b"SW-LFDJEA000.HEX"

    def test_decode_truncated_message_is_none(self):
        body = H("00 00 07 e8 10 1b 62 e6")
        frame = build_frame(0x08, len(body), *body)
        assert decode_network_frame(frame) is None


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class TestParser:
    def test_splits_concatenated_and_partial_chunks(self):
        ack = H("20 01 00 de")
        stream = ack + SF_REPLY + FF_REPLY
        parser = DviFrameParser()
        frames = []
        for i in range(0, len(stream), 5):
            frames += parser.feed(stream[i : i + 5])
        assert frames == [ack, SF_REPLY, FF_REPLY]

    def test_long_network_frame(self):
        body = H("00 00 07 e8 03 7f 22 31")
        frame = build_frame(0x09, 0, len(body), *body)
        assert DviFrameParser().feed(frame) == [frame]

    def test_skips_ascii_left_over_from_the_mode_switch(self):
        assert DviFrameParser().feed(b"OK\r\n>" + SF_REPLY) == [SF_REPLY]

    def test_resyncs_after_corrupt_frame(self):
        bad = bytearray(SF_REPLY)
        bad[-1] ^= 0xFF
        parser = DviFrameParser()
        assert parser.feed(bytes(bad) + SF_REPLY) == [SF_REPLY]

    def test_corrupt_frame_does_not_wedge_the_stream(self):
        """Review finding: one corrupted response whose data holds 0x09/0x7F
        must not stall the parser for the rest of the session."""
        clock = FakeClock()
        parser = DviFrameParser(clock=clock)
        body = H("00 00 07 e8 03 7f 36 78")
        bad = bytearray(build_frame(0x08, len(body), *body))
        bad[-1] ^= 0xFF
        good = H("20 01 00 de") + SF_REPLY
        frames = parser.feed(bytes(bad) + H("09 ff ff") + good * 3)
        for _ in range(3):
            clock.now += 1.0
            frames += parser.feed()
        frames += parser.feed(good)
        assert frames.count(SF_REPLY) >= 1
        assert frames[-1] == SF_REPLY

    def test_impossible_long_length_is_rejected(self):
        parser = DviFrameParser()
        assert parser.feed(H("09 ff ff") + SF_REPLY) == [SF_REPLY]

    def test_slow_steady_large_frame_is_kept(self):
        """A 1 KB response trickling in for longer than the timeout is fine as
        long as bytes keep arriving (the timeout means 'nothing received')."""
        clock = FakeClock()
        parser = DviFrameParser(clock=clock)
        body = bytes(4 + 2 + 1025)
        frame = build_frame(0x09, len(body) >> 8, len(body) & 0xFF, *body)
        frames = []
        for i in range(0, len(frame), 64):
            frames += parser.feed(frame[i : i + 64])
            clock.now += 0.04
            frames += parser.feed()
        assert frames == [frame]

    def test_short_first_frame_does_not_raise(self):
        body = H("00 00 07 e8 10")
        assert decode_network_frame(build_frame(0x08, len(body), *body)) is None

    def test_stalled_partial_frame_times_out(self):
        clock = FakeClock()
        parser = DviFrameParser(clock=clock)
        assert parser.feed(H("08 40 00 00")) == []  # declares 64 bytes, sends 2
        clock.now += 0.1
        assert parser.feed(SF_REPLY) == []  # still waiting inside the window
        clock.now += 1.0
        assert parser.feed() == [SF_REPLY]


def _split_host_frames(data):
    """Split bytes the host wrote into DVI command frames (0x11 = 2-byte len)."""
    frames = []
    while data:
        if data[0] == 0x11:
            total = ((data[1] << 8) | data[2]) + 4
        else:
            total = data[1] + 3
        frames.append(data[:total])
        data = data[total:]
    return frames


class FakeAdapter:
    """Serial stand-in that answers like an OBDX Pro in DVI mode."""

    def __init__(
        self,
        responder=None,
        dvi_reply=b"DXDP1 \r\nOK\r\n>",
        write_status=0,
        name=b"OBDX Pro VX",
        refuse_setting=None,
    ):
        self.rx = bytearray()
        self.writes = []
        self.closed = False
        self._responder = responder or (lambda payload: [])
        self._dvi_reply = dvi_reply
        self._write_status = write_status
        self._name = name
        self._refuse_setting = refuse_setting

    @property
    def in_waiting(self):
        return len(self.rx)

    def read(self, n):
        out = bytes(self.rx[:n])
        del self.rx[:n]
        return out

    def reset_input_buffer(self):
        self.rx.clear()

    def flush(self):
        pass

    def close(self):
        self.closed = True

    def write(self, data):
        self.writes.append(bytes(data))
        if data.endswith(b"\r"):
            if data.startswith(b"DXDP1"):
                self.rx += self._dvi_reply
            return
        for frame in _split_host_frames(bytes(data)):
            self._answer(frame)

    def _answer(self, frame):
        cmd = frame[0]
        if cmd == 0x22:
            info = {3: self._name, 1: bytes([1, 0, 1, 0])}[frame[2]]
            body = bytes([frame[2]]) + info
            self.rx += build_frame(0x32, len(body), *body)
        elif cmd in (0x31, 0x34):
            body = frame[2:-1]
            if self._refuse_setting is not None and body[0] == self._refuse_setting:
                body = body[:1] + bytes(len(body) - 1)
            self.rx += build_frame(cmd + 0x10, len(body), *body)
        elif cmd == 0x3A:
            self.rx += build_frame(0x4A, 4, 0, 0xFE, 0x33, 0xFC)
        elif cmd in (0x10, 0x11):
            payload = frame[6:-1] if cmd == 0x10 else frame[7:-1]
            self.rx += build_frame(cmd + 0x10, 1, self._write_status)
            for reply in self._responder(payload):
                self.rx += reply


def _sf(can_id, payload):
    body = can_id.to_bytes(4, "big") + bytes([len(payload)]) + payload
    body += bytes(4 + 8 - len(body))
    return build_frame(0x08, len(body), *body)


def _long_rx(can_id, payload):
    """A reassembled multi-frame response as a 0x09 (2-byte length) frame."""
    body = can_id.to_bytes(4, "big") + bytes([0x10 | (len(payload) >> 8)])
    body += bytes([len(payload) & 0xFF]) + payload
    return build_frame(0x09, len(body) >> 8, len(body) & 0xFF, *body)


def _transport(adapter, **kwargs):
    return ObdxTransport(port="COM9", serial_factory=lambda port: adapter, **kwargs)


class TestTransport:
    def test_open_configures_every_isotp_setting(self):
        adapter = FakeAdapter()
        t = _transport(adapter)
        t.open()
        assert t.adapter_name == "OBDX Pro VX"
        assert t.firmware == "1.0.1.0"
        assert t.is_tested_model
        for setting in (
            "31 02 01 02",  # HS-CAN
            "34 02 06 00",  # software filtering
            "34 02 0b 01",  # adapter does ISO-TP
            "34 02 0f 01",  # auto length
            "34 02 0d 00",  # pad byte
            "34 02 0e 01",  # padding on
            "34 03 10 00 0a",  # inter-frame delay
            "34 03 0a 00 01",  # flow-control delay
            "34 03 11 01 f4",  # flow-control timeout
        ):
            assert any(w.startswith(H(setting)) for w in adapter.writes), setting
        cleared = [w for w in adapter.writes if w[:3] == H("34 11 00") and w[3]]
        assert sorted(w[3] for w in cleared) == list(range(1, 28))
        assert (
            H("34 11 00 00 00 01 01 00 00 07 e8 00 00 07 ff 00 00 07 e0 dc")
            in adapter.writes
        )
        assert adapter.writes[-1] == H("31 02 02 01 c9")  # protocol enabled last

    def test_rejected_setting_fails_open(self):
        adapter = FakeAdapter(refuse_setting=0x0B)
        with pytest.raises(ObdxError, match="did not accept setting"):
            _transport(adapter).open()
        assert adapter.closed

    def test_vpw_only_model_is_refused(self):
        adapter = FakeAdapter(name=b"OBDX Pro VT")
        with pytest.raises(ObdxError, match="CAN-capable"):
            _transport(adapter).open()
        assert adapter.closed

    def test_untested_can_model_connects_but_is_flagged(self):
        t = _transport(FakeAdapter(name=b"OBDX Pro GT"))
        t.open()
        assert not t.is_tested_model

    def test_tester_present_round_trip(self):
        adapter = FakeAdapter(responder=lambda p: [_sf(0x7E8, b"\x7e")])
        t = _transport(adapter)
        t.open()
        UDSConnection(t).tester_present(timeout_ms=500)
        assert H("10 06 00 00 07 e0 3e 01 c3") in adapter.writes

    def test_reply_in_same_read_as_ack_is_kept(self):
        """The ack and the ECU response arriving in one USB read: the response
        must be queued, not dropped with the rest of the batch."""
        adapter = FakeAdapter(responder=lambda p: [_sf(0x7E8, b"\x7e")])
        t = _transport(adapter)
        t.open()
        t.send_message(b"\x3e\x01", 500)
        assert t.receive_message(50) == b"\x7e"

    def test_long_write_and_long_response(self):
        """TransferData-sized request (0x11 write, 0x21 ack) and a 0x09 reply."""
        block = bytes(range(256)) * 4
        adapter = FakeAdapter(responder=lambda p: [_long_rx(0x7E8, b"\x63" + block)])
        t = _transport(adapter)
        t.open()
        t.send_message(b"\x36" + block, 2000)
        sent = adapter.writes[-1]
        assert sent[0] == 0x11 and sent[7:-1] == b"\x36" + block
        assert t.receive_message(500) == b"\x63" + block

    def test_response_pending_reaches_the_uds_layer(self):
        replies = [_sf(0x7E8, b"\x7f\x23\x78"), _sf(0x7E8, b"\x63\xaa\xbb")]
        t = _transport(FakeAdapter(responder=lambda p: replies))
        t.open()
        resp = UDSConnection(t).send_request(0x23, b"\x00\x00\x00\x00\x00\x02")
        assert resp == b"\xaa\xbb"

    def test_receive_skips_other_can_ids(self):
        replies = [_sf(0x7E9, b"\x41\x0c"), _sf(0x7E8, b"\x62\x02\x00\x07")]
        adapter = FakeAdapter(responder=lambda p: replies)
        t = _transport(adapter)
        t.open()
        t.send_message(b"\x22\x02\x00", 500)
        assert t.receive_message(500) == b"\x62\x02\x00\x07"

    def test_receive_times_out_with_none(self):
        t = _transport(FakeAdapter())
        t.open()
        assert t.receive_message(20) is None

    def test_flush_drops_queued_responses(self):
        adapter = FakeAdapter(responder=lambda p: [_sf(0x7E8, b"\x7e")])
        t = _transport(adapter)
        t.open()
        t.send_message(b"\x3e\x01", 500)
        t.flush()
        assert t.receive_message(20) is None

    def test_refused_write_raises(self):
        t = _transport(FakeAdapter(write_status=1))
        t.open()
        with pytest.raises(ObdxError, match="refused"):
            t.send_message(b"\x3e\x01", 200)

    def test_serial_write_error_becomes_obdx_error(self):
        adapter = FakeAdapter()
        t = _transport(adapter)
        t.open()

        def unplugged(data):
            raise OSError("device disconnected")

        adapter.write = unplugged
        with pytest.raises(ObdxError, match="unplugged"):
            t.send_message(b"\x3e\x01", 200)

    def test_failed_mode_switch_closes_port(self):
        adapter = FakeAdapter(dvi_reply=b"?\r\n>")
        t = _transport(adapter)
        with pytest.raises(ObdxError, match="binary mode"):
            t.open()
        assert adapter.closed

    def test_close_disables_protocol_and_returns_to_elm_mode(self):
        adapter = FakeAdapter()
        t = _transport(adapter)
        t.open()
        t.close()
        assert adapter.writes[-2:] == [H("31 02 02 00 ca"), build_frame(0x25, 0)]
        assert adapter.closed
        t.close()  # idempotent

    def test_error_frame_after_the_reply_is_not_fatal(self):
        adapter = FakeAdapter()
        t = _transport(adapter)
        t.open()
        adapter.rx += build_frame(0x4A, 4, 0, 0xFE, 0x33, 0xFC)
        adapter.rx += build_frame(0x7F, 2, 0x10, 0x01)
        assert t._await_reply(0x4A, 0.5)[0] == 0x4A

    def test_short_voltage_reply_raises_obdx_error(self):
        adapter = FakeAdapter()
        t = _transport(adapter)
        t.open()
        adapter._answer = lambda frame: adapter.rx.extend(build_frame(0x4A, 1, 0))
        with pytest.raises(ObdxError, match="short voltage"):
            t.battery_voltage()

    def test_battery_voltage(self):
        t = _transport(FakeAdapter())
        t.open()
        assert t.battery_voltage() == pytest.approx(13.308)

    def test_no_adapter_found(self, monkeypatch):
        monkeypatch.setattr("src.ecu.obdx.find_obdx_ports", lambda: [])
        with pytest.raises(ObdxError, match="No OBDX Pro"):
            ObdxTransport().open()

    def test_auto_detect_skips_a_foreign_stm32_port(self, monkeypatch):
        """Another device using ST's generic USB ID must not block the OBDX."""
        foreign = FakeAdapter(dvi_reply=b"\x00\x00")
        obdx = FakeAdapter()
        monkeypatch.setattr("src.ecu.obdx.find_obdx_ports", lambda: ["COM3", "COM7"])
        ports = {"COM3": foreign, "COM7": obdx}
        t = ObdxTransport(serial_factory=lambda port: ports[port])
        t.open()
        assert t.port == "COM7"
        assert foreign.closed and not obdx.closed

    def test_busy_port_reports_another_program(self):
        def busy(port):
            raise OSError("Access is denied")

        with pytest.raises(ObdxError, match="in use"):
            ObdxTransport(port="COM7", serial_factory=busy).open()

    def test_factory_builds_obdx_transport(self):
        t = create_ecu_transport({"kind": "obdx", "port": "COM3"})
        assert isinstance(t, ObdxTransport)
        assert t.port == "COM3"


class TestSession:
    def test_obdx_session_connects_and_tears_down(self, monkeypatch):
        from src.ecu.session import ECUSession

        fake = FakeTransport(responses=[b"\x7e\x01"], description="OBDX (COM9)")
        monkeypatch.setattr("src.ecu.transport.create_ecu_transport", lambda cfg: fake)
        session = ECUSession(adapter_config={"kind": "obdx", "port": "COM9"})
        assert session.adapter_kind == "obdx"
        session.connect_ecu()
        assert session.is_connected
        assert session.transport is fake
        assert fake.sent_payloads == [b"\x3e\x01"]
        session.disconnect_ecu()
        assert fake.closed
        assert session.transport is None


def test_session_object_holds_no_shared_config():
    """The session copies the port out of the config (no shared mutable dict)."""
    from src.ecu.session import ECUSession

    cfg = {"kind": "obdx", "port": "COM9"}
    session = ECUSession(adapter_config=cfg)
    cfg["port"] = "COM1"
    assert session._obdx_port == "COM9"
