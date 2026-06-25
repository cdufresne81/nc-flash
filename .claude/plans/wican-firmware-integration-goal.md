# WiCAN Firmware Integration & No-Reboot Coexistence — Goal Driver

**Covers tasks #35, #36, #37.** This is the execution driver for the final WiCAN
firmware roadmap. It is deliberately thin on rationale — the durable, fully
reasoned design lives in **`docs/internal/WICAN_SLCAN_COEXISTENCE_PLAN.md`**
(read it first; the §-references below point into it). This doc tracks *where we
are*, *what's next*, and the *done-gate* for each goal.

> **PRIME DIRECTIVE applies in full.** Every goal here touches the brick-critical
> flash path. Nothing is "done" until it is **hardware-validated** on a
> recoverable bench ECU (architecture rule: ECU paths need a real hardware test,
> not a mock). A soft-bricked ECU needs a reflash; a bricked one in a car is a
> tow + dealer event. No exceptions.

---

## Mission

Make firmware-driven WiCAN flashing (the proven SD-staged fast-write) **coexist**
with the WiCAN datalogger on one CAN controller — **no protocol-switch reboot,
brick-safe** — and teach the host to detect and drive that path while enforcing
the engine-off gate in code. End state: the user clicks Flash in NC Flash; the
adapter stays in datalogging mode, the datalogger parks for the flash, the flash
streams over a dedicated always-on SLCAN port, then logging resumes — with zero
~6 s reboot and zero chance of a stray poll frame corrupting the UDS session.

---

## Current state (verified 2026-06-25)

**Firmware fork** `../nc-flash-wican-fw` (fork of `meatpiHQ/wican-fw`; "main" =
`wican-pro`). Three branches matter, all forked from base `77e1a19`:

| Branch | Role | State (verified) |
|---|---|---|
| `wican-pro` | integration target | **Now carries FWD.** `origin/wican-pro` @ `49d43ec` contains `components/fast_log/poll_log.c`, `csv_logger`, `fast_log` (merged via PR #6, `9f1049c`). **Does NOT yet contain FWB** (no `ncflash_fastwrite.c`). |
| `feature/option-b-sd-write` (**FWB**) | SD-staged firmware flash (fastread + fastwrite + `/upload/sd`) | @ `2b9284e`. **Live-validated byte-perfect, standalone.** Not yet integrated onto `wican-pro`. |
| `claude/nc-datalogger-polllog` (**FWD**) | datalogger / fast-poll | **Merged → `wican-pro`** (Task #34 ✅). |

**Host repo** `nc-rom-editor` @ `master` `ec08bf4`: WiCAN transport, fast-read,
host-side fast-write glue, discovery, and the `WiCANConfigurator` reboot-switch
stopgap are all landed. The RPM gate lives **only** in the UI
(`src/ui/ecu_window.py:514`), not in the flash boundary.

**So the sequence (plan §4) stands at:** Step 0 ✅, Step 1 ✅, **Step 2 (FWD →
wican-pro) ✅ = #34**. Next: **Step 3 = #35**, then **Step 4 = #36**, with the
**host track = #37** runnable in parallel.

---

## Goal #35 — Integrate FWB (SD flash) onto `wican-pro` + re-bench

**Objective:** Land the proven SD-staged flash (FWB) on top of the new
datalogger baseline (`wican-pro`), resolve the merge landmines deliberately, and
**re-prove byte-perfect flash on the integrated base** — the byte-perfect result
was measured on FWB *alone*, and Step 3 edits files the flash depends on.

**Tasks:**
1. Branch off `wican-pro`; rebase/merge FWB's flash work in (`ncflash_fastread.c`,
   `ncflash_fastwrite.c`, `/upload/sd` endpoint, main.c dispatch).
2. Resolve the four **Step-3 merge landmines** (plan §4 "Step-3 merge landmines"):
   - **`vehicle.h`** — take FWD's `engine_on_volt` (hysteresis), NOT FWB's
     `voltage_at_ignition` (false "engine running" at 12.6–12.8 V parked). HARD
     compile error if mixed.
   - **`config_server.c`** — take FWD's `max_uri_handlers = 48` (FWB left it at 38
     and never bumped it for `/upload/sd` → can 404 the `/*` file server). ⚠️
     Worth verifying on deployed FWB regardless.
   - **`config_server.h` enum** — take FWD's (adds `FAST_LOG=5`, `POLL_LOG=6`).
   - **`CMakeLists.txt`** — take FWD's requires-superset (`csv_logger`,
     `fast_log`, `sd_filemgr`), then append FWB's fastwrite/upload components.
   - **`main.c`** — non-overlapping; keep FWB's fastread/fastwrite dispatch AND
     FWD's FAST_LOG/POLL_LOG blocks.
3. Build clean on the integrated branch (ESP-IDF v5.5.3; see notes).

**Done-gate (plan §7 "Step 3"), hardware-required:**
- [ ] Byte-perfect **full flash** on the integrated firmware == source ROM.
- [ ] Full ROM read byte-perfect; DTC read/clear; RAM scan all still pass.

**Depends on:** #34 (done). **Blocks:** #36 (coexistence builds on this base).

---

## Goal #36 — Build no-reboot SLCAN coexistence (firmware)

**Objective:** On the integrated branch, add an **always-on dedicated SLCAN TCP
port** plus the **single-CAN mutual-exclusion interlock** so flash and datalog
share one TWAI controller without bricking. **This is its own PR** — never smuggle
brick-critical CAN arbitration into the two-feature merge (#35).

**Tasks (plan §3 firmware-REQUIRED + §5):**
1. `types.h` — add `DEV_SLCAN_PORT` to `dev_channel_t` (~5 LOC).
2. `comm_server.c` — 2nd TCP listener on **port 35001**; tag frames
   `dev_channel = DEV_SLCAN_PORT`; queue to existing `xMsg_Rx_Queue` (~40 LOC).
3. `main.c can_tx_task` (~:287) — early dispatch branch **before** the
   `protocol == SLCAN` check: if `dev_channel == DEV_SLCAN_PORT`, route to
   fastread/fastwrite/`slcan_parse_str` regardless of persisted protocol (~35).
4. `main.c can_rx_task` (~:435) — **RX reply forwarding must run independent of
   `protocol`** (today gated `if(protocol==SLCAN)`). ⚠️ **This is the #476
   "opens clean but no frames flow" trap — the single most-missed detail** (~15).
5. **`FLASH_ACTIVE_BIT` interlock (load-bearing, brick-critical — plan §5):**
   - `can.c` — add `FLASH_ACTIVE_BIT` to the CAN event group + set/clear/is_set.
   - `ncflash_fastwrite.c` + `ncflash_fastread.c` — `set` before suspending
     `can_rx_task`; `clear` in the clean-teardown label **before** `vTaskResume`,
     on **every** exit path (success / host-gone / abort / socket close).
   - `poll_log.c::polllog_rx_task` — if `FLASH_ACTIVE_BIT` set, **short-sleep and
     skip** (NEVER `portMAX_DELAY` — would trip the WDT while flash holds the bit).
   - Unify `s_fastop_busy` / `s_fwbusy` so fast-read and fast-write also exclude
     each other.
   - **Invariant:** at most one TX producer owns the bus at any instant, on every
     exit path.

**Open design decisions to confirm before building (plan §6):** RPM==0 gate
strictness (leaning hard-block + off-by-default override); datalog silencing
(leaning park-and-hold flag); engine-run detect (voltage-to-wake + RPM-to-confirm
hybrid — **needs user decision**); port 35001 (confirm no binding conflict).

**Done-gate (plan §7 "Step 4"), hardware-required:**
- [ ] Flash with `poll_log` enabled → **zero CSV rows** and **zero `0x7E0` poll
      frames** for the whole flash (interlock proof).
- [ ] WiFi drop at ~50 % of a flash → **next** flash recovers **without a device
      reboot** (clean-teardown + bit-clear proof).
- [ ] Dedicated port forwards ECU replies **while in `poll_log` mode** (no #476
      RX-trap): connect → `O` → fastread returns bytes, no protocol switch.
- [ ] Concurrent fast-read + fast-write → the second is **refused**, not run.

**Depends on:** #35 (integrated base). **Pairs with:** #37 (host drives this).

---

## Goal #37 — Host: capability-detect dedicated port + RPM gate at flash boundary

**Objective:** Teach the host to detect the coexist-capable firmware and use the
dedicated port instead of the reboot dance, and **enforce engine-off in code**,
not just in the UI. **Non-breaking and firmware-agnostic** — works against both
old reboot-firmware and new coexist-firmware — so it can land **any time, in
parallel** with #35/#36.

**Tasks (plan §3 host-REQUIRED):**
1. `src/ecu/session.py::_connect_wican()` — call `transport.version_ping()` first
   (`wican_transport.py:828`); coexist build → skip `WiCANConfigurator`, connect
   to the dedicated port. **Keep `WiCANConfigurator` 100 % intact as the fallback**
   for stock/old firmware (`version_ping() == None`).
2. `src/ecu/flash_manager.py` / `session.py` — add `enforce_rpm_gate()`: one-shot
   `read_engine_rpm()` (PID 0x0C, `protocol.py:277`) **before** `session.acquire()`.
   Move the gate out of `ui/ecu_window.py:514` into the flash boundary. **Critical
   ordering:** the RPM read MUST precede `diagnostic_session(0x85)` — once in
   session, OBD Mode-01 returns NRC 0x11 and RPM is unreadable (`protocol.py:294`).
3. `src/ecu/wican_sd_flash.py::_trigger_firmware_flash` — command datalog to
   quiesce + confirm, settle ~150–200 ms before entering the programming session.

**Nice-to-have:** extend the `%TEMP%` crash-recovery sidecar
(`wican_config.py:154`) to record "datalog stopped" for restart-prompt on a
mid-flash crash. Default acceptable: manual restart via web UI.

**Done-gate:**
- [ ] Against coexist firmware: connect takes the dedicated-port path, **no
      reboot**; against old firmware: falls back to `WiCANConfigurator` unchanged.
- [ ] Flash refused in code when RPM ≥ 1.0 (override checkbox off by default);
      unit tests for `enforce_rpm_gate()` ordering vs session entry.
- [ ] No regression in the existing J2534 flash path (transport-agnostic seam).

**Depends on:** nothing hard (capability-detect degrades gracefully). Best
**validated** end-to-end once #36 firmware exists, but can be **built + unit-tested
now** against `FakeTransport`.

---

## Sequencing & dependency graph

```
#34 (FWD→wican-pro) ✅
        │
        ▼
#35 (FWB onto wican-pro + re-bench)  ──►  #36 (coexistence firmware PR)
                                                  │
        #37 (host capability + RPM gate) ◄────────┘  (pairs for E2E)
        └─ buildable + unit-testable in parallel, any time ─┘
```

Rule of thumb (plan §4): never integrate the two firmware features until each is
independently finished and validated; build the coexistence **last**, as its own
change, on the integrated base. The reboot-switcharoo **works today** and is an
acceptable stopgap until #36 lands.

---

## Brick-critical guardrails (non-negotiable)

- **Re-bench after every integration.** Byte-perfect proofs are only valid on the
  exact firmware that produced them; #35 changes flash-adjacent files → re-prove.
- **`FLASH_ACTIVE_BIT` must be hardware-validated** with the interlock bench
  checklist — a missed exit-path clear is a soft-brick waiting to happen.
- **RPM==0 before session entry**, enforced in flash code, override off by default.
- **Single TX owner invariant** holds on *every* exit path (success/timeout/abort/
  socket-close), or the merge is not done.
- Hardware steps run on a **recoverable bench ECU**, never a vehicle's installed
  unit, for first attempts.

---

## Re-entry prompt (after a break)

> Resume the WiCAN firmware integration. Read
> `.claude/plans/wican-firmware-integration-goal.md` and
> `docs/internal/WICAN_SLCAN_COEXISTENCE_PLAN.md`. #34 (FWD→wican-pro) is done.
> Start at **#35** (integrate FWB onto wican-pro + re-bench) — or tell me which.

The project memory pointer `project_wican_slcan_coexistence` also auto-surfaces
this context.
