# Example Files

This directory contains example ROM binary files for testing.

## Files

### lf9veb.bin
Stock ROM binary file for LF9VEB ECU.

- Size: ~1 MB
- Type: OEM stock ROM
- ECU: Mazda MX-5 NC (2006-2015)
- Memory Model: SH7058

## Usage

Load the ROM in the application:
1. Run `python main.py`
2. File → Open → Select `examples/lf9veb.bin`
3. Browse tables in the left panel
4. Click a table to view its data

## Note

These are example files for development and testing. Always keep backups of your actual ECU ROM files.

### nc_standard_roms.ncpack

The standard ROM pack read by **Tools > Move ROM to Its Standard ROM...**: the release images and definitions of the 78 manual-gearbox ROM IDs, the manifest that maps each ID to one of the 18 standard ROMs, and the presets. One tar file, xz-compressed. Built by `tools/standard_manifest.py --pack-file` in the nc-flash-re project; see `docs/internal/STANDARD_ROMS.md`.
