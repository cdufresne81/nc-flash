# Standard ROMs

How NC Flash moves a ROM onto the "standard" ROM of its line (Tools > Move ROM to Its Standard ROM...).
The analysis behind it lives in the nc-flash-re project (issue #30, `analysis/rom_traits/2026-11-13_standard_roms_level2.md`).

## The idea

The MX-5 NC factory images come as 78 ROM IDs. They reduce to 13 standards: the latest revision Mazda issued for each software line. Every other ID is one of:

- an older revision of a standard (same car, earlier software), or
- a variant of it (gearbox, market, engine) that differs by a small byte preset.

So patches and definitions only need to target the 13.

## What the feature does

1. **Identify.** Reads the ROM ID from the file and looks it up: which standard, which image the car would run, and whether that is a newer revision.
2. **Base.** Standard image + the variant's preset. The result must equal the variant's own release image outside the boot block; otherwise it stops.
3. **Move the tune.** A table counts as edited when it differs from the factory values of the ROM's own ID. It is moved to the base by table name.
4. **Check.** The release manifest's checks run on the result. A failure means nothing is written.
5. **Save.** A new ROM file plus `<name>.report.json`. The source ROM is never changed.

## Rules (all in `src/core/standard_rom.py`)

- A table moves only if its name is unambiguous in both definitions, its size and scaling are the same on both sides, and **Mazda's factory values for it are the same in both programs**. If Mazda changed the table between the two revisions, it is listed as not moved and the user decides.
- A table and its "(Data Integrity)" copy move together or not at all. The "(pair with ...)" suffix is ignored when matching names because it holds an address.
- Nothing outside defined tables is copied: no patch code, no feature blocks, no unnamed bytes. Feature settings (the release manifest's `no_transfer` ranges) are never copied.
- An ID whose variant has no factory image on the standard ("carried" in the manifest) is refused.
- The boot block is never changed.
- Checksums are not recomputed here; the flash path does that.

## The standard pack

The feature reads everything from a folder the user picks (remembered in settings). NC Flash ships no ROM images. The pack is built in nc-flash-re:

```bash
python tools/standard_manifest.py --pack <folder>
```

Layout:

```
manifest.json               release manifest (image hashes, no_transfer, code_pin, checks)
standard_manifest.json      ROM ID -> standard / image / preset
presets/<image>_on_<standard>.json
<ROMID>/<romid>_ncf_<release>.bin
<ROMID>/<romid>.xml
```

Every image is checked against its hash in `manifest.json` before use.

## Limits

- Tables are matched by name across two definitions. Between revisions that is a claim about meaning, not a proof.
- A table that is not moved keeps the factory values of the new ROM, so the car can drive differently from the source tune until the user moves it by hand.
- A tune from before the Data Integrity copies were exposed often has a table edited without its copy. The checks refuse it and name the table; the tool does not make the two halves equal on its own.
- The original ROM ID is recorded in the report only. Nothing in the converted image says what the car ran before.
- No converted ROM has been run on a car. Treat the result as something to review, not something cleared to flash.
