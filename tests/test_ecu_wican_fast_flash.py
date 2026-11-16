"""WiCAN fast flash client (src/ecu/wican_fast_flash.py) against a fake WiCAN.

The fake serves the firmware's HTTP surface (``/host_caps``, ``/upload/sd/<name>``,
``/fast_flash``) and a TCP port standing in for the SLCAN port 35001 that answers
the ``K`` trigger with scripted status lines in the firmware's exact formats
(nc-flash-wican-fw ``main/fast_flash_logic.c``). The real
:class:`~src.ecu.wican_transport.WiCANTransport` connects to it (socket only).
No hardware, no ECU.
"""

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import socket
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.ecu import wican_fast_flash as wff
from src.ecu.fast_flash_package import FastFlashPackage
from src.ecu.wican_sd_upload import WiCANUploadError
from src.ecu.wican_transport import WiCANTransport

RES = (
    '{"v":1,"mode":"L","ok":1,"nothing":0,"kv":3,"cls":1,"ready":1,"resumed":0,'
    '"boot_same":1,"cut":0,"diff":[3,9,15],"rewritten":[3,9,15],"bytes":61234}'
)

HAPPY = [
    b"NCFFSYNC\n",
    b"NCFFSTEP kernel\n",
    b"NCFFKERN v=3 cls=1 ready=1 resumed=0\n",
    b"NCFFPROBE ok=1 lost=0\n",
    b"NCFFSTEP compare\n",
    b"NCFFCMP diff=3,9,15 boot=1 cut=0 rewrite=3,9,15\n",
    b"NCFFSTEP write\n",
    b"NCFFPROG 0/3 blk=3\n",
    b"NCFFNOTE resend kernel a=0400\n",
    b"NCFFPROG 1/3 blk=9\n",
    b"NCFFPROG 2/3 blk=15\n",
    b"NCFFPROG 3/3 blk=0\n",
    b"NCFFSTEP verify\n",
    b"NCFFSTEP commit\n",
    b"NCFFSTEP reset\n",
    b"NCFFNOTE reset ok\n",
    b"NCFFRES " + RES.encode() + b"\n",
    b"NCFFDONE mode=L nothing=0 rewritten=3\n",
]


def fferr(st, ponr=0, app="1", nrc="E5", a="0E0000", blk=0):
    """The firmware's ff_finish_fail line (fast_flash_logic.c)."""
    return f"\r\nFFERR a={a} st={st} nrc={nrc} blk={blk} ponr={ponr} app={app}\r\n".encode()


def fferr_early(st, a="000000"):
    """The firmware's ff_fail_early line as released: no app= field."""
    return f"\r\nFFERR a={a} st={st} nrc=00 blk=0 ponr=0\r\n".encode()


NAME = "ff_x.ffp"


def fw_status(
    state,
    name=NAME,
    mode="L",
    *,
    phase=None,
    ponr=0,
    err=None,
    result=None,
    done=0,
    total=0,
):
    """GET /fast_flash exactly as ncflash_fastflash.c formats it."""
    return {
        "rev": 1,
        "live_allowed": 1,
        "state": state,
        "name": name,
        "mode": mode,
        "phase": phase or {"done": "done", "failed": "failed"}.get(state, "write"),
        "blk": 0,
        "done": done,
        "total": total,
        "ponr": ponr,
        "elapsed_ms": 12345,
        "lines_dropped": 0,
        "stack_free": 2100,
        "err": err or {"st": 0, "nrc": 0, "a": 0, "blk": 0, "ponr": 0},
        "result": result,
        "crc": None,
    }


def fw_result(ok=1, nothing=0, app="1", st=0, ponr=0):
    """The firmware's result object (ff_build_result)."""
    return {
        "v": 1,
        "mode": "L",
        "ok": ok,
        "nothing": nothing,
        "kv": 3,
        "cls": 1,
        "ready": 1,
        "resumed": 0,
        "boot_same": 1,
        "cut": 1 if app == "0" else 0,
        "diff": [3, 9, 15],
        "rewritten": [] if nothing else [3, 9, 15],
        "bytes": 0,
        "resends": 0,
        "reerases": 0,
        "restarts": 0,
        "outages": 0,
        "foreign": 0,
        "pendings": 0,
        "ms": {
            "kernel": 1,
            "compare": 1,
            "write": 1,
            "verify": 1,
            "commit": 1,
            "total": 9,
        },
        "probe": {"ok": 1, "lost": 0},
        "app": app,
        "err": {"st": st, "a": 0, "nrc": 0, "blk": 0, "ponr": ponr},
    }


#: What the firmware serves after a dongle restart past an op's point of no return
#: (ff_check_journal: state interrupted, the journal's name, ponr 1, mode "-").
INTERRUPTED = fw_status("interrupted", mode="-", phase="", ponr=1)


class FakeWiCAN:
    """HTTP + one TCP 'SLCAN port' connection, scripted."""

    def __init__(self):
        self.caps = {
            "ncfr_rev": 6,
            "protocol": "poll_log",
            "fast_flash": 1,
            "ff_live": 1,
        }
        self.status = {"state": "idle"}
        self.status_after_drop = None
        self.status_sequence = []  # served one by one after the first read
        self.status_reads = 0
        self.truncate_status = False
        self.refuse_upload = False
        self.uploads = {}
        self.script = list(HAPPY)
        self.close_after = None  # close the TCP link after this many script lines
        self.commands = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/host_caps":
                    if fake.caps is None:
                        return self._json(404, {})
                    return self._json(200, fake.caps)
                if self.path == "/fast_flash":
                    fake.status_reads += 1
                    if fake.truncate_status:
                        body = json.dumps(fake.status).encode()
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(body) + 50))
                        self.end_headers()
                        self.wfile.write(body[:10])
                        self.wfile.flush()
                        self.close_connection = True
                        return None
                    current = fake.status
                    if fake.status_sequence:
                        fake.status = fake.status_sequence.pop(0)
                    return self._json(200, current)
                return self._json(404, {})

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n)
                if not self.path.startswith("/upload/sd/"):
                    return self._json(404, {})
                if fake.refuse_upload:
                    return self._json(409, {"error": "a flash is running"})
                start = body.index(b"\r\n\r\n") + 4
                end = body.rindex(b"\r\n--")
                data = body[start:end]
                fake.uploads[self.path[len("/upload/sd/") :]] = data
                return self._json(
                    200, {"bytes_written": len(data), "crc32": zlib.crc32(data)}
                )

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.http_port = self.http.server_address[1]
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.lsock = socket.socket()
        self.lsock.bind(("127.0.0.1", 0))
        self.lsock.listen(1)
        self.tcp_port = self.lsock.getsockname()[1]
        threading.Thread(target=self._serve_tcp, daemon=True).start()

    def _serve_tcp(self):
        try:
            conn, _ = self.lsock.accept()
        except OSError:
            return
        with conn:
            buf = b""
            while b"\r" not in buf:
                chunk = conn.recv(256)
                if not chunk:
                    return
                buf += chunk
            self.commands.append(buf.split(b"\r")[0].decode())
            for i, line in enumerate(self.script):
                if self.close_after is not None and i >= self.close_after:
                    if self.status_after_drop is not None:
                        self.status = self.status_after_drop
                    return  # the link drops
                conn.sendall(line)
            time.sleep(0.5)

    def close(self):
        self.http.shutdown()
        self.lsock.close()


@pytest.fixture
def fake():
    f = FakeWiCAN()
    yield f
    f.close()


def _client(fake):
    t = WiCANTransport("127.0.0.1", fake.tcp_port)
    t.open_socket_only()
    off = [0.0]  # sleeps advance a virtual offset on top of the real clock

    def sleep(sec):
        off[0] += sec

    c = wff.WiCANFastFlashClient(
        t,
        http_port=fake.http_port,
        clock=lambda: time.monotonic() + off[0],
        sleep=sleep,
    )
    return c, t


def _run(fake, mode="L", **kw):
    c, t = _client(fake)
    events, progress = [], []
    try:
        out = c.run(
            "ff_x.ffp",
            mode,
            on_event=events.append,
            progress=lambda m, p: progress.append((m, p)),
            **kw,
        )
        return out, events, progress
    finally:
        t.close()


class TestParse:
    def test_every_line_kind(self):
        kinds = [wff.parse_line(x.decode()).kind for x in HAPPY]
        assert kinds[:7] == ["sync", "step", "kern", "probe", "step", "cmp", "step"]
        assert kinds[-2:] == ["res", "done"]
        cmp = wff.parse_line("NCFFCMP diff=3,9,15 boot=1 cut=0 rewrite=-")
        assert wff._blocks(cmp.fields["diff"]) == [3, 9, 15]
        assert wff._blocks(cmp.fields["rewrite"]) == []
        p = wff.parse_line("NCFFPROG 2/5 blk=9")
        assert (p.num("done"), p.num("total"), p.num("blk")) == (2, 5, 9)
        e = wff.parse_line(fferr(62, ponr=1, app="u").decode())
        assert e.kind == "err" and e.num("st") == 62
        assert e.fields["ponr"] == "1" and e.fields["app"] == "u"
        assert e.num("a", base=16) == 0x0E0000
        assert wff.parse_line("t7E88...") is None and wff.parse_line("") is None

    @pytest.mark.parametrize("st", sorted(wff.STAGE_TEXT))
    def test_every_stage_has_a_message(self, st):
        msg = wff.user_message(wff.parse_line(fferr(st).decode()))
        assert wff.STAGE_TEXT[st] in msg

    def test_ecu_state_wording(self):
        def ev(st, ponr=0, app="1"):
            return wff.parse_line(fferr(st, ponr, app).decode())

        assert wff.ecu_state(ev(62, 1, "u")) is wff.EcuState.WRONG_ENTRY
        assert "Do NOT switch the ignition off" in wff.user_message(ev(62, 1, "u"))
        assert wff.ecu_state(ev(61, 1, "u")) is wff.EcuState.MAY_BE_COMPLETE
        assert wff.ecu_state(ev(42, 1, "0")) is wff.EcuState.NO_APPLICATION
        assert "resume" in wff.user_message(ev(42, 1, "0"))
        assert wff.ecu_state(ev(31, 0, "0")) is wff.EcuState.NO_APPLICATION
        assert wff.ecu_state(ev(31, 0, "1")) is wff.EcuState.UNTOUCHED
        assert "not changed" in wff.user_message(ev(5, 0, "u"))
        assert "Resume that flash" in wff.user_message(ev(26, 1, "u"))
        assert "full flash" in wff.user_message(ev(23, 0, "u"), interrupted_before=True)


class _Kernel:
    class pin:
        version = 3


class TestGate:
    def test_gate(self, sample_rom_path):
        rom = sample_rom_path.read_bytes()
        caps = {"ncfr_rev": 6, "fast_flash": 1, "ff_live": 1}
        from src.ecu.rom_utils import rom_hardware_generation

        gen = rom_hardware_generation(rom)
        ok, why = wff.fast_flash_available(caps, "L", rom, _Kernel())
        assert ok == (gen == "NC2"), why
        assert wff.fast_flash_available(None, "C", rom, _Kernel())[0] is False
        assert (
            wff.fast_flash_available({"ncfr_rev": 6}, "C", rom, _Kernel())[0] is False
        )
        assert (
            wff.fast_flash_available({**caps, "ncfr_rev": 5}, "C", rom, _Kernel())[0]
            is False
        )
        no_live = {**caps, "ff_live": 0}
        assert wff.fast_flash_available(no_live, "L", rom, _Kernel())[0] is False
        assert wff.fast_flash_available(no_live, "R", rom, _Kernel())[0] is False
        assert wff.fast_flash_available(no_live, "D", None, None) == (True, "")
        assert wff.fast_flash_available(caps, "C", rom, None)[0] is False
        assert wff.fast_flash_available(caps, "X", rom, _Kernel())[0] is False


class TestHttp:
    def test_caps_status_upload(self, fake):
        c, t = _client(fake)
        try:
            assert c.caps()["fast_flash"] == 1
            fake.status = {"state": "done", "mode": "C"}
            assert c.status()["state"] == "done"
            data = b"\x00" * 1000
            pkg = FastFlashPackage(
                "ff_x.ffp", data, {"package_crc32": zlib.crc32(data)}, b'{"a":1}'
            )
            c.upload(pkg)
            assert fake.uploads == {"ff_x.ffp": data, "ff_x.json": b'{"a":1}'}
        finally:
            t.close()

    def test_refused_upload(self, fake):
        fake.refuse_upload = True
        c, t = _client(fake)
        try:
            pkg = FastFlashPackage(
                "ff_x.ffp", b"\x01", {"package_crc32": zlib.crc32(b"\x01")}, b"{}"
            )
            with pytest.raises(WiCANUploadError, match="409"):
                c.upload(pkg)
        finally:
            t.close()

    def test_old_firmware_has_no_caps(self, fake):
        fake.caps = None
        c, t = _client(fake)
        try:
            assert c.caps() is None
        finally:
            t.close()


class TestRun:
    def test_live_happy_path(self, fake):
        out, events, progress = _run(fake)
        assert fake.commands == ["KLff_x.ffp"]
        assert out.done.fields == {"mode": "L", "nothing": "0", "rewritten": "3"}
        assert json.loads(out.result)["rewritten"] == [3, 9, 15]
        assert not out.via_status and not out.nothing_to_do
        assert [e.kind for e in events][-1] == "done"
        assert progress[-1] == ("Done", 100.0)
        pcts = [p for _, p in progress]
        assert pcts == sorted(pcts)

    def test_leading_can_frames_are_skipped(self, fake):
        fake.script = [b"t7E8803710000\rt1F00\r"] + HAPPY
        out, _, _ = _run(fake)
        assert out.done.kind == "done"

    @pytest.mark.parametrize("st", sorted(wff.STAGE_TEXT))
    def test_every_fferr_stage(self, fake, st):
        ponr = 1 if st >= 40 or st == 26 else 0
        app = "u" if st in (61, 62, 26, 23) else ("0" if ponr else "1")
        pre = [b"NCFFSYNC\n", b"NCFFSTEP kernel\n"] if st >= 15 else []
        # stages 1..14 are ff_fail_early lines (no app=, no NCFFRES), as released
        if st < 15:
            fake.script = [fferr_early(st)]
        else:
            fake.script = pre + [b"NCFFRES {}\n", fferr(st, ponr, app)]
        with pytest.raises(wff.WiCANFastFlashError) as ei:
            _run(fake)
        e = ei.value
        assert e.stage == st and e.ponr == bool(ponr)
        assert wff.STAGE_TEXT[st] in str(e)
        if st == 62:
            assert e.state is wff.EcuState.WRONG_ENTRY
        elif st == 61:
            assert e.state is wff.EcuState.MAY_BE_COMPLETE
        elif ponr:
            assert e.state is wff.EcuState.NO_APPLICATION
        elif st in (9, 14):
            assert e.state is wff.EcuState.BUSY and not e.untouched_proof
        elif st < 20 or app == "1":
            assert e.state is wff.EcuState.UNTOUCHED

    def test_refusal_before_sync(self, fake):
        fake.script = [fferr_early(5)]  # no app= on the early refusals
        with pytest.raises(wff.WiCANFastFlashError) as ei:
            _run(fake)
        e = ei.value
        assert e.stage == 5 and e.state is wff.EcuState.UNTOUCHED and e.untouched_proof
        assert e.app == "u" and e.ignition_cycle_ok

    def test_streamed_fferr_ponr_decides(self, fake):
        # L1: "NCFFSTEP write" came, then st 34 with ponr=0: nothing was erased
        fake.script = HAPPY[:7] + [fferr(34, 0, "1", nrc="22", a="0E0000", blk=15)]
        with pytest.raises(wff.WiCANFastFlashError) as ei:
            _run(fake)
        e = ei.value
        assert e.stage == 34 and e.ponr is False and e.untouched_proof
        assert e.state is wff.EcuState.UNTOUCHED

    def test_app0_before_ponr_is_no_application_and_no_proof(self, fake):
        # M4 (streamed): an ECU found without an application keeps the kernel
        fake.script = HAPPY[:6] + [fferr(31, 0, "0", nrc="E9")]
        with pytest.raises(wff.WiCANFastFlashError) as ei:
            _run(fake)
        e = ei.value
        assert e.state is wff.EcuState.NO_APPLICATION and not e.untouched_proof
        assert e.resume_recommended and not e.ignition_cycle_ok

    def test_busy_says_wait_not_ignition(self, fake):
        # M2: st 9 while another op owns the bus, no cycle advice
        fake.script = [fferr_early(9)]
        fake.status = fw_status("idle", name="", mode="-")
        with pytest.raises(wff.WiCANFastFlashError) as ei:
            _run(fake)
        e = ei.value
        assert e.state is wff.EcuState.BUSY and not e.ignition_cycle_ok
        assert not e.untouched_proof
        assert "ignition off and on" not in str(e) and "Leave the ignition on" in str(e)

    def test_busy_with_an_earlier_op_running(self, fake):
        fake.script = [fferr_early(9)]
        fake.status = fw_status("running", name="ff_older.ffp")
        with pytest.raises(wff.WiCANFastFlashError) as ei:
            _run(fake)
        assert ei.value.state is wff.EcuState.BUSY and "ff_older.ffp" in str(ei.value)

    def test_busy_but_it_is_this_package_follows_it(self, fake):
        fake.script = [fferr_early(9)]
        fake.status = fw_status("running", phase="write", ponr=1)
        fake.status_sequence = [
            fw_status("running", phase="verify", ponr=1),
            fw_status("done", ponr=1, done=3, total=3, result=fw_result()),
        ]
        out, _, _ = _run(fake)
        assert out.via_status and out.done.fields["rewritten"] == "3"

    def test_link_drop_after_ponr_learns_success_over_http(self, fake):
        fake.close_after = 9  # after "NCFFSTEP write" and one progress line
        fake.status_after_drop = fw_status(
            "done", ponr=1, done=3, total=3, result=fw_result()
        )
        out, events, _ = _run(fake)
        assert out.via_status and out.done.fields["rewritten"] == "3"
        assert out.done.fields["nothing"] == "0"
        assert json.loads(out.result)["app"] == "1"  # JSON text, not a repr
        assert not out.restart_confirmed  # not known over HTTP
        assert any(e.kind == "step" and e.text == "write" for e in events)

    def test_nothing_to_do_learned_over_http(self, fake):
        fake.close_after = 6
        fake.status_after_drop = fw_status("done", result=fw_result(nothing=1))
        out, _, _ = _run(fake)
        assert out.nothing_to_do

    def test_link_drop_after_ponr_then_failure(self, fake):
        fake.close_after = 9
        fake.status_after_drop = fw_status(
            "failed",
            ponr=1,
            err={"st": 42, "nrc": 0x72, "a": 0x20000, "blk": 9, "ponr": 1},
            result=fw_result(ok=0, app="0", st=42, ponr=1),
        )
        with pytest.raises(wff.WiCANFastFlashError) as ei:
            _run(fake)
        e = ei.value
        assert e.stage == 42 and e.state is wff.EcuState.NO_APPLICATION
        assert e.ponr and not e.untouched_proof and e.resume_recommended

    def test_failed_status_with_ponr_0_is_proof(self, fake):
        # H2: a name-matched failure with ponr 0 at both levels
        fake.close_after = 4
        fake.status_after_drop = fw_status(
            "failed",
            err={"st": 33, "nrc": 0xE5, "a": 0, "blk": 0, "ponr": 0},
            result=fw_result(ok=0, app="1", st=33),
        )
        with pytest.raises(wff.WiCANFastFlashError) as ei:
            _run(fake)
        e = ei.value
        assert e.stage == 33 and e.untouched_proof and e.state is wff.EcuState.UNTOUCHED

    def test_failed_status_app0_from_result(self, fake):
        # M4: app comes from the status result
        fake.close_after = 6
        fake.status_after_drop = fw_status(
            "failed",
            err={"st": 31, "nrc": 0xE9, "a": 0, "blk": 0, "ponr": 0},
            result=fw_result(ok=0, app="0", st=31),
        )
        with pytest.raises(wff.WiCANFastFlashError) as ei:
            _run(fake)
        e = ei.value
        assert e.app == "0" and e.state is wff.EcuState.NO_APPLICATION
        assert e.resume_recommended and not e.untouched_proof

    def test_stale_done_of_another_package_is_not_success(self, fake):
        # H1: the K never ran; the status still holds an earlier op's success
        fake.script = []  # the adapter says nothing; the link then drops
        fake.status = fw_status("done", name="ff_OLD.ffp", done=2, result=fw_result())
        with pytest.raises(wff.WiCANFastFlashError) as ei:
            _run(fake, idle_timeout_s=0.3)
        e = ei.value
        assert e.state is wff.EcuState.NOT_STARTED and not e.untouched_proof
        assert e.event is None

    def test_stale_failed_of_another_package_is_not_ours(self, fake):
        fake.script = []
        fake.status = fw_status(
            "failed",
            name="ff_OLD.ffp",
            err={"st": 42, "nrc": 0, "a": 0, "blk": 0, "ponr": 1},
        )
        with pytest.raises(wff.WiCANFastFlashError) as ei:
            _run(fake, idle_timeout_s=0.3)
        assert ei.value.state is wff.EcuState.NOT_STARTED and ei.value.stage is None

    def test_same_name_other_mode_is_not_ours(self, fake):
        fake.script = []
        fake.status = fw_status("done", mode="C", result=fw_result())
        with pytest.raises(wff.WiCANFastFlashError) as ei:
            _run(fake, idle_timeout_s=0.3)
        assert ei.value.state is wff.EcuState.NOT_STARTED

    def test_idle_after_running_is_lost_at_once(self, fake):
        # M3: the op was seen running, then the adapter forgot it
        fake.close_after = 9
        fake.status_after_drop = fw_status("running", ponr=1)
        fake.status_sequence = [fw_status("idle", name="", mode="-")]
        with pytest.raises(wff.WiCANFastFlashError) as ei:
            _run(fake)
        e = ei.value
        assert e.state is wff.EcuState.UNKNOWN and not e.untouched_proof
        assert fake.status_reads <= 4  # ended at once, not after 30 min

    def test_interrupted_status(self, fake):
        fake.close_after = 9
        fake.status_after_drop = INTERRUPTED
        with pytest.raises(wff.WiCANFastFlashError) as ei:
            _run(fake)
        e = ei.value
        assert e.state is wff.EcuState.NO_APPLICATION and e.ponr is True
        assert "error stage 0" not in str(e) and "restarted" in str(e)

    def test_status_reply_cut_short_is_unknown(self, fake):
        # H2: an IncompleteRead in GET /fast_flash is "not known", never proof
        fake.close_after = 4
        fake.truncate_status = True
        with pytest.raises(wff.WiCANFastFlashError) as ei:
            _run(fake, follow_up_s=20.0)
        e = ei.value
        assert e.state is wff.EcuState.UNKNOWN and not e.untouched_proof

    def test_link_drop_and_no_end_says_ecu_may_wait(self, fake):
        fake.close_after = 9
        fake.status_after_drop = fw_status("running", ponr=1)
        with pytest.raises(wff.WiCANFastFlashError) as ei:
            _run(fake, follow_up_s=20.0)
        assert "keep the ignition on" in str(ei.value).lower()
        assert not ei.value.untouched_proof

    def test_silent_adapter_times_out_into_follow_up(self, fake):
        fake.script = [b"NCFFSYNC\n", b"NCFFSTEP kernel\n"]
        fake.status = fw_status(
            "failed",
            err={"st": 20, "nrc": 0x31, "a": 0x8000, "blk": 0, "ponr": 0},
            result=fw_result(ok=0, app="u", st=20),
        )
        with pytest.raises(wff.WiCANFastFlashError) as ei:
            _run(fake, idle_timeout_s=0.3)
        assert ei.value.stage == 20 and ei.value.ponr is False

    def test_restart_confirmed_is_carried(self, fake):
        out, _, _ = _run(fake)
        assert out.restart_confirmed
        fake2 = FakeWiCAN()
        try:
            fake2.script = [x.replace(b"reset ok", b"reset no_answer") for x in HAPPY]
            out2, _, _ = _run(fake2)
            assert not out2.restart_confirmed
        finally:
            fake2.close()

    def test_st23_after_an_interrupted_op_says_full_flash(self, fake):
        fake.script = [b"NCFFSYNC\n", b"NCFFSTEP kernel\n", fferr(23, 0, "u")]
        fake.status = INTERRUPTED
        with pytest.raises(wff.WiCANFastFlashError) as ei:
            _run(fake, mode="R")
        assert "full flash" in str(ei.value)

    def test_bad_mode(self, fake):
        with pytest.raises(wff.WiCANFastFlashError):
            _run(fake, mode="Z")


# --- the driver's archive rules (H2, L2, L7) ------------------------------------------


class _Sd:
    def __init__(self, calls, fail_upload_auth=False):
        self.calls = calls
        self.fail = fail_upload_auth
        self._safeguards = SimpleNamespace(_gate=lambda: calls.append("gate"))

    @contextmanager
    def _datalog_fence(self):
        yield

    def _authenticate_ecu(self, flash_counter=True):
        if self.fail:
            raise RuntimeError("auth refused")

    def _check_ecu_generation(self, pkg):
        self.calls.append(("gen", bytes(pkg.corrected_rom[:4])))

    def _check_flash_counter(self):
        pass


class _Client:
    def __init__(self, error=None):
        self.error = error
        self.pkgs = []

    def caps(self):
        return {"ncfr_rev": 6, "fast_flash": 1, "ff_live": 1}

    def upload(self, pkg):
        self.pkgs.append(pkg)

    def run(self, name, mode, *, on_event=None, progress=None):
        if self.error:
            raise self.error
        ev = wff.FFEvent("done", "NCFFDONE mode=L nothing=0", {"nothing": "0"})
        return wff.FastFlashOutcome(mode, ev, [ev])


@pytest.fixture
def driver_env(sample_rom_path, tmp_path, monkeypatch):
    from src.ecu import fast_flash_kernel as fk

    monkeypatch.setattr(wff, "fast_flash_available", lambda *a: (True, ""))
    rom = sample_rom_path.read_bytes()
    archive = tmp_path / "ncflash.rda"
    archive.write_bytes(b"old archive")
    kernel = fk.LoadedKernel(_kernel_for_driver(), "test", None)
    return rom, archive, kernel


def _kernel_for_driver():
    k = bytearray(b"\x00" * 0x1800)
    k[0x1800 - 0x40 : 0x1800 - 0x38] = b"*SBL_END"
    return bytes(k)


def _drive(env, error=None, resume=False, fail_auth=False):
    from src.ecu.flash_manager import ARCHIVE_ASIDE_SUFFIX

    rom, archive, kernel = env
    calls = []
    client = _Client(error)
    f = wff.WiCANFastFlasher(object(), client=client, sd_flasher=_Sd(calls, fail_auth))
    raised = None
    try:
        f.fast_flash(rom, str(archive), kernel=kernel, resume=resume)
    except BaseException as e:  # noqa: BLE001 - inspected by the test
        raised = e
    aside = Path(str(archive) + ARCHIVE_ASIDE_SUFFIX)
    return raised, archive, aside, client


class TestDriverArchive:
    def test_success_writes_the_package_image(self, driver_env):
        raised, archive, aside, client = _drive(driver_env)
        assert raised is None
        pkg = client.pkgs[0]
        c = pkg.manifest["commit"]
        # L7: the archive is the very image the package was built from
        assert archive.read_bytes()[0xFFF80:] == pkg.data[c["off"] : c["off"] + 128]

    def test_failure_before_k_restores(self, driver_env):
        raised, archive, aside, _ = _drive(driver_env, fail_auth=True)
        assert isinstance(raised, RuntimeError)
        assert archive.read_bytes() == b"old archive" and not aside.exists()

    def test_proof_restores(self, driver_env):
        err = wff.WiCANFastFlashError("x", untouched_proof=True)
        raised, archive, aside, _ = _drive(driver_env, err)
        assert raised is err and archive.read_bytes() == b"old archive"

    @pytest.mark.parametrize(
        "state",
        [wff.EcuState.NOT_STARTED, wff.EcuState.UNKNOWN, wff.EcuState.UNTOUCHED],
    )
    def test_no_proof_leaves_the_archive_aside(self, driver_env, state):
        # H2: "could not learn", not started, even "untouched" without proof
        err = wff.WiCANFastFlashError("x", state=state, ponr=False)
        raised, archive, aside, _ = _drive(driver_env, err)
        assert raised is err and not archive.exists()
        assert aside.read_bytes() == b"old archive"

    def test_resume_sets_the_archive_aside_too(self, driver_env):
        # L2
        err = wff.WiCANFastFlashError(
            "x", state=wff.EcuState.MAY_BE_COMPLETE, ponr=True
        )
        raised, archive, aside, _ = _drive(driver_env, err, resume=True)
        assert raised is err and not archive.exists() and aside.exists()


class TestReReview:
    def test_another_package_running_in_follow_up_is_busy(self, fake):
        # R2: never "switch the ignition off and on" while another op owns the ECU
        fake.script = []
        fake.status = fw_status("running", name="ff_other.ffp", ponr=1)
        with pytest.raises(wff.WiCANFastFlashError) as ei:
            _run(fake, idle_timeout_s=0.3)
        e = ei.value
        assert e.state is wff.EcuState.BUSY and not e.ignition_cycle_ok
        assert not e.untouched_proof and "ff_other.ffp" in str(e)
        assert "off and on" not in str(e)

    def test_resume_st23_app_u_is_no_proof(self, fake):
        # R3: in R only app=1 proves the archive still describes the ECU
        fake.script = [b"NCFFSYNC\n", b"NCFFSTEP kernel\n", fferr(23, 0, "u")]
        fake.status = fw_status("failed", mode="R")
        with pytest.raises(wff.WiCANFastFlashError) as ei:
            _run(fake, mode="R")
        assert ei.value.stage == 23 and not ei.value.untouched_proof

    def test_resume_with_app1_is_proof_but_live_app_u_is(self, fake):
        ev = wff.parse_line(fferr(31, 0, "1").decode())
        assert wff._untouched_proof("R", ev)
        ev_u = wff.parse_line(fferr(23, 0, "u").decode())
        assert not wff._untouched_proof("R", ev_u) and wff._untouched_proof("L", ev_u)
        assert not wff._untouched_proof("L", wff.parse_line(fferr(31, 0, "0").decode()))
        assert not wff._untouched_proof("L", wff.parse_line(fferr_early(9).decode()))
