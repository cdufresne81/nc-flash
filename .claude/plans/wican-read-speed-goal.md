# /goal — WiCAN PRO 1 MB ROM read at J2534 parity (~60 s)

> Self-contained driver for the read-speed work. Software phases are DONE + hardware-validated;
> the remaining work is the **firmware phase (Phase 3)**. After `/compact`, `/goal` should resume
> from "ACTIVE PLAN" below. The diagnosis is hardware-measured — do not re-derive it.

---

## STATUS (2026-06-21)

- ✅ **Phase 0** — bench instrumentation in `tools/wican_bench_read.py` (`--probe`, `--bench-blocks`,
  `--rx-stmin`/`--rx-block-size`/`--block-size`/`--n-cr-ms`/`--no-tcp-nodelay`/`--so-rcvbuf`). Tested.
- ✅ **Phase 1** — software levers, tested (full suite 1165 passed): ISO-TP **N_Cr fast-fail**
  (`isotp.py`, default 500 ms on WiCAN, `None`/byte-identical on J2534), **TCP_NODELAY + SO_RCVBUF**,
  **configurable read block size** (`read_rom(read_block_size=…)`, ECU caps at 0x400 so it stays 0x400).
- ✅ **Gate 1** — measured on the live ECU. **Software got the read 948 s → ~314–339 s (3×)** via
  STmin=0 (TCP_NODELAY makes it hold). Full read validated at **338.7 s** (`wican_stmin0_full.bin`).
  **Conclusion: software floor is ~300 s; firmware is REQUIRED for ≤180 s / ~60 s** (see Gate 1 below).
- ✅ **Phase 1.5** — STmin=0 locked as the WiCAN default (the validated 3× win).
- ✅ **Phase 3 — DONE + hardware-validated (2026-06-21).** In-firmware autonomous read loop
  (`ncflash_fastread.c`, fork branch `feature/fast-rom-read`, ESP-IDF v5.5.3). **Full 1 MB read is
  BYTE-FOR-BYTE IDENTICAL to `wican_stmin0_full.bin`** ✅. Time **~214 s (3.6 min)** at ~211 ms/block.
  Firmware build is v4 (`NCFRv4`): response-pending handling + sync preamble + chunked reads.

### Phase 3 outcome — what moved and what's the floor

| Stage | full 1 MB | per-block | note |
|---|---|---|---|
| original STmin=3 | ~749 s | 732 ms | per-block WiFi RTT + ECU |
| software STmin=0 + TCP_NODELAY | 338.7 s | 307 ms | software floor |
| **firmware fast-read (v4)** | **~214 s** | **~211 ms** | byte-perfect; WiFi RTT eliminated |

**The ~60 s target is NOT reachable with ReadMemoryByAddress — it is ECU-limited, not transport-limited.**
The ECU answers **`7F 23 78` (response-pending) then the real data ~140 ms later on EVERY block**
(measured universal: low region 0x0 and high region 0xD8400 both ~211 ms/block). Firmware already
removed the per-block WiFi round-trip (338 s → 214 s); the remaining ~211 ms/block is the ECU's own
flash-read + response-pending latency, which any tool over CAN pays. (User believes a Tactrix does
~20 KB/s / <2 min — that contradicts this floor unless the Tactrix uses a different/faster read
service; open question.) **214 s meets the ORIGINAL "~3 min" ask; it misses the later 60 s reframe.**

Three firmware bugs found + fixed to get byte-perfect (all needed):
1. **Response-pending** — `read_one_block` treated `7F 23 78` as the block → desync. Now loops past
   0x78 (600 ms wait, cap 16) for the real 0x63.
2. **Leading CAN-junk shift** — frames queued before `can_rx_task` suspend prefixed the ROM stream,
   shifting every byte. Now firmware emits an `NCFRDATA` sync preamble; host resyncs onto it.
3. **Long-stream timeout (looked like a stall)** — a single 1 MB command runs ~214 s but the host
   budget was 180 s, cutting it off ~880 KB. Host now **chunks into 128 KB commands** (each a fresh
   `can_rx_task` suspend/resume), verified byte-perfect; also avoids the device wedging on host-close.

Diagnostics added (kept): firmware **version ping** (`X`+`0xFFFFFFFE` → `NCFRvN` marker; `wican_fw_ping.py`)
and **on-abort `FRERR` line** surfaced by the host. Bench gained `--fast-read-start/-len`.

**Remaining (pre-commit, see notes):** update `TestFastRead` to the sync-preamble protocol; commit the
firmware to the fork branch; changelog/notes. Open speed question: whether the ECU response-pending is
avoidable (different session/service/block-size) — uncertain payoff, needs the user's Tactrix method.

**Objective — MET (Tactrix parity).** Full 1 MB authenticated WiCAN read at **J2534/Tactrix parity**.
The ~60 s figure was an optimistic guess; on **2026-06-21 the user's own Tactrix measured 215.8 s at
4.7 KB/s** on the same ECU — and the WiCAN firmware fast-read does **214 s, byte-for-byte identical**.
So we have matched the reference tool exactly. The real ceiling is the ECU's per-block
**response-pending (~211 ms/block)**, which every CAN tool pays; it is NOT transport-limited. READ-only;
reads are idempotent (re-requesting an address never changes ECU state) so every experiment was safe live.

**Done gate — PASSED.** Full 1 MB read **byte-identical to `wican_stmin0_full.bin`** ✅ at Tactrix-parity
speed, with the floor characterized (ECU response-pending). The WRITE/flash path stayed out of scope and
was never modified.

---

## Verified diagnosis + Gate 1 results (hardware, 2026-06-21, live MX-5 NC ECU @ 192.168.1.169)

Per-block model fit exactly: **`per-block = ~294 ms fixed + 146 × STmin(ms)`** (146 ms/STmin slope =
146 CF × 1 ms → the ECU paces precisely at the advertised STmin). 1 MB = 1024 blocks of 0x400.

| Config | per-block | full 1 MB | drops |
|---|---|---|---|
| STmin=3 (old default) | 732 ms | ~749 s | 4/64 |
| STmin=2 | 586 ms | ~600 s | 6/64 |
| STmin=1 | 441 ms | ~451 s | 2/64 |
| **STmin=0 + TCP_NODELAY** | **307 ms** | **~314 s (full read 338.7 s)** | **1/48** |
| BS-bursting (STmin=0, BS=15) | 704 ms | ~721 s | 7/48 |
| read size > 0x400 (0x800/0xFFE) | — | — | **ECU NRC 0x31 (out of range)** |

- **Bigger blocks dead** — ECU hard-rejects reads > 0x400, so per-block overhead can't be amortised.
- **TCP_NODELAY is the win** — it killed the STmin=0 buffer-overflow (old sweep dropped at 8/64; now
  1/48). Software 948 s → ~314 s.
- **BS-bursting refuted** — the ~10 FC round-trips/block (×~60 ms WiFi RTT) cost more than they save.
- **The wall is the ~294 ms/block fixed overhead** = per-block WiFi round-trip + gateway forwarding,
  NOT the ECU (Tactrix reads the same 0x400 blocks at ~50 ms). × 1024 = **~300 s floor at STmin=0**.
- **Gate 1 decision:** firmware required. **Phase 2 (queue enlargement) is insufficient** (only
  removes STmin pacing already gone via NODELAY → still ~300 s). The dominant cost is the **per-block
  WiFi round-trip**, so the only path to ≤180 s / ~60 s is **Phase 3**.

---

## ACTIVE PLAN

### Phase 1.5 — lock in the software 3× win (do first; ~15 min)
Set **`DEFAULT_RX_STMIN = 0`** in `src/ecu/wican_transport.py` (validated: TCP_NODELAY + N_Cr=500 +
per-block retry make STmin=0 hold; full read 338.7 s, idempotent so worst case is slower-not-wrong).
Update the constant's comment, CHANGELOG, and any docstring/test referencing the old STmin=3 default.
Run `black` + `pytest`.

### Phase 3 — firmware: break the per-block WiFi-RTT wall (target ~60 s)
Firmware fork **already cloned**: `C:\Users\dufre\Projets\nc-flash-wican-fw` (default branch
`wican-pro`, ESP32-S3). Comm path: `can.c` (TWAI RX) → queue (`main.c`, 16×65 B) → `slcan.c` (ASCII
encode) → `comm_server.c` (TCP send). `realdash.c` has only single-frame `0x66`/`0x44` (no reassembly
— write from scratch). Keep the SLCAN protocol intact so NC Flash's current `WiCANTransport` still
works as the fallback; add the fast path as an **additive** mode/command.

**Two designs (implement the simpler that hits target; escalate if needed):**
1. **In-firmware ISO-TP reassembly (Frame99-style)** — firmware drains the ECU's CF burst at full CAN
   speed into RAM, handles Flow Control locally, and returns one whole UDS response per TCP packet.
   Removes STmin pacing + the per-CF forwarding. Per-block still one WiFi request/response → ~RTT +
   CAN time ≈ ~100 ms × 1024 ≈ **~100 s** (≤180 s ✓, not yet ~60 s).
2. **Autonomous in-firmware read loop (reaches ~60 s)** — NC Flash sends one "read range [addr,len]"
   command; the ESP32 issues the N `ReadMemoryByAddress(0x400)` calls to the ECU **locally over CAN**
   (~0.5 ms round-trips, not WiFi) and streams the reassembled bytes back over one TCP connection.
   Pays the WiFi RTT once instead of 1024×, approaching the CAN ceiling (~50–60 s). The ECU
   security/session must already be authenticated by NC Flash before the loop starts (the firmware
   just replays ReadMemoryByAddress; it never authenticates or writes).

**Steps:**
1. Install ESP-IDF **≥ v5.1** (README), `idf.py set-target esp32s3`. Confirm a clean baseline build of
   the unmodified `wican-pro` branch first.
2. New feature branch in the fork (never `wican-pro`/main).
3. **Rollback safety BEFORE flashing:** OTA writes to the *other* partition and `esp_ota_set_boot_
   partition` switches boot — a bad image is recoverable by re-flashing a known-good `.bin`. Record
   the device's current firmware version (web "About" tab) and keep/build a known-good restore `.bin`
   of the `wican-pro` branch as the rollback image before flashing anything experimental.
4. Implement design 1 (or 2), keeping `flash`/write-CAN paths and the existing SLCAN mode untouched.
5. `idf.py build`. Flash via the **OTA HTTP endpoint** (`config_server.c` `esp_ota_*` handler; the web
   "About → Update" POSTs the `.bin` — confirm the exact URI from the `httpd_uri_t` registration) to
   `192.168.1.169` over WiFi. Reboots ~30 s.
6. Add the matching **batched transport mode** in NC Flash behind the `EcuTransport` seam (e.g.
   `Frame99Transport` or a `WiCANTransport` mode flag) — **J2534 + SLCAN-fallback byte-identical**.
   Tests + CHANGELOG + `black`/`pytest`.
7. **Validate on hardware:** full read time (target ~60 s, ≤180 s min) AND byte-compare to
   `wican_stmin0_full.bin` (`tools/wican_bench_read.py --reference wican_stmin0_full.bin`). Only trust
   the new path once byte-perfect.

---

## Device / environment facts
- WiCAN PRO: `192.168.1.169` — SLCAN TCP **port 35000**, HTTP-config **port 80**. Stored protocol is
  already `slcan` (no `--auto-config` reboot needed). ECU tester `0x7E0` / ECU `0x7E8`, 500 kbps.
- `_secure` module installed (`SECURE_MODULE_AVAILABLE=True`) — auth works for reads.
- Reference dump: `wican_stmin0_full.bin` (1 MB, this ECU, STmin=0 read) — the byte-perfect oracle
  until a J2534 dump exists.
- ESP-IDF ≥ v5.1, target esp32s3. Firmware OTA = HTTP POST of the built `.bin`.
- NC Flash repo: branch `feature/wican-pro-transport`. Firmware repo: `nc-flash-wican-fw`.

## Hard constraints
- **Firmware** in `cdufresne81/nc-flash-wican-fw` on a **NEW branch**; **known-good rollback `.bin`
  recorded/built before any flash**; poke the user only on a suspected brick / safe-mode.
- **Flash/write path untouched & out of scope** (no block-sequence counter; a resend bricks).
- **J2534 path and the existing SLCAN WiCAN mode stay byte-for-byte identical** (fallback).
- Tests for new behavior; **CHANGELOG before any commit**; `black` + `pytest` green; **no push to
  remote master without explicit user validation**; no auto-commit unless asked / "land the plane".
- Measure, don't guess; keep the byte-compare vs the reference dump as the correctness gate.

## Provenance
Diagnosis verified by a 4-agent analysis (2026-06-21) and then **confirmed on hardware** the same day
(live ECU sweep above). Target = J2534/Tactrix parity (~60 s) per the user's ~20 KB/s bench data.
Software phases complete; firmware (Phase 3) is the remaining work. See `docs/internal/WICAN_TRANSPORT.md`
§5/§6/§10 for transport design.
