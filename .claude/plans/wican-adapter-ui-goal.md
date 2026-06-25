# /goal — WiCAN adapter-selector UI + settings (3rd phase — ACTIVE PLAN)

> **Prerequisite met.** Goal 2 (`wican-ecu-functions-goal.md`) landed: READ RAM / DTC / CLEAR DTC are
> hardware-confirmed over WiCAN and the host-driven WRITE safety layer (`WiCANFlasher`) is built. This
> goal **exposes all of it from the NC Flash UI** — adapter selection + WiCAN settings — so the user can
> run read/RAM/DTC/clear and (for the first time on hardware) flash over WiCAN from the app, not just the
> bench tools. Design below was fixed in a short grill-me (2026-06-21).

---

## STATUS
- ✅ Unblocked (goal 2 complete).
- ✅ Design resolved (grill-me 2026-06-21) — see **Decisions**.
- ⬜ Implementation not started.

## Decisions (grill-me outcomes — these are settled)

1. **Adapter choice = persistent setting.** Add **Settings ▸ ECU ▸ Adapter** dropdown (`J2534` /
   `WiCAN`, default `J2534`) plus a **Settings ▸ ECU ▸ WiCAN** page. The read/flash flow reads the saved
   adapter and builds the transport via the existing `create_ecu_transport({"kind": ...})` factory. No
   per-action chooser, no toolbar quick-switch (the latter is a possible later add, out of scope here).
2. **WiCAN fields = minimal.** Host (default `192.168.1.169`), Port (default `35000`), **Auto-configure
   adapter** toggle (switch device to SLCAN on connect + restore on disconnect via `WiCANConfigurator`,
   default ON), and a **Test Connection** button (pings firmware + reports link quality). Everything else
   (CAN tx/rx IDs, ISO-TP BS/STmin, connect timeout, padding) stays at the hardware-validated defaults and
   is **not shown**.
3. **WRITE is in scope — wire flash + safety layer.** When `adapter == wican`, the UI flash path routes
   through `WiCANFlasher` (pre-flight link gate + battery guard + abort-and-restart, **never** a mid-stream
   resend) instead of calling `FlashManager.flash_rom` directly. The **J2534 flash path stays byte-for-byte
   unchanged.** The WiCAN Flash button carries a clear **"experimental — not yet hardware-validated"** note
   until the first real WiCAN flash succeeds. This is what makes the integrated bench write test possible.
4. **Connect UX = honest progress + link readout.** On WiCAN connect, show a progress note that the adapter
   reboots into SLCAN (~6 s) and restores on disconnect. Surface a link-quality line (loss % / p95 ms, from
   `WiCANFlasher.preflight`) in the flash dialog and on **Test Connection**.

### Decided without asking (carry-over defaults — stated for the record)
- **BLE: deferred, no placeholder.** WiFi/SLCAN-over-TCP only (all read-speed work settled on WiFi).
- **Reuse `ECUSession` / `FlashManager` — do NOT add a WiCAN-specific session.** They are already
  transport-agnostic; the WiCAN-awareness lives in `ECUSession`'s connect/disconnect only. (Honours the
  "no new mixins" architecture rule — extend the seam, don't special-case the call sites.)
- **No mDNS discovery this goal.** Manual host entry (the field defaults to the known device IP).
- **No separate mid-flash recovery wizard.** `WiCANFlasher` already restarts-from-scratch internally; the
  flash dialog just surfaces its final failure message (incl. "keep ignition ON") on give-up.

---

## Scope
Purely **UI + settings-plumbing + one transport seam change**. The transport, ECU functions, and WRITE
safety layer are already built (goals 1–2). Today `ECUSession.__init__(dll_path)` and its
`connect_ecu` worker construct `J2534Device → J2534Transport → UDSConnection` unconditionally
(`src/ecu/session.py:94-115`); that is the only place that must learn about adapter kind. Everything
downstream — the ECU window's read/RAM/DTC actions and `FlashSetupDialog._ECUInfoWorker` — already runs
over `session_uds`, so it works over any transport once the session can build a WiCAN one.

---

## Work breakdown

### Part A — Settings plumbing
- **`src/utils/settings.py`** — add getters/setters mirroring `get/set_j2534_dll_path` (line 249):
  - `get_ecu_adapter() -> str` / `set_ecu_adapter(str)` — `"j2534"` (default) or `"wican"`.
  - `get_wican_host() -> str` / `set_wican_host(str)` — default `"192.168.1.169"`.
  - `get_wican_port() -> int` / `set_wican_port(int)` — default `35000`.
  - `get_wican_auto_config() -> bool` / `set_wican_auto_config(bool)` — default `True`.
- **Acceptance:** unit tests for round-trip of each setting + defaults when unset.

### Part B — Settings dialog (ECU ▸ Adapter + ECU ▸ WiCAN)
- **`src/ui/settings_dialog.py`** — add to `SETTINGS_REGISTRY`:
  - `ecu.adapter.kind` — combobox `[("J2534","j2534"),("WiCAN","wican")]`, subcategory **Adapter**.
  - `ecu.wican.host` (path-style text), `ecu.wican.port` (spinbox 1..65535),
    `ecu.wican.auto_config` (checkbox), `ecu.wican.test_connection` (button →
    `_test_wican_connection`), subcategory **WiCAN**.
  - Add `"WiCAN"` and `"Adapter"` under the existing `"ECU"` category (already conditional on the
    secure/flash module importing).
- **`_test_wican_connection`** — build a `WiCANTransport` from the fields, open (auto-config if enabled),
  ping firmware version, run `WiCANFlasher.preflight`, show fw + `loss% / p95 ms` in a `QMessageBox`
  (mirror `_test_j2534_connection`). Always restore protocol + close on exit.
- **Acceptance:** GUI test script under `tests/gui/` opens settings, switches adapter, edits WiCAN fields.

### Part C — Session seam (the one core change)
- **`src/ecu/session.py`** — let `ECUSession` accept an **adapter config** instead of only `dll_path`
  (keep a back-compat path so existing `ECUSession(dll_path)` callers/tests still work). In the connect
  worker, branch on `kind`:
  - `j2534` → unchanged (`J2534Device`/`J2534Transport`), byte-for-byte.
  - `wican` → if auto-config, `WiCANConfigurator` switch to SLCAN (emit the ~6 s reboot progress note);
    build via `create_ecu_transport({"kind":"wican", host, port, ...})`; `open()`; wrap in
    `UDSConnection`. On disconnect, **restore protocol + clean teardown** (the Part C clean-teardown
    finding — bounded timeout, no CAN-wedge).
- **`src/ui/flash_mixin.py`** — `_on_ecu_connect` builds the adapter config from settings
  (`get_ecu_adapter()` + WiCAN fields) and passes it to `ECUSession`, instead of only `_get_j2534_dll_path()`.
- **Acceptance:** session unit tests with a `FakeTransport`-backed WiCAN config; connect/disconnect emits
  the reboot progress note exactly once; protocol restored on disconnect.

### Part D — Flash dialog routing + WRITE over WiCAN
- **`src/ui/flash_setup_dialog.py`** — when `adapter == wican`:
  - Run `WiCANFlasher.preflight()` and show the **link line** (`loss% / p95 ms`, ✓/✗) before enabling Flash.
  - Show the **"WiCAN flash is experimental — not yet hardware-validated. Keep ignition ON."** note.
  - On Flash, route the actual write through `WiCANFlasher.flash_rom` / `dynamic_flash` (gate → battery →
    abort-and-restart → optional verify). **J2534 stays on the existing `FlashManager` path untouched.**
- **Acceptance:** unit/mocked tests that `adapter == wican` selects `WiCANFlasher` (and a failed pre-flight
  refuses the flash with the gate message); `adapter == j2534` still calls `FlashManager` directly.

### Part E — Connect/progress UX
- Surface the SLCAN-switch reboot as a progress note on connect; surface link quality on Test Connection and
  in the flash dialog (Part B/D already wire `preflight`). Keep within **max 2 signal hops**; **CSS in one
  place**; **no shared mutable dicts** (adapter config is a transient mapping, not shared state).

---

## Constraints (carry-overs — non-negotiable)
- J2534 remains the default; WiCAN is opt-in; **J2534 path byte-for-byte unchanged**.
- Architecture rules: no new mixins, no shared mutable dicts, CSS in one place, max 2 signal hops.
- WRITE invariant: **never resend a block mid-stream** (no sequence counter → resend bricks). A WiCAN
  flash failure restarts the whole flash; it never patches mid-stream.
- WiCAN flash button stays marked experimental until a real hardware flash succeeds (task #20).
- CHANGELOG before commit; `black` + `pytest` green; **no push to master without explicit validation**.
- Never OTA-flash the WiCAN PRO without a secured firmware backup (unrelated to this goal, but standing).

## Tests
- Settings round-trip (Part A). Session WiCAN-config connect/disconnect over `FakeTransport` (Part C).
- Flash routing: `wican → WiCANFlasher`, `j2534 → FlashManager`; pre-flight refusal (Part D).
- GUI scripts under `tests/gui/*.txt` for the selector + WiCAN settings page (`tools/test_runner.py`).
- No live-ECU test is required to *land* this goal; the real WiCAN **flash** is the user-gated hardware
  step (task #20) after the UI is in place.

## Out of scope
- BLE transport; mDNS discovery; toolbar quick-switch indicator; firmware no-reboot SLCAN port
  (tracked separately, task #21/Part C findings); larger-block read optimisation (task #23).

## References
- `wican-ecu-functions-goal.md` (goal 2), `wican-read-speed-goal.md` (goal 1).
- `docs/internal/WICAN_TRANSPORT.md` §3 (seam), §6 (WRITE/`WiCANFlasher`), §8b (protocol switch).
- `docs/internal/WICAN_PART_C_FINDINGS.md` (clean teardown / reboot avoidance).
- Code: `src/ecu/session.py` (seam), `src/ui/flash_mixin.py` (`_on_ecu_connect`),
  `src/ui/flash_setup_dialog.py` (flash routing), `src/ui/settings_dialog.py` (`SETTINGS_REGISTRY`,
  `_test_j2534_connection`), `src/utils/settings.py` (getters), `src/ecu/transport.py`
  (`create_ecu_transport`), `src/ecu/wican_config.py` (`WiCANConfigurator`),
  `src/ecu/wican_flash.py` (`WiCANFlasher`), `src/ecu/link_quality.py` (`check_link_quality`).
- Tracker task #7 (this goal); task #20 (hardware WiCAN flash, gated).
