# WiCAN Dead-Man's-Switch — Brick-Safe Datalog Auto-Resume

**Status:** IMPLEMENTED + device-validated (2026-06-26). Host half + firmware core built;
firmware OTA'd to the bench WiCAN (192.168.1.169) and the reaper auto-resume confirmed on
hardware (host vanish → lease expiry → bus idle → auto-resume at claim-TTL+grace ≈ 78 s, zero
ECU contact). Brick fix below is triple-lens adversarially re-verified clean. Remaining: the
brick-risk live-ECU coexist flash (HW-1 mid-auth kill + HW-9 normal), user-gated.
**Reference before:** touching datalog pause/resume, the `/datalog` endpoint, the
`FLASH_ACTIVE_BIT` interlock, or the host flash auth window.
**Companion docs:** `WICAN_SLCAN_COEXISTENCE_PLAN.md`, `WICAN_PART_C_FINDINGS.md`.

> **IMPLEMENTATION NOTE (supersedes parts of §3–§4 below).** The host-owned park + claim
> state are NOT FreeRTOS event-group bits. A first adversarial pass found a **brick-class
> TOCTOU**: the reaper snapshotted the lease under the spinlock, released it, then force-cleared
> the bit **unconditionally**, so a fresh `bus_claim`/`pause` re-armed in the gap was destroyed,
> un-parking the poller into a live auth session. The fix moved park + claim ENTIRELY under the
> `s_park_mux` spinlock as `{volatile bool flag + token + owner-gen + u64 deadline}` (only
> `FLASH_ACTIVE_BIT` stays a codec-owned event-group bit, INV-1 intact), and made the reaper a
> **token+deadline-matched compare-and-act** (`can_host_bus_claim_reap` / `can_park_lease_reap`):
> the clear happens only if the lease is STILL the exact `(token,deadline)` the reaper sampled,
> so any arm/renew in the gap bumps the token/deadline and ABORTS the reap. `can_should_park()`
> ORs the lock-free flag reads with `FLASH_ACTIVE_BIT`. Read the firmware as the source of truth;
> the bit names (`DATALOG_PARK_BIT`/`HOST_BUS_CLAIM_BIT`) in §1–§4 are historical.

> **Why this exists.** With a Tactrix OpenPort, "flash mode" only exists while the
> USB cable is physically plugged in — yank the cable and you're back to a normal
> bus. We are wireless, so there is no cable. If NC-Flash pauses the datalogger to
> flash and then **vanishes** (laptop lid closed, host crash, Wi-Fi drop), today the
> WiCAN stays parked in flash mode **forever** (until reboot). This doc designs the
> wireless analog of "cable unplugged → auto-recover" **without** ever resuming the
> datalogger on top of a live ECU write (which soft-bricks the ECU).

---

## TL;DR

1. **Today there is NO auto-resume.** No window-close handler, no heartbeat, no
   firmware timer clears `DATALOG_PARK_BIT`. A host that pauses and disappears
   leaves the WiCAN parked until the **next** NC-Flash launch runs `reconcile()`,
   or until a power cycle.
2. **The naive fix bricks.** "Auto-resume when no flash is active" is wrong,
   because the brick fence (`FLASH_ACTIVE_BIT`) only covers the firmware
   `fast_write` codec — but the host drives a full **UDS programming session
   (tester-present → `0x10` → `0x27` security → flash-counter) BEFORE the codec
   ever sets that bit.** Auto-resuming during that auth window injects a `0x7E0`
   poll frame into a live security-unlocked session → ISO-TP/UDS corruption →
   soft-brick (survives power cycle, needs reflash). All three candidate designs
   were independently broken on this exact window by every adversarial lens.
3. **The fix** is one new host-asserted bit — `HOST_BUS_CLAIM_BIT` — that brackets
   the **entire** host-driven bus-owning window, folded into `can_should_park()`
   and a firmware **dead-man reaper** that resumes datalog only when the bus is
   provably unowned, the host is provably gone, and the bus is provably idle.
4. **Separately blocking #36 today:** the dedicated-port **RX-forward path is
   missing** — see [§6](#6-prerequisite-bug-the-rx-forward-half-of-36-is-missing).
   Both fixes ship in one firmware build + OTA cycle.

---

## 1. Current state (ground truth from the firmware/host read)

### The two interlock bits
| Bit | Owner | Set by | Cleared by | Auto-cleared? |
|---|---|---|---|---|
| `FLASH_ACTIVE_BIT` (BIT1) | **Codec** (brick guarantee) | `can_flash_active_set()` at `ncflash_fastread.c:266` / `ncflash_fastwrite.c:349`, **before** suspending `can_rx_task` | codec `cleanup:` label, `fastread.c:401` / `fastwrite.c:531`, **last** on every `goto cleanup` exit | **No.** No timer/watchdog. Held only for the synchronous codec run on `can_tx_task`. |
| `DATALOG_PARK_BIT` (BIT2) | **REST** (advisory) | `can_datalog_park_set()` — one site, `csv_logger.c:1033` (POST `/datalog?op=pause`) | `can_datalog_park_clear()` — one site, `csv_logger.c:1039` (POST `op=resume`) | **No.** Only an explicit `op=resume`. Pause-and-vanish ⇒ stuck until reboot. |

`can_should_park()` today = `FLASH_ACTIVE_BIT | DATALOG_PARK_BIT`. `poll_log`
(`poll_log.c:425`) and `autopid` park on it. They are kept **separate** on purpose:
the REST layer must never write the codec's bit, or a stray/duplicate `resume`
from a reconnect or 2nd client could un-park a **live** flash (last-writer-wins
brick trap).

### What already self-heals
- **SD `fast_write` self-completes** with no host: it reads the staged image from
  `/sdcard/roms`, CRC32-gates it, drives `RequestDownload`/`TransferData`/
  `TransferExit`/`ECUReset` entirely from SD, sets+clears `FLASH_ACTIVE_BIT`
  itself, and emits `NCFWDONE`. A vanished host → bounded `xQueueSend`
  (`TX_QUEUE_SEND_TIMEOUT_MS = 2000`) times out into the clean teardown. So a host
  that dies **mid-flash** still releases the bus.
- **`FLASH_ACTIVE_BIT` recovery is structural, not a timer:** every codec error
  path is `goto cleanup`, and `cleanup:` always resumes `can_rx_task`, drains
  stale RX, read-clears TWAI alerts, and clears the bit (the Part-C #21
  single-clean-teardown).
- **35001 socket** has `SO_KEEPALIVE` (idle 5 s / intvl 5 s / cnt 3 ⇒ ~20 s
  half-open detection) + the `s_conn_gen` stale-recv guard.

### The asymmetry that bites
`fast_write` clears **only** `FLASH_ACTIVE_BIT` on `NCFWDONE`. It does **not**
clear a host-set `DATALOG_PARK_BIT` or restore the pre-pause manual mode. So if
the host did a REST pause around the flash (it does) and then vanished, the flash
finishing does **not** bring the datalogger back — that still needs an explicit
`op=resume` that will never arrive.

---

## 2. The brick trap (the headline finding)

`wican_sd_flash.py:_trigger_firmware_flash` drives this wire order:

```
pause()  (409)        → firmware sets DATALOG_PARK_BIT
sleep(PRE_SESSION_SETTLE_S = 0.2s)
_authenticate_ecu()   (422) → tester_present
                              → diagnostic_session(0x10 programming)
                              → security_access seed/key (0x27)
                              → check_flash_counter()
fast_write()          (443) → codec sets FLASH_ACTIVE_BIT  ← brick fence starts HERE
```

**During the whole `_authenticate_ecu()` window the ECU is in a live,
security-unlocked programming session over the single CAN bus, yet
`FLASH_ACTIVE_BIT` is CLEAR.** Any dead-man's-switch gated only on
`!flash_active` will un-park the poller and inject `0x7E0` into that session.

It gets worse: the auth handshake has `7F xx 78` "response pending" gaps up to
`TIMEOUT_RESPONSE_PENDING_MAX = 60000 ms` **per request**, plus a host-side
security-key compute pause — so the bus legitimately goes idle for >300 ms
mid-session, and a single legal auth request can outlast a 12–30 s TTL. A
bus-idle test or a short TTL is **not** sufficient to fence this window.

> `wican_config.py:reconcile()`'s docstring (lines 586–587) literally encodes the
> **false** belief that "the pre-flash pause window is harmless to resume into."
> It is not. That docstring must be corrected.

---

## 3. Recommended design — host bus-claim + firmware dead-man reaper

**Primary mechanism:** a single host-asserted **`HOST_BUS_CLAIM_BIT` (BIT3)** that
the host raises **before** the auth handshake and holds until the codec's
`FLASH_ACTIVE_BIT` takes over (and through to flash end), **plus** a firmware
**dead-man reaper** task that auto-resumes the datalogger **only** when the bus is
unowned, the host is gone, and the bus is idle.

- **"Host is gone"** = the dedicated **35001 TCP connection is dead** (FIN/RST
  sub-second on a clean crash; ~20 s `SO_KEEPALIVE` on a half-open lid-close)
  **OR** a renewable **TTL lease** expired (the backstop for half-open sockets).
- **"Bus is owned"** = `FLASH_ACTIVE_BIT | HOST_BUS_CLAIM_BIT` — the claim bit
  covers exactly the auth window the codec bit misses. The reaper's hard gate is
  `!can_flash_active() && !host_bus_claim_active()`, **bits, not time budgets**, so
  no legitimate multi-second auth can expire out from under the host.
- The live 35001 socket is the wireless analog of "cable plugged in"; the TTL
  lease only governs **host-gone detection**, never the bus-owned interval.

### Invariants (the brick-safety guarantees)
- **INV-1 — single brick guarantee untouched:** `FLASH_ACTIVE_BIT` is set/cleared
  ONLY at the four existing codec sites. This design adds **zero** new writers of
  BIT1. CI-asserted.
- **INV-2 — extended fence covers auth:** host holds `HOST_BUS_CLAIM_BIT` for the
  entire host-driven interval (armed before `_authenticate_ecu()`, released only
  after `fast_write()` returns / in `finally`). BIT1 and BIT3 overlap ⇒ **no
  instant in `[auth-start .. flash-end]` where the bus is owned but neither bit is
  set.**
- **INV-3 — triple OR-interlock:** `can_should_park() = FLASH_ACTIVE_BIT |
  DATALOG_PARK_BIT | HOST_BUS_CLAIM_BIT`. Even if a resume cleared BIT2, BIT1 or
  BIT3 independently keeps every producer parked.
- **INV-4 — reaper can't resume into an owned bus:** resumes ONLY when ALL hold:
  park-active ∧ `!flash_active` ∧ `!host_bus_claim` ∧ host-gone (lease-expired ∧
  owner-socket-gone) ∧ bus-idle ≥ `BUS_IDLE_QUIESCE` ∧ `!s_fwbusy` ∧
  `!s_fastop_busy`. Evaluated fresh each tick under a spinlock.
- **INV-5 — resume touches only the advisory bit:** both REST `op=resume` and the
  reaper call one factored `datalog_resume_locked()` that clears ONLY BIT2 and
  restores the snapshotted pre-pause manual mode. Never writes BIT1/BIT3.
- **INV-6 — claim is a bit, not a TTL:** the `!host_bus_claim_active()` gate is a
  hard bit, so a slow `7F..78`-pending auth / slow key compute / long session can
  never trigger a resume. The TTL only detects a **gone** host.
- **INV-7 — bounded liveness:** every blocking call inside the codecs is
  deadline-bounded ⇒ `FLASH_ACTIVE_BIT` always reaches its clear. CI/lint fails any
  new `portMAX_DELAY` / un-deadlined loop / un-budgeted `f_read` in a file holding
  BIT1. **No BIT1 auto-clear watchdog** (auto-clearing the brick bit is itself
  brick-unsafe) — a stuck flash raises a "power-cycle required" ALARM instead.
- **INV-8 — claim can't wedge forever:** the claim carries its own renewable lease;
  if the host vanishes mid-auth the lease expires and the reaper force-clears BIT3
  — but ONLY after the bus is idle AND a teardown grace elapsed, and ONLY by
  resuming the datalogger, never by touching BIT1. The host MUST send a UDS
  default-session/`ECUReset` teardown in its `finally` so a mid-auth vanish leaves
  no live programming session.
- **INV-9 — soft-degrading, never aborts a flash:** every `/datalog` + bus-claim
  REST call is failure-tolerant (404/405/timeout/unreachable → `None`, swallowed).
  A port-only build with no `/datalog` falls back to the `FLASH_ACTIVE_BIT`
  interlock; there is no coexisting datalogger to un-park there.

### Timing contract (shared host ⇄ firmware, in `constants.py`; firmware `*_US = *_S × 1e6`)
| Constant | Value | Why |
|---|---|---|
| `PARK_LEASE_TTL` | 12 s | host-gone backstop for the advisory park |
| `HOST_CLAIM_LEASE_TTL` | **75 s** | **must exceed** worst-case auth: `TIMEOUT_RESPONSE_PENDING_MAX` 60 s + settle + key-compute + margin. 30 s is provably too small. |
| `DATALOG_KEEPALIVE_INTERVAL` | 4 s | ⅓ of park TTL ⇒ tolerates 2 lost keepalives; under `DATALOG_TIMEOUT_S` 5 s. Renews **both** leases. |
| `SO_KEEPALIVE` (35001) | 5 s / 5 s / 3 (unchanged) | half-open lid-close detected ~20 s; clean crash sub-second |
| `BUS_IDLE_QUIESCE` | 300 ms | SD flash drives blocks ~211 ms apart; 300 ms of no TX **and** no RX proves "between operations" |
| `HOST_SESSION_TEARDOWN_GRACE` | 3 s | after a claim-lease expiry, wait this long before resuming (ECU drops its session on host silence) |
| reaper tick | 1 Hz | latency dominated by TTLs, not tick |
| `STUCK_FLASH_CEILING` | 180 s | **alarm only**, never clears BIT1 |

Worst-case auto-resume latency: ~12 s (+1 tick +300 ms) after a vanish in a
non-owned window; (SD self-complete) +~12 s after a vanish mid-flash; ~75 s +3 s
after a vanish mid-auth (longer, but **correct** — resuming sooner could inject
into a possibly-live session; the host's `finally` teardown normally collapses
this to immediate).

---

## 4. Firmware change list (separate fork `../nc-flash-wican-fw`, `wican-pro`)

- **`can.c`** — add `HOST_BUS_CLAIM_BIT` (BIT3) + accessors
  `can_host_bus_claim_set(token,ttl)/renew(token,ttl)→bool/clear()/active()` and
  lease state (`s_host_claim_token` u32, `s_host_claim_deadline_us` u64) under a
  `portMUX` spinlock `s_park_mux`. NULL-group-safe.
- **`can.c`** — `can_should_park()` ⇒ `FLASH_ACTIVE_BIT | DATALOG_PARK_BIT |
  HOST_BUS_CLAIM_BIT` (the one-line auth-window fence; `poll_log`/`autopid`
  unchanged, they already call it).
- **`can.c`** — add `s_last_tx_us`/`s_last_rx_us` (volatile u64) + getters; stamp
  TX at the end of `can_send()` (single TX chokepoint) and RX in the TWAI rx path.
  The "bus provably idle" evidence.
- **`can.c`** — add the `DATALOG_PARK` lease state (`s_park_gen`, `s_park_token`,
  `s_park_owner_fd` = 35001 `s_conn_gen` at pause, `s_park_deadline_us`) +
  `can_park_lease_set/renew/expired/owner_alive`. **`*_expired()` must check
  `deadline != 0` FIRST** (0 = DISARMED, never "expired at epoch").
- **`slcan_port.c`** — expose `uint32_t slcan_port_conn_gen(void)` (returns
  `s_conn_gen` if `PORT_OPEN_BIT` else 0) so the handler can stamp the lease owner
  and the reaper can check owner-alive. On the in-flash reconnect (host reconnects
  on every ECU-reset boundary) the host re-stamps the owner under the same token
  via `op=keepalive`, so an owner-gen bump mid-operation does NOT read host-gone.
- **`csv_logger.c`** — factor `datalog_resume_locked()` (clear BIT2 + restore
  pre-pause mode), called by BOTH `op=resume` AND the reaper (one un-park path).
- **`csv_logger.c` `datalog_control_handler`** — `op=pause` also arms the park
  lease (`s_park_gen++` → token, `can_park_lease_set(token, conn_gen, PARK_TTL)`)
  and returns the token + lease fields. NEW `op=bus_claim` (arm BIT3 + claim
  lease, return token), `op=bus_release` (token-matched clear), `op=keepalive`
  (token-matched, renew BOTH leases, never touches a bit). `op=resume` now
  **token-matched** (reject stale 2nd-client resume with 409).
- **`csv_logger.c` `datalog_state_json`** — add `datalog_parked`,
  `host_bus_claimed`, `park_token`, `lease_ttl_ms`, `claim_ttl_ms`, `bus_idle_ms`,
  `stuck_flash_alarm` (token-aware reconcile + host verification).
- **NEW `main/datalog_lease_task.c`** — the dead-man reaper (prio 2, PSRAM stack,
  1 Hz), started on WICAN_PRO after `slcan_port_init` + `csv_logger` init. Per tick
  under `s_park_mux`: **claim-reap** (force-clear BIT3 if claim-expired ∧
  owner-gone ∧ bus-idle ∧ teardown-grace) and **datalog-reap**
  (`datalog_resume_locked()` if park-active ∧ `!flash` ∧ `!claim` ∧ presence-lost ∧
  bus-idle). Emit best-effort `NCDLAUTORESUME` / `NCCLAIMREAP` notes.
- **NEW stuck-flash ALARM** — if `FLASH_ACTIVE_BIT` set > `STUCK_FLASH_CEILING`,
  set `stuck_flash_alarm` + log loudly. **Never clears BIT1.**
- **EXPLICIT NON-CHANGE** — `ncflash_fastwrite.c` / `ncflash_fastread.c` untouched;
  the codecs remain the sole owners of `FLASH_ACTIVE_BIT`. (Design-1's appended
  cleanup-label resume trigger was an inter-flash TOCTOU; rejected.)
- **CI/lint** — assert exactly the two `can_flash_active_clear` call-sites; fail on
  any new unbounded wait in a BIT1-holding file (INV-7).

> See also [§6](#6-prerequisite-bug-the-rx-forward-half-of-36-is-missing) — the
> RX-forward fix lands in the **same** firmware cycle.

## 5. Host change list (`nc-rom-editor`)

- **`wican_sd_flash.py:_trigger_firmware_flash`** (load-bearing) — new order:
  rev-gate → **`bus_claim()`** (arm BIT3 + start keepalive) → `pause()` →
  `sleep(PRE_SESSION_SETTLE_S)` → `_authenticate_ecu()` → `fast_write()` →
  `finally:` UDS default-session/`ECUReset` teardown → `bus_release()` →
  `resume()`. Claim armed **before** auth, released **after** `fast_write`. All
  soft-degrading.
- **`wican_config.py:WiCANDatalogClient`** — add `bus_claim()/bus_release(token)/
  _keepalive(token)` (renews both leases) + a **daemon keepalive thread** started by
  `bus_claim()/pause()`, stopped+joined by `bus_release()/resume()/close()`, tied
  to `atexit` AND an explicit stop-`Event` (not GC/`__del__`) so a leaked thread
  can't pin a lease.
- **`wican_config.py:pause()/resume()`** — carry `park_token`/`claim_token` in the
  breadcrumb; `resume()` stops+joins the thread first, treats a 409
  "already auto-reaped" as success.
- **`wican_config.py:reconcile()`** — token-aware + **fix the false docstring**:
  leave paused if `flash_active OR host_bus_claimed`; resume (with token) only if
  `datalog_parked ∧ !flash_active ∧ !host_bus_claimed`. Keep the two-instance guard.
- **`wican_transport.py:open()`** — set `SO_KEEPALIVE` + `TCP_KEEPIDLE=5 /
  KEEPINTVL=5 / KEEPCNT=3` on the persistent session socket (Windows
  `SIO_KEEPALIVE_VALS` via ioctl, platform-guarded). Faster mutual death detection
  (not load-bearing; the firmware reaper is authoritative).
- **`ecu_window.py:closeEvent` + `main.py:MainWindow.closeEvent`** — best-effort
  graceful resume **GATED on** `GET /datalog` showing `!flash_active &&
  !host_bus_claimed`; if a claim/flash is live, do NOT resume here (let the worker
  `finally` + firmware reaper handle it). Deterministically stop the keepalive
  thread.
- **`session.py:_teardown_wican`** — stop any keepalive thread (idempotent). Keep
  `disconnect_ecu` REFUSED while BUSY.
- **`constants.py`** — the shared timing constants from §3.
- **Tests** — token-aware pause/resume/reconcile, 409 handling, keepalive renews
  both leases, leaked-thread stops on `atexit`; a **REAL-thread** keepalive
  lifecycle test (`qInstallMessageHandler`/`threading.Event`, not mocked, per the
  QThread-real-test rule); reconcile-leaves-paused-when-claimed; closeEvent-gate.

---

## 6. Prerequisite bug: the RX-forward half of #36 is missing

Found on the bench 2026-06-26 (smoke test over 35001 hangs 60 s, full read times
out 320 s) **after** OTA of the #36 build:

**#36 wired the TX half, not the RX half.** `can_tx_task` dispatches
`DEV_SLCAN_PORT` frames to the bus (works), but the only code that forwards bus
frames back to a TCP client is `can_rx_task`, which is **gated off entirely in
`POLL_LOG`** (`main.c:439`, keyed on `protocol`, which `/datalog pause` never
changes) and whose forward logic only knows `protocol == SLCAN → xMsg_Tx_Queue`
(port 35000) — **no route to `xMsg_SlcanPort_Tx_Queue`** (confirmed: that queue
appears only in `can_tx_task`, never in `can_rx_task`). So host-driven UDS
(tester-present, the `0x10`/`0x27` auth, read sessions) sends fine, the ECU
replies, and the reply is dropped. This blocks **both** read and the SD-flash
auth window over the dedicated port.

**Fix (same firmware cycle):** when a coexist session owns the bus
(`can_should_park()` true ⇒ `poll_log` parked, so TWAI is free), `can_rx_task`
must take over as the TWAI consumer, parse frames as SLCAN **regardless of
`protocol`**, and route them to `xMsg_SlcanPort_Tx_Queue`. i.e. the gate becomes
"park `can_rx_task` in `POLL_LOG`/`FAST_LOG` **only while the logger is actively
consuming**; when the logger is parked for a coexist session, `can_rx_task` owns
the bus and forwards to the dedicated port." Single-TWAI-consumer discipline is
preserved (exactly one of {poll_log, can_rx_task, codec} drains at a time).

---

## 7. Hardware tests (bench MX-5 NC ECU, brick-authorized; run `WICAN_MANUAL_TEST.md`)

> **DONE on hardware (2026-06-26, bench WiCAN 192.168.1.169, NCFRv6):**
> - `tools/wican_deadman_verify.py --reaper` (zero ECU contact, brick-safe) — version ping rev 6;
>   all `/datalog` deadman state fields + lease TTLs; full lease round-trip (`bus_claim`→claim_token,
>   `pause`→park_token, `keepalive` both, `bus_release`, `resume`) incl. **stale-token → HTTP 409**;
>   and the **reaper auto-resume** (host vanish → claim+park auto-cleared at ~78 s = claim-TTL+grace,
>   `bus_idle_ms` climbing as the claim quiesced poll_log). Covers the no-ECU heart of HW-3/HW-6.
> - **HW-1 PASSED on the LIVE ECU (the headline brick test, pre-erase):** raised the fence, entered
>   programming session `0x10 0x85` (FLASH_ACTIVE_BIT clear = the §2 unfenced window), VANISHED the
>   host mid-auth (no teardown). The fence HELD (`host_bus_claimed` True + poll_log parked, bus_idle
>   24→76304 ms = **zero `0x7E0` injected**) for the full 81 s; the reaper resumed ONLY at ≈81 s; the
>   **ECU survived** (ROM-ID `SW-LFDJEA000.HEX` still readable, not bricked). No flash erased.
> - **HW-9 PASSED on the LIVE ECU (normal coexist flash, byte-perfect):** real SD coexist flash
>   (byte-identical LFDJEA reflash) over the coexist port with the fence — 1022/1022 blocks → NCFWDONE
>   in 70 s, fence released cleanly, **WiCAN did NOT reboot**; after a physical power-cycle a fenced
>   1 MB read-back (215 s) was **sha256 byte-identical to source**. HW-9 also caught + fixed a real host
>   bug — the preflight link gate pinged the ECU BEFORE the fence was raised, hanging on the coexist
>   port; the fence is now a `_datalog_fence` contextmanager bracketing the whole host-driven window
>   (gate→auth→fast_write) per INV-2, with a settle so poll_log parks before the first ping.
>
> **#36 is now fully device-validated.** Remaining open questions in §8 are hardening follow-ups.

- **HW-1 (the auth-window brick test all designs missed):** flash with poll_log on;
  KILL the host **during** `_authenticate_ecu()` (between `0x10` and
  `security_access_send_key` — BIT1 clear, BIT3 set). Wait past the park TTL into
  the claim TTL. ASSERT **zero** `0x7E0` frames reach the ECU until claim-expiry +
  teardown-grace (then resume) or explicit teardown; ASSERT no soft-brick.
- **HW-2 (mid-flash kill, known-safe):** kill the host mid-flash (BIT1 set). ASSERT
  zero CSV rows + zero `0x7E0` until `NCFWDONE`; firmware self-completes; BIT1
  clears; datalog auto-resumes ~12 s + quiesce; byte-perfect flash.
- **HW-3 (lid-close while paused, not flashing):** pause + claim released, close
  lid (half-open TCP). ASSERT `SO_KEEPALIVE` reaps the socket ~20 s, park lease
  ~12 s, reaper resumes once idle; no spurious early resume; no brick.
- **HW-4 (graceful window close mid-auth):** close the window during auth. ASSERT
  the closeEvent resume is GATED OFF (`host_bus_claimed`); the worker `finally`
  runs teardown + bus_release + resume; no brick.
- **HW-5 (in-flash reconnect owner re-stamp):** on the ECU-reset boundary the host
  reconnects 35001 (`s_conn_gen` bumps). ASSERT the reaper does NOT read host-gone
  mid-operation (owner re-stamped under the same token).
- **HW-6 (leaked-keepalive ceiling):** hold the claim past the firmware max-lease
  ceiling. ASSERT the renewal ceiling force-clears the claim and the reaper resumes
  once idle.
- **HW-7 (port-only soft-degrade):** run against a build with no `/datalog`. ASSERT
  bus_claim/pause/resume all return `None` and are swallowed; flash byte-perfect.
- **HW-8 (two-instance reconcile):** B connects mid-flash. ASSERT `B.reconcile()`
  sees `flash_active OR host_bus_claimed` and leaves it paused; datalog resumes
  exactly once.
- **HW-9 (no-fault coexistence regression):** normal flash, host present,
  keepalives flowing. ASSERT clean pause at bus_claim, no mid-window resume,
  immediate resume at bus_release, pre-pause manual mode restored exactly.

---

## 8. Open questions / hardening follow-ups

- **Auto-arm vs explicit claim:** infer `HOST_BUS_CLAIM_BIT` firmware-side from the
  first `0x10` programming-session frame on the dedicated port (auto-arm on session
  start, auto-release on `ECUReset`/timeout)? Removes the host "forgot to claim"
  footgun but adds frame-snooping coupling. **Recommend explicit REST claim for v1**
  (matches the `/datalog` contract, testable in isolation); auto-arm as hardening.
- **Collapse `pause` into `bus_claim`:** a single `op=flash_begin` (set BIT2+BIT3 +
  arm both leases) / `op=flash_end` would make the host a strict begin/`finally`
  pair and delete the intermediate states the adversaries exploited. **Leaning yes.**
- **Ground the teardown grace:** measure the NC-ECU S3 session-timeout so
  `HOST_SESSION_TEARDOWN_GRACE` is grounded, not guessed.
- **Reaper lock discipline:** the reaper reads lease state on its task while the
  handler writes it on the config-server task and the codecs write BIT1 on
  `can_tx_task` — needs a real-task contention test (not mocked) to confirm no torn
  read of the multi-field lease.
- **`check_flash_counter()` traffic** is inside `_authenticate_ecu()` (line 319) ⇒
  inside the claim window; confirm no host CAN traffic exists outside
  `[bus_claim .. bus_release]`.
- **`PRE_SESSION_SETTLE_S`:** with BIT3 now parking the poller before auth, is the
  0.2 s drain still needed, or just latency? Likely keep as cheap insurance for
  firmware-park-propagation; confirm producers are observed parked
  (`datalog_state_json`) before the first auth frame.

---

*Source: 13-agent adversarial design workflow (run `wf_831b2db5-a27`, 2026-06-26):
3 understand → 3 competing designs → 2 adversaries/design (brick + wedge) →
synthesis. All three initial designs were broken on the auth window; the
recommendation is the hybrid with the bus-claim fence grafted in as the
load-bearing element.*
