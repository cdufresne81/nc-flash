# ROM Modification Tracking - Quick Summary

**Issue ID:** nc-rom-editor-1kt
**Status:** Open (Not Started)
**Priority:** P2
**Estimated Effort:** 10 weeks (part-time)

---

## What Is This?

A Git-like modification tracking system for ROM edits, replacing manual Excel spreadsheets with automatic change tracking and project organization.

---

## Key Features at a Glance

### 🗂️ Project Management
- Create projects (.ncproj files) to organize related ROMs
- Give ROMs user-friendly aliases ("Stock", "Stage 1", "Final Tune")
- Store everything in a single SQLite database file
- No more confusion with multiple "stock.bin" files

### 📝 Automatic Change Tracking
- Every table edit is recorded automatically
- Stores: what changed, when, old/new values, your comments
- Preserves original ROM for comparison
- Optional comment dialog when saving changes

### 📊 Change History Viewer
- See all modifications in chronological order
- Filter by date, table name, or comment
- Add/edit comments retroactively
- Revert individual changes
- Export reports to PDF/CSV

### 🔍 ROM Comparison
- Compare current ROM to original baseline
- Compare any two ROMs in your project
- Visual diff viewer
- Highlight changed tables and cells
- Show delta values (+5.0, -10%, etc.)

### 📤 Export & Documentation
- Generate professional change logs (PDF/text/CSV)
- Share modification history with tuners/dyno shops
- Complete audit trail for safety

---

## Example Workflow

```
1. Create Project
   └─> "NC1 Stage 1 Tune"

2. Add ROMs
   ├─> "Stock" (baseline)
   └─> "Working Tune" (current)

3. Edit Tables
   └─> App auto-tracks changes
       └─> Optional: add comment "Richened WOT for safety"

4. View History
   └─> See all changes with timestamps and comments

5. Compare ROMs
   └─> Stock vs Working Tune
       └─> Visual diff of all differences

6. Export Report
   └─> PDF changelog for dyno shop
```

---

## Implementation Breakdown

### Phase 1: Project Foundation (2 weeks)
**Goal:** Basic project creation and file management
- ✅ Create/open/save projects
- ✅ Add ROMs with aliases
- ✅ SQLite database backend
- ✅ Basic UI integration

### Phase 2: Change Tracking (2 weeks)
**Goal:** Automatically record modifications
- ✅ Make tables editable
- ✅ Intercept changes
- ✅ Store in database
- ✅ Comment dialog

### Phase 3: Change History UI (2 weeks)
**Goal:** View and manage change history
- ✅ History viewer window
- ✅ Filter by date/table/comment
- ✅ Edit comments
- ✅ Revert changes

### Phase 4: Comparison Features (2 weeks)
**Goal:** Compare ROMs and visualize differences
- ✅ ROM comparison engine
- ✅ Visual diff viewer
- ✅ Modified table indicators
- ✅ Delta calculations

### Phase 5: Export & Polish (2 weeks)
**Goal:** Professional reports and UX polish
- ✅ Export to PDF/CSV/text
- ✅ Recent projects menu
- ✅ Project templates
- ✅ Documentation

**Total: 10 weeks (part-time development)**

---

## Technical Architecture

### Data Storage
```
MyProject.ncproj          # SQLite database
├── Project metadata      # Name, description, author
├── ROMs table            # ROM files with aliases
│   ├── id, alias, path
│   └── baseline_data (original bytes)
└── Changes table         # All modifications
    ├── timestamp, table, cell
    ├── old/new values
    └── comment
```

### File Structure
```
MyTuneProject/
├── MyTuneProject.ncproj      # Project database
├── roms/                     # ROM files
│   ├── stock_original.bin
│   └── working_tune.bin
└── exports/                  # Generated reports
    └── changelog_2026-01-06.pdf
```

---

## Dependencies

**Prerequisites:**
- Table editing functionality (nc-rom-editor-3in.6) - **Must be done first**
- Basic ROM reading/writing - ✅ Already implemented

**New Python Packages:**
```bash
pip install reportlab>=4.0.0  # For PDF export
```

**Existing Packages:**
- SQLite (built into Python)
- PySide6 (already used)

---

## Benefits Over Excel

| Excel Spreadsheet | NC ROM Editor |
|-------------------|---------------|
| ❌ Manual entry | ✅ Automatic tracking |
| ❌ Prone to errors | ✅ Always accurate |
| ❌ Hard to organize | ✅ Projects with aliases |
| ❌ Can't compare ROMs | ✅ Visual diff viewer |
| ❌ No undo | ✅ Revert any change |
| ❌ Hard to share | ✅ Export to PDF |
| ❌ No timestamps | ✅ Precise timestamps |
| ❌ Separate from app | ✅ Integrated workflow |

---

## Why This Matters for Tuning

### Safety
- Complete audit trail of all changes
- Easy to revert mistakes
- Know exactly what was changed when

### Professional Documentation
- Generate reports for dyno shops
- Share change logs with other tuners
- Prove modifications for insurance/regulations

### Efficiency
- No manual data entry
- Automatic tracking saves time
- Easy comparison between versions

### Organization
- Multiple tune versions in one project
- Clear naming with aliases
- All related ROMs in one place

---

## Potential Use Cases

### 1. Progressive Tuning
```
Project: "My NC1 Build"
├─ Stock                    (baseline)
├─ Stage 1 - Intake         (compare to stock)
├─ Stage 2 - Exhaust        (compare to stage 1)
└─ Final - E85 Conversion   (compare to stage 2)
```

### 2. A/B Testing
```
Project: "Fuel Map Testing"
├─ Conservative AFR         (baseline)
├─ Test 1 - Lean            (try leaner)
├─ Test 2 - Rich            (try richer)
└─ Final - Optimal          (best performing)
```

### 3. Multiple Cars
```
Projects/
├─ NC1_MX5.ncproj          (2006 Miata)
├─ NC2_MX5.ncproj          (2009 Miata)
└─ NC3_MX5.ncproj          (2013 Miata)
```

### 4. Before/After Mods
```
Project: "Turbo Install"
├─ Before - NA Tune
└─ After - Turbo Tune
    └─ History: see all changes made for turbo
```

---

## Future Enhancements (Post-Launch)

**Version Control:**
- Branching (try different directions)
- Tags (mark milestones)
- Cherry-pick changes

**Collaboration:**
- Share projects
- Merge changes from multiple people
- Cloud sync

**AI/ML:**
- Suggest common modifications
- Detect potentially dangerous changes
- Learn from successful tunes

**Templates:**
- Save/load modification patterns
- Common tune adjustments
- Community-shared templates

---

## Questions & Decisions

### Q: Where are projects stored?
**A:** User's Documents/NC ROM Editor/Projects/ (configurable)

### Q: How granular is change tracking?
**A:** Individual cell changes (not whole table saves)

### Q: Auto-save or manual?
**A:** Auto-save with optional comment dialog (best of both)

### Q: Can I update the baseline ROM?
**A:** No, baseline stays original. Use snapshots for milestones.

### Q: What happens to the original ROM file?
**A:** Copied into project and preserved. Original file untouched.

### Q: Can I import from my Excel spreadsheet?
**A:** Future feature (Phase 5+)

---

## Getting Started (When Implemented)

**Step 1: Create a Project**
```
File > New Project
├─ Name: "My First Tune"
├─ Description: "Testing modification tracking"
└─ Add initial ROM: stock.bin (alias: "Stock")
```

**Step 2: Open a Table**
```
Double-click table in browser
└─> Opens in new window
```

**Step 3: Edit Values**
```
Click cell > type new value > Enter
└─> App prompts: "Add comment?" (optional)
    ├─ "Increased for better throttle response"
    └─ Save
```

**Step 4: View History**
```
Tools > Change History
└─> See all your modifications
```

**Step 5: Export Report**
```
Tools > Export Change Log > PDF
└─> Professional report generated
```

---

## Next Steps

1. **Review this plan** - Make sure it meets your needs
2. **Prioritize** - Should this be done before or after other features?
3. **Dependencies** - Need table editing (3in.6) first
4. **Feedback** - Any changes to the design?
5. **Implementation** - Start with Phase 1 when ready

---

## Related Issues

- **nc-rom-editor-3in.6** - Table editing operations (prerequisite)
- **nc-rom-editor-3in.8** - ROM comparison tool (synergy)
- **nc-rom-editor-3in.9** - Import/export functionality (synergy)

---

**For full technical details, see:** `MODIFICATION_TRACKING_PLAN.md`
