# Fast flash (experimental, bench only)

**Status: not in the UI, never run on an ECU.** Written against a simulated ECU. The first hardware step is a
compare-only run on the bench ECU.

## What it is

Today's flash uploads Mazda's RAM flash program, which erases and rewrites everything from a start block to the end of
the ROM. Fast flash uploads a different RAM program (the "kernel") that can report a CRC of any ROM range, erase one
block and program one range. Only the blocks that differ are rewritten, and the result is verified by CRC.

- Kernel source, design, evidence and limits: the `nc-flash-re` project, `tools/fast_flash/` and
  `analysis/fast_flash/2026-11-13_stage1_kernel_emulation.md`.
- Host side: `src/ecu/fast_flash.py`. The existing flash path (`flash_manager.py`) is not changed.
- Bench tool: `tools/fast_flash_bench.py`. Compare-only by default.
- Cable (J2534) only. The WiCAN SD-staged flash is driven by the adapter firmware and does not know this protocol.

## Sequence

1. Same session, security and flash-counter steps as the normal flash.
2. Upload the kernel (RequestDownload size = 0xFE000 + kernel length; 0x400-byte blocks; the kernel answers the last one).
3. Identify. Compare every application block (2 to 15) by CRC; the counter page 0xFFB00-0xFFB7F is left out.
4. Erase block 15 first. The boot loader then does not start the application.
5. Erase and program each other changed block, then block 15 without its last page. Pieces that are all 0xFF are not sent.
6. Compare everything written so far. Any difference is an error, and the ECU is still waiting in its boot loader.
7. Commit: program the last page (0xFFF80, which holds the start address and the pointer the boot loader tests) and
   verify it. If it does not verify, block 15 is erased again at once.

If the ECU's entry pointer reads erased at the start, an earlier flash was cut short: every block is rewritten whatever
the CRCs say. The boot block is compared too; a difference is reported, never written.

## Rules that keep it recoverable

- Nothing below 0x2000 is erased or programmed. The host refuses, and the kernel refuses.
- The kernel refuses blocks 2 to 14 until block 15 has been erased in the same session, and takes the last page only as
  a step of its own.
- Do not send a request before the previous answer has finished on the bus: the kernel empties its receive mailbox
  right after it starts an answer.
- Once the first erase is sent, the host no longer honours an abort: finishing is the safe direction.
- A fast flash that stops part-way leaves the ECU in its boot loader. Recover with a normal full flash.

## Before it can go further

- Bench: compare-only run, then a first flash, read-back compare and power-cut tests (`nc-flash-re`, step 4).
- The kernel binary is not shipped with NC Flash. Where it lives (the private security module) is undecided.
- No UI, no setting, no WiCAN support yet.
