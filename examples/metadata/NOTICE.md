# ROM definitions: origin and permission

Most of the NC ECU definitions in this folder come from speeps' RomDrop project:

- Source: https://github.com/speepsio/romdrop, `metadata/` folder
- Commit: `647f409bf2c27e3576696bbfec412a23b191705e` (2021-07-18)
- Permission: speeps gave NC Flash permission by email to include these
  definitions.

The reverse-engineering work behind these tables is speeps'. Thank you.

## Changes since the import

- Reformatted for readability: indentation, LF line endings, no byte-order mark,
  empty elements self-closed. No content change.
- "KR Accumulator - Exit Delay" is a 1-byte value in every calibration. The
  original files defined it as a 4-byte float, so saving it overwrote the
  knock-retard increment rate next to it.
- Left out: `l3r3ee.xml` (a Mazda6 ECU, not an NC; one of its tables was also
  sized wrong).
- `lf9veb.xml` is not speeps' file. It is NC Flash's own, extended version
  (built from speeps' file, maintained in nc-flash-re).

Not from speeps: `LFG1TF000_v02.xml`, `LFG1TG000_v02.xml` (TCM definitions from
the NC_TCM project).

Check any edit with `python tools/metadata_lint.py check`.
