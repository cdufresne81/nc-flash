# /goal — WiCAN ECU functions: READ RAM / DTC, CLEAR DTC, + WRITE logic (full + dynamic flash)

> Self-contained driver for the **second** WiCAN phase. The read-speed phase is DONE + closed
> (`wican-read-speed-goal.md`, Tactrix parity). This goal makes the *remaining ECU functions*
> work over WiCAN, and **builds (does not hardware-test) the WRITE/flash logic**.
> The **adapter-selector UI + WiCAN settings is explicitly OUT of scope** — it is the 3rd goal
> (`wican-adapter-ui-goal.md`), gated on a short grill-me after this one lands.
> After `/compact`, `/goal` should resume from "ACTIVE PLAN" below.

---

## STATUS (2026-06-21)

- ✅ **Part A — READ RAM / READ DTC / CLEAR DTC over WiCAN — DONE + HARDWARE-CONFIRMED** (2026-06-21).
  Code + tools + 17 unit tests, all three functions run on the live MX-5 NC ECU (WiCAN PRO @
  192.168.1.169 — memory `project_wican_hardware_in_loop`) via `tools/wican_bench_ecu.py`.
  - ✅ **READ DTC** — returned the bench ECU's 17 stored codes.
  - ✅ **READ RAM** — clean 48 KB dump. Hardware exposed a bug — `scan_ram` had no per-page retry, so
    a single dropped frame aborted the scan; fixed to use `_read_block_with_retry` (idempotent), now
    recovers dropped pages. (CHANGELOG "Fixed".)
  - ✅ **CLEAR DTC** — user-authorized; reduced 17 → 7 (10 cleared, 7 hard faults re-set immediately).
- 🚧 **Part B — WRITE logic: full flash + dynamic flash over WiCAN** — **Option A BUILT** (host-driven
  safety layer: pre-flight link-quality gate + battery guard + abort-and-restart-from-scratch +
  optional read-back verify; `src/ecu/link_quality.py` + `src/ecu/wican_flash.py`; 23 tests). **NOT
  hardware-flashed** (build-only, as scoped). The A-vs-B decision was resolved toward **Option A**
  (recommended; #22 endorses keeping the streaming read). Option B (SD-autonomous flash) remains a
  documented future alternative — see "DECISION REQUIRED" below + `WICAN_PART_C_FINDINGS.md`.
- ✅ **Part C — Investigate avoiding device reboots** — DONE (investigation). Root cause of the
  CAN-wedge reboot confirmed against the firmware + a clean-teardown fix and a no-reboot protocol
  switch recommended in `docs/internal/WICAN_PART_C_FINDINGS.md`. Firmware change is gated/future.

**Done gate:** Part A confirmed on the live ECU (RAM dump completes + plausible; DTC read matches
the J2534 read back-to-back; clear empties DTCs). Part B: WRITE path implemented behind the
existing transport seam, fully unit-tested with a drop-injecting `FakeTransport`, `black` + `pytest`
green, integrable into `FlashManager`/UI — **without** being flashed to hardware. Part C: a written
recommendation (and, if cheap + safe, a firmware spike) on reboot-free flash operation.

---

## What already exists (do NOT rebuild — REUSE)

The transport refactor (read-speed phase) already made the stack transport-agnostic. Confirmed by
reading the source on 2026-06-21:

- **`EcuTransport` seam** (`src/ecu/transport.py`) at the UDS-message level. `WiCANTransport`
  (SLCAN/TCP + Python ISO-TP) and `J2534Transport` both implement it. `FakeTransport` exists for
  unit tests (scripted-queue or responder callable, records `sent`).
- **`UDSConnection`** (`src/ecu/protocol.py`) is transport-agnostic and already implements every
  service these functions need:
  - READ RAM → `read_memory_by_address()` / `scan_ram()` (0xFFFF0000, 192×0x100 pages).
  - READ DTC → `read_dtc_count()` (0x22) + `read_dtc_status()` (0x18, mask 0x00FF00).
  - CLEAR DTC → `clear_dtc()` (0x14, `FF 00`).
  - WRITE → `request_download()` (0x34, KWP raw addr4+size4), `transfer_data()` (0x36, **no block
    sequence counter** — verified vs romdrop; NEVER add one), `request_transfer_exit()` (0x37),
    `ecu_reset()` (0x11), `check_flash_counter()` (0x31).
- **`FlashManager`** (`src/ecu/flash_manager.py`) is already injectable with any transport:
  - `use_uds(uds)` — inject a pre-built `UDSConnection` over **any** transport (incl. a WiCAN one);
    sets `_owns_connection=False` so `_connect()` only does Tester Present and `_cleanup()` closes
    nothing. This is the seam for non-flash ops.
  - `read_dtcs(uds=...)`, `clear_dtcs(uds=...)`, `scan_ram(uds=...)`, `read_vin_block(uds=...)`
    already accept a borrowed `UDSConnection`.
  - `flash_rom()` / `dynamic_flash()` call `_flash_rom_inner()`, which sends `transfer_data()` over
    `self._uds` — i.e. over **whatever transport was injected**. So a host-driven WiCAN flash needs
    **no new flash core**; it needs the lossy-link *safety wrapper* (Part B, Option A).
- **Read resilience already lives in `FlashManager`** (`_read_block_with_retry`, 4× retry, tight
  per-block budget, `flush()` between attempts) — reads only, idempotent. Do not touch.

**Implication:** Part A is ~90% wiring + a tool + a hardware confirm. Part B's *flash core* exists;
the new work is the **lossy-link safety wrapper** (Option A) — or a firmware build (Option B).

---

## DECISION REQUIRED — WRITE architecture (resolve before Part B)

The user's earlier grill-me proposed a **two-step SD-card autonomous flash** (upload ROM to the
WiCAN SD card over WiFi, then have the firmware flash it to the ECU locally over CAN, to dodge
wireless frame drops). **This is the prior investigation the user couldn't find — and the
design-of-record REJECTED it.** Both positions are now on the table and the context has shifted, so
this is a real fork to confirm with the user, not pick silently.

> `WICAN_TRANSPORT.md` §2 (Rejected): *SD-card / "smart device" autonomous flashing … would require
> a second safety-critical protocol implementation in ESP32 C, and the SD checksum it was meant to
> provide is redundant with our existing in-RAM ROM validation. One protocol implementation only.*

> `WICAN_TRANSPORT.md` §6 (Safety model, WRITE): **no mid-stream resend** (no block sequence
> counter → a resend bricks); resilience = TCP reliability + NRC 0x78 pending-wait + **clean
> abort-and-restart-from-scratch**; **pre-flight link-quality gate (flash only)** ~25 TesterPresent
> round-trips @ 0 loss; battery/voltage guard; optional read-back verify. *"the flash is lock-step
> ACK'd — a dropped frame yields a clean timeout/abort, never silent corruption."*

### Option A — Host-driven block-by-block (design-of-record) ✅ recommended
NC Flash drives `transfer_data()` over `WiCANTransport`, exactly as J2534 does. Each 0x36 block is
lock-step ACK'd by the ECU before the next is sent, so a WiFi drop = clean timeout → **abort →
restart from scratch** (re-auth, re-SBL, re-transfer). Never a mid-stream resend, so **no brick**.
- **Build (Part B):** a flash-only **pre-flight link-quality gate**, **abort-and-restart-from-scratch**
  orchestration, **optional read-back verify**, ensure the **battery/voltage guard** runs on the
  WiCAN path. All **pure Python**, behind the existing seam. Flash core is reused as-is.
- **Pros:** brick-safe (per design-of-record); ~80% already built; one protocol implementation; no
  new safety-critical C; trivially integrable (`FlashManager.flash_rom`/`dynamic_flash` already
  transport-agnostic). **Cons:** slow (~13–14 min full flash, §5); a flaky link can force several
  full restart-from-scratch attempts (safe, but a poor completion rate on bad WiFi).

### Option B — Two-step SD-card autonomous flash (user's grill-me preference)
Host uploads checksum-corrected ROM + host-computed SBL + flash-plan to the WiCAN SD card (FTP or a
new HTTP endpoint), then issues a "flash-from-SD" command; firmware runs RequestDownload/TransferData/
TransferExit **locally over CAN** from SD, no WiFi in the timing loop; host monitors progress markers.
- **Build:** new safety-critical ESP32-C flash state machine; SD upload + integrity-verify protocol;
  firmware progress/abort protocol; host upload tooling + a new `FlashManager` strategy. SBL/security
  stays **host-side** (the `_secure` module is not on the device) — firmware only replays bytes.
- **Pros:** WiFi quality can't disrupt the flash loop (only the retryable upload); fastest, most
  drop-immune *completion*. **Cons:** reverses a documented rejection; **duplicates the
  safety-critical flash sequence in C** (the exact thing §2 warned against); much larger; firmware
  brick-risk now sits in our code. SD hardware exists (`sdcard.c`: FAT mount, RW, OTA-from-SD), but
  generic file upload to `/sdcard` + the flash state machine are **new**.

**Note:** SD is **not** required for brick-safety — §6 already solves that with lock-step ACK +
no-resend + abort/restart. SD's only real advantage is *completion rate on a bad link*. Recommend
**Option A** unless the user prioritizes flash completion on poor WiFi over implementation cost/risk.

### If Option B: unify READ + WRITE — #22 RESOLVED (2026-06-21): keep the streaming read
The user asked: if we go SD for WRITE, should READ *also* become SD-standalone for one unified
mechanism? **Task #22 investigated this and the answer is NO** — keep the proven TCP-streaming
fast-read; use a **MIXED** architecture (streaming READ untouched, SD added *only* for WRITE if
Option B is chosen). Rationale: the read is **ECU-limited (~211 ms/block)**, so SD (100-1000× faster
than the ECU ceiling) gains zero throughput while discarding a field-validated byte-perfect path and
adding FAT/mount failure modes. Full finding + the `ncflash_fastwrite()` sketch:
`docs/internal/WICAN_PART_C_FINDINGS.md` §3.

**Net effect on the WRITE decision:** #22 removes "unify the read onto SD" from the table (it
*reduces* Option B's scope — the read stays as-is) but does **not** by itself pick A vs B for the
WRITE path. Option B still means a new safety-critical ESP32-C flash state machine.

**Decision state:** A-vs-B for the WRITE path is OPEN. **Recommend Option A** (host-driven,
design-of-record, brick-safe, ~80% already built, build-only). Option B (SD-autonomous WRITE) stays
a heavier future enhancement for completion-rate on poor WiFi. **On final greenlight, prune this
section to the chosen option.**

---

## ACTIVE PLAN

### Part A — READ RAM / READ DTC / CLEAR DTC over WiCAN  (achieve + hardware-confirm)

1. **Integration tool** — build a small, NC-Flash-integrable helper that, given a WiCAN host/port,
   opens `WiCANTransport`, authenticates with `_secure` (reuse the read path's auth: tester_present
   → diagnostic_session(programming) → security seed/key), and exposes `scan_ram`, `read_dtcs`,
   `clear_dtcs` over that connection via `FlashManager.use_uds()` / the `uds=` params. Prefer
   extending the proven `tools/wican_bench_read.py` path (it already does auth + slcan + recovery)
   over a brand-new script, so behaviour is shared and easily lifted into the UI later.
2. **READ RAM** — dump RAM (0xFFFF0000, 48 KB) over WiCAN; confirm it completes and returns
   plausible (non-empty, non-garbage) data. RAM is volatile, so no byte-oracle; sanity-check
   structure and that a J2534 dump taken seconds apart is *structurally* comparable.
3. **READ DTC** — read DTCs over WiCAN and over J2534 back-to-back on the same ECU state; confirm
   the **same DTC set** (handle NRC 0x22 "conditions not correct" → empty, already coded).
4. **CLEAR DTC** — *state-mutating, but benign/standard.* With at least one DTC present: read (non-empty)
   → `clear_dtcs` over WiCAN → re-read (empty). Caution: confirm with the user before running on a
   car they care about; clearing codes is routine but it is a write.
5. **Tests** — unit-test the wiring with `FakeTransport` (scripted DTC/RAM/clear responses,
   NRC-0x22 path, response-pending). No hardware in CI.
6. **Docs** — extend `docs/internal/WICAN_MANUAL_TEST.md` with RAM/DTC/clear steps + expected results.

### Part B — WRITE logic: full flash + dynamic flash  (BUILD ONLY — no hardware flash)

*(Steps below assume **Option A**. If Option B is chosen, replace with the firmware + SD-upload plan.)*

1. **Pre-flight link-quality gate (FLASH ONLY)** — ~25 TesterPresent round-trips over WiCAN; require
   0 loss + p95 latency under a ceiling (+ RSSI if exposed). Block the flash otherwise. Reads/diag are
   never gated. New, pure-Python, called at the top of the WiCAN flash path.
2. **Abort-and-restart-from-scratch orchestration** — on any mid-flash transport timeout/drop, do
   **not** resend mid-stream: abort cleanly, then re-auth → re-SBL → re-transfer from the start, up to
   N attempts, surfacing state for a future recovery UX (UI is goal 3). Preserve the no-block-counter
   invariant absolutely.
3. **Optional read-back verify** — after `request_transfer_exit`, optionally read the written region
   back over WiCAN and byte-compare to `rom_buf` (off by default, matches the trusted cable flow).
   Reuse `_read_block_with_retry`.
4. **Battery/voltage guard** — ensure the existing 12.0 V check runs on the WiCAN flash path (the
   historical brick cause).
5. **Wire `flash_rom` + `dynamic_flash` over WiCAN** — via `use_uds()` with a WiCAN-backed
   `UDSConnection`. `dynamic_flash` already computes the minimal region from the archive diff; reuse
   unchanged. Keep the **J2534 path byte-for-byte identical** (it is the proven flash path).
6. **Tests (the deliverable for Part B)** — `FakeTransport`-driven unit tests: full flash happy path,
   dynamic flash region math, link-gate pass/fail, drop → abort-and-restart (assert **no** mid-stream
   resend ever occurs), read-back verify mismatch. `black` + `pytest` green.
7. **Integrability** — expose the WRITE path so the goal-3 UI can call it with only a transport/host
   choice; no UI in this goal. **Do NOT flash hardware in this goal.**

### Part C — Investigate avoiding device reboots for flash functions

**✅ DONE (investigation, 2026-06-21) — see `docs/internal/WICAN_PART_C_FINDINGS.md` §1–§2.**
Root cause of the CAN-wedge reboot was confirmed against the firmware: `ncflash_fast_read`
uses `xQueueSend(..., portMAX_DELAY)`, so a host socket-close mid-stream blocks it forever and
`can_rx_task` is never resumed → wedged CAN → reboot. Fix = a bounded `xQueueSend` timeout + a
single clean-teardown path (resume rx_task on every exit, clear TWAI alerts, flush TX, reset SLCAN).
For the protocol-switch reboot, recommended a **coexisting always-on SLCAN port** over a risky hot
switch. Firmware implementation is gated/future. Original brief retained below for reference:

Today two reboot triggers get in the way of fluid flash operations:
- **Protocol-switch reboot** — switching the device to `slcan` rewrites `config.json` and reboots
  (~6 s); restoring the prior mode on disconnect reboots again (`WiCANConfigurator`, §8b). Overlaps
  existing task #10 (firmware-side hot protocol switch / always-available SLCAN port).
- **CAN-wedge reboot** — an aborted/host-closed fast-read leaves `can_rx_task` suspended / the TX path
  wedged, needing `POST /system_reboot` before the next `S6` handshake.

**Investigate and write up** (and, if cheap + safe on the fork, spike): (a) a firmware **clean
resume/teardown** so `can_rx_task` always re-enables and the TX/SLCAN state resets on fast-op exit
(abort included) — no reboot needed between ops; (b) a **hot protocol switch** or a dedicated
always-on raw-CAN/SLCAN port that coexists with the custom `poll_log` mode (task #10), so connecting
for a flash needs no config-rewrite reboot. Output: a recommendation + risk note; firmware change is
optional this goal. Reference `WICAN_TRANSPORT.md` §8b and the read-speed goal's firmware notes.

---

## Hard constraints

- **PRIME DIRECTIVE:** hospital-critical; a bad flash bricks an ECU. Failure is not an option.
- **WRITE = no mid-stream resend, ever.** No block sequence counter (verified vs romdrop). Resilience
  is abort-and-restart-from-scratch. Never add counter validation to `transfer_data`.
- **Build, don't flash (Part B).** This goal ships WRITE *logic + tests + integration hooks* only —
  **no hardware flash**. Hardware flash is a later, user-gated step.
- **J2534 path and the existing SLCAN read path stay byte-for-byte identical.**
- **Adapter-UI is OUT of scope** (goal 3).
- ROM validated/`correct_rom_checksums` BEFORE any ECU contact (already in `_flash_rom_inner`).
- Tests for all new behavior (`FakeTransport`, no hardware in CI); **CHANGELOG before any commit**;
  `black` + `pytest` green; **no push to remote master without explicit user validation**; no
  auto-commit unless asked / "land the plane".
- Firmware (if Option B / Part C spike) in `cdufresne81/nc-flash-wican-fw` on a **NEW branch**;
  known-good rollback `.bin` secured before any flash; poke the user only on suspected brick/safe-mode.

## Device / environment facts
- WiCAN PRO `192.168.1.169` — SLCAN TCP **port 35000**, HTTP **port 80**. ECU `0x7E0`/`0x7E8`, 500 kbps.
  **Live ECU connected and reachable from the dev machine** — run the bench tools directly to test
  against real hardware (memory `project_wican_hardware_in_loop`). Reads are safe; flash needs the
  link-gate + ignition ON + explicit go.
- `_secure` installed (`SECURE_MODULE_AVAILABLE=True`) — auth + SBL (`get_sbl_data`) host-side only.
- Firmware fork has SD (`sdcard.c`: FAT mount, RW, OTA-from-SD), FTP (`ftp.c`), HTTP config
  (`config_server.c`, multipart OTA upload). Generic ROM→/sdcard upload + a flash state machine are
  NOT yet present (relevant only to Option B).
- ESP-IDF v5.5.3; fast-read firmware build marker `NCFRv4`; `version_ping()` confirms the live build.

## References
- `wican-read-speed-goal.md` — phase 1 (done): transport seam, fast-read firmware, parity result.
- `docs/internal/WICAN_TRANSPORT.md` — §2 (SD rejection), §3 (seam), §6 (WRITE safety model), §8b
  (protocol-switch reboot + task #10), §9 (task checklist).
- `docs/internal/WICAN_MANUAL_TEST.md` — hardware checklist (extend for RAM/DTC/clear).
- Source: `flash_manager.py` (`use_uds`, `flash_rom`, `dynamic_flash`, `scan_ram`, `read_dtcs`,
  `clear_dtcs`), `protocol.py` (UDS services), `transport.py` (seam + `FakeTransport`).

## Provenance
Grounded in a 2026-06-21 source read of the ECU module (transport seam, FlashManager, UDS protocol)
and the WiCAN firmware (`sdcard.c`, `config_server.c`). The WRITE-architecture fork surfaces the
`WICAN_TRANSPORT.md` §2 SD rejection against the user's grill-me SD preference — **resolve with the
user before Part B.** Tasks #17–20 (tracker) map to this goal; reboot investigation extends task #10.
