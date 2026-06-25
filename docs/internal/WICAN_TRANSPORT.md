# WiCAN PRO Wireless Transport — Design & Build Plan

Status: **In progress** (branch `feature/wican-pro-transport`)
Last updated: 2026-06-15

This document is the design of record for adding **WiCAN PRO (WiFi/BLE) READ/WRITE**
support to NC Flash, alongside the existing J2534 (Tactrix OpenPort) path. It captures
the decisions reached during the design interview so the rationale is not lost again.

---

## 1. Goal & Scope

Allow READ ROM, WRITE/flash (full + dynamic), read/clear DTC, and RAM scan over a
**WiCAN PRO** adapter using **WiFi** first (BLE deferred). The J2534 path must remain
**byte-for-byte unchanged** — it is the proven, hardware-tested flash path and the
prime directive forbids regressing it.

## 2. Architecture: thin bridge, NC Flash drives

The WiCAN is a **generic, dumb CAN gateway** with zero knowledge of Mazda, UDS, ISO-TP,
or NC Flash. All intelligence stays in Python. NC Flash speaks the WiCAN's published
SLCAN protocol; the WiCAN just shuttles raw CAN frames to/from the ECU.

**Rejected:** SD-card / "smart device" autonomous flashing. It would require a second
safety-critical protocol implementation in ESP32 C, and the SD checksum it was meant to
provide is redundant with our existing in-RAM ROM validation (`correct_rom_checksums`
runs and re-verifies before any frame is sent). One protocol implementation only.

## 3. Transport seam

A new `EcuTransport` abstraction at the **UDS-message level** — exactly the granularity
`UDSConnection.send_request` already works at:

```python
class EcuTransport(ABC):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def send_message(self, payload: bytes, timeout_ms: int) -> None:
        """Send one complete UDS request payload (SID + data). Impl handles ISO-TP TX."""
    def receive_message(self, timeout_ms: int) -> bytes | None:
        """Return one reassembled UDS response payload, or None on timeout.
           Impl handles ISO-TP RX and strips any transport framing."""
    @property
    def description(self) -> str: ...
```

- **`J2534Transport`** — thin wrapper over the existing `J2534Device` + ISO15765 channel.
  `send_message` = `build_isotp_msg` + `write_msgs`; `receive_message` = `read_msgs(1)` +
  strip 4-byte CAN-ID prefix (`Data[4:]`). The OpenPort firmware keeps doing ISO-TP.
  Behaviour identical to today. Device lifecycle stays with `ECUSession`/`FlashManager`.
- **`WiCANTransport`** — SLCAN (LAWICEL ASCII) **client over TCP** + a **new Python
  ISO-TP engine** (the device does NOT reassemble — confirmed by wican-fw #514). Owns its
  own TCP socket lifecycle.

`UDSConnection`, `ECUSession`, and `FlashManager` become transport-agnostic. The NRC 0x78
retry loop, positive/negative parsing, and all timeouts stay in `UDSConnection`.

## 4. WiCAN integration contract (verified against wican-fw source)

- **Wire protocol:** SLCAN / LAWICEL ASCII over TCP. Data frame `t<3-hex-id><dlc><hexdata>\r`
  (e.g. `t7E008...\r`). Control: `O\r` open channel, `C\r` close, `S6\r` = 500 kbps, `V\r`
  version. Frames pass **verbatim** in SLCAN mode (OBD/filter logic compiled out — #400),
  so `0x7E0`/`0x7E8` flow untouched and the OBD interpreter chip is bypassed.
- **Roles:** WiCAN = TCP server, NC Flash = TCP client.
- **Config (device `config.json`):** `protocol:"slcan"`, `can_datarate:"500K"`,
  `wifi_mode:"AP"`, `port_type:"tcp"`. Set via web UI (v1) or, as a stretch, programmatically
  (`GET /load_config` → mutate → `POST /store_config` → reboot; internal endpoint, not a
  guaranteed-stable API).
- **Connect-time gotchas (from #476, resolved):**
  1. **Enable "monitoring" in the web UI** — the socket passes no frames until it is on.
  2. Bitrate must be **`S6` (500K)** (`-s8` was the bug in #476).
  3. Port is configurable — read it off the device (general socket 3333; SLCAN reported on
     23 on the PRO). Do not assume.
- **Defaults:** AP SSID `WiCAN_<id>`, pwd `@meatpi#`; PRO AP IP `192.168.0.10` (standard
  `192.168.80.1`); mDNS `wican_<id>.local`.

## 5. Throughput reality (wican-fw #204, OPEN)

Plain frame-by-frame SLCAN over TCP is slow: a 1.2 MB UDS flash took **~980 s (~48 kbit/s)**
vs ~70 s on J2534, with ~57 ms latency per ISO-TP frame round-trip and buffer overflows on a
loaded bus. → A full 1 MB flash is **~13–14 min**; dynamic flash proportionally less.

- **v1 ships raw SLCAN + Python ISO-TP** (confirmed-working, full control, safe).
- **Frame99** (a meatpi RealDash extension that does ISO-TP *in firmware*, batching a whole
  message per TCP packet — commands `0x11` ISO-TP, `0x03` bitrate, `0x04` ECU TX/RX IDs,
  `0x05` padding) is the **escape hatch** if bench numbers are intolerable. It is proprietary,
  firmware-version-dependent, and cedes ISO-TP timing to the device — only if needed.
- ISO-TP flow control (BS/STmin) must be tuned to pace consecutive frames **down** so the
  gateway buffer does not overflow. **CONFIRMED ON HARDWARE (2026-06-20):** at STmin=0 the ECU
  blasts ~146 CFs back-to-back per 1 KB read and the WiCAN's CAN→TCP buffer overflows — frames
  are dropped *inside the gateway* (so TCP reliability cannot recover them), the ISO-TP response
  never completes, and the read times out (no brick — reads are idempotent). Tuning the STmin we
  advertise when **receiving** fixes it: STmin=0 dropped at block 8/64, STmin=1 at 40/64, STmin=2
  read 64/64 cleanly. `WiCANTransport` now defaults to **STmin=3** (margin over the first value
  that held; more separation strictly reduces overflow) with **BS=0** (STmin already paces the
  burst, so no extra FC round-trips). Real throughput ≈ **1.4–1.7 KB/s** (~10–13 min for a full
  1 MB read) — slow but reliable, the right trade for a read. `rx_block_size`/`rx_stmin` are
  constructor knobs if a different gateway needs different pacing.
- **First-frame-after-`open()` prime (CONFIRMED ON HARDWARE):** the WiCAN reliably drops the very
  first CAN frame sent after the SLCAN `O` ack (its CAN peripheral is still coming up), which made
  the first real request hang for the full ~60 s receive timeout. `WiCANTransport.open()` now
  sends one throwaway TesterPresent (`3E 80`) warm-up frame and **drains its reply** (the NC ECU
  rejects the `0x80` suppress-positive-response sub-function with `7F 3E 12`; the drain stops that
  stray frame polluting the first real request). After the prime the first real request answers in
  ~60 ms instead of timing out.
- **Read-speed work (target: J2534/Tactrix parity ~60 s, was ~16 min).** The dominant cost is the
  STmin pacing floor (`~149,504 CF × STmin`); at STmin=3 that alone is ~448 s. The Tactrix proves the
  ECU/CAN ceiling is ~50–60 s (full-rate STmin=0 ≈ 20 KB/s), so the gap is gateway-transport tax, and
  **≤ 180 s is firmware-gated** (the WiCAN's CAN→TCP path drops frames below STmin≈2 ms). Plan,
  diagnosis (4-agent-verified), and ordered levers live in `.claude/plans/wican-read-speed-goal.md`.
  **Phase 0/1 landed (software):** `tools/wican_bench_read.py` gained a `--probe` (ECU max read size)
  and `--bench-blocks` per-block timing harness with `--rx-stmin`/`--rx-block-size`/`--block-size`/
  `--n-cr-ms`/`--no-tcp-nodelay`/`--so-rcvbuf` sweep knobs; `WiCANTransport` now sets **TCP_NODELAY**
  (and optional SO_RCVBUF) and advertises an **ISO-TP N_Cr** (`DEFAULT_N_CR_MS=500`) so a dropped
  Consecutive Frame is detected in ~0.5 s instead of the full per-block budget; `FlashManager.read_rom`
  takes a configurable `read_block_size` (default 0x400, cap 0xFFE). Defaults are behaviour-preserving;
  the bench sweep (hardware-gated) tunes the final STmin/BS/N_Cr/block-size and decides whether the
  firmware queue-enlargement / Frame99 phases are needed.

## 6. Safety model

- **Reads** (ROM/RAM, `ReadMemoryByAddress` 0x23): per-block auto-retry by re-requesting the
  address — idempotent, safe. **IMPLEMENTED:** `FlashManager.read_rom` retries a lost/garbled
  block up to 4×, each attempt on a tight ~4 s budget (so a drop fails fast instead of stalling the
  60 s response-pending budget) — and, with the ISO-TP **N_Cr** fast-fail (§5, `DEFAULT_N_CR_MS=500`),
  a *mid-message* dropped Consecutive Frame is now detected in ~0.5 s rather than waiting out that
  whole per-block budget — flushing stale frames between attempts via `EcuTransport.flush()`
  (no-op on J2534, frame-drain on WiCAN). Confirmed on hardware: with STmin=3 pacing a drop occurs
  roughly once per ~25 blocks and every one is recovered on the next attempt, so the full 1 MB read
  completes. The flow-control pacing (§5) keeps drops rare; this retry recovers the residual.
- **Writes/flash** (`TransferData` 0x36): **NO mid-stream resend.** The Mazda protocol has no
  block sequence counter (verified vs romdrop); the ECU writes sequentially, so resending a
  consumed block shifts everything = brick. Resilience instead = TCP reliability + NRC 0x78
  pending-wait + **clean abort-and-restart-from-scratch** (re-auth, re-SBL, re-transfer).
  **IMPLEMENTED (build-only, NOT hardware-validated):** `src/ecu/wican_flash.py` (`WiCANFlasher`)
  + `src/ecu/link_quality.py` — pre-flight link-quality gate, battery guard, abort-and-restart-from-
  scratch (a fresh whole flash per attempt, never a mid-stream resend), and optional read-back
  verify. Bench-validate (user-gated) before production use. See `.claude/plans/wican-ecu-functions-goal.md`.
- **Pre-flight link-quality gate (FLASH ONLY):** ~25 TesterPresent round-trips; require 0 loss
  + stable latency (p95 under a ceiling) + RSSI ok, else block the flash. Reads/diagnostics are
  never gated (idempotent/recoverable).
- **Recovery UX:** on mid-flash drop → immediate "🔴 KEEP IGNITION ON, do not disconnect",
  block app exit, auto-reconnect + restart-from-scratch ×3, then a guided
  `[Reconnect & Re-flash]` screen.
- **Battery/voltage guard:** keep the existing 12.0 V check — the actual historical brick cause.
- **Read-back verify:** **optional, off by default** (matches the trusted cable flow; WiFi
  corruption risk ≈ cable because the CAN hop is identical + WiFi FCS + TCP). A manual
  `[Verify flash]` button reads the written region back and byte-compares to `rom_buf`.
- **Why corruption is a non-risk:** the flash is lock-step ACK'd — a dropped frame yields a
  clean timeout/abort, never silent corruption. WiFi adds an *interruption* category, not a
  corruption category.

## 7. Gated build order

Flash is built **last**, behind proof the new ISO-TP engine is byte-perfect.

0. **Bench spike** (needs hardware): read the 1 MB ROM over raw SLCAN on the bench →
   measures real throughput (non-destructive) AND diffs vs a J2534 dump (byte-perfect gate).
   GO/NO-GO + SLCAN-vs-Frame99 decision point.
1. `EcuTransport` refactor — zero behaviour change; existing tests + hardware smoke pass.
2. WiCAN SLCAN/TCP link.
3. Python ISO-TP engine.
4. DTC read/clear + RAM scan over WiCAN (non-destructive).
5. ROM read over WiCAN == J2534 dump (the bench spike, formalized).
6. Flash over WiCAN — adds link gate + recovery + manual verify.

## 8. Adapter UX

Explicit `Interface: J2534 (OpenPort) | WiCAN (WiFi)` dropdown in the ECU window; WiCAN
IP/port persisted in settings (mDNS discovery offered); **J2534 default** so current behaviour
is unchanged; active transport always visible during a flash.

## 8b. Device protocol mode (slcan switch)

The WiCAN's active CAN protocol is a **persisted device setting** (`protocol` in its
`config.json`), not a per-connection option — raw SLCAN only flows when `protocol == "slcan"`.
The user runs a **custom firmware fork**, where the stock value is a custom `poll_log` mode
(slcan itself is currently left stock-equivalent).

- **Now:** `src/ecu/wican_config.py` `WiCANConfigurator` does a **targeted** HTTP read-modify-write
  (`GET /load_config` → change *only* the top-level `protocol` token, preserving every other
  field incl. plaintext WiFi/MQTT passwords → `POST /store_config` → device reboots ~6 s →
  verify) to switch to `slcan` on connect and **restore** the previous mode on disconnect.
  Proven against the real device (2026-06-20): `poll_log → slcan → poll_log`, ~6 s reboot.
- **Real-hardware validation (2026-06-20, live MX-5 NC ECU on `192.168.1.169:35000`):**
  full stack proven end-to-end. TCP + `C/S6/O` handshake accepted; **"monitoring" not required to
  open the socket**; port **35000**; ~55 ms TesterPresent round-trip (25/25, 0% loss). The
  `open()` warm-up prime eliminated the dropped-first-frame hang. Security access **authenticated
  with the private `_secure` seed/key** and multi-frame ISO-TP reassembly was validated (VIN
  `JM1NC2FF0A0207980`). With STmin pacing, the **full authenticated 1 MB ROM read** runs cleanly
  (see §5 for the throughput/pacing numbers). NOTE: rapid repeated re-auth trips the ECU's
  security-access cooldown (NRC 0x22) — leave a few seconds between programming-session attempts.
- **Goal (task #10):** avoid the reboot entirely via a **firmware-side hook** (hot protocol
  switch with no reboot, or a dedicated always-available raw-CAN/SLCAN port that coexists with
  the custom protocol). NC Flash should target that once it exists; the HTTP+reboot configurator
  then becomes the **fallback** for stock/unmodified firmware. Keep the seam swappable.

## 9. Task checklist

Software-only (this session, no hardware needed — all unit-tested):

- [ ] `src/ecu/transport.py` — `EcuTransport` ABC + `J2534Transport` (behaviour-preserving) + `FakeTransport` for tests
- [ ] `src/ecu/isotp.py` — Python ISO-TP engine (FF/CF/FC, STmin/BS, padding, reassembly)
- [ ] `src/ecu/slcan.py` — SLCAN ASCII codec (encode/decode `t…` frames + control commands)
- [ ] `src/ecu/wican_transport.py` — `WiCANTransport` (SLCAN/TCP client + ISO-TP) + connect sequence (open, S6, monitoring note)
- [ ] Refactor `UDSConnection` to take an `EcuTransport`; keep NRC 0x78 / parsing / timeouts
- [ ] Wire `ECUSession` + `FlashManager._connect` to wrap their device in `J2534Transport`
- [ ] Transport factory + interface selection plumbing
- [ ] Unit tests for all of the above; `pytest` green; `black` clean
- [ ] Adapter-selector UI (`ecu_window`) + persisted WiCAN settings

Hardware-gated (follow-up, needs the user's bench + WiCAN PRO):

- [ ] **Bench spike:** SLCAN/TCP bring-up + bidirectional `0x7E0`/`0x7E8` + throughput measure
- [ ] ROM read over WiCAN diffed vs J2534 dump (byte-perfect gate)
- [ ] DTC/RAM over WiCAN on the bench
- [ ] Link-quality gate + recovery UX + flash enablement (only after the gate passes)
- [ ] Decide SLCAN vs Frame99 from real numbers

## 10. References

- wican-fw repo: https://github.com/meatpiHQ/wican-fw
- `slcan.c`: https://raw.githubusercontent.com/meatpiHQ/wican-fw/main/main/slcan.c
- `config_server.c`: https://raw.githubusercontent.com/meatpiHQ/wican-fw/main/main/config_server.c
- CAN config docs: https://meatpihq.github.io/wican-fw/config/can/
- #476 (socketcan bring-up, resolved): https://github.com/meatpiHQ/wican-fw/issues/476
- #204 (slcan throughput, open): https://github.com/meatpiHQ/wican-fw/issues/204
- #400 (filter compiled out): https://github.com/meatpiHQ/wican-fw/issues/400
- #514 (device does not reassemble ISO-TP): https://github.com/meatpiHQ/wican-fw/issues/514
