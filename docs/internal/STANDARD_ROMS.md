# Standard ROMs

How NC Flash moves a ROM onto the "standard" ROM of its line (Tools > Move ROM to Its Standard ROM...).
The analysis behind it lives in the nc-flash-re project (issue #30, `analysis/rom_traits/2026-11-13_standard_roms_level2.md`).

## The idea

The MX-5 NC factory images come as 78 ROM IDs. They reduce to 18 standards: the latest revision Mazda issued for each of the 13 software lines, plus 5 older revisions kept for the variants Mazda never built on the latest one. Every other ID is one of:

- an older revision of a standard (same car, earlier software), or
- a variant of it (gearbox, market, engine) that differs by a small byte preset.

So patches and definitions only need to target the 18.

The 5 older revisions are LF9HED, LFDJEA, LFGJED, LFLMEC and LFLMEA. A car whose variant exists on the latest revision of its line still goes there; only the 9 IDs with no such factory image (for example a 2010 federal 6-speed, LFDJEA) stay on the older revision, and so do not get Mazda's later changes. The number is not in the code: NC Flash follows whatever the pack's manifest says.

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
- An ID whose variant has no factory image on its standard ("carried" in the manifest) is refused. The current pack has none; a pack built before the 18-standard plan has 9.
- The boot block is never changed.
- Checksums are not recomputed here; the flash path does that.

## The standard pack

The feature reads everything from one pack file. NC Flash ships one, `examples/nc_standard_roms.ncpack` (bundled with the whole `examples` folder); the dialog starts with it unless the user picked another pack that still exists. The shipped pack's path is never stored in settings, because it changes with each install. The pack holds the factory-based release images of all 78 IDs. It is built in nc-flash-re, and the copy here is replaced by hand when the manifest changes:

```bash
python tools/standard_manifest.py --pack-file standard.ncpack
```

The file is about 2 MB. It is a tar of the layout below, xz-compressed, and is read into memory (about 110 MB while the dialog is open); nothing is extracted to disk except one definition at a time, for as long as it takes to parse. A pack can also be a plain folder with the same layout (`--pack <folder>`, about 110 MB), picked with the **Folder...** button.

All 78 images are in the pack, not only the 18 standards: an edit is found by comparing a ROM with the factory image of its own ID.

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

## Files with a header

Some reading tools save the 1 MB image behind a header of tagged blocks (4-letter tag, 4-byte big-endian length, data): `INFO`, `DRMI`, `MEML`, then `MEMD` holding the image to the end of the file (324 bytes of header on the sample seen). `unwrap_rom` returns the image inside such a file and a note for the report; any other file is used as it is. Only the dialog's input goes through it. The converted ROM is always written as a plain 1 MB image.
