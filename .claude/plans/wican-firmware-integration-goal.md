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
adapter stays in datalogging mode, the host issues **one REST `pause` call** that
stops SD logging *and* parks the CAN poller, the flash streams over a **dedicated
always-on SLCAN port**, then the host issues **`resume`** — with zero ~6 s reboot
and zero chance of a stray poll frame corrupting the UDS session. The in-firmware
`FLASH_ACTIVE_BIT` interlock remains the **brick-critical local guarantee** behind
the best-effort REST layer.

---

## Current state (verified 2026-06-26)

**Firmware fork** `../nc-flash-wican-fw` (fork of `meatpiHQ/wican-fw`; "main" =
`wican-pro`). Branches that matter:

| Branch | Role | State (verified) |
|---|---|---|
| `wican-pro` | integration target | Carries FWD (datalogger: `poll_log`, `csv_logger`, `fast_log`). |
| `claude/integrate-fwb-onto-wican-pro` (**#35**) | FWB (SD flash) merged onto wican-pro | @ `c8bcd54`. **OTA'd + HARDWARE-VALIDATED (see #35 below). ✅** |
| `claude/coexistence-slcan-port` (**#36**) | no-reboot coexistence (off #35) | @ `6bea7e3` + **uncommitted working tree**: 36.A dedicated port, 36.C `/datalog`, AUTO_PID/mqtt/can_rx_task brick-fixes all **BUILT + COMPILE CLEAN** (ESP-IDF 5.5.3 esp32s3, 0 warn/err, 30% partition free). **Pending: commit + HARDWARE validation.** |

**Host repo** `nc-rom-editor`: WiCAN transport, fast-read, host-side fast-write
glue, discovery, and the `WiCANConfigurator` reboot-switch stopgap are all
landed on `master`. **#37 is on branch `feature/wican-host-rpm-gate-coexist`,
PR #79 open** (RPM gate in code + dedicated-port capability probe; 1453 tests
green).

**Sequence (plan §4):** Step 0–2 ✅, **Step 3 = #35 ✅ (done 2026-06-26)**. Next:
**Step 4 = #36** (re-oriented — see below), with the **host track = #37** in PR.

---

## 🔑 Conclusion from the REST-coexistence investigation (2026-06-26, ultracode workflow `w2zymuhjg`)

The user proposed: *"on flash-tool connect, REST-stop datalogging; flash; on
disconnect, REST-restore it."* A 6-agent investigation across both repos +
adversarial critique concluded:

1. **REST cannot replace the dedicated port — it rides on it.** The firmware runs
   **one mode per boot**; there is **no dedicated SLCAN port** (the `DEV_SLCAN_PORT`
   enum exists with **zero consumers**), and the flash codecs are trapped behind
   `if(protocol==SLCAN)` (`main.c:291`). In `poll_log` mode, inbound TCP frames are
   tagged `DEV_WIFI` and **never dispatched** to fastread/fastwrite. ⇒ Stopping the
   datalogger does **not** make the flash path reachable. **The dedicated always-on
   port (#36 core) is the load-bearing prerequisite.**
2. **No endpoint stops the CAN poller.** `POST /csv_logger?op=start|stop` only
   toggles **SD persistence** (`csv_logger.c:938`), not the bus. The poll task is
   created once at boot, never suspended. The only runtime way to make the poller
   yield the bus is the existing **`FLASH_ACTIVE_BIT`** the poll task already parks
   on (`poll_log.c:415-428`) — but nothing exposes it over HTTP today.
3. **Adopt REST as the coordination layer via a new minimal endpoint**
   `POST /datalog?op=pause|resume` that drives **both** `csv_logger_set_manual_override()`
   **and** `can_flash_active_set()/clear()`. ~25 LOC, reuses two existing public funcs.
4. **KEEP `FLASH_ACTIVE_BIT` as the brick-critical guarantee.** REST is best-effort
   over WiFi (can be lost, late, race the flash, or skipped by an old/3rd-party
   client). The flash codec's own synchronous `set`/`clear` on **every exit path**
   stays load-bearing; REST `pause` is an *additional, earlier, advisory* set.
5. **Crash-safety reuses the existing `%TEMP%` recovery sidecar** (write
   "datalog_stopped" before pause, clear after confirmed resume, reconcile on next
   connect) — backed by firmware self-heal (poll auto-resumes when the bit clears;
   SD mode resets to AUTO on reboot).

**Adversarial critique verdict: "sound-with-fixes."** The must-fixes below are
folded into #36/#37 and are **brick-critical, not polish.**

---

## Goal #35 — Integrate FWB (SD flash) onto `wican-pro` + re-bench  ✅ COMPLETE (2026-06-26)

**Branch `claude/integrate-fwb-onto-wican-pro` @ `c8bcd54`, OTA'd to the bench adapter.**

- Merge done; all **5 semantic landmines** verified correct (engine_on_volt;
  `max_uri_handlers=48`; `FAST_LOG=5`/`POLL_LOG=6`; CMake superset; both `main.c`
  dispatch blocks + `/upload/sd`). Built clean (ESP-IDF v5.5.3, esp32s3).
- **Hardware-validated end-to-end on the bench ECU (192.168.1.169):**
  - NCFRv5 ping; DTC read (10 codes) + **DTC clear** over WiCAN; RAM scan + auth seed/key.
  - **Live full flash 1022/1022 `NCFWDONE`** (round-trip of the ECU's own ROM).
  - Power cycle → **app booted** (authenticated RMBA works; real powertrain DTCs).
  - ✅ **Read-back byte-compare PASS** — full 1 MB read == `wican_roundtrip_source.bin`,
    byte-for-byte identical (`wican_readback_postcycle.bin`; ROM ID `SW-LFDJEA000.HEX`;
    29 dropped blocks all recovered).

**Done-gate — both met. Task #35 → completed.**

> **Op gotcha learned here (memory `project_wican_protocol_revert_gotcha`):** after a
> reboot the WiCAN reverts to `poll_log`, NOT slcan. Raw bench tools **without
> `--auto-config`** connect to :35000, ack `C/S6/O`, but the device doesn't bridge
> CAN → fake "bricked ECU." Always use `--auto-config` (or NCFlash). One driver at a
> time — a 2nd client's protocol-switch reboot desyncs the other's ISO-TP stream.

---

## Goal #36 — No-reboot coexistence: dedicated port + REST `/datalog` + interlock (firmware)

**Re-oriented 2026-06-26** to the host-REST coordination model. **This is its own PR** —
never smuggle brick-critical CAN arbitration into the #35 merge.

**Objective:** On the integrated branch, deliver (a) the **always-on dedicated SLCAN
port** so the host can flash while `poll_log` firmware is loaded with **no reboot**,
(b) a new **`POST /datalog?op=pause|resume`** endpoint that stops SD logging *and*
parks the poller, and (c) keep the **`FLASH_ACTIVE_BIT` interlock** as the
single-CAN-owner guarantee.

### 36.A — Dedicated SLCAN port (load-bearing prerequisite — plan §3 + PART_C §2b)  ✅ BUILT (compiles clean; HW-pending)
1. `types.h` — `DEV_SLCAN_PORT` in `dev_channel_t` ✅ (already on branch).
2. **NEW `main/slcan_port.c` + `.h`** ✅ — dedicated always-on listener on **35001**
   (NOT in comm_server.c: that's a single-conn singleton; a self-contained server shares
   zero state with the proven datalogger path). Tags `DEV_SLCAN_PORT`, shares
   `xMsg_Rx_Queue`, drains a **private** `xMsg_SlcanPort_Tx_Queue` to its own socket.
   Hardened per audit: **connection-generation guard** (reconnect TOCTOU) + **bind retry**
   (boot netif self-heal). Added to `main/CMakeLists.txt`.
3. `main.c can_tx_task` ✅ — early `DEV_SLCAN_PORT` branch **before** the `protocol==SLCAN`
   gate → fastread/fastwrite/`slcan_parse_str` to the private queue, `continue`. Inline on
   can_tx_task (this serializes REALDASH/SAVVYCAN/SLCAN/ELM327 out of the flash window).
4. **Plain-slcan async reply (the #476 trap)**: DEFERRED on purpose. The flash path uses
   only **self-contained** codecs (version-ping/fastread/fastwrite drain their own TWAII),
   which is exactly the done-gate. Un-gating `can_rx_task` for plain-slcan reply-forward in
   poll_log = TWO TWAI consumers stealing each other's ISO-TP frames = brick. Defer to a
   future FLASH_ACTIVE-guarded request/response codec.
5. `ncflash_fastread.h` ✅ — `NCFRv5 → NCFRv6` (host `COEXIST_MIN_FW_REV=6` detect). Marker
   means ONLY "dedicated port exists"; `/datalog` is a SEPARATE soft-probed capability.

### 36.B — `FLASH_ACTIVE_BIT` interlock (brick-critical — plan §5)  ✅ core done on branch
- `can.c` — `FLASH_ACTIVE_BIT=BIT1` + NULL-safe `can_flash_active_set/clear/active()` ✅
- `poll_log.c::polllog_rx_task` — park (20 ms sleep + skip, never `portMAX_DELAY`) ✅
- `ncflash_fastwrite.c` + `ncflash_fastread.c` — `set` before suspend, `clear` last in
  clean-teardown on **every** exit path; unified `s_fwbusy`/`s_fastop_busy` exclusion ✅
- **Invariant:** at most one TX producer owns the bus at any instant, every exit path.

### 36.C — New REST endpoint `POST/GET /datalog?op=pause|resume`  ✅ BUILT (compiles clean; HW-pending)
- `csv_logger.c` ✅ — `datalog_control_handler` + `datalog_status_handler` (modelled on
  `csv_control_handler`). **CHANGED from the original plan per the audit:** drives a
  **separate `DATALOG_PARK_BIT`**, NOT `can_flash_active_set/clear`. Reusing the codec-owned
  `FLASH_ACTIVE_BIT` from REST was a last-writer-wins **brick trap** (a stray/duplicate
  `resume` could un-park a LIVE flash). Now: `pause` → `set_manual_override(false)` then
  `can_datalog_park_set()`; `resume` → `can_datalog_park_clear()` then **restore the exact
  pre-pause mode** (snapshotted on first pause; AUTO/ON/OFF — won't flip an AUTO device to
  force-on). Idempotent. Echo `{ok, flash_active, datalog_parked, manual_mode}`.
- `can.c/.h` ✅ — `DATALOG_PARK_BIT (BIT2)` + `can_datalog_park_set/clear/active()` +
  `can_should_park()` (FLASH_ACTIVE_BIT | DATALOG_PARK_BIT). poll_log + autopid park on
  `can_should_park()`; `FLASH_ACTIVE_BIT` alone stays the brick-safety guarantee.
- `config_server.c` ✅ — registered both URIs. `max_uri_handlers=48`, ~41 live → fits (+2).
- `GET /datalog` ✅ — live `{flash_active, datalog_parked, manual_mode}` so the host can
  *verify* quiesce/resume (audit: "HTTP 200 ≠ parked" — the codec's synchronous set is the
  real guard; REST pause is advisory).

### 🔴 MUST-FIX (from adversarial critique — brick-class)  ✅ ALL DONE (compiles clean; HW-pending)
- [x] **AUTO_PID coverage.** ✅ `autopid_task` now parks on `can_should_park()` at loop-top
      (`autopid.c`, mirrors poll_log). **Option B (refuse on `protocol==AUTO_PID`) REJECTED**
      by the audit: the host flashes over 35001 with NO protocol switch, so `protocol` STAYS
      `AUTO_PID` during a coexist flash → a refusal would break the exact flow #36 enables.
      Verifier note: on WICAN_PRO autopid drives a *separate STN CAN controller*, so the bit
      is the firmware's only lever — refusal wouldn't even stop its TX.
- [x] **Other `can_send` TX producers.** ✅ `mqtt.c:302` (out-of-band event-loop task) now
      guarded with `can_flash_active()`. REALDASH/SAVVYCAN/SLCAN/ELM327 are serialized by the
      inline can_tx_task dispatch (documented). `can_send` itself carries a contract comment
      listing every producer + its guard. `can_rx_task` now parks on `can_flash_active()` too
      (stops it stealing the codec's reply frames in auto_pid/realdash).
- [x] **"HTTP 200 ≠ parked."** ✅ Documented in the handler + `GET /datalog` added so the host
      confirms rather than assumes; the codec's synchronous `FLASH_ACTIVE_BIT` is the guarantor.

**Done-gate (plan §7 "Step 4"), hardware-required:**
- [ ] Dedicated port forwards ECU replies **while in `poll_log` mode** (no #476 trap):
      connect :35001 → `O` → fastread returns bytes, **no protocol switch / reboot**.
- [ ] Flash with `poll_log` enabled → **zero CSV rows** and **zero `0x7E0` poll frames**
      for the whole flash (interlock proof).
- [ ] `POST /datalog?op=pause` → `{flash_active:true, manual_mode:off}`; `resume` →
      `{flash_active:false, manual_mode:on}`; idempotent on repeat.
- [ ] **AUTO_PID:** flash either parks the autopid poller OR is refused with `protocol==AUTO_PID`.
- [ ] WiFi drop at ~50 % of a flash → **next** flash recovers **without a device reboot**.
- [ ] Concurrent fast-read + fast-write → the second is **refused**, not run.

**Depends on:** #35 (done). **Pairs with:** #37 (host drives this).

---

## Goal #37 — Host: capability-detect dedicated port + RPM gate + REST datalog client

**PR #79 open** (`feature/wican-host-rpm-gate-coexist`). Capability probe + RPM gate
shipped; the **datalog-quiesce wiring is re-scoped** to the REST `/datalog` endpoint.

**Done / shipped:**
1. `session.py::_connect_wican` — `version_ping()`-first; coexist build → dedicated-port
   path; `WiCANConfigurator` intact as fallback. ✅
2. `flash_manager.py::enforce_rpm_gate()` — one-shot PID 0x0C **before**
   `diagnostic_session(0x85)` (OBD Mode-01 → NRC 0x11 in-session); hard-block RPM ≥ 1.0,
   override off by default; unreadable RPM doesn't block. ✅ (1453 tests green)
3. `wican_sd_flash.py::_trigger_firmware_flash` — `PRE_SESSION_SETTLE_S` settle. ✅

**New for the REST model — ✅ BUILT + UNIT-TESTED (uncommitted host working tree):**
- [x] `wican_config.py` — **`WiCANDatalogClient`** (stdlib-only): `pause()`/`resume()`
      (POST `/datalog`) + `get_state()` (GET). Returns the parsed JSON echo or `None`. ✅
- [x] **🔴 Standalone resume path.** ✅ Resume lives in `WiCANSdFlasher._trigger_firmware_flash`'s
      `try/finally` (runs on success AND on a mid-transfer `FWERR`), keyed on the transport
      host — NOT folded into `_restore_wican_protocol` (which no-ops on the coexist path).
- [x] **Quiesce at the flash boundary, not every connect.** ✅ `pause()` fires in
      `_trigger_firmware_flash` only; plain read/DTC connects never pause logging.
- [x] **Crash-safety sidecar.** ✅ `%TEMP%/wican_datalog_<host>.json` breadcrumb written
      BEFORE `pause`, cleared after a confirmed `resume`; `reconcile()` runs at the top of
      `_connect_wican` on every WiCAN connect.
- [x] **🔴 Two-instance guard.** ✅ `reconcile()` checks `GET /datalog` `flash_active` and
      **skips resume while a flash is active** (the owning instance clears the breadcrumb);
      a separate datalog sidecar file (not the protocol one) avoids the active-flash hazard.
- [x] **🔴 Decouple `/datalog` from the rev gate.** ✅ Every `/datalog` call soft-degrades to
      `None` on 404/timeout/unreachable/non-JSON — a port-only `NCFRv6` build NEVER aborts a
      flash; the firmware `FLASH_ACTIVE_BIT` interlock remains the guard.
- [ ] Optional (not done): wire resume into `MainWindow.closeEvent`. The next-connect
      `reconcile()` is load-bearing and covers the crash/close path, so this stays optional.

Tests: `tests/test_ecu_wican_config.py` (+10: round-trips, 404/500/garbage/unreachable
soft-degrade, breadcrumb-before-request, reconcile resume-when-idle / skip-when-flash-active),
`tests/test_ecu_wican_sd_flash.py::TestDatalogCoexistence` (+3: pause→auth→flash→resume order,
resume-runs-on-FWERR, pause-failure-never-aborts). black + flake8(E9,F63,F7,F82) clean.

**Done-gate:**
- [ ] Against coexist firmware: connect → dedicated-port path, **no reboot**; against old
      firmware: `WiCANConfigurator` fallback unchanged; against port-only firmware:
      `/datalog` 404 degrades gracefully.
- [ ] `pause`→flash→`resume` round-trips; CSV resumes after; crash mid-flash → next
      connect reconciles + resumes; 2nd instance does NOT resume mid-flash.
- [ ] No regression in the J2534 flash path.

---

## Sequencing & dependency graph

```
#34 (FWD→wican-pro) ✅
        │
        ▼
#35 (FWB onto wican-pro + re-bench) ✅
        │
        ▼
#36 (coexistence firmware PR)            #37 (host: capability + RPM gate) — PR #79 ✅ base
   36.A dedicated port  ◄── load-bearing      └─ REST datalog client + standalone resume
   36.B interlock ✅core                          + sidecar + two-instance guard  ◄─ pairs
   36.C /datalog endpoint + GET state            with 36.C for E2E
   🔴 AUTO_PID coverage (brick-class)
```

**Build order (workflow `build_steps`):** ~~36.A dedicated port~~ ✅ → ~~36.C `/datalog`~~ ✅
→ ~~AUTO_PID/mqtt/can_rx_task brick-fixes~~ ✅ → **all BUILT + compile clean (2026-06-26).**
Remaining: **(1) commit** the `claude/coexistence-slcan-port` working tree → **(2) live bench
E2E** (user-gated, brick-risk, needs deployed-build backup first): dedicated-port fastread in
poll_log w/ no reboot; flash w/ poll_log enabled → zero CSV rows / zero `0x7E0`; `/datalog`
pause/resume round-trip; AUTO_PID flash parks the poller; WiFi-drop recovery → **(3) host REST
datalog client** (#37 follow-ups: `pause/resume_datalog`, standalone resume path, sidecar,
two-instance guard, `/datalog` 404 soft-degrade).

---

## Brick-critical guardrails (non-negotiable)

- **Re-bench after every integration.** Byte-perfect proofs are only valid on the exact
  firmware that produced them.
- **`FLASH_ACTIVE_BIT` is the load-bearing guarantee, not the REST call.** REST `pause`
  is advisory; the codec's synchronous set/clear on **every** exit path is the brick guard.
- **AUTO_PID must be covered** (park on the bit OR refuse the flash) — today it is an
  unguarded brick path.
- **RPM==0 before session entry**, enforced in flash code, override off by default.
- **Single TX owner invariant** holds on *every* exit path; audit *every* `can_send`
  reachable during a flash, not just the poll task.
- **Datalogging is never silently left off forever** — host sidecar reconcile + firmware
  self-heal (poll auto-resumes; SD → AUTO on reboot). (SD persistence may stay off until
  WiFi returns / reboot; the CAN bus is always freed.)
- Hardware steps run on a **recoverable bench ECU**, never a vehicle's installed unit.

---

## Re-entry prompt (after a break)

> Resume the WiCAN firmware integration. Read
> `.claude/plans/wican-firmware-integration-goal.md` and
> `docs/internal/WICAN_SLCAN_COEXISTENCE_PLAN.md`. #34 + **#35 are done** (FWB
> integrated, flash byte-perfect-validated). **#37 is in PR #79.** Start **#36**:
> build 36.A (dedicated SLCAN port :35001 + no-reboot dispatch) first, then 36.C
> (`POST /datalog?op=pause|resume`), keeping the `FLASH_ACTIVE_BIT` interlock.
> **Resolve AUTO_PID coverage as a hard gate before any live flash.** Then wire the
> host REST datalog client (standalone resume path, sidecar, two-instance guard).

The project memory pointers `project_wican_slcan_coexistence` and
`project_wican_protocol_revert_gotcha` also auto-surface this context.

> **Note:** the durable design doc `docs/internal/WICAN_SLCAN_COEXISTENCE_PLAN.md`
> predates the REST re-orientation — fold the `/datalog` endpoint, the AUTO_PID
> must-fix, and the two-instance sidecar hazard into it when #36 lands.
