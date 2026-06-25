# /goal — WiCAN WRITE: Option B (SD-staged, firmware-driven local-CAN flash)

> Self-contained driver for getting **WRITE / full + dynamic flash working over WiCAN** (task #20).
> Supersedes the "DECISION REQUIRED" section of `wican-ecu-functions-goal.md`: **Option A
> (host-driven block-by-block WiFi write) is REJECTED** — it is both too slow (~8–10 min) and
> brick-prone (interrupted programming session soft-bricks the ECU; see memory
> `project_wican_write_bricks_on_interrupt`). **Option B is the path.**
> After `/compact`, resume from "ACTIVE PLAN / WORK BREAKDOWN" below.
> Grounded in a 2026-06-23 source read of the host (`src/ecu/*`, `src/ui/ecu_window.py`) and the
> firmware fork (`C:\Users\dufre\Projets\nc-flash-wican-fw`, branch `feature/fast-rom-read`, NCFRv4),
> via the `scope-wican-option-b` workflow (6 readers + synthesis).

---

## STATUS (2026-06-23)

- ✅ **SD upload metered.** A 1 MB HTTP multipart upload to the WiCAN sustains **~1 s/MB**
  (~0.95 MB/s ≈ 7.6 Mbit/s), measured non-destructively via `tools/wican_upload_meter.py` against the
  live device. → **Option B time budget ≈ ~1 s upload + ~55 s firmware flash ≈ Tactrix parity.** The
  WiFi link is NOT a concern.
- ✅ **Handoff boundary resolved in code.** Host keeps all secrets/compute; firmware replays plain
  bytes. The one residual unknown is hardware-only (auth-session continuity across the bus handoff).
- 🚧 **Nothing built yet.** This is the scope/plan. Phase 0 (make-safe) is shippable immediately.

---

## THE CORE IDEA

Replace the host-driven, block-by-block WiFi `TransferData` loop with an **SD-staged, firmware-driven
local-CAN flash**, taking WiFi *out of the flash loop* (the brick driver):

1. **Host** checksum-corrects the ROM, computes the SBL + seed/key (all `_secure`, host-only), and
   **HTTP-uploads** the staged ROM (+SBL/manifest) to the WiCAN SD card over a NEW `/upload/sd/<name>`
   endpoint (reliable TCP, ~1 s/MB, the only WiFi step — verifiable *before* the ECU is touched).
2. **Host** authenticates the ECU over CAN (short, idempotent, not brick-prone), then issues a
   **flash-from-SD trigger**.
3. **Firmware** `ncflash_fastwrite` (a mirror of the proven `ncflash_fastread`) drives
   `RequestDownload → TransferData(SBL) → TransferData(program) → TransferExit → ECUReset` **locally
   over CAN at line rate**, sourcing bytes from the SD file, streaming progress markers back.
4. **Host** does a **mandatory final read-back byte-compare** (its only integrity proof for the
   off-host bulk write).

**Why the brick mechanism disappears:** the ECU's `BS=0/STmin=0` Flow Control is harmless when the
Consecutive Frames go out over **local CAN** — there is no WiFi gateway TCP→CAN buffer to overflow
(the exact thing the host-driven path overran). The firmware read loop already relies on this.

---

## THE HANDOFF BOUNDARY (host vs firmware) — load-bearing

**HOST-ONLY (never crosses to the device):**
- `compute_security_key(seed)` — the seed→key LFSR IP (`_secure/_security.py:20`). The key is computed
  **live** from the ECU's per-session seed, so it *cannot* be pre-staged. Host stays in the auth loop.
- `get_sbl_data(flash_start_index, generation)` — decrypts/patches the SBL (`_secure/_sbl.py:66`, key
  `b'!5XMCECN'`, encrypted blobs, patch offsets). Embedding this in a USB-dumpable ESP32 would leak the
  IP. Only the **output** (the patched 0x1800 SBL bytes) crosses, staged to SD.
- `correct_rom_checksums` — ECU-only checksum correction (`checksum.py`, table @0xFF650). Firmware has
  no checksum routine. The integrity digest covers the **exact corrected bytes**.
- Dynamic-flash diff (`find_first_difference` / `calculate_flash_start_index`) and the `ncflash.rda`
  archive. Firmware only ever sees the final program slice + a flash-plan.
- The `SECURE_MODULE_AVAILABLE` guard stays on the host entry point (SD-staging impossible without it).

**FIRMWARE (plain bytes only):** open the staged SD file → emit the 4 fixed UDS services → lock-step
wait for ACKs → stream progress. No secrets, no checksum, no diff, no resend, no block counter.

---

## EXACT ECU SEQUENCE THE FIRMWARE REPLAYS

(Verbatim from the host `_flash_rom_inner`, `flash_manager.py:555-726`; all bytes verified vs romdrop.)

1. `RequestDownload` **once**: `[0x34] + addr(0x00008000, 4 BE) + size(0x000FF800, 4 BE)` — **no**
   dataFormatIdentifier/ALFID (KWP2000-style, romdrop 0x0040472F) → await `0x74` (skip `7F 34 78`).
2. `TransferData` loop for the **SBL** (0x1800 B) in 0x400 blocks: `[0x36] + raw` — **NO block
   sequence counter** (`protocol.py:416-421`, romdrop 0x004047A6) → await `0x76` per block.
3. `TransferData` loop for the **program** (`rom_buf[flash_start_index:]`, from the SD image offset)
   in 0x400 blocks, identical lock-step.
4. `RequestTransferExit`: `[0x37]` → await `0x77`.
5. `ECUReset`: `[0x11,0x01]` best-effort (no-response/timeout is expected and OK).

Per 0x400 block the firmware sends ISO-TP **FF + Consecutive Frames** (1025 payload bytes ⇒ always
multi-frame), **receives** the ECU Flow Control (`30 00 00`), streams CFs at line rate, then waits for
the positive `0x76` (riding out `7F 36 78` response-pending NRCs). **Advance only on ACK — never
retry a block mid-message.** A single 0x34 covers **both** the SBL and program transfers.

Constants (host `constants.py`): `DOWNLOAD_ADDR=0x8000`, `DOWNLOAD_SIZE=0xFF800`, `BLOCK_SIZE=0x400`,
`SBL_SIZE=0x1800`, `ROM_FLASH_START_MIN=0x2000` (full-flash start; program = `rom_buf[0x2000:]`).

---

## COMPONENTS

### Firmware (`C:\Users\dufre\Projets\nc-flash-wican-fw`, new NCFRv5 build)
- **`ncflash_fastwrite.c` (NEW, ~500–600 LOC)** — mirror of `ncflash_fastread.c:201-330`, **inverted**:
  ISO-TP *sender* (FF+CF, 12-bit length, seq wrap 1..F, 8-byte pad, honor ECU FC), lock-step ACK-wait,
  SD reader (fopen once, fread per block), pre-erase integrity gate, progress/done/error markers.
  Reuses the read scaffold: `can_tx_task` ownership, `can_rx_task` suspend/resume (`:232-233`/`:324`),
  `recv_matching`/`send_frame`, NRC-0x78 skip (MAX_PENDING=16), WDT yield every 32 blocks, FRERR-style
  diag. **Must NOT copy** the `portMAX_DELAY` strand bug.
- **flash-from-SD command parser + dispatch** — new `ncflash_is_fastwrite_cmd` checked before
  `slcan_parse_str` in the `main.c:286-298` DEV_WIFI/SLCAN branch. First byte **`W`** (distinct from
  `X` and from SLCAN `t/T/r/R`/hex).
- **`/upload/sd/<name>` HTTP endpoint** — reuses `multipart_upload_handle` + `file_on_part_*`
  (`config_server.c:2050-2106`), template `upload_car_data_handler` (`:2192-2250`), but targets
  **`/sdcard/roms/`** (NOT `/littlefs`). Wildcard `/upload/sd/*` HTTP_POST; `mkdir` roms if missing;
  filename guard (reject `..`/`/`/`\`/trailing-slash, require extension); size cap in `file_on_part_data`
  (none today); atomic temp+rename; **no reboot, no can/ble disable**; return `{bytes, crc32}`.

### Host (`nc-rom-editor`)
- **`WiCANSdFlasher` (NEW)** — replaces `WiCANFlasher`'s inner `_one_flash_rom`/`_one_dynamic_flash`,
  same `flash_rom`/`dynamic_flash`/`preflight` surface (one-line swap at `_build_flash_driver`).
  **Reuses verbatim:** `preflight()` link gate, `_gate()` battery guard (≥12.0 V), `_run_with_restart()`
  abort-and-restart, `_verify()` read-back compare (`wican_flash.py:120/184/208/271`).
- **`wican_sd_upload.py` (NEW)** — HTTP multipart upload + trigger/progress client, modeled on
  `WiCANConfigurator` urllib pattern (`wican_config.py:143/356`). New
  `WiCANTransport.fast_write()` mirrors `fast_read`/`version_ping`/`_frerr_suffix`.
- **Host pre-compute & package step** — checksum-correct + SBL + generation detect + flash_start_index
  + SHA-256/CRC32 + manifest, with host-side integrity self-checks.
- **UI integration + make-safe** — drop `WiCANSdFlasher` into `_build_flash_driver`; **immediately**
  hard-disable the host-driven write (`_confirm_wican_flash` → False) until Option B ships.

---

## ROM STORAGE (per user directive)

- **Directory:** `/sdcard/roms/` (created via `mkdir` if missing; NOT `/littlefs`).
- **Filename:** `<ROM_ID>_<YYYYMMDD>-<HHMM>.bin`
  - **Example:** `SW-LFDJEA000_20260623-1745.bin`
  - Mirrors NC Flash's existing read auto-save `<ROM_ID>_<YYYYMMDD>_<HHMMSS>.bin`
    (`ecu_window.py:1109-1115`, e.g. `SW-LFDJEA000_20260621_175529.bin`) but with the requested
    `yyyymmdd-hhmm` form (date–time joined by a hyphen, minute precision).
  - **Rationale:** ROM_ID prefix ties the staged-and-flashed bytes to the exact ROM the user
    loaded/edited (audit trail for a hospital-critical flash); `yyyymmdd-hhmm` sorts chronologically
    in a FAT listing; the timestamp prevents clobbering a prior staged copy.
  - **Collision (same ROM_ID + minute):** **reject-or-suffix, never silently overwrite** (preserve
    traceability). Decide reject vs suffix in Phase 2.

---

## SAFETY INVARIANTS (PRIME DIRECTIVE — a bad flash bricks an ECU)

1. **NO mid-stream resend, EVER.** A dropped CF / missed `0x76` / SD-read error / timeout aborts the
   **whole** session → restart-from-scratch (re-auth → new live seed/key → re-run the full sequence).
   **Do NOT port the read loop's `BLOCK_RETRIES` into the write loop.**
2. **NO block-sequence counter** on `TransferData` (`[0x36]+raw` only). Adding one shifts the image and
   bricks. (`protocol.py:416-421`.)
3. **Security/SBL/auth stay host-side.** Only computed outputs cross (patched SBL bytes; the live 3-byte
   key). `SECURE_MODULE_AVAILABLE` guard preserved.
4. **ROM checksum-corrected host-side** before upload; host re-verifies zero residual corrections on the
   staged image before triggering.
5. **Pre-erase integrity gate is HARD-BLOCKING.** Firmware recomputes SHA-256/CRC32 over the staged SD
   file and **refuses** `RequestDownload`/erase on mismatch (verify-then-stream from the same region,
   TOCTOU-safe). A corrupted upload aborts harmlessly with NO ECU contact.
6. **Clean-teardown on EVERY exit** with **bounded** `xQueueSend` (resume `can_rx_task`, drain RX,
   `twai_clear_alerts`, close SD, flush queues, reset SLCAN). The Part-C #21 CAN-wedge fix is a
   **prerequisite** — a post-erase abort needs a re-auth-able CAN channel to restart.
7. **Battery guard stays mandatory** (≥12.0 V). Brown-out is the historical brick cause, independent of
   transport; the ECU still self-programs for the full duration.
8. **Mandatory final host read-back compare**, default **ON** for Option B (only integrity proof for the
   off-host write).
9. **"Abort once erase has begun" is NOT safe** — it means stop sending + KEEP-IGNITION-ON +
   restart-from-scratch. UI offers a safe abort *only* in the pre-trigger upload/auth phase.
10. **J2534 + existing SLCAN streaming READ stay byte-for-byte unchanged.** Only a new command token, a
    new endpoint, and a new firmware file are added.

---

## ACTIVE PLAN / WORK BREAKDOWN

**Phase 0 — Make-safe (host only, ship immediately, no firmware)**
- Hard-disable the host-driven `WiCANFlasher` write path in the UI (`_confirm_wican_flash` → False, or
  `_build_flash_driver` refuses flash/dynamic_flash for WiCAN). Reads/scan/DTC unaffected.
- Regression test asserting WiCAN flash/dynamic_flash is blocked at the UI seam. CHANGELOG (Changed).

**Phase 1 — Prerequisite firmware teardown fix (no ECU write)**
- Land the Part-C #21 clean-teardown fix in the shared fast-op scaffold (bounded `xQueueSend`, single
  teardown always resumes `can_rx_task`, drains RX, `twai_clear_alerts`, flush, SLCAN reset; socket-close
  detection). Re-entry mutex (one fast-op at a time). Verify READ path byte-for-byte unchanged + no longer
  wedges CAN on host disconnect (WICAN_MANUAL_TEST).

**Phase 2 — SD upload endpoint (no ECU write, fully testable)**
- Firmware `/upload/sd/<name>` (handler + wildcard URI, guard, mkdir, size cap, atomic rename,
  `{bytes,crc32}`, non-destructive). Host `wican_sd_upload.py` client (multipart with `filename=`;
  verify returned crc32/size vs host digest).
- Bench: upload ~1 MB ROM, read back + byte-compare; traversal/oversize rejection; atomic-rename. Unit
  tests for the host client.

**Phase 3 — Host pre-compute + package + orchestrator skeleton (no ECU write)**
- Host package step (correct_rom_checksums, get_sbl_data, generation detect, flash_start_index for
  full+dynamic, SHA-256/CRC32, manifest) + integrity self-checks. `WiCANSdFlasher` reusing
  preflight/_gate/_run_with_restart/_verify, wired behind `_build_flash_driver` but trigger **stubbed/
  rev-gated off** (version_ping refuses on fast-read-only firmware). Unit-test packaging.

**Phase 4 — Firmware `ncflash_fastwrite` DRY-RUN (NO erase, NO RequestDownload)**
- Implement ISO-TP sender, lock-step ACK-wait, SD reader, progress markers, pre-erase digest gate — but
  STOP before `RequestDownload` (verify digest, parse plan, stream NCFWPROG no-op, report would-flash
  counts). Bench: digest gate hard-blocks a corrupted upload; program slice offset/len match manifest;
  clean teardown on host disconnect mid-dry-run.

**Phase 5 — Live flash on a RECOVERABLE/sacrificial ECU (the brick-critical step)**
- **Resolve the auth-handoff unknown FIRST** (session carries over vs seed-relay) via
  `tools/wican_flash_diag.py`. Enable the full sequence; validate lock-step ACKs, FC handling,
  no-resend abort, NCFWDONE, mandatory read-back compare, interrupted-flash → clean abort →
  restart-from-scratch recovery. Capture a TransferData FC on the bench.

**Phase 6 — Productionize + enable**
- Enable `WiCANSdFlasher` behind the version_ping rev-gate; upload as a distinct progress phase; abort
  only pre-program; read-back verify ON by default. Integration test (faked transport+HTTP). Amend
  `WICAN_TRANSPORT.md` §2 (SD-for-WRITE-only accepted), CHANGELOG, README, notes.

---

## INTERFACES

- **`POST /upload/sd/<name>`** — wildcard `/upload/sd/*`, multipart/form-data, file part **must** carry
  `filename=` (`file_on_part_begin` skips parts without it). → `/sdcard/roms/<sanitized>` atomic;
  `{bytes_written, crc32}`; 503 if SD unmounted; 500+unlink on write fail.
- **Flash trigger `W`** — SLCAN/DEV_WIFI command carrying `{SD filename, program_offset(=flash_start_index),
  program_len, sbl_offset/len or 'embedded', download addr/size}`. Fixed-width hex fields, CR-terminated.
- **Progress markers** (firmware→host ASCII): `NCFWSYNC` (lock-on), `NCFWPROG <done>/<total>` (every N
  blocks), `NCFWDONE` (success), `FWERR a=… st=… nrc=… f=…` (failure).
- **`WiCANTransport.fast_write()`** — sends `W`, hunts NCFWSYNC, parses PROG/DONE/FWERR → `FlashProgress`.
- **Flash-plan manifest** — `{download_addr, download_size, block_size, flash_start_index,
  generation, sbl_len, sbl_offset, program_offset, program_len, rom_sha256, rom_crc32}`. Firmware reads
  the program slice from `program_offset` in the SD image (**not** byte 0 — load-bearing for dynamic).
- **Auth handoff** — HOST (live CAN, `_secure`-gated, idempotent): tester_present → diag_session(0x85) →
  seed → compute_security_key → send_key → check_flash_counter. FIRMWARE (exclusive CAN, bytes only):
  RequestDownload → TransferData(SBL) → TransferData(program) → TransferExit → ECUReset.

---

## PROGRESS REPORTING — how NC Flash sees the remote flash (verified)

**Data path (all primitives already exist and are proven by `fast_read`):**
`ncflash_fastwrite` (in `can_tx_task`) fills the static `s_out` xdev_buffer with an ASCII line
(`dev_channel=DEV_WIFI`) and `xQueueSend`s it onto `xMsg_Tx_Queue` (the 4-line idiom at
`ncflash_fastread.c:219-222/244-248/288-291/299-313`) → a **separate** FreeRTOS task
`tcp_server_tx_task` (`comm_server.c:195-236`, bound via `main.c:1009-1019`) drains the queue to the
TCP socket → host `WiCANTransport.fast_write()` reads **raw socket bytes** off `self._sock`
(select+recv, mirroring `_fast_read_one` `wican_transport.py:560-660`), resyncs on a sentinel marker,
parses ASCII progress lines → maps each into the **existing** `FlashProgress`
(`flash_manager.py:144-159`) through the **existing** `ProgressCallback` → `_FlashWorker._on_progress`
→ Qt signal → `_on_flash_progress` (`ecu_window.py:957-960`) updates the same state label / progress
bar / detail text the J2534 flash drives.

**Why it works mid-flash:** the firmware suspends **only** `can_rx_task` (`ncflash_fastread.c:232-233`,
resumed `:324`), never the TCP-writer task — so the socket keeps flowing while the firmware owns CAN.
This is the exact window `fast_read` already streams ROM bytes through.

**Wire contract (newline-delimited ASCII sentinels that cannot collide with SLCAN hex frames):**
- `NCFWSYNC\n` — once, after the firmware takes the bus; host resync anchor (discard everything up to
  it). → synthesize `FlashState.TRANSFERRING_PROGRAM`.
- `NCFWPROG <done>/<total>\n` — per block (or per N); host parses `(done,total)` → `progress_cb` →
  `FlashProgress(TRANSFERRING_PROGRAM, percent scaled into the 35–90% band, bytes_sent/total)`.
- `NCFWDONE\n` — terminal success → FINALIZING→COMPLETE, bar 100%.
- `FWERR a=<addr> st=<stage> nrc=<XX>\n` — terminal failure (mirror of `FRERR`); host tail-scans it
  (a `_frerr_suffix` clone) → `WiCANError` → `_on_flash_finished(False, …)` → **"flash incomplete,
  re-flash required"** (no mid-stream resend; recover via restart-from-scratch).

**Heartbeat / stall detection (adversarial — the load-bearing subtlety):** the host distinguishes a
*healthy slow* flash from a *dead* firmware with TWO timers reused from `fast_read` — a **short
idle/heartbeat** timeout (declares death in seconds when the stream goes silent) **plus** a generous
overall deadline backstop. A naive single total-timeout is wrong both ways (false-aborts a real erase,
or hangs on a dead device). **CRITICAL:** an ECU erase sits in NRC `0x78` response-pending for tens of
seconds, so the firmware MUST emit a keep-alive (`NCFWPROG` repeat or a dedicated `NCFWBUSY`) **while**
waiting on `0x78`, not only after a block completes — else a real erase reads as dead.

**NEW safety rule (the key new hazard):** every device→host `xQueueSend` in the read code uses
`portMAX_DELAY`. If the host stops reading, the writer blocks, the 32-slot `xMsg_Tx_Queue` fills, and
the next `xQueueSend` **inside the flash loop blocks forever** — freezing the flash mid-write (brick
risk). So `ncflash_fastwrite` progress sends MUST use a **bounded** `xQueueSend` timeout and **drop the
progress line on backpressure** rather than block the flash. Telemetry is display-only — never let it
stall programming. (Also replicate the periodic `vTaskDelay(1)` yield so the equal-priority writer
isn't starved on one core.)

**Abort:** deliberately NOT offered during write (`allow_abort = operation in ('read','scan_ram')`,
`ecu_window.py:911-913`; `WiCANFlasher` has no `abort`). The progress feed is **display-only**.
"Stop sending" ≠ "safe to power off" — killing the host/socket does not stop the firmware-owned flash;
the safe state is reached only at `NCFWDONE` or `FWERR`.

**Reuse vs new:** REUSE (battle-tested by `fast_read`/`version_ping`) = the stream primitive + writer
task + host raw-socket reader + sentinel-resync + `_frerr_suffix` tail-scan + the
`FlashProgress`→`_on_flash_progress` UI chain + `WiCANFlasher.flash_rom`'s existing `progress_cb`. NEW
= (1) the `ncflash_fastwrite` firmware, (2) the line-delimited wire contract above, (3)
`WiCANTransport.fast_write()` + the glue mapping markers → `FlashProgress`/`FlashState`. Note:
`fast_read`/`version_ping` are NOT wired into `FlashManager`/the flash UI today (bench-tools only), so
this glue is the **first production consumer** of a firmware-streaming command.

## OPEN DECISIONS

1. **AUTH HANDOFF (#1 hardware unknown, gates Phase 5):** does the host-authenticated programming
   session survive the host→firmware bus handoff (shared CAN context), or must auth move to a
   **seed-relay** (firmware relays the live seed up, host computes key, firmware sends it)? Both keep
   `compute_security_key` host-side. Answer on the real ECU via `wican_flash_diag.py`. **Default:** host
   does full auth over CAN then triggers firmware; pivot to seed-relay only if the session doesn't carry.
2. **TesterPresent during the flash** — at line rate the transfer is short; confirm against a bench-timed
   full flash whether the firmware must interleave `0x3E` to beat the ECU S3 timeout.
3. **`check_flash_counter` placement** — recommend host-side (needs the live authenticated session).
4. **Staged payload shape** — recommend a single checksum-corrected ROM image + a small sidecar manifest
   (SBL staged separately or appended); freeze the contract before Phase 4.
5. **ECU max WRITE block** — keep 0x400 to match the proven host sequence (task #23 may revisit).
6. **On-device digest cost** — full SHA-256 over ~1 MB on ESP32-S3 pre-erase vs CRC32 on-device +
   SHA-256 in the host read-back. Confirm S3 SHA throughput.
7. **`/sdcard/roms/` overwrite policy** — reject-or-suffix, never silent overwrite.
8. **SLCAN port-coexistence (Part-C §2, port 35001)** — recommend piggyback on the proven
   `protocol==SLCAN/DEV_WIFI` read dispatch initially; revisit if persisted-protocol issues surface.

---

## TOP RISKS

- `ncflash_fastwrite` is a NEW safety-critical ESP32 flash state machine — the single highest-risk item.
  A lock-step bug (advancing past an un-ACK'd block, an added counter, a malformed FF/CF, an SD-read
  error mid-block) bricks with no resend. **Hardware-validate on a sacrificial/recoverable ECU first.**
- The ISO-TP **sender** is entirely new (the read loop only receives multi-frame). Sender bug = brick.
- Auth-session continuity across the handoff is unproven on hardware.
- Copying the `portMAX_DELAY` strand bug → host disconnect mid-flash wedges CAN *and* leaves the ECU
  mid-program (unrecoverable). Teardown fix is a hard prerequisite.
- A pre-erase digest gate that is computed but not **hard-blocking** is worthless.
- Read-back verify must default ON (only off-host integrity proof).

---

## HARD CONSTRAINTS (carry-over)

- **PRIME DIRECTIVE:** hospital-critical; failure is not an option.
- **Build/test incrementally; never leave a way to brick untested.** No live ECU write before Phase 5,
  and only on a recoverable ECU.
- Firmware work in `cdufresne81/nc-flash-wican-fw` on a **NEW branch**; **secure a known-good rollback
  `.bin` before any OTA** (OTA is write-only — see memory `feedback_wican_firmware_backup`). Poke the
  user only on suspected brick/safe-mode.
- Tests for all new host behavior (`FakeTransport` + faked HTTP, no hardware in CI). **CHANGELOG before
  any commit.** `black` + `pytest` green. **No push to remote master without explicit user validation.**
  No auto-commit unless asked / "land the plane".
- Plan docs (`.claude/plans/*`) and `.bin` artifacts are NOT committed/tracked.

## DEVICE / ENVIRONMENT FACTS
- WiCAN PRO `192.168.1.169` — SLCAN TCP **35000**, HTTP **80**. ECU `0x7E0`/`0x7E8`, 500 kbps. Live MX-5
  NC ECU reachable from the dev machine (VIN JM1NC2FF0A0207980). SD mounts FAT at `/sdcard`
  (`sd_card_init` at `main.c:599`); internal config FS is `/littlefs`.
- `_secure` installed (`SECURE_MODULE_AVAILABLE=True`); auth+SBL host-side only. Rapid re-auth trips NRC
  0x22 cooldown — leave seconds between programming sessions (drives the restart backoff).
- Firmware fork ESP-IDF v5.5.3; build marker NCFRv4 → bump to NCFRv5 with fastwrite support;
  `version_ping()` rev-gates the trigger. FTP is compiled-in but DEAD (task #25 supersedes it with
  `/upload/sd`).

## REFERENCES
- `tools/wican_upload_meter.py` — the ~1 s/MB metering tool.
- `docs/internal/WICAN_PART_C_FINDINGS.md` — §1/§2 teardown + protocol-switch; §3 `ncflash_fastwrite` sketch.
- `docs/internal/WICAN_TRANSPORT.md` — §2 (SD rejection, to be amended), §6 (WRITE safety model).
- `wican-ecu-functions-goal.md` — the prior goal (Part A done; this supersedes its Part B / Option A).
- Host: `flash_manager.py` (`_flash_rom_inner`, `_authenticate`), `protocol.py` (UDS services),
  `wican_flash.py` (safeguards to reuse), `ecu_window.py` (`_build_flash_driver`, `_confirm_wican_flash`).
- Firmware: `ncflash_fastread.c` (the mirror template), `config_server.c` (upload handlers), `sdcard.c`.
