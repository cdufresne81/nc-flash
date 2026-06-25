# WiCAN No-Reboot SLCAN Coexistence — Sequencing Plan

**Status:** Investigation complete (2026-06-24); implementation NOT started.
**Goal:** Replace the clunky HTTP "switch `protocol` → ~6 s reboot → switch back"
dance with an **always-on dedicated SLCAN TCP port** that coexists with the
WiCAN datalogger, and make **datalogging** and **flashing** share one CAN
controller safely.

> This doc is the durable handoff. It is written to survive a context compaction:
> read the **Resume / re-entry** section first, then jump to the step you're on.

---

## Resume / re-entry (read this first after a break)

The work is split into a **firmware track** (in `../nc-flash-wican-fw`, a separate
fork of `meatpiHQ/wican-fw`) and a **host track** (this repo, `nc-rom-editor`).
"main" for the firmware fork = the **`wican-pro`** branch.

Three relevant firmware branches, all forked from base `77e1a19`:

| Branch | What it is | State |
|---|---|---|
| `feature/option-b-sd-write` (**FWB**) | SD-staged firmware-driven flash (fastread + fastwrite + `/upload/sd`) | **Committed + pushed** `2b9284e` (2026-06-24). Live-validated byte-perfect. |
| `claude/nc-datalogger-polllog` (**FWD**) | New datalogger / fast-poll firmware (csv_logger, poll_log, engine-run gate) | **In progress** by the user on another branch; not yet merged to `wican-pro`. |
| `wican-pro` | Fork main | The integration target. |

**Where we are in the sequence (see §4):** Step 0 done. The user is finishing
**Step 1** (the fast-polling / FWD work) on their side. Next action when they
return is **Step 2** (merge FWD → `wican-pro`), then Step 3, then Step 4.

**Re-prompt to resume:** see §8.

---

## 1. Verdict

**YES — with conditions.** The always-on coexisting SLCAN port is feasible and
sits on a real firmware seam: `slcan_parse_str()`, `ncflash_fast_read()` and
`ncflash_fast_write()` are **stateless codecs that don't read the persisted
`protocol` global** — they only need SLCAN frames on the wire. So a dedicated
listener that routes through them works regardless of the device being in
`poll_log`/`auto_pid` mode.

**The port is the easy 10%.** The brick-critical 90% is **single-CAN mutual
exclusion**: there is one TWAI controller and today **no arbiter** between the
five things that transmit (SLCAN, fast-read, fast-write, poll_log, elm327). Two
verified gaps make a naive merge brick-unsafe:

1. The fast-ops suspend `can_rx_task` only — **but that task never transmits.**
   The datalogger poll task (`poll_log.c:322`, `can_send(&tx,0)`) is **never
   stopped by a flash.** A poll frame firing mid-`TransferData` injects a stray
   `0x7E0` into the ECU's ISO-TP reassembler → session corruption → soft-brick
   (survives a power cycle; needs a reflash).
2. The fast-read and fast-write busy-guards are **separate per-file statics**
   (`s_fastop_busy` in `ncflash_fastread.c`, `s_fwbusy` in
   `ncflash_fastwrite.c`) with **no cross-interlock** — they don't exclude each
   other.

Fix: a single `FLASH_ACTIVE_BIT` (see §5). That, plus moving the host RPM gate
to the flash boundary, are the load-bearing conditions.

---

## 2. How it works (target design)

Grounded in the real dispatch (`main/main.c:287` protocol gate, `main/main.c:435`
RX-forward gate, `poll_log.c:322` poll TX, `wican_transport.py:828` version_ping,
`ecu_window.py:514` RPM):

```
DEFAULT (engine running, RPM>0)
  FWD boot: protocol=POLL_LOG → poll task owns TWAI, polls 0x7E0/0x7E8, logs CSV
  engine-run gate true → logging active
  Dedicated SLCAN port (35001) is LISTENING but idle

HOST clicks FLASH
  1. version_ping() → "NCFRv<rev>"         # capability probe — no CAN, no session
        ├─ marker present → dedicated-port path (no reboot, no WiCANConfigurator)
        └─ absent/None    → legacy WiCANConfigurator switch + ~6 s reboot (fallback)
  2. one-shot OBD PID 0x0C read → assert RPM < 1.0   (RPM==0 gate)
        └─ RPM>0 → REFUSE ("engine off to flash"); override checkbox off by default
  3. command datalog to quiesce → wait for ACK (poll task confirms parked)
  4. FLASH_ACTIVE_BIT set → poll task parks, can_rx_task suspended → SLCAN owns bus
        └─ host streams 'W' (fastwrite) / 'X' (fastread) over port 35001
            dispatch: dev_channel==DEV_SLCAN_PORT branch BEFORE the protocol==SLCAN gate
  5. NCFWDONE → FLASH_ACTIVE_BIT cleared → poll task resumes datalogging
        └─ read-back verify is a post-ignition-cycle step (ECU sits in bootloader)
```

**Critical ordering:** step 2's RPM read MUST happen **before** any UDS
programming session. Once `diagnostic_session(0x85)` is entered, OBD Mode-01
reads return NRC 0x11 and RPM is unreadable (`protocol.py:294`).

---

## 3. What must be built

### Firmware — REQUIRED (lands on the **merged** branch, see §4 step 4)

| File / function | Origin | Change | ~LOC |
|---|---|---|---|
| `types.h` `dev_channel_t` | FWB | Add `DEV_SLCAN_PORT` (today only `DEV_WIFI/_WS/_BLE/_UART`) | 5 |
| `comm_server.c` | FWB | 2nd TCP listener on dedicated port; tag frames `dev_channel = DEV_SLCAN_PORT`; queue to existing `xMsg_Rx_Queue` | 40 |
| `main.c` `can_tx_task` (line 287) | FWB | **Early dispatch branch BEFORE the `protocol == SLCAN` check**: if `dev_channel == DEV_SLCAN_PORT`, route to fastread/fastwrite/`slcan_parse_str` regardless of persisted protocol | 35 |
| `main.c` `can_rx_task` (line 435) | FWB | RX reply forwarding to the SLCAN client must run **independent of `protocol`** (today gated `if(protocol==SLCAN)`). **This is the #476 "opens clean but no frames flow" trap — the single most-missed detail.** | 15 |
| **FLASH_ACTIVE_BIT interlock** (`can.c` + `ncflash_fastwrite.c` + `ncflash_fastread.c` + `poll_log.c`) | both | The brick-critical mechanism — see §5. **This is the load-bearing change, not the port.** | 60 |

**Which ops actually need the dedicated port:** fast-read (`X`) and fast-write
(`W`) are the ones **trapped inside** `if(protocol==SLCAN)` at `main.c:287`; they
will NOT run in `poll_log`/`auto_pid` mode without the dedicated-port dispatch.
The UDS auth handshake rides `slcan_parse_str()`, which is already
protocol-agnostic — it doesn't strictly need the new path, but route it through
`DEV_SLCAN_PORT` anyway for one clean code path. **→ Build the dedicated port
primarily to unlock fast-read/write outside SLCAN boot mode.**

### Firmware — NICE-TO-HAVE
- `POST /pause_datalog` + `/resume_datalog` endpoints (only if you want
  host-driven pause separate from the flash interlock).
- `/api/capabilities` — convenience only; `version_ping` already does detection.

### Host (this repo) — REQUIRED (parallel track, non-breaking, can land anytime)

| File / function | Change |
|---|---|
| `src/ecu/session.py::_connect_wican()` | Call `transport.version_ping()` first; coexist build → skip `WiCANConfigurator`, connect to the dedicated port. **Keep `WiCANConfigurator` 100% intact as the fallback** for stock/old firmware (`version_ping()==None`). |
| `src/ecu/flash_manager.py` / `session.py` | Add `enforce_rpm_gate()`: one-shot `read_engine_rpm()` (PID 0x0C, `protocol.py:277`) **before** `session.acquire()`. Today the gate lives **only** in `ui/ecu_window.py:514` (`_rpm < 1.0`) on the UI thread — move it into the flash boundary so it's enforced in code, not just UI. |
| `src/ecu/wican_sd_flash.py::_trigger_firmware_flash` | Command datalog to quiesce + confirm, then settle ~150–200 ms before entering the programming session. |

### Host — NICE-TO-HAVE
- Extend the `%TEMP%` crash-recovery sidecar (`wican_config.py:154`) to record
  "datalog stopped" so a mid-flash crash can prompt a logging restart. Default
  acceptable: manual restart via the web UI.

---

## 4. Sequencing

> Rule of thumb: never integrate the two features until each is independently
> finished and validated; build the coexistence (port + interlock) **last**, as
> its own change, on the integrated base. The reboot-switcharoo **works today**,
> so it is an acceptable stopgap until then — don't smuggle brick-critical CAN
> arbitration into a two-feature merge.

- **Step 0 — DONE (2026-06-24).** Commit + push FWB's proven flash work.
  → `2b9284e` on `origin/feature/option-b-sd-write`. (`.bin` backups + build log
  left untracked on disk by design.)
- **Step 1 — IN PROGRESS (user).** Finish the fast-polling / datalogger on
  **FWD** (`claude/nc-datalogger-polllog`). Validate it standalone.
- **Step 2.** Merge **FWD → `wican-pro`** (main). Datalogger becomes the
  baseline — it carries the framework (engine-run predicate, poll task,
  `can_rx_task` protocol gating) the coexistence sits on.
- **Step 3.** Integrate **FWB onto the new `wican-pro`** (rebase FWB on top).
  Resolve the three landmines below. Because this changes files the flash
  depends on, **RE-BENCH the byte-perfect flash on the integrated base** — the
  proven result was on FWB-*alone*. (Prime directive: non-negotiable.)
- **Step 4.** Build coexistence on the integrated branch as **its own PR**:
  `DEV_SLCAN_PORT` dispatch + `can_rx_task` RX-forward fix + `FLASH_ACTIVE_BIT`
  interlock + poll-task park. Bench-validate the interlock (§7).
- **Host track (parallel, any time).** Capability-detect the dedicated port +
  move the RPM gate to the flash boundary. Non-breaking and firmware-agnostic
  (works against both old reboot-firmware and new coexist-firmware).

### Step-3 merge landmines (resolve deliberately)

- **`vehicle.h` — HARD COMPILE ERROR if mixed.** FWB has `voltage_at_ignition`;
  FWD renamed it to `engine_on_volt` with hysteresis. **Take FWD's
  `engine_on_volt`** — FWB's reused the ~12.4 V sleep threshold and would
  false-read "engine running" at 12.6–12.8 V parked, breaking the datalog gate.
- **`config_server.c` — latent bug already in FWB.** FWB sets
  `config.max_uri_handlers = 38` (~line 3887) and **never bumped it when
  `/upload/sd` was added** → can silently drop the catch-all `/*` file server
  (404s all web assets). FWD bumped it to **48** (~line 3833). **Take FWD's 48**
  and verify the post-merge handler count fits. ⚠️ *Worth checking on the
  current FWB firmware regardless of this feature.*
- **`config_server.h` enum.** FWB has `SLCAN..AUTO_PID` (0–4); FWD adds
  `FAST_LOG=5`, `POLL_LOG=6`. **Take FWD's.**
- **`main.c`.** Non-overlapping. Keep FWB's fastread/fastwrite dispatch AND
  FWD's `FAST_LOG`/`POLL_LOG` blocks. The new `DEV_SLCAN_PORT` branch lands here
  post-merge.
- **`CMakeLists.txt`.** FWD's requires-list is the superset (`csv_logger`,
  `fast_log`, `sd_filemgr`). **Take FWD's**, then append FWB's fastwrite/upload
  components. (FWB's `main.c` won't compile without FWD's component headers.)

---

## 5. Single-CAN mutual exclusion (brick-critical — the load-bearing piece)

Define one CAN-owner flag and make every TX producer respect it.

1. **`can.c`:** add `FLASH_ACTIVE_BIT` to the existing CAN event group (alongside
   `CAN_ENABLE_BIT`), with `set` / `clear` / `is_set` accessors.
2. **`ncflash_fastwrite.c` + `ncflash_fastread.c`:** `set(FLASH_ACTIVE_BIT)`
   **before** suspending `can_rx_task`; in the clean-teardown label,
   `clear(FLASH_ACTIVE_BIT)` **before** `vTaskResume`, on **every** exit path
   (success / host-gone timeout / abort / socket close). The Part-C #21
   clean-teardown (bounded `tx_send`, resume + drain + clear TWAI alerts) is
   already present in FWB — hook the bit-clear into it.
3. **`poll_log.c::polllog_rx_task`:** at the top of each loop iteration, if
   `FLASH_ACTIVE_BIT` is set, **sleep a short interval and skip** (NEVER block on
   `portMAX_DELAY` — that would trip the watchdog while fastwrite holds the bit).
   Do **not** flip to LISTEN_ONLY; just hold and resume when the bit clears.
4. **Unify the busy-guards:** fold `s_fastop_busy` / `s_fwbusy` into the
   `FLASH_ACTIVE_BIT` check (or have each test the other) so fast-read and
   fast-write also exclude each other.

**Invariant after this change:** at most one TX producer owns the bus at any
instant, on every exit path. **This must be hardware-validated** (see §7).

---

## 6. Design decisions

| Decision | Status | Note |
|---|---|---|
| RPM==0 flash gate strictness | **LEANING: hard-block + override** | Refuse to enter the programming session if RPM ≥ 1.0, enforced in flash code; explicit override checkbox, off by default. Confirm before building. |
| Datalog silencing during flash | **LEANING: park-and-hold flag** | Poll task sleeps on `FLASH_ACTIVE_BIT`, auto-resumes. No TWAI uninstall race. Confirm. |
| Engine-run detect for datalog (your "datalog only when RPM>0") | **OPEN** | Chicken-and-egg: to *know* RPM>0 the firmware must poll PID 0x0C, but you only want to poll while RPM>0. FWD already uses **battery-voltage hysteresis** (`engine_on_volt`) as the cheap proxy. Candidate resolution: **hybrid** — voltage to *wake* logging, RPM to *confirm*. **Needs user decision before Step 4.** |
| Dedicated port number | **LEANING: 35001** | Pin it, document it, discover capability via `version_ping` rather than assuming the port. Confirm no binding conflict on the deployed config. |
| Crash-recovery for datalog-stop | **LEANING: manual restart** | User restarts logging via web UI after an interrupted flash. Auto-restart is nice-to-have. |

---

## 7. Bench checklists

**Step 3 (integrated base):**
- [ ] Byte-perfect full flash on the integrated firmware == source ROM.
- [ ] Full ROM read byte-perfect; DTC read/clear; RAM scan.

**Step 4 (coexistence):**
- [ ] Flash with `poll_log` enabled → assert **zero CSV rows** and **zero
      `0x7E0` poll frames** for the entire flash duration (interlock proof).
- [ ] Simulate WiFi drop at ~50 % of a flash → confirm the **next** flash
      recovers **without a device reboot** (clean-teardown + bit-clear proof).
- [ ] Dedicated port forwards ECU replies **while in `poll_log` mode** (no #476
      RX-trap): connect → `O` → fastread returns bytes without a protocol switch.
- [ ] Concurrent fast-read + fast-write request → the second is refused, not run
      (unified guard proof).

---

## 8. Re-prompt to resume (after the user finishes FWD)

Paste something like:

> Resume the WiCAN no-reboot SLCAN coexistence work. Read
> `docs/internal/WICAN_SLCAN_COEXISTENCE_PLAN.md`. I've finished the fast-polling
> on `claude/nc-datalogger-polllog` and [merged it to `wican-pro` / NOT merged
> yet]. Let's start at **Step <N>**.

Tell me the FWD state (merged to `wican-pro` or not) and which step you want.
The project memory pointer (`project_wican_slcan_coexistence`) will also
auto-surface this context.

---

## 9. Reference (verified file:line)

Firmware (`../nc-flash-wican-fw`, FWB working tree unless noted):
`main/main.c:287` (protocol/TX gate), `main/main.c:435` (RX-forward gate),
`main/ncflash_fastread.c:256,268`, `main/ncflash_fastwrite.c:338,368,507`,
`main/config_server.c:3887` (FWB `max_uri_handlers=38`), `main/vehicle.h:51`,
`main/config_server.h`.
Datalogger (`claude/nc-datalogger-polllog`, FWD):
`components/fast_log/poll_log.c:322,587`, `main/config_server.c:3833` (`=48`),
`main/config_server.h:51-57` (`FAST_LOG`/`POLL_LOG`), `main/vehicle.h:51`
(`engine_on_volt`).
Host (this repo): `src/ui/ecu_window.py:514` (RPM gate today),
`src/ecu/protocol.py:277` (read RPM PID 0x0C), `:294` (NRC 0x11 in session),
`src/ecu/wican_transport.py:828` (`version_ping`),
`src/ecu/wican_sd_flash.py:357`, `src/ecu/wican_config.py:154` (recovery sidecar).

See also: `WICAN_PART_C_FINDINGS.md` (the prior, partly-stale investigation —
this plan corrects it where the live code differs), `WICAN_TRANSPORT.md`,
`WICAN_MANUAL_TEST.md`.
