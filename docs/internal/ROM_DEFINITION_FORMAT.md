# ROM Definition File Format

## Overview

RomDrop uses XML files to define the structure and location of data within ECU ROM binary files. This document describes the format based on the NC Miata (LF9VEB) ROM definition.

## File Structure

```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<roms>
  <rom>
    <romid>...</romid>
    <scaling>...</scaling>  <!-- Multiple scaling definitions -->
    <table>...</table>      <!-- Multiple table definitions -->
  </rom>
</roms>
```

## ROM ID Section

Identifies the ROM and ECU metadata:

```xml
<romid>
  <xmlid>LF9VEB</xmlid>
  <internalidaddress>b8046</internalidaddress>
  <internalidstring>LF9VEB</internalidstring>
  <ecuid>LF9VEB</ecuid>
  <make>Mazda</make>
  <model>MX5</model>
  <flashmethod>Romdrop</flashmethod>
  <memmodel>SH7058</memmodel>
  <checksummodule>21053000</checksummodule>
</romid>
```

**Key Fields:**
- `xmlid`: ROM identifier used in filename matching
- `internalidaddress`: Hex address where ROM ID is stored in the binary
- `internalidstring`: Expected string at that address
- `checksummodule`: Checksum algorithm identifier
- `memmodel`: CPU model (SH7058 for NC Miata)

## Scaling Definitions

Scalings define how raw binary values are converted to human-readable units:

```xml
<scaling
  name="851844"
  units="degrees"
  toexpr="x"
  frexpr="x"
  format="%0.2f"
  min="-12.000"
  max="59.900"
  inc="1"
  storagetype="float"
  endian="big"
/>
```

**Attributes:**
- `name`: Unique identifier (often an address)
- `units`: Unit of measurement (optional)
- `toexpr`: Expression to convert FROM raw to display (e.g., "x*0.01" or "x-40")
- `frexpr`: Expression to convert FROM display to raw
- `format`: Printf-style format string for display
- `min`/`max`: Display value range
- `inc`: Increment for editing
- `storagetype`: Data type (`float`, `uint8`, `uint16`, `int16`, etc.)
- `endian`: Byte order (`big` or `little`)

## Table Definitions

Tables define calibration maps/data in the ROM.

### 1D Tables (Single values)

```xml
<table
  level="4"
  type="1D"
  category="Fuel IPW - Base"
  swapxy="true"
  name="IPW Base"
  address="cc8a4"
  elements="1"
  scaling="837796"
/>
```

### 2D Tables (Single dimension array)

```xml
<table
  level="1"
  type="2D"
  category="DBW - Cruise Control"
  swapxy="true"
  name="CC Sensitivity - Non Gear"
  address="b902c"
  elements="39"
  scaling="757804"
>
  <table
    name="VSS_DELTA_525C"
    address="b8f90"
    elements="39"
    scaling="757648_SPD"
    type="Y Axis"
  />
</table>
```

### 3D Tables (2D grid/map)

```xml
<table
  level="4"
  type="3D"
  category="Spark Target - Base"
  swapxy="true"
  name="Spark Target | High Fuel Demand, High-Det"
  address="d01dc"
  elements="225"
  scaling="852444"
>
  <table
    name="LOAD_6F44"
    address="d01a0"
    elements="15"
    scaling="852384"
    type="X Axis"
  />
  <table
    name="RPM_6DD8"
    address="d0164"
    elements="15"
    scaling="852324"
    type="Y Axis"
  />
</table>
```

**Attributes:**
- `level`: Priority/visibility level (1-4, where 4 is most important)
- `type`: `1D`, `2D`, or `3D`
- `category`: Grouping category for UI organization
- `swapxy`: Whether to transpose the table display
- `name`: Human-readable table name
- `address`: Hex address in ROM file where data starts
- `elements`: Total number of data elements
  - 1D: Just the count
  - 2D: Number of Y values (X axis defined in child table)
  - 3D: X elements × Y elements (e.g., 15 × 15 = 225)
- `scaling`: Reference to a scaling definition by name

**Child Tables:**
- 2D tables have 1 child: Y Axis (the independent variable)
- 3D tables have 2 children: X Axis and Y Axis

### Interleaved 3D Tables (`layout="interleaved"`)

TCM-style self-describing tables where the axes and data share one in-ROM
structure instead of living at separate addresses:

```
[M][N][X axis: M bytes][Row 0: Y0 D0..DM-1][Row 1: Y1 D0..DM-1]...
```

- `M` (1 byte) = column count, `N` (1 byte) = row count
- Each row is `M+1` bytes: 1 Y-axis byte followed by M data bytes

```xml
<table type="3D" layout="interleaved" name="Protect_Ch4_Output_5x3"
       address="705c3" elements="15" scaling="raw_u8">
  <table name="Axis X" address="705c5" elements="5" scaling="raw_u8" type="X Axis"/>
  <table name="Axis Y" address="705ca" elements="3" scaling="raw_u8" type="Y Axis"/>
</table>
```

**Rules:**
- `address` MUST point at the `[M][N]` dimension header — not at the X axis,
  not at the first data byte.
- The child axis addresses are ignored for interleaved tables (everything is
  derived from the table address); the children supply the **scaling** and the
  **expected element counts**.
- The reader validates the ROM's `[M][N]` header against the children's
  `elements` and refuses to load the table on mismatch. A mismatch almost
  always means the `address` is stale — e.g. a definition copied from another
  firmware whose data regions shifted (LF9KT vs LFG1T shifted the same tables
  by -4/+72/+216 bytes). Without this check, a stale address renders a
  garbage grid whose dimensions come from arbitrary data bytes.

## Address Format

All addresses are in **hexadecimal** without `0x` prefix (e.g., `d01dc`).

## Data Storage

- Data is stored as **big-endian** (most significant byte first)
- Common storage types:
  - `float`: 32-bit IEEE 754 floating point
  - `uint8`: 8-bit unsigned integer
  - `uint16`: 16-bit unsigned integer
  - `int16`: 16-bit signed integer

## Example: Reading a 3D Table

Given this definition:
```xml
<table type="3D" address="d01dc" elements="225" scaling="852444">
  <table type="X Axis" address="d01a0" elements="15" scaling="852384"/>
  <table type="Y Axis" address="d0164" elements="15" scaling="852324"/>
</table>
```

Steps to read:
1. Read 15 floats (big-endian) starting at `0xd01a0` for X axis
2. Read 15 floats (big-endian) starting at `0xd0164` for Y axis
3. Read 225 floats (15×15 grid) starting at `0xd01dc` for Z values
4. Apply scaling transformations using the `toexpr` from each scaling definition
5. Display as 2D grid with X axis columns and Y axis rows

## Categories of Interest

Key tuning categories in the NC Miata ROM:
- `Fuel IPW - Base`: Base fuel injector pulse width
- `Spark Target - Base`: Ignition timing maps
- `DBW - Throttle Position`: Throttle mapping
- `Fuel - Target`: Target air-fuel ratio/lambda
- `Spark Correction - Knock Retard`: Knock protection

## Notes

- The ROM definition file (lf9veb.xml) is **3461 lines** long
- Contains hundreds of tables and scalings
- Many tables have variations for different conditions (e.g., different gears, det/non-det, flex fuel)
- Some tables are marked `[Flex]` for flex fuel specific calibrations
