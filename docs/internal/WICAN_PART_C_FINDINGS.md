# WiCAN goal-2 investigations — reboot avoidance (Part C / #21, #10) + unified-SD (#22)

Findings from the 2026-06-21 investigation (3 parallel firmware/transport research
agents) that feed goal 2 (`.claude/plans/wican-ecu-functions-goal.md`). These are
**investigation-grade recommendations** — the firmware line/function citations were
spot-checked against the source but MUST be re-verified before any firmware change.
No firmware was modified. Reference this before implementing the reboot fix, the
no-reboot protocol switch, or deciding the WRITE-over-SD architecture.

---

## 1. CAN-wedge reboot (#21) — root cause confirmed, clean-teardown design

**Verified root cause.** `ncflash_fast_read()` (`main/ncflash_fastread.c:201-330`)
suspends `can_rx_task` (line 233) and resumes it only once, at line 324, after the
read loop. Every data/sync/diag write uses `xQueueSend(*tx_queue, &s_out, portMAX_DELAY)`
(lines 248, 291, 312). **If the host closes the TCP socket mid-stream**, the WiFi TX
task stops draining `xMsg_Tx_Queue`, so the next `xQueueSend(..., portMAX_DELAY)`
**blocks forever** → `ncflash_fast_read` never returns → line 324 is never reached →
`can_rx_task` stays **suspended** → the CAN channel is wedged → the next SLCAN `S6`
fails and only `POST /system_reboot` recovers it. (Confirmed by reading the source.)

Contributing gaps the investigation flagged (all real):
- TX queue is flushed only at the START of a read (line 242), never on abort, so
  stale frames can poison the next `S6` handshake.
- No `twai_clear_alerts()` / TWAI state reset on exit — a bus-off/error-passive state
  persists after the read returns.
- SLCAN parser static state (`slcan.c`) is not reset after an aborted read.
- No detection of host socket-close inside the read loop.

**Recommended fix (firmware — effort ~Medium, 4-6 h + hardware test).** A single
clean-teardown path that runs on EVERY exit (normal / block-failure / host-close /
error):
1. **Replace `portMAX_DELAY` with a bounded `xQueueSend` timeout** on the data/diag
   sends. On timeout (TX path stalled — host likely gone), abort to the teardown.
   *This is the load-bearing fix*: it stops the forever-block that strands the task.
2. **One cleanup label/section** (goto or nested scope) that always: resumes
   `can_rx_task` (only if this call suspended it — track with a `was_suspended`
   flag so a power-mode suspend elsewhere isn't violated), drains the CAN RX queue,
   clears pending TWAI alerts, flushes `xMsg_Tx_Queue`, and resets the SLCAN parser.
3. Guard re-entry with a mutex (one fast-op at a time) and emit an explicit
   end-of-read marker so the host stops on a clean token instead of a timeout.

**Risk note.** A `can_disable()/can_enable()` cycle (full TWAI reinit) would also
clear a bus-off state but resets the ECU session — use only as a last resort, never
mid-flash. The 5 ms post-resume settle the agent suggested is harmless but verify it
is actually needed.

## 2. No-reboot protocol switch (#10) — coexisting SLCAN port (preferred)

The device's CAN protocol is a persisted `config.json` setting read once at boot
(`main.c:852 protocol = config_server_protocol()`); `POST /store_config`
*unconditionally* reboots (`config_server.c:1005-1007`). Two ways to avoid the
~6 s reboot when NC Flash needs `slcan`:

- **(a) Hot runtime switch** — atomically swap the global `protocol` + re-wire the
  `can_tx_task` dispatch (`main.c:286-369`) + drain queues. **Rejected as risky:**
  the tasks are created once at boot and never re-created; mid-stream re-wiring
  invites frame loss / races. ~200+ lines, 12-16 h, higher regression risk.
- **(b) Coexisting always-on SLCAN TCP port — RECOMMENDED.** Add a second listener
  (e.g. **35001**) that routes its frames straight through `slcan_parse_str` via an
  early `dev_channel == DEV_SLCAN_PORT` branch in `can_tx_task`, *before* the
  `protocol == SLCAN` check — so SLCAN works regardless of the persisted protocol.
  `can_rx_task` already broadcasts RX frames, so the receive side works "for free."
  **Precedent:** the fast-read command already runs alongside non-SLCAN protocols.
  NC Flash would try the dedicated port first and skip `WiCANConfigurator` entirely
  (no config rewrite, no reboot), falling back to the HTTP-switch path for stock
  firmware. ~50-100 lines, ~4 h, low risk. Files: `comm_server.c` (2nd listener),
  `main.c` (boot call + dispatch branch), `types.h` (`DEV_SLCAN_PORT`).

Both reboot items are **firmware work on the fork, gated** (new branch, known-good
rollback `.bin` first). They are NOT required for the host-side WRITE build.

## 3. Unified read+write on SD (#22) — KEEP the streaming read (mixed architecture)

**Conclusion: do NOT force READ to SD-standalone.** The proven streaming fast-read
(`NCFRv4`, byte-perfect, ~214 s = Tactrix parity) is **ECU-limited at ~211 ms/block**,
not transport-limited. SD write throughput (4-bit SDMMC high-speed, ~10-50 MB/s) is
100-1000× faster than the ~1.9 KB/s ECU read ceiling, so **routing the read through
SD gains zero throughput** while discarding a field-validated path and adding new
failure modes (FAT corruption under sustained writes, mount races, buffering during
CAN I/O).

**Recommended architecture if WRITE goes SD (Option B): MIXED.** Keep the streaming
READ exactly as-is; add SD **only** for the WRITE path (a separate
`ncflash_fastwrite()` ≈500-600 LOC mirroring the read loop but using the program
sequence + FAT read). Upload ROM→`/sdcard` over the existing HTTP multipart
(`config_server.c`) or FTP `STOR` (`ftp.c`) — both already serve `/sdcard`. Integrity
is host-side (SHA/CRC after transfer), independent of transport, for both paths.

**Impact on the WRITE decision (goal 2 Part B).** This does NOT by itself pick Option
A vs B; it removes "unify the read onto SD" from the table. Net effect: it **reduces
Option B's scope** (read untouched) but Option B still means a new safety-critical
ESP32-C flash state machine. Option A (host-driven, design-of-record, brick-safe,
~80% already built) remains the lower-risk default; Option B is a heavier future
enhancement for completion-rate on poor WiFi. See the goal doc's "DECISION REQUIRED".
