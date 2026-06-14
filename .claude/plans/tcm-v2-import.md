# TCM V2 Definition Import

## Phase A — V2 TCM definition import (this branch: feature/tcm-v2-definitions)

**Goal:** Make the V2 (NC2-era) TCM ROMs readable/editable in NC Flash by importing their RomDrop XML definitions. Read-only-grade scope: data + tests + docs, **zero `src/` changes**. Closes issue #70.

**What was done:**

- **Imported 4 V2 TCM definitions** from the NC_TCM project into `examples/metadata/`:
  - `LFG1TF000_v02.xml`
  - `LFG1TG000_v02.xml`
  - `LFACTA000_v02.xml`
  - `LFAMTA000_v02.xml`
- **Removed the legacy V1 definition** `examples/metadata/lfg1tf000.xml` (superseded by the V2 import).
- **Added a real TCM dump** `examples/LFG1TF000.bin` for detection/validation.
- **Added `tests/test_tcm_v2_detection.py`** — verifies the existing `RomDetector`/`DefinitionParser` correctly identify the V2 TCM ROM and load its definition.

**Why no code changed:**

- **Same XML schema.** V2 TCM definitions use the **identical RomDrop XML schema** already consumed by the existing ECU definitions. `DefinitionParser` and `RomDetector` parse them unchanged — there is no new table layout, scaling type, or structural element to support.
- **Detection works out of the box.** The V2 TCM ROM is matched via `internalidstring` `SW-LFG1TF000.HEX` located at `internalidaddress` 0x10612, exactly the mechanism the detector already uses for ECU ROMs. No detector logic needed touching.

**Validation status:**

- **LFG1TF000 — hardware-validated.** Detection and table reads confirmed against the real dump `examples/LFG1TF000.bin`.
- **LFG1TG000 / LFACTA000 / LFAMTA000 — NOT yet validated.** These were imported from NC_TCM but no real dumps are on hand. They must be checked against genuine ROM dumps before being trusted; treat their addresses/scalings as provisional until then.

**Net effect:** Users can open and inspect V2 TCM ROMs (reads only — there is no TCM write path). No risk to any existing ECU functionality because nothing in `src/` was modified.

---

## Phase B — TCM flashing (FUTURE, NOT in this branch)

**Status: does not exist anywhere yet.** No tool — public or private — can currently write/flash a TCM ROM. The NC_TCM README explicitly states there is **"no tool released to update the ROM."** This is therefore genuine reverse-engineering R&D, not a port of an existing capability. Phase A only makes the TCM *readable*; making it *writable* is an entirely separate, much higher-risk effort.

**Known unknowns to investigate before any TCM flash attempt:**

- **TCM CAN/UDS module addressing** — The TCM lives at a different bus address / diagnostic node than the ECU. The ECU flash path's addressing and request/response IDs do **not** transfer. Must be discovered and confirmed on the bus.
- **Security access (seed/key) algorithm for the TCM** — The ECU's seed→key algorithm ("MazdA" + 3-byte seed → 8-byte LFSR) is ECU-specific and almost certainly does **not** apply to the TCM. The TCM's own seed/key algorithm must be reverse-engineered (likely from the TCM bootloader/firmware), with no assumption of reuse.
- **Secondary bootloader (SBL) for SH7055 / SH7058** — Renesas SH-2 flashing typically requires uploading a secondary bootloader into RAM that performs the actual erase/program. The correct SBL for the TCM's specific microcontroller (SH7055 vs SH7058) and its upload/handoff sequence are unknown.
- **Erase / program command sequences** — The exact UDS (or kernel-level) command sequence to erase flash blocks and program new data on the TCM is unknown and must be derived empirically/from RE.
- **Checksum module** — The TCM ROM's checksum scheme (algorithm, covered ranges, table location) is unknown. It differs from the Mazda ECU checksum table at 0xFF650 and must be worked out independently before any written image will be accepted/run.

**Hard guardrails for Phase B:**

- **Must NOT touch the validated ECU flash path.** The ECU read/flash code (security algorithm, flash manager, J2534 bridge) is hardware-proven and live. TCM flashing must be built as a fully separate path with no shared mutable state and no edits to ECU flashing logic.
- **Must be bench-tested on a sacrificial TCM.** First write attempts go to a throwaway/bench TCM only — never a vehicle's installed unit. A bricked TCM in a car is a tow + dealer event; per the project's safety-critical posture, no exceptions.

**Recommendation:** Track Phase B as its **own dedicated GitHub issue** (separate from #70). It is a multi-stage RE project with hardware risk, and conflating it with the Phase A definition import would understate its scope. **Now tracked as #72.**
