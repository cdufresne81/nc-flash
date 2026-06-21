# WiCAN ↔ NC Flash — Manual Integration Test

Hardware-in-the-loop checklist for the WiCAN PRO ECU read path. These steps need
a real WiCAN PRO on the network and a powered MX-5 NC ECU (ignition ON), so they
can't run in CI — run them by hand after touching the transport, firmware, or the
adapter-selector UI.

**Reference values (live MX-5 NC ECU, 2026-06-21):** a full 1 MB authenticated
read is **~214 s** and **byte-identical** to `wican_stmin0_full.bin`. That matches
a Tactrix on the same ECU (215.8 s) — the ~211 ms/block floor is the ECU's
response-pending latency, not the link. A read much slower than ~220 s, or any
byte mismatch, is a regression.

## 0. Pre-flight

- WiCAN PRO reachable (default bench IP `192.168.1.169`); note its SLCAN TCP port.
- ECU powered, ignition ON (CAN traffic flowing).
- The private `_secure` module is installed (security-access seed/key for reads).
- A known-good oracle dump (`wican_stmin0_full.bin`) for byte-compare.

## 1. Confirm the firmware build (version ping)

```bash
python tools/wican_fw_ping.py --host 192.168.1.169 --port 35000
```

Expect `[RESULT] fast-read firmware live: NCFRv<rev>`. If it reports OLD/UNKNOWN,
the fast-read firmware isn't flashed — OTA it first (see `WICAN_TRANSPORT.md` /
the firmware fork) and bump the version string when wire behaviour changes.

## 2. Read the full ROM via the bench tool (works today)

```bash
python tools/wican_bench_read.py --host 192.168.1.169 --port 35000 \
    --auto-config --fast-read --reference wican_stmin0_full.bin --out wican_read.bin
```

- `--auto-config` flips the device to `slcan` and restores the prior protocol on exit.
- PASS criteria: `[GATE] RESULT: PASS — byte-for-byte identical` and `DONE in ~214 s`.
- To test just a region (e.g. the response-pending area), add
  `--fast-read-start 0xD8400 --fast-read-len 0x8000`, or use
  `tools/wican_fastread_verify.py` for a region + oracle byte-compare.

## 3. Read the ROM through the NC Flash UI

**Status: NOT YET WIRED.** `src/ui/flash_setup_dialog.py` currently constructs a
`J2534Transport` unconditionally — there is no WiCAN adapter option in the UI, so
the ROM cannot be read through the UI over WiCAN today. This is the pending
**adapter-selector UI + WiCAN settings** task. Until it lands, step 2 (bench tool)
is the supported manual read path.

Once the adapter selector exists, this section should verify:

1. Launch NC Flash; open the flash/read dialog.
2. Select the **WiCAN** adapter; enter host/port (and let it auto-config `slcan`).
3. Read the ECU ROM; confirm a progress indicator and successful completion.
4. Save the dump and byte-compare it to `wican_stmin0_full.bin` (identical).
5. Confirm the device protocol is **restored** to its previous value on disconnect.

## 4. Teardown

- Confirm the WiCAN protocol was restored (the bench tool / configurator does this
  automatically, even on Ctrl-C or error, via the recovery sidecar).
- If a run was killed mid-stream, the device may need a reboot
  (`curl -s -X POST http://<host>/system_reboot`) to clear a wedged CAN channel
  before the next `S6` handshake.
