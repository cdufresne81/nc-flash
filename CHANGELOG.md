# Changelog

All notable changes to NC Flash are documented here.

## [Unreleased]

### Added
- **WiCAN adapter mDNS auto-discovery — no more hardcoded IP (`src/ecu/wican_discovery.py`)** — The WiCAN firmware already advertises a `_wican._tcp` mDNS service (firmware `wc_mdns.c`, confirmed live on the deployed adapter: `WiCAN-WebServer._wican._tcp.local.` → IP + TXT `device_id`/`mac`), so the app can now *find* the adapter instead of making the user type its DHCP IP. A new headless, import-light module browses the LAN over `zeroconf` and returns the discovered adapters (IP, hostname, stable `device_id`/`mac`); `zeroconf` is imported **lazily** so the brick-critical stdlib-only transport/config modules stay dependency-free and the app still runs (degrading to manual entry) if the package is absent. Per-peer resolution is wrapped so a single malformed neighbour record (`BadTypeInNameException`, seen live) can never abort a scan, and the `firmware`/`hardware` TXT fields are treated as optional (deployed builds leave them blank). **Settings ▸ ECU ▸ WiCAN** gains a **"Scan…"** button next to the Host/IP field: it lists the adapters found and fills the field with the chosen device's current IP (the SLCAN *port* is deliberately left untouched — mDNS advertises the HTTP port 80, not the 35000 SLCAN port). Picking a device also persists its stable identity (`get/set_wican_device_id`), so on every connect `ECUWindow._resolve_wican_host` re-resolves that identity to the adapter's **current** IP over mDNS — the link now survives the adapter's address changing across DHCP leases. The re-resolve is opt-in (only when a Scan-stored identity exists), early-exits the instant the device is seen (sub-second on the happy path), is bounded (3 s), and is fully fail-safe: discovery unavailable / device offline / any error all fall back to the stored static host, so a connect is never blocked or broken. Typing a manual IP detaches the stored identity (so it won't be silently overridden). Two brick-safety properties from adversarial review: (1) **identity resolution refuses to guess** — if the same identity somehow answers at more than one distinct IP (only possible with cloned MACs), the re-resolve returns nothing and falls back to the user's stored host rather than risk talking to the wrong ECU; (2) the zeroconf browser's shared state is **lock-guarded** and the predicate/result only ever see a snapshot, so the listener thread can't race the caller. The connect-time re-resolve is intentionally synchronous (bounded ≤3 s, early-exits sub-second when the device is online) — consistent with the WiCAN connect path that already blocks on the adapter's SLCAN-mode reboot. New optional dependency: `zeroconf` (`requirements.txt`). Tests: `tests/test_ecu_wican_discovery.py` (TXT parse incl. blank-field + IPv6-filter + no-address skip, dedup incl. host-fallback when no stable id, missing-lib → `DiscoveryUnavailable`, id/mac colon-insensitive resolve incl. early-exit predicate + ambiguous-identity refusal, malformed-peer resilience), `tests/test_ecu_window_wican_resolve.py` (all six connect-path branches: no-identity, fresh-IP-persists, offline/exception/unchanged fall-backs, cache-write failure non-fatal), `tests/test_settings_dialog_wican_scan.py` (scan flow: zeroconf-missing/unavailable/OSError/empty/cancel/pick, identity staged on pick, manual-edit clears identity, apply persists), and `tests/test_settings_ecu_adapter.py` (device-id round-trip/default).
- **WiCAN Scan now shows a live progress dialog with a Cancel button (off-thread, bounded)** — The Settings ▸ ECU ▸ WiCAN **"Scan…"** button previously froze the dialog behind a wait-cursor for the full mDNS window with no feedback or way out. The scan now runs in a worker thread (`_WiCANScanWorker` on a `QThread`) behind a **"Scanning for WiCAN adapters… Ns (up to 4s)"** progress dialog whose elapsed-seconds counter ticks every 100 ms, so the UI stays responsive and the user can see it working. **Yes, there is a hard timeout** — the scan is bounded by `wican_discovery.DEFAULT_TIMEOUT_S` (4 s) and can never run forever; a determinate bar fills toward that bound. **Cancel** is now possible: a new `cancel_event` (`threading.Event`) is threaded down to the headless `discover()`/`_browse()` (still stdlib-only — `_wait_for_browse` polls the early-exit and cancel events on a monotonic deadline, leaving the connect-time resolve's `done.wait()` path byte-for-byte unchanged), so clicking Cancel — or closing the dialog mid-scan — returns the worker promptly (measured ~0.13–0.45 s vs the full 4 s on the live adapter) instead of lingering. The worker self-disposes via Qt's `finished/error → deleteLater` lifecycle; the result comes back as a signal argument on the GUI thread, and the synchronous picker + identity-staging logic is unchanged. Four adversarial-review fixes are folded in: closing the dialog mid-scan marks the scan cancelled so a late result can't pop a picker after the window is gone; the ticker stops on cancel so it can't overwrite the "Cancelling…" label; stale/duplicate worker signals are dropped when no scan is active; and the orchestration test now asserts the full signal-wiring (a signal-name typo can't slip through). The worker thread is owned by `_teardown_scan`, which `quit()`+`wait()`s it before disposal (and synchronously on dialog close) — **not** self-disposed by dropping references, which would let PySide6 GC the wrapper and destroy a still-running `QThread` (the `QThread: Destroyed while thread is still running` abort seen live after a scan found the adapter). Live-validated end-to-end against the deployed adapter (normal scan finds it in 4.0 s; pre-set and mid-scan cancels return sub-second; full GUI scan flow leaves zero Qt warnings). Tests: `tests/test_settings_dialog_wican_scan.py` (picker, the `_on_scan_*` slots, teardown + `_cleanup_scan_thread`, worker run, orchestration wiring, cancel/stale-signal guards, close-cancels-scan, **plus real-`QThread` end-to-end tests that install a Qt message handler and assert no destroyed-while-running warning — these reproduce and would have caught the abort the mocked tests couldn't**) and `tests/test_ecu_wican_discovery.py::TestCancelSupport` (cancel plumbing + `_wait_for_browse` early-exit/timeout).
- **Qt + native crash diagnostics (`src/utils/qt_diagnostics.py`)** — Routes Qt's own console warnings into the session log and, for the narrow set of threading/painting warnings that have preceded a hard crash (`QObject::setParent: Cannot set parent, new parent is in a different thread`, `QBackingStore::endPaint() called with active painter`, timer/`~QObject` cross-thread warnings), captures the **Python stack** at the instant Qt emits the warning — so an intermittent, hardware-only crash (observed 2026-06-23 after a dynamic flash that completed on the ECU but closed the app during the completion UI) leaves an actionable trace instead of an un-attributable console line. Also arms `faulthandler` so a genuine native fault dumps a C-level traceback to the session log. Installed once at startup (`main.py`, right after `QApplication`); pure diagnostic, never changes behaviour. Tests: `tests/test_qt_diagnostics.py` (level routing, stack attached only for known triggers, handler never raises).
- **WiCAN SD-staged flash — host-side foundation (Option B Phases 2–3, build-only, no ECU contact)** — The safe replacement for the disabled host-driven WiCAN write: instead of pushing ~150k ISO-TP frames over WiFi (which soft-bricks on a drop), the host stages a checksum-corrected ROM to the WiCAN SD card over reliable TCP and the firmware will drive the ECU program sequence locally over CAN (firmware half lands later). Three new headless, fully unit-tested modules: (1) **`src/ecu/wican_sd_package.py`** — turns a ROM into a self-checked staged image `[checksum-corrected ROM] ++ [SBL]` plus a JSON flash-plan manifest (download addr/size, block size, flash_start_index for full+dynamic, sbl/program offsets+lengths, SHA-256/CRC32). It replicates `FlashManager._flash_rom_inner`'s host prep exactly (validate → generation → checksum-correct + verify-zero-residual → SBL → program slice → assemble → digest), so a staged flash is byte-identical to a known-good J2534 flash; the staged filename mirrors the read auto-save as `<ROM_ID>_<YYYYMMDD>-<HHMM>.bin`. (2) **`src/ecu/wican_sd_upload.py`** (`WiCANSdUploader`) — multipart upload to a new `/upload/sd/<name>` endpoint that verifies the device's `{bytes_written, crc32}` reply against the host digest and refuses to trust a partial/corrupt upload (the only WiFi step, checkable before any ECU contact). (3) **`src/ecu/wican_sd_flash.py`** (`WiCANSdFlasher`) — orchestrates package → upload → firmware-trigger → optional read-back verify (opt-in, post-ignition-cycle — see the Phases 4–6 entry), reusing the proven `WiCANFlasher` link/battery gate + read-back compare by composition (no mixin). The trigger drives **`WiCANTransport.fast_write()`** (new), which sends the `W<mode><name>` command and parses the firmware's newline-delimited marker stream (`NCFWSYNC`/`NCFWPROG <done>/<total>`/`NCFWDONE`/`FWERR`) into the existing `FlashProgress` 35→90% band — the WRITE-path mirror of `fast_read`'s stream reader, with resync-past-CAN-traffic, FWERR surfacing, stall + peer-close detection, and **no host-side abort** (the firmware owns the flash). It is **rev-gated** (refuses on a fast-read-only `NCFRv4` build) **and** was initially kept behind the `WICAN_WRITE_ENABLED` make-safe gate at the UI; that gate is now **on** after the live-flash proof (see the Phases 4–6 entry). SD upload was metered at ~1 s/MB, so the Option B budget is ~1 s upload + ~55 s firmware flash ≈ Tactrix parity. Tests: `tests/test_ecu_wican_sd_package.py`, `tests/test_ecu_wican_sd_upload.py` (in-process HTTP server, real multipart round-trip), `tests/test_ecu_wican_sd_flash.py`, `tests/test_ecu_wican_fast_write.py` (socketpair replay of the marker stream). See `.claude/plans/wican-write-option-b-goal.md`.
- **WiCAN adapter selectable from the UI (settings + ECU window wiring)** — NC Flash can now talk to the ECU over a WiCAN PRO (WiFi/SLCAN) adapter from the app, not just the bench tools. A new **Settings ▸ ECU ▸ Adapter** dropdown (`J2534` default / `WiCAN` opt-in) plus a **Settings ▸ ECU ▸ WiCAN** page (Host, Port, *Auto-configure adapter* toggle, **Test Connection**) drive a single adapter-aware `ECUSession`: the read/RAM/DTC/clear and flash actions in the ECU Programming window pick the saved adapter and build the transport via the existing `create_ecu_transport()` factory. The **J2534 (wired) path is unchanged** — same `FlashManager` + `use_session` flow. For WiCAN, reads/RAM/DTC ride the transport-agnostic `FlashManager.use_uds`, and **flash routes through `WiCANFlasher`** (pre-flight link gate + battery guard + abort-and-restart, **never** a mid-stream block resend); the WiCAN Flash action is gated behind an *experimental — not yet hardware-validated, keep ignition ON* confirmation. Auto-config switches the device into SLCAN **once per session** (a ~6 s reboot) and restores the original protocol only on a real disconnect / app exit — never on the internal post-read auto-reconnect (which reuses the session) — so a session never reboots the adapter more than once. `Test Connection` opens the link and reports packet-loss / p95 latency, always restoring the cursor, transport, and protocol. Tests: `tests/test_ecu_session_wican.py` (switch-once / restore-once / reconnect-keeps-SLCAN / connect-failure-restores / acquire-without-device) + `tests/test_settings_ecu_adapter.py` (adapter + WiCAN settings round-trip/defaults). **WiCAN flashing remains build-only and bench-unvalidated** (task #20) — the UI wiring is what enables that gated hardware test. See `.claude/plans/wican-adapter-ui-goal.md`.
- **WiCAN host-driven flash safety layer (WRITE logic — BUILT, not yet hardware-validated)** — `src/ecu/wican_flash.py` (`WiCANFlasher`) + `src/ecu/link_quality.py` add the lossy-link safeguards a wireless flash needs *around* the existing transport-agnostic `FlashManager`, with the J2534 path unchanged: (1) a flash-only **pre-flight link-quality gate** — fires Tester-Present round-trips and refuses the flash if there is any packet loss or the p95 latency is high (the write path has no mid-stream resend, so a clean link is a hard precondition; reads/diagnostics are never gated); (2) a **battery/voltage guard** at `BATTERY_VOLTAGE_WARNING` (12.0 V, the historical brick cause); (3) **abort-and-restart-from-scratch** on a mid-flash transport drop — each retry is a *fresh whole flash* (re-auth, re-SBL, re-transfer) built on a new `FlashManager`, **never** a mid-stream block resend, preserving the no-resend brick-safety invariant; (4) an optional **read-back verify** that byte-compares the flashed region to the checksum-corrected source. `WiCANFlasher.flash_rom` / `dynamic_flash` / `preflight` are the integration points for the UI. **NO real flash has been performed over WiCAN — this is build-only and MUST be bench-validated (user-gated) before production use.** Tests: `tests/test_ecu_link_quality.py` + `tests/test_ecu_wican_flash.py` (23) cover the gate verdict logic, the restart-from-scratch path (asserting a fresh `FlashManager` per attempt — no resend), non-restartable-error passthrough, and read-back verify pass/mismatch. See `.claude/plans/wican-ecu-functions-goal.md` (Part B, Option A) — Option B (SD-autonomous flash) remains a documented future alternative (`docs/internal/WICAN_PART_C_FINDINGS.md`).
- **WiCAN ECU diagnostic functions — READ RAM / READ DTC / CLEAR DTC + bench tool** — `tools/wican_bench_ecu.py` runs the RAM scan and DTC read/clear over a WiCAN adapter by driving the existing transport-agnostic `FlashManager` seam (`scan_ram` / `read_dtcs` / `clear_dtcs` over a borrowed WiCAN `UDSConnection`) — the same code path the J2534 adapter uses, so nothing WiCAN-specific is added to the flash core. READ RAM and READ DTC are non-destructive (idempotent reads); CLEAR DTC mutates ECU state (a benign but real write) and is gated behind an explicit `--yes`. `--auto-config` reuses `WiCANConfigurator` to flip the device to `slcan` and restore the prior protocol. Added `tests/test_ecu_wican_ecu_functions.py` proving the three functions over a `FakeTransport` (the exact path the WiCAN connection rides — no hardware/`_secure` needed) plus the tool's pure helpers (RAM sanity summary, DTC formatting, clear verdict); hardware-confirm steps added to `docs/internal/WICAN_MANUAL_TEST.md` §3b. **Confirmed on the live MX-5 NC ECU over WiCAN (all three):** READ DTC returned the bench ECU's 17 stored codes; READ RAM dumped a clean 48 KB after the scan-retry fix (below); CLEAR DTC reduced the set 17 → 7 (10 cleared, 7 hard faults re-set immediately). See `.claude/plans/wican-ecu-functions-goal.md` (Part A) and the reboot/SD investigation findings in `docs/internal/WICAN_PART_C_FINDINGS.md`.
- **WiCAN autonomous fast-read transport (experimental; requires custom firmware) — HARDWARE-VALIDATED, byte-perfect** — `WiCANTransport.fast_read(start, length)` sends a one-line `X<8 hex start><8 hex length>` command and reads the raw ROM stream straight back, for use with a matching firmware fork (`cdufresne81/nc-flash-wican-fw`, branch `feature/fast-rom-read`) that does the per-block `ReadMemoryByAddress` loop on the ESP32 locally over CAN — paying the WiFi round-trip once instead of per 1 KB block. On a live MX-5 NC ECU a full authenticated 1 MB read is **byte-for-byte identical to the J2534 oracle** at **~214 s (4.8 KB/s)** — matching the user's own Tactrix (215.8 s) on the same ECU. The remaining floor is the ECU's per-block **response-pending (~211 ms/block, universal)**, not the transport (the firmware already eliminated the per-block WiFi round-trip: 339 s → 214 s). The earlier ~60 s goal was an optimistic guess; this is true reference-tool parity. Three firmware fixes were needed to get byte-perfect: (1) handle ECU **response-pending** (`7F 23 78`) per block, (2) a **sync preamble** (`NCFRDATA`) so the host discards CAN frames queued before CAN-forwarding suspends (which otherwise shift the whole stream), (3) the host **chunks reads into 128 KB commands** because a single very long command degrades the firmware TX/WiFi path ~880 KB in. `tools/wican_bench_read.py --fast-read` (with `--fast-read-start`/`--fast-read-len` for sub-ranges) measures and byte-compares it. The ECU must already be authenticated over the normal SLCAN path; the firmware only replays reads, never authenticates or writes, and a block failure stops the stream short (surfacing the firmware's `FRERR` diagnostic) so the caller falls back to the per-block read. See `.claude/plans/wican-read-speed-goal.md` and `docs/internal/WICAN_MANUAL_TEST.md`.
- **WiCAN firmware version ping (`WiCANTransport.version_ping()`, `tools/wican_fw_ping.py`)** — Confirms which fast-read firmware build is live before a read/flash: a fast-read at a sentinel address makes the firmware stream a fixed `NCFRv<rev>` marker without touching CAN, so the host can verify an OTA actually took. `tools/wican_fastread_verify.py` fast-reads an arbitrary ROM range and byte-compares it to the oracle (for isolating a region). See `docs/internal/WICAN_MANUAL_TEST.md`.
- **WiCAN read-speed software levers (Phase 1)** — Three behaviour-preserving optimisations toward J2534/Tactrix read parity, all defaulting safe and tunable from the bench. (1) **ISO-TP N_Cr fast-fail** (`IsoTpSession(n_cr_ms=…)`): once a multi-frame response has started, a gap longer than N_Cr before the next Consecutive Frame means a dropped frame on the lossy link, so the read fails *fast and definitively* (a non-timeout `IsoTpError`) and the idempotent per-block retry re-requests immediately — instead of stalling the full ~4 s per-block budget on every dropped block. `WiCANTransport` enables it by default (`DEFAULT_N_CR_MS = 500`); the J2534 path passes `None` and is byte-for-byte unchanged. A clean read never trips it (frames arrive milliseconds apart). (2) **Socket tuning** on `WiCANTransport`: **TCP_NODELAY** on by default (no Nagle batching of small ISO-TP frames) plus an optional **SO_RCVBUF** size. (3) **Configurable ROM read block size** — `FlashManager.read_rom(read_block_size=…)` issues fewer, larger `ReadMemoryByAddress` requests (up to the ISO-TP `0xFFE` cap) to amortise per-request latency; default stays the hardware-validated `0x400`. `tools/wican_bench_read.py` exposes all of these (`--n-cr-ms`, `--no-tcp-nodelay`, `--so-rcvbuf`, `--block-size`). See `.claude/plans/wican-read-speed-goal.md`.
- **WiCAN read-speed bench instrumentation (`tools/wican_bench_read.py`)** — Additive, non-destructive harness to diagnose and tune the slow WiCAN ROM read (currently ~16 min for 1 MB; goal is J2534/Tactrix parity ~60 s). `--probe` checks whether the ECU honours `ReadMemoryByAddress` sizes above 0x400 (tries 0x400/0x800/0xFFE — reads are idempotent, so probing larger sizes can never change ECU state). `--bench-blocks N` times N raw block reads and reports the per-block latency distribution (min/avg/p95/max) plus the extrapolated full-1 MB time, so each pacing lever becomes a number. New sweep knobs `--rx-stmin` (ISO-TP STmin we advertise — ms or 0xF1–0xF9 µs), `--rx-block-size` (Flow-Control Block Size, for STmin=0 + small-BS burst pacing), `--block-size` (read bytes per request), and `--read-timeout-ms` are plumbed into `WiCANTransport`. No change to the read/flash path — measurement only. See `.claude/plans/wican-read-speed-goal.md`.
- **WiCAN transport hardware-hardening (read path validated end-to-end)** — Three reliability fixes found and confirmed against a live MX-5 NC ECU over a WiCAN PRO. (1) **Open warm-up prime:** the adapter reliably drops the first CAN frame after the SLCAN `O` ack (CAN peripheral still coming up), which made the first real request hang for the full ~60 s timeout; `WiCANTransport.open()` now sends one throwaway TesterPresent warm-up frame and drains the ECU's reply so it can't pollute the first real request. (2) **Receive flow-control pacing:** at STmin=0 the ECU streams ~146 consecutive frames per 1 KB read faster than the gateway can forward, overflowing its CAN→TCP buffer and silently dropping frames. `WiCANTransport` now advertises **STmin=3** (BS=0) when receiving, tuned on hardware (STmin 0→drop@8/64, 1→40/64, 2→64/64 clean); `rx_block_size`/`rx_stmin` are constructor knobs. (3) **Per-block read retry:** pacing makes drops rare but not impossible over 1024 blocks, so `FlashManager.read_rom` now re-requests any block whose response is lost/garbled (reads are idempotent — re-requesting never changes ECU state), using a tight per-block timeout so a drop fails in seconds instead of stalling the 60 s pending budget, and flushing stale frames between attempts via a new `EcuTransport.flush()` (no-op on J2534, frame-drain on WiCAN). The flash/write path is untouched — it must never resend a block. With all three, a full authenticated 1 MB ROM read (seed/key via the private `_secure` module) completes cleanly end-to-end at ~1.1–1.7 KB/s, retrying the occasional dropped block transparently. See `docs/internal/WICAN_TRANSPORT.md` §5/§6/§8b.
- **WiCAN auto protocol switch (`WiCANConfigurator`)** — New headless, stdlib-only `src/ecu/wican_config.py` that flips the WiCAN device's HTTP-config `protocol` to `slcan` (required before the SLCAN socket passes any CAN traffic) and restores the user's previous protocol afterwards. To avoid mangling the device config (which holds WiFi/MQTT passwords in plaintext), it performs a surgical regex edit of only the top-level `"protocol"` token on the raw config text — never a `json` round-trip — with an exactly-one-match guard and a negative lookbehind so the `home_/drive_/batt_alert_protocol` siblings are never touched, plus a defensive parse-check before writing. `switch_to_slcan()` returns the previous protocol for crash-recovery; the device reboots on write and the configurator polls `/load_config` until it confirms the new value. Wired into `tools/wican_bench_read.py` behind an opt-in `--auto-config` flag (`--http-port`, default 80) that switches before opening the link and restores in a `finally` (even on error/Ctrl-C); OFF by default.
- **WiCAN PRO wireless transport (in progress)** — Introduced an `EcuTransport` abstraction so ECU communication can run over either J2534 (Tactrix OpenPort, unchanged) or a WiCAN PRO adapter over WiFi using SLCAN + a Python ISO-TP engine. `UDSConnection` now takes an `EcuTransport` instead of a raw J2534 device/channel; `ECUSession` and `FlashManager` wrap their J2534 device in a `J2534Transport` (default everywhere, byte-for-byte identical flash I/O). Added a `create_ecu_transport(config)` factory (`{"kind": "j2534"|"wican", ...}`) and a transport-agnostic `FlashManager.use_uds(uds)` injection point so non-flash ops can run over a WiCAN-built connection without a J2534 device. Software foundation only this release (transport layer, SLCAN codec, ISO-TP engine, transport-agnostic `UDSConnection`/session/flash wiring, all unit-tested); WiCAN flashing is gated behind hardware bench validation and not yet user-exposed. See `docs/internal/WICAN_TRANSPORT.md`.
- **V2 TCM ROM definitions** — Imported transmission control module (TCM) definitions from the NC_TCM project (djobes) for `LFG1TF000`, `LFG1TG000`, `LFACTA000`, and `LFAMTA000` (Mazda MX-5 NC transmission control module). Note: `LFG1TG000`, `LFACTA000`, and `LFAMTA000` are imported but NOT yet validated against real TCM dumps — only `LFG1TF000` has a hardware-backed test.
- **TCM sample dump and detection test** — Added `examples/LFG1TF000.bin` sample TCM dump and `tests/test_tcm_v2_detection.py` validating real detection of the `LFG1TF000` V2 definition.

### Changed
- **WiCAN staged SD file is now named after the ROM shown in NC Flash (not a bare cal-ID)** — When you flash a ROM over WiCAN, the file written to the device's SD card was previously `<CAL_ID>_<YYYYMMDD>-<HHMM>.bin` (e.g. `LF9VEB_20260623-1745.bin`) — a timestamp that says nothing about the tune. It is now `<display-filename>_<YYYYMMDD>_<HHMM>.bin`: the loaded file's name as shown in NC Flash, made transport-safe, then suffixed with a minute-precision timestamp (e.g. `Testé AFR à 12.5.bin` → `Teste_AFR_a_12.5_20260624_1039.bin`). The staged name is used verbatim in three ASCII-only, space-hostile hops — the FAT SD filename, the `/upload/sd/<name>` HTTP path, and the firmware's `W<mode><name>\r` SLCAN trigger (`.encode("ascii")`) — so the new `_sanitize_filename_stem` **transliterates accents to ASCII** (`é→e`, `à→a`, recognisable, not mangled to `_`), replaces spaces and other unsafe chars with `_`, **collapses any `..`** (which the upload's path-traversal guard would otherwise reject, silently blocking a legit flash) while keeping a single dot (so `12.5` survives), drops the source extension + re-appends `.bin`, length-caps the stem (64), and falls back to `ecu_rom` if nothing printable remains — output is always non-empty pure ASCII with no spaces/separators. Threaded UI→packager via a new `source_name` on `build_flash_package` / `WiCANSdFlasher` (the manifest `rom_id` identity is unchanged; `source_name` only names the staged file, falling back to the cal-ID label when absent). No change to the staged *bytes*, the manifest plan, or the flash sequence — purely the SD filename. Tests: `tests/test_ecu_wican_sd_package.py` (transliteration/space/`..`/length/empty cases + `source_name` drives the name, blank falls back), `tests/test_ecu_wican_sd_flash.py::TestSourceName`, `tests/test_ecu_window_flash_driver.py` (UI forwards the display filename to the SD flasher).
- **Activity Log scoped to relevant subsystems + expected-NRC noise quieted (no protocol/flash behaviour change)** — Three log-level/routing fixes so the user-facing ECU Activity Log no longer shows benign or unrelated noise mid-flash, while the session log file keeps everything. (1) **Context-aware NRC quieting:** `UDSConnection.send_request` gained an optional `quiet_nrcs` set; when the ECU returns an NRC the caller expects and handles gracefully, the generic `UDS NRC:` record is logged at **DEBUG** instead of WARNING (the `NegativeResponseError` is still raised — only the log level changes). Threaded through `read_obd_pid` (battery-voltage / RPM best-effort reads) and the DTC reads (`read_dtc_count` / `read_dtc_status`) for `NRC_CONDITIONS_NOT_CORRECT` (0x22), which the ECU returns on a post-op reconnect; this removes the two scary SID=0x01 WARNINGs and the DTC double-log. **A genuine/unexpected NRC — including a 0x22 refusing a flash/security/reset request — still logs at WARNING** (security-access, transfer-data, and the flash-manager post-commit reset deliberately do *not* pass `quiet_nrcs`). (2) **Qt diagnostics kept out of the console but in the session file:** a handler-side `logging.Filter` on `LogConsole` drops `qt`-logger records (the "Qt: Cannot set parent…" warnings + Python-stack dumps from `qt_diagnostics.py`) from the console only; the `qt` logger still propagates to the root's session file handler, so the diagnostics are preserved on disk. (3) **ECU Activity Log restricted to relevant loggers:** `LogConsole` gained an optional `allowed_logger_prefixes` allowlist; the ECU Programming window passes `["src.ecu", "src.ui.ecu_window", "__main__"]` (and `drop_qt_logger=True`) so unrelated subsystems can't unnerve the user mid-flash. Defaults are unchanged (`allowed_logger_prefixes=None`, `drop_qt_logger=False`), so the always-visible main-window Activity Log still shows every logger at INFO+. Tests: `tests/test_ecu_protocol.py::TestSendRequestQuietNrcs` (+ DTC level guards), `tests/test_ecu_obd.py::TestReadObdPid::test_passes_quiet_nrcs`, new `tests/test_log_console.py` (qt drop + still-reaches-root-handler, allowlist, default-unchanged, min_level interaction).
- **WiCAN SD-staged flash ENABLED at the UI (Option B Phases 4–6) — live, byte-perfect on the MX-5 NC ECU** — Wireless flashing is now available from the app over the SD-staged, firmware-driven path, replacing the brick-prone host-driven write. The host stages a checksum-corrected ROM (+SBL) to the WiCAN SD card over reliable TCP (CRC-verified before any ECU contact), then the firmware (`ncflash_fastwrite`, `NCFRv5+`) drives `RequestDownload → TransferData(SBL) → TransferData(program) → TransferExit → ECUReset` locally over CAN — **WiFi is not in the flash loop, so a dropped link can no longer interrupt the programming session** (the previous soft-brick cause). The master gate `WICAN_WRITE_ENABLED` (`src/ecu/wican_flash.py`) is now **`True`**, routing WiCAN `flash`/`dynamic_flash` to `WiCANSdFlasher` behind the firmware rev-gate (`NCFRv5+`), the link/battery pre-flight gate, and the SD-image CRC32 digest gate; it still works as a kill-switch when flipped off. **Hardware-validated 2026-06-23:** a full live flash wrote 1022/1022 blocks and the post-ignition-cycle read-back equals `correct_rom_checksums(source)` byte-for-byte (bar the ECU-stamped flash counter @0xFFB00). **Write integrity is firmware-confirmed** — every program block gets a positive ECU response and `TransferExit` is acknowledged (the same bar as the trusted J2534 path, which also does not read back) — so the inline read-back verify is **decoupled** (`verify` defaults **off**): the NC ECU sits in its bootloader after the flash's `ECUReset` (identical `0x11 0x01` on J2534 and WiCAN) and only a **physical ignition cycle** boots the new calibration, which the host cannot trigger. Both flash paths now end with a clear "**Flash written & confirmed — cycle the ignition (key OFF ~10 s, then ON)**" completion step (the standard documented Mazda NC final step, required for J2534 and WiCAN alike). Tests: `tests/test_ecu_window_flash_driver.py` (WiCAN writes route to the SD flasher by default + the gate kill-switch still disables), `tests/test_ecu_wican_sd_flash.py` (firmware-confirmed completion with no inline verify; opt-in verify runs the read-back compare; the not-readable case guides the ignition cycle). See `.claude/plans/wican-write-option-b-goal.md` (Phases 4–6).
- **Quieter WiCAN read/RAM logging — dropped-then-recovered blocks no longer spam WARNINGs** — At the validated `STmin=0` pacing (the 3× read-speed default) the lossy WiFi link drops ~1 block in ~48; the idempotent per-block retry re-requests it and the ROM/RAM is still byte-perfect. Each transient drop previously logged a `WARNING` (immediately followed by a recovery `INFO`), flooding the Activity Log on a normal read. The per-attempt retry chatter is now `DEBUG`; instead, `read_rom`/`scan_ram` count the recoveries and emit a single end-of-read `INFO` summary (e.g. "ROM read complete: … — N block(s) re-requested … all recovered (ROM is byte-perfect)"). A block that exhausts all of its retry attempts still raises `FlashError` — the real failure signal is unchanged. No behaviour/timing change to the read itself.
- **WiCAN read default STmin 3 → 0 (3× faster reads, hardware-validated)** — With TCP_NODELAY now on by default, the CAN→TCP buffer overflow that previously forced STmin pacing is gone (a 2026-06-21 live-ECU sweep read STmin=0 with only ~1/48 dropped blocks, all recovered by the N_Cr fast-fail + idempotent per-block retry). `DEFAULT_RX_STMIN` is now `0`: a full authenticated 1 MB ROM read dropped from ~948 s to **338.7 s (~3.0 KB/s)**. A read remains idempotent, so the worst case for a too-aggressive STmin on a degraded link is a slower read (re-requests), never corruption. Measurement also pinned the remaining wall: the ECU rejects `ReadMemoryByAddress` sizes > 0x400 (NRC 0x31), and the ~294 ms/block fixed WiFi-round-trip overhead (×1024 ≈ 300 s) caps the SLCAN approach — reaching J2534 parity (~60 s) requires the firmware path (see `.claude/plans/wican-read-speed-goal.md`).
- **TCM read-only documentation & checksum guard-note** — README now documents TCM ROM read support (read/inspect only — no TCM flashing) plus the V2 defs and example dump. Added an ECU-only guard-note to `correct_rom_checksums()` clarifying it must never run on a TCM ROM (the TCM needs its own, not-yet-implemented checksum routine — see #72). No behavior change; `correct_rom_checksums()` was already ECU-flash-only and never touches TCM ROMs.

### Fixed
- **DTC Activation Flags toggle switch crashed on every flip — `cell_changed` emitted the table address instead of the Table object** — Flipping a DTC-flag toggle (a 1-D toggle-category table, e.g. `P0222`/`P0122`) raised `AttributeError: 'str' object has no attribute 'address'` in both the modification tracker (`table_viewer._on_cell_changed_track_modifications`) and the undo/redo recorder (`main._on_table_cell_changed → table_undo_manager.record_cell_change`). The toggle handler `_on_toggle_changed` emitted `cell_changed` with `current_table.address` (a `str`), while every other edit path — and every one of the three `cell_changed` consumers — passes/expects the **Table object** and reads `table.address`/`table.name` itself. Pre-existing since the toggle feature shipped (commit `05ebbeb`, 2026-02-07); surfaced now that the consumers always dereference `.address`. Fixed by emitting `self._ctx.current_table`, matching the normal cell-edit emit in `editing.py`. Tests: `tests/test_table_viewer_window.py::TestSignalForwarding::test_toggle_emits_table_object_not_address` drives the real toggle handler (the prior signal-forwarding tests emitted manually, so they never exercised this path).
- **ECU operations crashed / threw cross-thread Qt warnings on completion — `finished`/`error` handlers ran on the worker thread** — `_start_flash` connected `worker.finished`/`worker.error` to **bare lambdas** with `Qt.QueuedConnection`. A bare lambda has no receiver QObject, so Qt ran the slot in the **worker** thread instead of the GUI thread; `_on_flash_finished` then mutated widgets (`setVisible`, the completion `QMessageBox`, repaint) off the GUI thread, emitting `QObject::setParent: Cannot set parent, new parent is in a different thread` and intermittently crashing on a cross-thread paint (`QBackingStore::endPaint() called with active painter`). Seen after a dynamic flash **and** a RAM scan — both of which had actually completed on the ECU; the failure was purely in the post-operation UI. Fixed by connecting **bound methods** of the window (`_on_worker_finished` / `_on_worker_error`): the window is a GUI-thread QObject, so the queued slot is delivered to the GUI thread (the same reason `progress → self._on_flash_progress` was always safe). The handlers read the stored `_flash_thread`/`_flash_worker`. Root-caused by the new Qt diagnostics below — the captured Python stack pointed straight at `_on_flash_finished → _btn_done.setVisible(True)`. Tests: `tests/test_ecu_window_flash_driver.py::TestWorkerFinishedHandlers`.
- **WiCAN ROM read aborted near completion on a brief link stall — more idempotent retries + backoff** — A live 1 MB read over WiCAN failed at block ~957/1024 (offset `0x0EF400`) after only 4 back-to-back `ReadMemoryByAddress` drops at one offset (ISO-TP consecutive-frame N_Cr timeout), discarding ~5 minutes of work to a sub-second transient (WiFi roam / interference burst / momentarily wedged gateway). A single dropped frame already fails one attempt fast by design, but four *consecutive* drops mean a brief stall, not a dead link. `READ_BLOCK_RETRIES` is raised 4 → 8 and each retry is now spaced by a short, growing backoff (`0.2 s × attempt`, capped at 1 s) so the stall clears before re-requesting — only the (rare) failing block ever waits, and never after the final attempt. Reads are idempotent, so extra attempts can never change ECU state; a clean J2534/WiCAN block still returns on attempt 1 with no wait. The flash/write path is untouched (it must never resend a block). Tests: `tests/test_ecu_flash_manager.py::TestReadBlockRetry` (backoff grows between attempts, never after the last; recovery still flushes between attempts).
- **Spurious "Invalid state transition blocked: idle → authenticating" error during WiCAN SD flash** — `WiCANSdFlasher._authenticate_ecu` called `FlashManager._authenticate()` straight from the `IDLE` state, which the state machine rejects (only `CONNECTING → AUTHENTICATING` is valid), logging a red-herring `ERROR` on every SD flash even though auth then proceeded. It now calls `_connect()` first (borrowed mode → Tester Present + `IDLE → CONNECTING`), exactly like the read path, so the transition is valid, the log is clean, and the ECU gets a liveness check before auth. No behaviour change to a successful flash.
- **WiCAN WRITE/flash failed on the first `TransferData` block — outbound consecutive-frame pacing added** — A real FULL FLASH over the WiCAN failed: auth, security, and `RequestDownload` all succeeded, but the very first SBL `TransferData` (SID 0x36) timed out after 60 s ("Timed out waiting for response to SID 0x36"). Root cause (hardware-confirmed): on a write the **tool** sends the 1 KB block as an ISO-TP First Frame + ~146 Consecutive Frames, and the NC ECU's Flow Control advertises **BS=0, STmin=0** ("send everything back-to-back") — so the unpaced burst overruns the WiCAN gateway's TCP→CAN forwarding buffer, a frame is dropped *inside the gateway*, the ECU's reassembly never completes, and it never answers. This is the exact mirror of the receive-side overflow `rx_stmin` already guards (reads work because the ECU is the multi-frame *sender* and the host has N_Cr fast-fail + idempotent retry; writes have no mid-stream resend by design). Fix: `IsoTpSession` gains a `tx_stmin` outbound STmin **floor** applied as `max(peer_stmin, floor)` between our Consecutive Frames; `WiCANTransport` defaults it to **3 ms** (`DEFAULT_TX_STMIN`). It only changes inter-frame *timing* within one message — never the payload, never a resend, never a block split — so the no-mid-stream-resend (anti-brick) invariant is preserved; J2534 and reads are unaffected (default floor 0 / single-frame requests). **Hardware-validated at the block level:** with the 3 ms floor a paced 147-frame SBL block ACKed cleanly in 638 ms on the live MX-5 NC ECU (`tools/wican_flash_diag.py`, pre-erase diagnostic). Full-1 MB flash completion over WiFi (no resend across ~150k frames) is the remaining open validation; SD-staging (Option B) stays the robust long-term path. Tests: `tests/test_ecu_isotp.py::TestOutboundTxStminFloor` (floor paces at STmin=0, peer wins when larger, default 0 unchanged, applies regardless of `honor_peer_fc`, payload byte-identical) + `tests/test_ecu_wican_transport.py` (floor wired into the session). See `docs/internal/WICAN_TRANSPORT.md` §6.
- **WiCAN RAM scan aborted on a single dropped frame** — `FlashManager.scan_ram` read each RAM page with a bare `read_memory_by_address` (no retry), so over the lossy WiCAN/WiFi link one dropped consecutive frame failed the whole 48 KB scan (caught on the real ECU: the scan died at page 53/192 with an N_Cr timeout). It now uses the same idempotent `_read_block_with_retry` as `read_rom` — a dropped page is re-requested (reads never change ECU state), flushing stale frames between attempts. Confirmed on the live MX-5 NC ECU over WiCAN: a full 48 KB RAM dump completes, transparently recovering the occasional dropped page. The J2534 (reliable) path is unaffected — the first attempt always succeeds, so it stays a straight passthrough.

### Removed
- **Legacy V1 TCM definition** — Removed `examples/metadata/lfg1tf000.xml` (superseded by `LFG1TF000_v02.xml`), resolving ambiguous detection.

## [v2.8.0] - 2026-05-31

### Added
- **Calibration mismatch warning before dynamic flash (#68)** — If the ROM being flashed has a different calibration ID than the last ROM written to the ECU (per the archive), a warning dialog is shown before proceeding. Prevents accidentally bricking the ECU by dynamic-flashing a ROM from the wrong project. User can still force-flash with confirmation.

## [v2.7.2] - 2026-05-27 - 2026-04-07

### Fixed
- **Paste silently dropped out-of-range cells** — `TableClipboardHelper.paste_selection` clamped pasted values against the XML-declared scaling `min`/`max`, silently skipping any cell outside that range. This broke copy between sibling tables when the source held raw bytes exceeding the stated max (e.g. `VCT Target` → `[Flex] VCT Target` with `35`s against a `max=25` scaling), and fully disabled paste for tables whose scaling had placeholder `min=0/max=0` (e.g. `Speed Density - Volumetric Efficiency`). Removed the clamp — `display_to_raw` is the real safety net. Added two regression tests.

## [v2.7.0] - 2026-04-05

### Added
- **Comparison Copy All** — Two new buttons in the compare window table headers (`→→|` and `|←←`) copy all eligible differing tables between ROMs in one operation, with confirmation dialog, progress bar, and cancellation support
- **Workspace directory** — Single configurable root directory for all user content (ROMs, projects, metadata, exports, screenshots, colormaps, ECU reads). All path settings derive defaults from the workspace root, with individual overrides still supported. First-run migration copies bundled metadata and colormaps into the workspace.
- **Settings dialog redesign** — Replaced tab-based settings with tree sidebar navigation, stacked pages, and instant search with highlighted results. Data-driven `SettingDescriptor` registry makes adding new settings a one-line change.
- **New path settings** — `ROMs Directory`, `Screenshots Directory`, and `Reads Directory` settings with workspace-derived defaults. File dialogs (Open ROM, Save As, Screenshot, Project Wizard) now default to the appropriate workspace subdirectory.
- **MCP STDIO launcher for compiled builds** — `packaging/run-mcp.bat` enables Claude Desktop integration with installed NCFlash via STDIO transport; included in the Windows installer
- **4 new MCP command API endpoints** — `/api/rom-info`, `/api/list-tables`, `/api/table-statistics`, `/api/compare-tables` — all served by the app, enabling MCP tools to work with any ROM the app can open
- **Code audit documentation** — `docs/internal/CODE_AUDIT.md` captures full codebase audit findings (bugs, dead code, duplication, test gaps) from the v2.6.1 audit pass
- **UI test coverage** — 70 new tests covering compare_window diff computation, table_browser filtering/search/selection, graph_viewer color calculations, and table_viewer_window signal forwarding and coordinate extraction
- **Interpolation regression tests** — 24 new tests for extracted pure functions `compute_interpolated_1d_values()` and `compute_interpolated_2d_values()`, including dedicated auto-round regression tests that catch the `round_one_level_coarser` bug

### Changed
- **Settings dialog height** — Reduced default height from 700 to 640 pixels
- **CI: upgrade GitHub Actions to Node.js 24** — Bumped `actions/checkout` v4→v5 and `actions/setup-python` v5→v6 in CI and release workflows to resolve Node.js 20 deprecation warnings
- **Version diff reads snapshots directly** — Eliminated unnecessary temp file round-trip when comparing ROM versions in History Viewer; snapshot `.bin` files are now passed directly to `RomReader`
- **ROM reader log level** — Downgraded ROM initialization log messages from INFO to DEBUG to reduce log noise
- **MCP connection dialog** — Now shows correct `run-mcp.bat` path for Claude Desktop config (dev and compiled builds) instead of broken inline Python command
- **MCP single source of truth** — All MCP tools now delegate to the running NC Flash app via its command API. Removed standalone ROM detection and definition loading from the MCP server — the app is the single source of truth for ROM definitions and table data. Fixes MCP tools failing for ROMs whose definition XML wasn't bundled with the MCP server (e.g., LF4XEG)
- **Architectural refactoring** — Four phases of structural cleanup with no behavior changes:
  - Unified table CSS into shared `get_table_stylesheet()` function, eliminating 3 duplicate stylesheet blocks
  - Replaced null-byte `\0` composite keys with `TableKey` namedtuple for type-safe dict keys across undo/change tracking
  - Extracted shared edit pipeline (`_apply_external_cell_edits`, `_apply_external_axis_edits`, `_capture_table_originals`) — compare-copy and MCP edit now share one code path instead of three duplicated copies
  - Simplified 4-hop signal chain to 2 hops — `TableViewer` now emits `Table` objects directly, removing 4 forwarding signals and methods from `TableViewerWindow`
- **`.gitignore` cleanup** — Added `tests/gui/debug_*.txt` pattern and lowercase `thinking-pad.md` to `.gitignore`; removed 6 one-off debug GUI test scripts
- **Coverage is now opt-in** — Removed `--cov` flags from `pytest.ini` addopts so test runs are faster by default. Run `pytest --cov=src --cov-report=term-missing` when coverage is needed
- **Interpolation log level reduced** — Interpolation success messages (horizontal, vertical, 2D bilinear) downgraded from `info` to `debug` to reduce log noise during normal editing
- **README feature list updated** — Added interleaved 3D tables, column visibility, round key, ECU Programming window, and other v2.6.1 features to the README
- **ECU reads path now configurable** — Replaced hardcoded `~/.nc-flash/reads/` in ECU window with settings-based `get_reads_directory()`, defaulting to workspace/reads
- **Settings path getters deduplicated** — Extracted `_get_workspace_path()` helper in `AppSettings`, eliminating 7 near-identical getter methods with inline imports

### Fixed
- **CI: fix hanging IPC server tests** — `TestMainWindowIpcServer` created a full `MainWindow` that triggered modal dialogs in CI, hanging the pipeline at 83%. Replaced with lightweight `_IpcTestWidget` that tests only IPC logic
- **MCP `write_table` param validation** — Changed `cells: list[dict]` to `cells: list` in server tool signature to fix FastMCP Pydantic rejection of valid JSON arrays (`-32602: Invalid request parameters`)
- **MCP table name whitespace mismatch** — Added stripped-name fallback in `_build_name_cache` and `.strip()` on API input boundaries so table names with trailing whitespace (from XML definitions) resolve correctly across all MCP tools
- **Deduplicate `_auto_save_rom` and `_auto_save_ram_dump`** — Extracted shared logic into `_auto_save_to_reads_dir` in `ecu_window.py`; both methods now delegate to the common helper
- **Stale README version and project structure** — Updated version from v2.3.0 to v2.6.1, added missing `src/ecu/` module tree (13 files) and new UI files (`ecu_window.py`, `flash_mixin.py`, `flash_setup_dialog.py`, `patch_dialog.py`), and refreshed the development status description
- **Select All skips first data row in 3D tables** — `select_all_data` started selection at row 2 instead of row 1, missing the first data row in 3D tables
- **display_to_raw bypasses `^` to `**` expression conversion** — `display_to_raw` and `_axis_display_to_raw` called `simple_eval` directly on scaling `frexpr` without converting calculator-style `^` exponentiation to Python `**`. Now delegates to `ScalingConverter.from_display()` which handles the conversion
- **Compare window cleanup for version comparisons** — CompareWindow `closeEvent` now clears both `compare_window` and `_compare_window` attributes on the parent, fixing a leak where history-viewer comparisons were never cleaned up due to an attribute name mismatch
- **Redundant x-axis read in interleaved 3D tables** — `_read_interleaved_3d()` read x-axis data twice; removed the duplicate read inside the scaling branch since the unconditional read above already populated `x_raw`
- **Deduplicate `handle_rom_operation_error`** — Extracted the duplicated error handler from `main.py` and `project_mixin.py` into a shared `src/ui/error_helpers.py` module, eliminating code duplication
- **Orange selection CSS inconsistency** — `display.py` helper had an orange selection style that was never applied; replaced with the blue selection style used by the actual code path
- **Inline `Path` re-import in `main.py`** — `_find_document_by_rom_path` redundantly imported `Path as _Path`; now uses the module-level `Path` import
- **Stale `run-mcp.bat` reference** — MCP connection info dialog referenced a non-existent batch file; now shows the actual `python -m src.mcp.server` command
- **test_runner set_level_filter bug** — `set_level_filter()` accessed non-existent `self.main_window.table_browser`; now correctly retrieves the table browser from the current ROM document via `get_current_document()`
- **Interpolation auto-round destroys precision** — Vertical, horizontal, and 2D interpolation used `round_one_level_coarser()` (the "Round Selection" function) when auto-round was enabled, coarsening values by one decimal level (e.g., 2.04 → 2.0, 0.01 → 0.0). Now uses `round(val, precision)` to preserve the format's full decimal precision, matching smoothing's correct behavior
- **MCP workspace.json invisible to compiled builds** — App wrote `workspace.json` to `get_app_root()` which resolves to a per-process `_MEIPASS` temp directory in PyInstaller builds. The MCP server subprocess got a different temp dir and could never find the file. Moved to `get_user_data_dir()` (`%APPDATA%/NCFlash`) via new `get_workspace_path()` helper in `paths.py`

### Removed
- **Dead `GraphViewer` class** — Standalone graph window class in `graph_viewer.py` was never imported; removed along with its `matplotlib.pyplot` import and `APP_NAME` constant
- **Dead `_apply_table_style` method** — Unused delegation method in `table_viewer.py` that was superseded by `_apply_table_style_internal`
- **Dead code removed** — Unused `apply_table_style()` method in `display.py`
- **Trivial `_make_icon`/`_make_toolbar_icon` wrappers** — Removed pass-through methods in `MainWindow` and `TableViewerWindow` that simply delegated to `make_icon()`; callers now invoke `make_icon()` directly
- **Dead flash mixin code** — Removed ~475 lines of dead code from `flash_mixin.py` (`_FlashWorker`, `FlashProgressDialog`, `_on_flash_rom`, `_on_read_rom`, `_on_read_rom_finished`, `_on_clear_dtcs`, `_on_ecu_info`, `_run_flash_operation`) superseded by `ecu_window.py`

## [v2.6.1] - 2026-04-03

### Added
- **Table browser column visibility setting** — New checkboxes in Settings > Appearance > Table Browser to show/hide the Type and Address columns (both shown by default)
- **Screenshot buttons (F12)** — Camera toolbar button and menu entry in both the main window (Tools > Screenshot) and table viewer window (File > Screenshot). Captures the window as PNG via save dialog with auto-generated filename
- **J2534 device layer tests (#54)** — 53 tests covering message construction, all 26 error codes, ISO-TP filter setup, read/write, open/close, and connect/disconnect
- **Security stub tests (#55)** — 5 always-run CI tests verifying stub raises `SecureModuleNotAvailable` and flash operations are blocked when the private module is absent
- **Flash abort scenario tests (#56)** — 14 tests covering abort during SBL upload, ROM transfer, pre-transfer phase, connection drops, and cleanup failures
- **.bin file association in installer** — Optional checkbox (unchecked by default) to associate `.bin` files with NC Flash during installation. Sets file type, icon, and open command; cleaned up on uninstall
- **Single-instance support** — Double-clicking a `.bin` file when NC Flash is already running opens the ROM in the existing window instead of launching a second instance. Uses QLocalServer/QLocalSocket IPC
- **Command-line file argument** — `NCFlash.exe file.bin` opens the specified ROM on launch

### Changed
- **Tab bar spans full window width** — ROM tabs now sit above the splitter instead of inside the left pane, giving long filenames room to display without truncation. Tabs no longer elide text; scroll buttons appear when tabs exceed window width
- **Table browser columns auto-sized** — Type and Address columns are now fixed-width (compact), Name column stretches to fill available space. Resizing the splitter automatically adjusts the Name column width
- **Splitter position persisted** — The main splitter between table browser and activity log now saves/restores its position across sessions

### Fixed
- **CI pipeline failures** — Added missing `pytest-qt` dependency to `requirements-dev.txt` (single-instance IPC tests use `qtbot` fixture) and reformatted 12 files with black
- **Inconsistent selection highlight on Type/Address columns** — Empty cells in Type and Address columns showed a pale gray instead of matching the Name column's selection highlight. Custom delegate now paints consistent backgrounds for all cells
- **Search highlight bold causes text overlap in table browser** — Removed bold font from search match highlighting; the yellow background is sufficient and bold caused width miscalculation that squashed adjacent characters
- **DTC read failure crashes ECU info worker (#52)** — ReadDTCByStatus (SID 0x18) NRC 0x22 "Conditions not correct" now returns empty results gracefully instead of raising. DTC read failures no longer discard already-read VIN and ROM ID in the flash setup dialog and ECU info view
- **Smoothing snaps values to coarse increments** — Smoothing used `round_one_level_coarser` which reduced precision by one decimal level (e.g. 2.03 → 2.0 for `.2f` tables). Now rounds to the format's native precision instead
- **Interleaved 3D read has no bounds checking (#57)** — `_read_interleaved_3d()` now validates M/N are non-zero and total table footprint fits in ROM before any data access. Corrupt ROMs raise `RomReadError` with clear diagnostics instead of crashing
- **Windows installer build fails on Inno Setup 6.7** — `ChangesAssociations=askifneeded` is not a valid value; changed to `yes` (associations are already gated by the optional task checkbox)
- **Interleaved 3D write can overflow ROM bounds (#58)** — `write_table_data()` interleaved branch now validates entire write footprint fits in ROM and rejects multi-byte storage types incompatible with interleaved stride
- **Integer overflow in scaling conversion (#59)** — All three write methods (`write_table_data`, `write_cell_value`, `write_axis_value`) now validate integer values against storage type bounds before `struct.pack()`. Values outside the valid range raise `RomWriteError` instead of crashing or silently wrapping
- **Cell write index not validated (#60)** — `write_cell_value()` validates row/col against table dimensions and `write_axis_value()` validates index against axis length before computing addresses. Out-of-bounds indices raise `RomWriteError` instead of silently corrupting neighboring tables
- **Project file writes not atomic (#61)** — ROM snapshot copies, working ROM overwrites on revert, and project creation copies now use atomic tmp+fsync+rename pattern. Crash during save no longer corrupts ROM snapshots or working files

## [v2.5.0] - 2026-04-01

### Added
- **Clear DTCs from read dialog (#33)** — After reading DTCs, the results dialog now shows a "Clear DTCs" button alongside OK, allowing immediate clearing without navigating to a separate action
- **Scan RAM button in ECU window** — Reads ECU RAM at 0xFFFF0000–0xFFFFBFFF (192 pages of 0x100 bytes, 48 KB) via UDS and saves the dump to `~/.nc-flash/reads/`. Uses the existing session, shows page-by-page progress, and supports abort. Based on romdrop's `uds_ScanRAM`

### Fixed
- **Compiled version opens a second blank window for MCP server (#41)** — In PyInstaller builds, `sys.executable` points to the app exe, so spawning the MCP server via `python -m src.mcp.server` re-launched the entire GUI. Now uses an `NCFLASH_MCP_MODE` environment variable to bypass the GUI and run only the MCP server, plus suppresses window creation on Windows
- **DTC toggle switch not showing on Windows 10 (#32)** — Window auto-sizing was based on the hidden table widget's tiny 1-cell dimensions, leaving no room for the toggle container. Now sizes from the toggle's own size hint when in toggle mode
- **DTC toggle animates on window open** — Toggle switch now snaps to its initial position immediately instead of visually sliding into place when the window opens
- **Tables with `%d` or `%x` format display as `0.00` after editing** — `format_value()` failed on integer/hex format specifiers because Python's `d`/`x` formats reject floats. Now converts to `int` first. Affects 176 scalings using `%d` and 3 using `%08x`
- **ROM comparison sidebar too narrow** — Sidebar max width increased from 300px to 600px so long table names are not clipped

### Changed
- **Remove RomDrop references from UI (#39)** — About dialog and README now say "Native ECU flashing via J2534/UDS" instead of referencing RomDrop, reflecting the current native flashing support
- Toggle switch shows a pointing-hand cursor on hover for better click affordance
- Toggle switch clears its background before painting for consistent rendering across Windows versions

## [v2.4.1] - 2026-03-29

### Fixed
- **P0601/P0606 after flashing with NC Flash** — Checksum table offset was 0xFF658 instead of the correct 0xFF650 (8-byte misalignment), causing every entry to be misread and all 35 checksums to be overwritten with garbage before flashing. Additionally, the end address in each entry is inclusive (last byte) but was treated as exclusive, producing off-by-one sums. Verified against romdrop.exe disassembly and validated on real ROM

## [v2.4.0] - 2026-03-28

### Added
- **Round Selection (R key)** — New operation to round selected cells one decimal level coarser based on the scaling format. Press repeatedly: 12.11 → 12.1 → 12.0. Works on both data and axis cells
- **Auto-round setting** — New checkbox in Settings > Editor to automatically round interpolation and smoothing results one decimal level coarser than the table's display format

### Fixed
- **Save As breaks future saves and edits** — After using Save As, the internal ROM path was not updated, causing all subsequent table opens and saves to fail with "No document found for rom_path=..." (#34)
- **DTC codes don't match RomDrop** — Live DTC reading returned garbage codes (e.g. P03C1 instead of C0121) due to two bugs: the KWP2000 response count byte was not skipped, misaligning all DTC parsing; and chassis codes (C-codes) used standard OBD-II keys (0x4xxx) instead of Mazda NC's actual encoding (0xCxxx)

## [v2.3.3] - 2026-03-28

### Fixed
- **Broken UI on Windows dark theme** — Hardcoded light-theme colors clashed with Windows dark mode system palette, causing unreadable text and selection highlights. App now forces light color scheme via `Qt.ColorScheme.Light`

## [v2.3.2] - 2026-03-28

## [v2.3.2] - 2026-03-28

### Fixed
- **PermissionError when installed for all users** — Session logs and auto-saved ROM reads were written to the app install directory (`Path(__file__).parent`), which is read-only under `C:\\Program Files`. Both now write to `~/.nc-flash/` (logs → `~/.nc-flash/logs/`, reads → `~/.nc-flash/reads/`)

## [v2.3.1] - 2026-03-27

### Fixed
- **Battery voltage warning too severe for Read ROM** — Read ROM now shows a softer "communication timeouts" warning instead of the "bricking" language used for flash operations, since a failed read is safely retryable (#21)

## [v2.3.0] - 2026-03-26

### Added
- **Native ECU flashing** — Full J2534/UDS flash module replacing RomDrop integration. Read and write ECU ROMs directly via Tactrix OpenPort 2.0
- **Drag-and-drop ROM files** — Drag `.bin` or `.rom` files onto the main window to open them. Visual overlay indicates the drop zone during drag-over. Invalid file types are rejected with a descriptive error message (#20)
- **ECU Programming window** — Dedicated window (Tools > ECU Programming) replacing scattered ECU menu items. Auto-connects, shows battery voltage/engine RPM/ECU info in status cards, one-click flash with dynamic/full auto-detection, inline progress, auto-save ROM reads
- **ECU Connect/Disconnect** — New menu actions in ECU menu to establish and hold a persistent J2534 connection. Operations reuse the open device instead of reconnecting each time. Status bar shows real connection state
- **OBD-II PID reading** — Battery voltage (PID 0x42) and engine RPM (PID 0x0C) via standard OBD-II Service 0x01
- **J2534 32-bit bridge** — Subprocess bridge for 64-bit Python to talk to 32-bit J2534 DLLs, with auto-build in dev mode
- **Per-session log files** — Each app launch saves a complete log to `~/.nc-flash/logs/` directory
- **UDS log direction prefixes** — Protocol log messages now show `ECU >>` or `Tool >>` to indicate who is speaking
- **Window geometry persistence** — Main window remembers its position and size between sessions
- **CI: private _secure module** — CI and release workflows now pull the private `nc-flash-secure` repo so security tests run and release builds include the secure module

### Changed
- **Patch ROM dialog** — Replaced sequential file-dialog chain with a single all-in-one dialog showing stock ROM, patch file, and output path fields with inline results after patching
- **Checksum optimization** — 67x faster ROM checksum calculation using struct.unpack batch decoding
- **"ROMs are identical" is no longer an error** — Dynamic flash with no differences shows "Nothing to flash" in grey instead of a red error with traceback

### Fixed
- **J2534 bridge not loading in built exe** — PyInstaller frozen builds threw a different OSError than expected, bypassing the 32-bit bridge fallback. The DLL loader now detects both native bitness mismatch and PyInstaller's frozen-app errors
- **J2534 bridge exe not found in built app** — PyInstaller puts data files in `_internal/` (sys._MEIPASS) but bridge lookup only searched next to the exe
- **J2534 bridge console window visible** — The 32-bit bridge subprocess no longer opens a visible cmd window on Windows
- **DTC count discrepancy** — Activity log showed raw DTC count (with duplicates) while UI showed deduplicated count. Log now shows both (e.g., "Read 15 DTCs (7 unique)")
- **Tester Present log spam** — Keepalive messages demoted from INFO to DEBUG level
- **Checksum bounds checking** — Invalid checksum table entries (out-of-bounds addresses) no longer crash the flash process

## [v2.2.0] - 2026-03-23

### Added
- **Interleaved 3D table support** — TCM-style ROMs that store Y-axis values interleaved with data rows are now fully supported. Read, bulk write, single-cell edit, and Y-axis edit all handle the interleaved layout. Enabled via `layout="interleaved"` attribute in XML definitions

## [v2.1.1] - 2026-03-16

### Fixed
- **Settings dialog crash on fresh install** — Clicking Settings did nothing on release builds because the ECU tab imported `src.ecu.flash_manager` which doesn't exist without the ECU module. The import now fails early and the ECU tab is gracefully skipped (#16)
- **Version mismatch in About dialog** — Release builds showed `v2.0.0` regardless of the git tag. The release pipeline now stamps `APP_VERSION` from the tag before building (#16)

## [v2.1.0] - 2026-03-05

### Changed
- **Extracted shared icon factory** — Moved QPainter toolbar icons from `main.py` (143 lines) and `table_viewer_window.py` (102 lines) into `src/ui/icons.py` with dispatch table
- **Consolidated duplicated format utilities** — Created `src/utils/formatting.py` with shared `printf_to_python_format`, `format_value`, `get_scaling_range`, `get_scaling_format` (was duplicated 3-4x across modules)
- **Unified interpolation functions** — Merged near-identical `interpolate_vertical`/`interpolate_horizontal` (~250 lines each) into shared `_interpolate_1d(direction)` with extracted helpers
- **Extracted MCP mixin** — Moved MCP server management (6 methods), command API bridge (3 methods), and API handlers (4 methods) from `main.py` into `src/ui/mcp_mixin.py`. `main.py` reduced from 2,606 to 1,970 lines
- **Refactored test_runner command dispatch** — Replaced 159-line if/elif chain with dispatch table + small handler methods
- **Separated dev dependencies** — Split `requirements.txt` into runtime-only + `requirements-dev.txt` for pytest/black/flake8
- **Cleaned up compare_window.py** — Consolidated 3 color helpers into shared `_gradient_color`, moved `_all_nan` and `_get_axis_format` to `formatting.py`, eliminated inline ratio computation in 3D populate
- **Updated README** — Fixed Python version (3.10+ not 3.12+), removed stale "In Development" / "Next Priorities" sections, updated project structure tree to reflect all current files
- **Archived abandoned design docs** — Moved `MODIFICATION_TRACKING_PLAN.md` and `SUMMARY.md` to `docs/archive/` (described never-built SQLite design)
- **Updated ROM comparison spec** — Marked implemented "Out of Scope" items (cross-definition compare, copy-table editing)

### Fixed
- **Latent API import bug** — `main.py` API handlers imported renamed `_printf_to_python_format` from `rom_context.py` (would fail at runtime); now imports from `src.utils.formatting`
- **Horizontal interpolation emit timing** — Was emitting changes per selection range instead of once after all ranges (matching vertical behavior)
- **Silent exception swallows** — Three `except: pass` blocks in `main.py` now log with `logger.debug`
- **Exception chaining** — `project_manager.create_project` now chains exceptions with `from e`
- **Test fix** — `test_get_table_font_size_default` updated to match actual default (11, not 9)

### Removed
- Dead code cleanup: 4 unused dataclasses from `version_models.py`, legacy `ScalingEditDialog`, unused `HistoryPanel`, 4 deprecated methods across `table_viewer.py`, `change_tracker.py`, `table_browser.py`

## [v2.0.0] - 2026-03-02

### Changed
- **Rebranded from "NC ROM Editor" to "NC Flash"** — App name, exe name, installer, asset filenames, QSettings keys, user data directory, MCP server name, all documentation, and GitHub URLs updated. Exe is now `NCFlash.exe`, installer outputs `NCFlash-{version}-Setup.exe`, user data moves to `%APPDATA%/NCFlash`. GitHub repo is now `cdufresne81/nc-flash`
- **Settings reorganization** — Moved Metadata Directory setting from General > Paths to Tools > RomDrop group, alongside the RomDrop executable path

## [v1.6.0] - 2026-03-01

### Added
- **Tuning log** — Every commit auto-generates a `TUNING_LOG.md` entry with version name, description, table change summary with direction indicators, and a "Results" section to fill in after testing
- **Revert to version** — Restore a previous ROM snapshot as the working file. Newer versions are soft-deleted. Available from the History viewer
- **Soft delete versions** — Remove bad snapshots by moving them to `_trash/`. Deleted versions are hidden in history (toggleable with "Show deleted" checkbox)
- **Version History toolbar button** — Clock icon in the toolbar, enabled when a project is open
- **Read-only version comparison** — Double-click a table in History or click "Compare Versions..." to open a side-by-side comparison (reuses ROM Compare window with copy buttons hidden)
- **Window geometry persistence** — History viewer and compare window remember their size, splitter position, and column widths across sessions
- **37 new tests** for project management: tuning log generation, soft delete, revert, commit flow, backward compatibility

### Changed
- **Mandatory version names** — Every commit now requires a version name (e.g., "egr_delete") and always creates a named ROM snapshot. The snapshot checkbox and optional suffix have been replaced with a single required field
- **Simplified working ROM naming** — Working file is now `{ROMID}.bin` instead of `v1_{ROMID}_working.bin`
- **Projects always enabled** — Removed `--enable-projects` feature flag. Project menu items (New Project, Commit Changes, Commit History) are always visible
- **Commit dialog redesigned** — Version name field (required, auto-sanitized), filename preview, optional description. Removed snapshot checkbox and QuickCommitDialog
- **History viewer columns** — Replaced Version + Message columns with a single Snapshot column showing the filename
- **Commit author defaults to system user** — Uses `os.getlogin()` instead of hardcoded "User"

### Fixed
- **Commit clears modified flag** — Committing no longer leaves the document marked as modified, preventing a spurious "unsaved changes" prompt on close
- **Commit message line breaks** — Multi-line commit messages now render correctly in the history details panel

### Removed
- `--enable-projects` feature flag — projects are now a core feature
- `last_suffix` and `settings` fields from Project model (dead code)
- `QuickCommitDialog` class (unused)

## [v1.5.0] - 2026-03-01

### Added
- **RomDrop setup wizard** — First-run wizard now asks for the RomDrop installation folder (not just a definitions directory). Step 1 selects the folder, Step 2 confirms derived paths for `romdrop.exe` and `metadata/` with green/red validation indicators. Both paths are editable for non-standard layouts
- **Configurable CSV export directory** — New "Export Directory" setting in Settings > General lets you choose a default folder for CSV exports (Ctrl+E). Leave empty to keep the default behavior (exports next to the ROM file)

### Changed
- **"Definitions" renamed to "Metadata"** — All UI labels, settings keys, CLI flags, and log messages now use "metadata" instead of "definitions" to match RomDrop's naming convention. Settings key changed from `paths/definitions_directory` to `paths/metadata_directory`. MCP server flag changed from `--definitions-dir` to `--metadata-dir`
- **Bundled XML files moved to examples/metadata/** — The `definitions/` directory has been restructured to `examples/metadata/` since it contains example/bundled data
- **README updated for Linux** — Installation section now documents Linux `.tar.gz` download alongside Windows
- **Project structure reorganized** — Moved build/packaging files (`build.bat`, `installer.iss`, `NCFlash.spec`, `requirements-build.txt`) into `packaging/` directory; moved `WINDOWS_SETUP.md` into `docs/`

### Fixed
- **"Modified only" filter now expands categories** — Toggling the "Modified only" checkbox in the table browser auto-expands categories with modified tables, matching search filter behavior
- **run.sh argument passthrough** — Linux/macOS launcher now passes CLI arguments (`"$@"`) to `main.py`, matching `run.bat` parity

## [v1.4.2] - 2026-03-01

### Added
- **Linux build in release pipeline** — Release workflow now builds a `NCFlash-{version}-linux-x86_64.tar.gz` package alongside the Windows installer
- **Cross-platform PyInstaller spec** — `NCFlash.spec` detects the OS and sets the icon accordingly (`.ico` on Windows, skipped on Linux)

### Changed
- **CI matrix optimized** — Reduced from 9 jobs (3 OS x 3 Python) to 4 jobs (Ubuntu 3.10+3.12, Windows 3.12, macOS 3.12). Cuts macOS billing from ~60 to ~20 minutes per run.
- **NumPy version relaxed** — Lower bound `numpy>=2.4.0` → `numpy>=2.2.0` so Python 3.10 and 3.11 can install dependencies

### Fixed
- **CI pipeline failures** — Fixed `black --check` failing on 63 unformatted files and `numpy>=2.4.0` blocking Python 3.10/3.11 installs
- **Linux CI crashes** — Install `libegl1` and set `QT_QPA_PLATFORM=offscreen` for headless PySide6 on GitHub Actions runners
- **Test port conflict** — Command server tests now use a dedicated port (18766) to avoid conflicts with a running app instance

## [v1.4.1] - 2026-03-01

### Fixed
- **MCP connection dialog** — Shows STDIO config for Claude Desktop, fixed missing `os` import

## [v1.4.0] - 2026-02-28

### Added
- **MCP server for AI assistant access** — Model Context Protocol server (`python -m src.mcp.server`) exposes 9 tools for ROM inspection and editing. Supports STDIO and SSE transports. Works with Claude Code, Claude Desktop, ChatGPT, and Gemini. LRU-cached ROM loading (4 entries).
- **AI write access to ROM tables** — New `write_table` MCP tool lets AI modify table values through the app's editing pipeline with full undo support. Changes appear in the app immediately and can be undone with Ctrl+Z.
- **Live table reading for AI** — New `read_live_table` and `list_modified_tables` MCP tools read current in-memory values (including unsaved edits) from the running app, instead of stale on-disk data.
- **Command API server** — Lightweight HTTP bridge (`src/api/command_server.py`) on port 8766 that routes MCP requests to the Qt main thread via queue + QTimer polling. Starts/stops automatically with the MCP server. No new dependencies.
- **Workspace state file for MCP auto-discovery** — App writes `workspace.json` listing open ROMs (path, xmlid, make/model/year, modified flag, active tab). MCP server reads it via new `get_workspace` tool so AI assistants can discover open ROMs without manual path entry. File is written on open/close/save and deleted on app exit.
- **MCP server toggle in app** — Start/stop the MCP server directly from the Tools menu or toolbar (broadcast antenna icon, green when running). Uses SSE transport on `http://127.0.0.1:8765/sse` so any MCP client can connect. Optional "Start MCP server on startup" setting in Settings > Tools. Server subprocess is automatically stopped on app exit.

## [v1.3.0] - 2026-02-28

### Added
- **Windows installer** — Inno Setup installer with Start Menu shortcut, optional Desktop shortcut, and uninstaller
- **PyInstaller packaging** — Standalone Windows exe build via `build.bat`, no Python required to run
- **Flash ROM to ECU** — One-click flash via RomDrop integration (`Ctrl+Shift+F`) with safety warning dialog
- **RomDrop settings** — Configurable RomDrop executable path in Settings > Tools
- **GitHub Actions release pipeline** — Automatically builds and publishes the installer on tagged releases
- **App icon** — Custom icon for the exe, taskbar, and installer

### Changed
- **Unified Open action** — Single "Open..." (`Ctrl+O`) replaces separate ROM/Project openers
- **Projects behind feature flag** — Project management UI hidden unless `--enable-projects` is passed

## [v1.2.0] - 2026-02-27

### Added
- **ROM comparison tool** — Side-by-side comparison of two ROMs (`Ctrl+Shift+D`) with change highlighting
- **Cross-definition comparison** — Compare ROMs with different ECU definitions (e.g., NC1 vs NC2)
- **Table viewer toolbar** — 12 quick-access buttons for editing, interpolation, and visualization
- **Main window toolbar** — Open, Save, Compare, Settings buttons with programmatic icons
- **Copy table between ROMs** — Copy table values from one ROM to another in compare view

### Fixed
- **Table viewer auto-sizing** — Fixed last row being clipped behind horizontal scrollbar
- **3D graph performance** — 45% faster initial render, 55% faster selection updates
- **Multi-ROM undo isolation** — Undo stacks no longer shared between ROMs with same definition

## [v1.1.0] - 2026-02-07

### Added
- **Per-table undo/redo** — Each table has its own undo stack
- **Bulk operation performance** — Single repaint for multi-cell operations
- **Min/max coloring from scaling definitions** — Instead of current data values
- **Uniform graph cell sizes** — Non-uniform axis values no longer cause thin edge cells

### Fixed
- **40 code audit findings remediated** — Security (XXE prevention), memory leaks, performance, error handling
- **Atomic file writes** — Prevents ROM corruption on crash
- **Paste uses bulk signal** — Single undo entry instead of N individual entries

## [v1.0.0] - 2026-01-16

### Added
- ROM file reading and writing for NC Miata ECUs
- Automatic ROM ID detection and XML definition matching
- 1D, 2D, and 3D table viewing with axis labels
- Cell editing with validation
- Interactive 3D surface plots and 2D line graphs
- Thermal color gradient with configurable colormaps
- Copy/paste, CSV export, clipboard support
- Interpolation (vertical, horizontal, bilinear) and smoothing
- Multi-ROM tabs with session restore
- Category-based table browser with search
