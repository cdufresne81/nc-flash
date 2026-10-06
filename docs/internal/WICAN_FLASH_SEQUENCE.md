# WiCAN Flash Sequence (SD-staged write)

How a flash over the WiCAN PRO works end to end, and where the ECU can and cannot be
bricked. Snapshot of 2026-09-27: host `master` @ 7560072, firmware
`nc-flash-wican-fw` `wican-pro` @ 7faf422. Pre-flight voltage guard updated for #130 (2026-10-05). Re-check the code refs below before relying on
this for a change to the write path.

**Short version:** the PC builds the final image and uploads it to the WiCAN's SD card
over WiFi, where it is CRC-checked. Then the WiCAN firmware flashes the ECU from SD over
local CAN. Nothing on the ECU is erased until the last SBL block is sent (the "point of no
return"). After that, WiFi loss no longer stops the flash.

## Sequence

```
 NC Flash (PC)                         WiCAN PRO                          ECU (MX-5 NC)
                                       (firmware + SD card)
      │                                     │                                  │
 ① CONNECT  (when you click Connect)
      │── TCP SLCAN port 35001 ────────────▶│                                  │
      │── HTTP /datalog claim + pause ─────▶│ datalogger parked,               │
      │                                     │ CAN bus reserved for the PC      │
      │                                     │                                  │
 ② CLICK "FLASH"
      │ voltage warning, RPM gate           │                                  │
      │ (engine off), WiCAN warning dialog  │                                  │
      │ stops trip-log sync                 │                                  │
      │ dynamic flash if ncflash.rda        │                                  │
      │ archive exists, else full           │                                  │
      │                                     │                                  │
 ③ PRE-FLIGHT GATE   (fails → stop, nothing written)
      │── Tester Present pings ────────────▶│──────────── CAN ────────────────▶│
      │◀── packet loss / latency judged ────│◀─────────────────────────────────│
      │── read battery voltage ────────────▶│─────────────────────────────────▶│
      │   too low → refuse                  │                                  │
      │   unreadable → refuse; the          │                                  │
      │   operator may override it          │                                  │
      │                                     │                                  │
 ④ PACKAGE   (PC only, no network)
      │ fix checksums (must leave 0 left)   │                                  │
      │ build SBL (the ECU's small          │                                  │
      │   flashing program), pick start     │                                  │
      │ image = ROM (1 MB) + SBL (0x1800)   │                                  │
      │ manifest: offsets, SHA-256, CRC32   │                                  │
      │                                     │                                  │
 ⑤ UPLOAD   (fails → ECU untouched)
      │── POST /upload/sd/<rom>_<date>.bin ▶│ writes file to SD                │
      │◀── {bytes_written, crc32} ──────────│                                  │
      │ PC compares CRC, mismatch → stop    │                                  │
      │── POST manifest ───────────────────▶│                                  │
      │                                     │                                  │
 ⑥ FIRMWARE CHECK
      │── version ping ────────────────────▶│                                  │
      │◀── "NCFRv<n>"  (needs n ≥ 5) ───────│ too old → stop, no ECU contact   │
      │                                     │                                  │
 ⑦ AUTHENTICATE   (security key computed on the PC; nothing erased or written)
      │── Tester Present ──────────────────▶│─────────────────────────────────▶│
      │── programming session (0x10 85) ───▶│─────────────────────────────────▶│
      │── SecurityAccess seed/key (0x27) ──▶│─────────────────────────────────▶│
      │── flash-counter routine (0x31) ────▶│─────────────────────────────────▶│
      │                                     │                                  │
 ⑧ FLASH   (WiCAN flashes the ECU from SD over CAN)
      │── "WL<filename>" command ──────────▶│ SD image CRC vs manifest         │
      │                                     │   (mismatch → abort, no ECU I/O) │
      │◀── NCFWSYNC ────────────────────────│                                  │
      │                                     │── RequestDownload (0x34) ───────▶│
      │                                     │── TransferData SBL blocks 1-5 ──▶│
      │                                     │                                  │
      │   ═══════ POINT OF NO RETURN (set before the last SBL block) ═══════   │
      │                                     │── TransferData SBL block 6 ─────▶│
      │◀── keepalive every 5 s ─────────────│      ECU ERASES (~12.6 s, silent)│
      │                                     │── TransferData ROM blocks ──────▶│
      │◀── NCFWPROG done/total ─────────────│◀── 0x76 "OK" per block ──────────│
      │    progress bar 35 → 90 %           │── TransferExit (0x37) ──────────▶│
      │                                     │◀── 0x77 "OK" ────────────────────│
      │                                     │── ECUReset ─────────────────────▶│
      │◀── NCFWDONE  = write confirmed ─────│                                  │
      │                                     │                                  │
 ⑨ FINISH
      │ save corrected ROM → ncflash.rda    │                        ECU resets and
      │   (baseline for next dynamic flash) │                         runs new cal
      │ bus stays reserved until Disconnect │                                  │
      │   (then release + datalog resume)   │                                  │
      │                                     │                                  │
 ⑩ AFTER  (operator)
      dynamic flash: nothing to do, the ECU answers OBD again ~7 s after
        the reset (seen 2026-09-29) and is ready for another session
      full flash: not confirmed; if OBD reads fail (NRC 0x11 = bootloader),
        key OFF ~10 s → ON
      failed flash: the ECU stays in bootloader (OBD → NRC 0x11); re-flash
        to recover (the pre-flash checks let a recovery flash through)
      optional read-back verify (off by default): run it once OBD answers
```

Block counts for a full flash: SBL 6 × 1 KB, program 1016 × 1 KB (0x2000 → end).

## What can and cannot brick the ECU

"Brick" here means **soft brick**: the application area is erased or half-written, and the
ECU needs a re-flash. The region below `ROM_FLASH_START_MIN` (0x2000) is never
written.

| Where it goes wrong | Result | Why |
|---|---|---|
| ③ gate, ④ package, ⑤ upload, ⑥ version | Safe: ECU untouched | Each step raises before any ECU contact. |
| ⑦ authenticate on a lossy link | Safe: no erase or write | Only session, security and counter requests. Worst case the ECU is left in programming session; the host sends default session (0x10 01), or the ECU drops the session on its own after tester-present silence. Repeated bad keys lead to a security lockout delay, not a brick. |
| ⑧ before the point of no return (RequestDownload, SBL blocks 1–5) | Safe | Firmware aborts if the PC stops reading for 2 s (`host_gone`), or on any ECU error. Nothing is erased yet. |
| ⑧ WiFi or PC lost **after** the point of no return | Safe: flash completes | Progress lines become drop-and-ignore sends. The PC may report "stalled" for a flash that actually succeeded. The firmware event log says "the flash finished without it". |
| ⑧ power loss after the point of no return (key off, battery sag, OBD unplugged) | **Soft brick** | The ECU is erased and the flash can't finish. The battery guard only runs at ③. An unreadable voltage refuses the flash unless the operator overrides it (the ECU must still answer Tester Present); a low reading always refuses. |
| ⑧ WiCAN crash, watchdog, or reboot after the point of no return (includes an OTA, config save or reboot from another session on the shared bench) | **Soft brick** | The firmware is the only thing driving the flash. |
| ⑧ a block gets no ACK or an NRC after the erase (CAN error, another tool on the bus) | **Soft brick** | TransferData has no sequence counter, so there is no resend. FWERR, then abort. |
| ⑧ SD read error mid-flash | **Soft brick** | `fread` fails (FWERR stage 6). The image was CRC-checked just before, so this is unlikely. |
| Dynamic flash with a stale `ncflash.rda` (ECU was flashed from elsewhere since) | **Inconsistent ECU** | The region before the diff is not rewritten, so the ECU mixes old and new content. The UI only catches a cal-ID mismatch. This also applies to J2534; it isn't WiFi-related. |

## Code references

- Host orchestration: `src/ecu/wican_sd_flash.py` (`flash_rom`, `_trigger_firmware_flash`)
- Packaging and manifest: `src/ecu/wican_sd_package.py` (`build_flash_package`)
- Upload and CRC check: `src/ecu/wican_sd_upload.py`
- `W` command and progress parsing: `src/ecu/wican_transport.py` (`fast_write`, `_FAST_WRITE_IDLE_MS`)
- Pre-flight gate: `src/ecu/wican_flash.py` (`_gate`)
- UI entry and checks: `src/ui/ecu_window.py` (`_on_flash_current`, `_build_flash_driver`)
- Firmware: `nc-flash-wican-fw` `main/ncflash_fastwrite.c` (point of no return
  `s_ponr`, `RESP_ERASE_TIMEOUT_MS`, `KEEPALIVE_MS`, `tx_send`)
- Related: `WICAN_DEADMAN_AUTORESUME.md` (bus claim and datalog fence),
  `WICAN_SLCAN_COEXISTENCE_PLAN.md` (FLASH_ACTIVE_BIT interlock)
