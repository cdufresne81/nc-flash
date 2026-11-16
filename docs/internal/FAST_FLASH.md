# Fast flash (experimental, opt-in)

**User-facing name: Dynamic Flash** (owner decision 2026-11-15). "Fast flash" is the internal name only (module,
`FlashManager.fast_flash`, worker operation, this doc). Users see Dynamic Flash: the block-compare method is an
improvement of it, not a new feature, and everything they read (labels, messages, progress, Activity Log, CHANGELOG)
says Dynamic Flash.

**Status (2026-11-15):** in the app behind Settings > ECU > Flashing > "Dynamic Flash: use the new block-compare method
(experimental)", **off by default** (owner to confirm: default on, or remove the setting), Tactrix (J2534) and OBDX Pro
only. The v3 kernel is pinned (size 0x1800, SHA-256
`8f21aa273495cc3d2cb1fd5927c5928f1867b4cacb814ede25e841e8eb63a62c`, CRC-32 0xD0556A29; nc-flash-re branch `claude/clever-feynman-nutih0`, WIP,
CI run 43772197996). Bench-run on one ECU (NC2, LFDJEA, chip class 1) over a Tactrix clone. Not yet run over the OBDX,
on an NC1, or with a power cut mid-flash (see the checklist at the end). Release builds still need the kernel in the
private security module (`_fastflash.py`); without it only a machine with `fast_flash/kernel.bin` can use it.

## What it is

The normal flash uploads Mazda's RAM flash program, which erases and rewrites everything from a start block to the end
of the ROM. Fast flash uploads a different RAM program (the "kernel") that can report a CRC of any ROM range, erase one
block and program one range. Only the blocks that differ from the ECU (by CRC, not by the local archive) are rewritten,
LZMA-compressed, and the result is verified by CRC before the ECU is made bootable again.

| Piece | Where |
|---|---|
| Kernel source, design, evidence, limits | `nc-flash-re`: `tools/fast_flash/kernel/`, `analysis/fast_flash/` |
| Protocol (host side) | `src/ecu/fast_flash.py` |
| Kernel delivery, pin, availability rules | `src/ecu/fast_flash_kernel.py` |
| Orchestration (session, checks, states, archive, reset) | `FlashManager.fast_flash` / `fast_compare` in `src/ecu/flash_manager.py` |
| Interrupted-flash detection | `EcuMode.FAST_FLASH_KERNEL` in `src/ecu/recovery.py` |
| UI | `src/ui/ecu_window.py` (one-click selection, labels, banner, reset offer, outcomes) |
| Bench tool | `tools/fast_flash_bench.py` (compare-only by default; same code path as the app) |

**WiCAN:** the firmware runs the same sequence itself (the `K` command; contract in nc-flash-wican-fw
`docs/internals/fast_flash.md`). The PC builds a package (`src/ecu/fast_flash_package.py`, same ranges and kernel
rules as `fast_flash.py`), uploads it to the SD card and follows the status lines (`src/ecu/wican_fast_flash.py`; the
socket read loop is `WiCANTransport.fast_flash`, next to `fast_write`). The window's driver is `WiCANFastFlasher`:
- offered only when `/host_caps` reports `fast_flash` >= 1, `ncfr_rev` >= 6 and `ff_live=1` (read once per
  connection), plus the same kernel, generation and setting rules as the cable path; otherwise the SD-staged Dynamic
  Flash runs, silently;
- inside the bus reservation (`_datalog_fence`): link/battery gate, package upload, then for mode `L` the SD flasher's
  session steps (authentication without the counter, generation check, flash counter), then `K L`. Mode `R` (resume)
  needs no session;
- a resume is offered when the last failure had stage 26 or was past the point of no return, when the adapter reports
  an unfinished op (`GET /fast_flash`), or when the connect probe finds the kernel running. Stage 62 messages say
  never to cycle the ignition, and the window adds no such advice;
- the firmware's error messages are shown as they are; a refusal before any erase (state "untouched") falls back to
  the standard method for that ECU like the cable path's refusal;
- same archive rule: set aside before contact, written after the op is done, put back only if the write never
  started (never on a resume).
The progress callbacks only emit a Qt signal: the firmware drops the PC after 2 s without reads before its point of
no return. The bench tool's `--wican` remains compare-only.

## Kernel protocol (v3)

Uploaded through the ECU's ROM loader: RequestDownload at 0x8000 with size = 0xFE000 + kernel length, 0x400-byte
TransferData blocks, `*SBL_END` in the last block only. The loader answers the last block `7F 36 78` and jumps; the
kernel sends the `76`.

| Request | Answer | Meaning |
|---|---|---|
| `3E` | `7E` | tester present |
| `31 00` | `71 00 'N' 'F' ver chip ready` | identify (`ready` = flash routines downloaded) |
| `31 01 a2 a1 a0 l2 l1 l0` | `71 01 crc32` | CRC-32 (zlib) of ROM [a, a+l) |
| `31 02 blk` | `7F 31 78`, then `71 02 blk` | erase block 2..15 (silent up to 20 s) |
| `31 03 n1 n0` | `71 03 crc32` | CRC of the first n bytes of the kernel in RAM (v3) |
| `34 addr4 len4` / `36 data` | `74` / `76` | program a range (128-byte aligned, >= 0x2000) |
| `34 10 lp pb addr4 len4` / `36 seq stream` | `74` / `76 seq` | the same range LZMA-compressed (v2+) |
| `37` | `77` | close a complete range |
| `11 01` | `51`, then halt | end; the ECU restarts by itself |

Everything else is answered `7F sid 11`. In v3, a foreign request during a compressed stream (another tester on the
bus) is refused without ending the stream, and the host ignores a negative answer that names another service.

## Sequence

1. Same session, security, **generation check** and flash-counter steps as the normal flash (`verify_ecu_generation`
   runs before the counter: after it the ECU refuses memory reads).
2. Upload the kernel, identify it, and check the kernel image in RAM by its CRC (`31 03`) against the file sent. A
   damaged kernel is never given an erase.
3. **Before any erase**, the app's policy: kernel version = the pinned one, self-check present (v3), chip class in
   `ALLOWED_CHIP_CLASSES` (`{1}`), and the ECU's boot-code signature (by CRC) = the ROM's hardware generation. Anything
   else: kernel reset (`11 01`), `FastFlashUnavailable`, nothing changed.
4. Compare every application block (2-15) by CRC; the counter page 0xFFB00-0xFFB7F is left out. Nothing differs: kernel
   reset, "ROMs are identical" (the archive is updated, since the ECU holds exactly this image).
5. Erase block 15 first. The boot loader then no longer starts the application.
6. Erase and program each other changed block, then block 15 without its last page.
7. Compare everything written so far. Any difference is an error; the ECU still cannot start.
8. Commit: program the last page (0xFFF80: start address and the entry pointer the boot loader tests) and verify it. If
   it reads wrong, block 15 is erased again at once. A lost answer alone never erases a good flash: the CRC decides.
9. Save the archive (same as the normal flash), reset the kernel.

**Archive:** the archive is renamed to `<archive>.before-dynamic-flash` before any contact with the ECU and written again
only after a verified flash. If the flash ends before its first erase (refusal, error, "identical"), it is put back
("identical" writes a fresh one). After a flash that did not finish, there is no archive, so the archive-based Dynamic
Flash falls back to a full flash instead of computing a partial flash from a stale copy.

**Restart:** if the kernel does not answer the final `11 01`, it is sent once more; if there is still no answer, the
success message tells the user to cycle the ignition before starting the engine.

Any failure after the kernel started and before the first erase stops it (`11 01`, when the link still answers), so the
ECU restarts instead of waiting; in a resume a mere link error leaves it running (a second resume can finish).
Late answers are drained before every CRC request (a CRC answer carries no address): reads of `DRAIN_READ_MS` = 5 ms,
never 0 (the OpenPort clone DLL blocked forever on a zero timeout, bench 2026-11-15), at most 16, and a `None` that
came back at once (a J2534 indication frame) does not end the drain.

**Refusal memory:** a refusal is kept for that ECU (VIN + ROM ID read at connect) for the rest of the app run, so
Connect does not send it back to the block compare; another ECU clears it.

If the entry pointer reads erased at the start, an earlier flash was cut short: every block is rewritten whatever the
CRCs say. The boot block is compared too; a difference is reported, never written.

## Rules that keep it recoverable

- Nothing below 0x2000 is erased or programmed: the host refuses, and the kernel refuses.
- The kernel refuses blocks 2-14 and every program range (v3) until block 15 has been erased in the same session, and
  takes the last page only as a range of its own.
- Once the first erase is sent the host never aborts: finishing is the safe direction. There is no fallback after it.
- After the first erase, every failure is reported as "the ECU is waiting; flash it again".
- Identify (`31 00`) is never sent to an ECU that may run its application or the ROM boot loader (the Mazda RAM
  loader: see the open items): only after OBD 0x01,
  0x21 and 0x22 were all refused with NRC 0x11 (the kernel's signature; the boot loader refuses 0x22 with 0x31), or
  during the upload, where only the loader or the kernel can listen.

## How the app chooses

One-click **Flash Current ROM** (labelled "Dynamic") uses the block compare when **all** of these hold, else exactly
today's choice, silently (archive-based Dynamic Flash with an archive, else full). The block compare needs no archive,
so with it the button says Dynamic even when no archive exists; the archive is still saved after success.

- the setting is on;
- the adapter is J2534 or OBDX;
- the ECU is not in recovery mode (boot loader / suspect);
- an accepted kernel is found (below) and the ROM's hardware generation is in `ALLOWED_GENERATIONS` (`{"NC2"}`);
- the fast flash has not refused this ECU earlier in this run of the app.

The calibration-ID warning against the archive still applies. **Full Flash always uses the Mazda flash.**

Fallback tiers:
- (a) before any contact (setting off, adapter, no kernel, NC1 ROM): the normal flash is chosen directly.
- (b) after the upload, before the first erase (identify fails, policy refuses, flash routines not ready): the kernel
  is reset, the user is told "Dynamic Flash can't compare blocks on this ECU: <reason>. Nothing was changed", and the
  next click uses the standard method. Chaining into a normal flash on the same session is **not** done (the second programming session's
  effect on the flash counter is not measured).

## Interrupted fast flash

- **Kernel still running** (tool closed or link lost, no ignition cycle): on connect the probe recognises it
  (`EcuMode.FAST_FLASH_KERNEL`) and shows "Dynamic Flash interrupted". With the setting on, Flash Current ROM finishes it
  (resume: every block is rewritten, no session/security step; the generation is checked by the boot-code CRC). With it
  off, or on Full Flash, the window offers a **kernel reset** (bare `11 01`, after recognising the kernel again).
- **After an ignition cycle** (or the kernel reset) with the application incomplete: the boot loader's recovery server
  (seed 55AA55), the existing recovery banner, and a full flash recovers it.
- An interrupted *normal* flash left the Mazda loader running: the kernel upload is refused with NRC 0x22, which shows
  the existing "still in programming mode" reset offer.

## Kernel delivery and pin

`load_kernel()` tries, in order: `get_fast_flash_kernel()` in the private security module (`src/ecu/_secure/_fastflash.py`,
git-ignored; the stub raises `SecureModuleNotAvailable`), then `fast_flash/kernel.bin` under the app root (bench and
development; git-ignored; **never read by a release (frozen) build**). A kernel is used only if its `(size, SHA-256)`
is in `FAST_FLASH_KERNELS` (a CRC-32 could be forged in four bytes, and the kernel may erase the boot block); its pinned version
must then match what the kernel reports. With no pin, a kernel file is accepted only when `NCFLASH_FAST_FLASH_UNPINNED=1`
(bench). The app accepts **v3 only** (`APP_KERNEL_VERSIONS`); v1/v2 stay supported in `fast_flash.py` for its tests.

To pin the CI-built kernel, print its line and add it to `FAST_FLASH_KERNELS`:

```
python -m src.ecu.fast_flash_kernel path/to/kernel.bin 3
# KernelPin(size=0x1800, sha256="...", version=3),
```

Still to do outside this repo: the private `_fastflash.py` and a release-workflow check that the pinned kernel imports.

## Link errors and retries

- A request without an answer is retried (late answers flushed first) a few times; erases, CRCs, range opens and
  compressed pieces are safe to resend (a piece carries a sequence number). A lost CRC or TransferExit answer is given
  up on after its own timeout (10 s), not the 60 s default.
- **Adapter faults are fatal**, never resent: a dead J2534 bridge or J2534 write error, a closed port, an OBDX that
  refused or did not acknowledge a write (the OBDX does not say whether the ECU saw part of it).
- Waiting out a silent link for minutes (`LINK_OUTAGE_S`) happens only when the caller asks (`wait_out_outages`): the
  bench tool's `--wican`. The app never asks (J2534/OBDX).

## Bench results (real ECU, Tactrix clone, 2026-11-15)

| Change | Block compare | Archive-based Dynamic Flash |
|---|---|---|
| One block | 3.9-4.0 s (v3) | 7.9 s |
| Mid-ROM change (3 blocks) | 15.1 s; 15.2 s (v3) | 48.7 s |
| Full ROM | 45 s | 64.7 s (full) |

Kernel v3 specifically:
- The self-check (`31 03`) ran before every erase.
- A deliberately corrupted compressed stream was refused with NRC 0x72; the block was erased again and resent, and the
  flash verified (18.6 s).
- 11 KB of 0x00 in one piece decoded fine.

- After the kernel's `11 01` the ECU restarted its application within a few seconds, no ignition cycle, every run.
- Tool killed mid-flash: the kernel kept running; a new run found it and finished (every block rewritten).
- Interrupted flash + kernel reset: the ECU sat in the boot loader's recovery server; both a fast flash from there and
  the normal full flash recovered it.
- From the boot loader the RPM request is refused (NRC 0x11) and the engine-off gate does not block (existing
  behaviour).

These runs predate the app integration (bench tool on the previous `FlashManager` API).

## Hardware checklist still open

Before the setting is offered in a release, on the bench ECU (never the car first):

**J2534 (Tactrix)**
1. App run with the setting on (v3 pinned): one-block change, then identical ROM ("identical", ECU restarts), then a
   full rewrite (`tools/fast_flash_bench.py --flash --all` or a ROM with every block changed).
2. Power cut during the first erase of block 15, then recover with a normal full flash. Then the same mid-block and
   just before the commit. Record what the ECU answers after each cut.
3. Close NC Flash mid-flash: reconnect shows "Dynamic Flash interrupted"; finish it with Flash Current ROM; separately, use
   the reset offer and confirm the ECU restarts (or shows recovery mode).
4. A whole block of one constant non-0xFF value (worst-case LZMA expansion per piece; 11 KB of 0x00 already passed):
   it must finish within the piece wait.
5. Fallback tier (b): a chip class outside the allow-list (edit the constant temporarily on the bench): "not available,
   nothing changed", ECU restarts, next click does a normal flash; read the flash counter before and after.
6. The WiCAN logger polling on the bus during a compressed flash (foreign requests refused without breaking it).

**OBDX Pro**
7. Compare-only run (`tools/fast_flash_bench.py --obdx`), then a one-block flash and a full rewrite; note the times.
8. Pull the USB mid-flash: the flash stops at once (adapter fault, no retries); reconnect finds the kernel; finish it.
9. A 1024-byte compressed piece at the adapter's frame delay: no consecutive-frame overrun on the kernel's single
   receive mailbox (check the bench log for resent pieces).

**Not tested at all yet:** any NC1, the 80 MHz chip classes, chip class 0 on hardware.

## Open items from the independent review (2026-11-15)

- **Power cut during the block-15 erase or the commit (finding 5):** a cut mid-erase can leave 0xFFFFC holding neither
  its old value nor erased, and the boot loader may jump to garbage. Whether that is recoverable over CAN is
  unconfirmed. Same exposure as the Mazda kernel. Blocks release, not bench work (checklist item 2).
- **Identify may reach the Mazda RAM kernel (finding 12, unconfirmed):** if an interrupted normal flash's Mazda kernel
  refuses 0x01, 0x21 and 0x22 all with NRC 0x11, the probe sends it `31 00`. Only the ROM boot loader's answer (0x22 ->
  0x31) is proven. Low risk (its erase runs at entry, not on request), but check its answers on the bench. The same
  applies to a WiCAN SD flash in progress when the user clicks Connect (marginal).
- **Progress-callback exceptions (finding 13, theoretical):** a progress callback that raised after the first erase
  would stop the flash part-way. Qt signal emits do not raise.
- The bench tool's `--wican` (compare only) does not take the shared WiCAN bench lock.

## WiCAN: reset block and adapter records (review 2026-11-15)

- The kernel-reset offer (`11 01`) is withheld on a WiCAN connection when a reset could act like the forbidden ignition
  cycle or cut an op short: the last op on that WiCAN ended WRONG_ENTRY (st 62), MAY_BE_COMPLETE (st 61) or BUSY, or the
  adapter's own `GET /fast_flash` record (read off the UI thread at connect) shows an op `running` or `failed` /
  `interrupted` at st 61/62 (so the block survives an NC Flash restart). It is keyed to that WiCAN host and the ECU's
  identity at flash time, cleared by a successful flash or by a connect that reads a running application, and never
  applies to the Tactrix / OBDX. Other endings (no application, unknown, not started) keep the reset: it is the way to
  a full flash.
- While the kernel runs on a WiCAN connection, the reset is also withheld until that `GET /fast_flash` record has been
  read successfully (final review F1): after an NC Flash restart it is the only memory of an st 62. Pending → "still
  asking, click again"; unreadable → "do not reset; finish with Flash Current ROM, or reconnect / use a cable".
- `/host_caps` that answers nothing is read again at most 3 times per connection (5 s, then 15 s apart), never during
  a flash. If it gives up while the kernel runs and the reset is blocked, the user is told to keep the ignition on and
  finish with updated WiCAN firmware or a Tactrix / OBDX.
- Open (re-review R6, low, kept as is): the follow-up treats any `interrupted` adapter record as this op's
  NO_APPLICATION, even when the `K` never ran (first GET after a dongle boot). Conservative: the resume that follows
  ends at st 23 and the user then does a full flash.
