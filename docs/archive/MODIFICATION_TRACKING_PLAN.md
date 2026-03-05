# ROM Modification Tracking & Project Management
## Implementation Plan

**Issue:** nc-rom-editor-1kt
**Type:** Feature
**Priority:** P2

---

## Overview

Replace manual Excel-based change tracking with a built-in Git-like modification tracking system. Organize multiple ROMs under projects with aliases and maintain complete audit trails of all table edits.

---

## Problem Statement

**Current Pain Points:**
- Users manually track changes in Excel spreadsheets
- Multiple ROMs have same filename (e.g., "stock.bin", "tune.bin")
- No automatic tracking of what was changed when
- Difficult to compare ROM versions
- Hard to share modification history with others
- Risk of forgetting what changes were made

**Goal:**
Create a professional ROM project management system with automatic change tracking, similar to version control for code.

---

## Core Concepts

### 1. Projects (.ncproj files)
- Container for related ROMs
- Single SQLite database file
- Contains: metadata, ROM list, change history
- Example: "NC1_Stage1_Tune.ncproj"

### 2. ROM Aliases
- User-friendly names for ROMs in a project
- Examples: "Stock", "Working Tune", "Final v3"
- Same file can exist in multiple projects with different aliases

### 3. Change Tracking
- Automatic recording of all table modifications
- Each change stores: timestamp, table, old/new values, comment
- Baseline ROM preserved for comparison

### 4. Change History
- Complete audit trail of modifications
- Filterable by date, table, comment
- Editable comments (add notes retroactively)

---

## Data Model

### SQLite Schema

```sql
-- Project metadata
CREATE TABLE project (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    author TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ROMs in this project
CREATE TABLE roms (
    id TEXT PRIMARY KEY,  -- UUID
    alias TEXT NOT NULL,  -- User-friendly name
    file_path TEXT NOT NULL,  -- Relative to .ncproj file
    rom_id TEXT,  -- Detected ROM ID (e.g., "LF9VEB")
    baseline_data BLOB,  -- Original ROM bytes
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notes TEXT
);

-- Table modifications
CREATE TABLE changes (
    id TEXT PRIMARY KEY,  -- UUID
    rom_id TEXT NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    table_name TEXT NOT NULL,
    table_address TEXT NOT NULL,
    table_category TEXT,  -- For filtering

    -- For specific cell changes
    cell_row INTEGER,
    cell_col INTEGER,

    -- Values
    old_value REAL,
    new_value REAL,

    -- Metadata
    comment TEXT,
    author TEXT,

    FOREIGN KEY (rom_id) REFERENCES roms(id)
);

-- Indexes for performance
CREATE INDEX idx_changes_rom_id ON changes(rom_id);
CREATE INDEX idx_changes_timestamp ON changes(timestamp);
CREATE INDEX idx_changes_table_name ON changes(table_name);
```

### File Structure

```
MyTuneProject/
├── MyTuneProject.ncproj      # SQLite database
├── roms/
│   ├── stock_original.bin    # Baseline ROM
│   ├── working_tune.bin      # Current working copy
│   └── final_tune_v3.bin     # Another version
└── exports/                   # Generated reports
    ├── changelog_2026-01-06.pdf
    └── comparison_stock_vs_final.csv
```

---

## Implementation Phases

### Phase 1: Project Foundation (Week 1-2)
**Goal:** Basic project creation and management

**Tasks:**
1. Create `src/project/` module structure
2. Implement `Project` class with SQLite backend
3. Implement `Rom` class for ROM management
4. Create project file format (.ncproj)
5. Add File > New Project dialog
6. Add File > Open Project dialog
7. Add File > Save Project functionality
8. Add "Add ROM to Project" dialog
9. Update main window to show current project

**Files to Create:**
- `src/project/__init__.py`
- `src/project/project.py` - Project class
- `src/project/rom.py` - Rom class
- `src/project/database.py` - SQLite operations
- `src/ui/project_dialogs.py` - New/Open/Add ROM dialogs

**Deliverables:**
- Can create new projects
- Can add ROMs with aliases
- Can save/load projects
- Basic project info shown in UI

---

### Phase 2: Change Tracking Backend (Week 3-4)
**Goal:** Automatically record table modifications

**Tasks:**
1. Create `ChangeTracker` class
2. Modify `TableViewerWindow` to be editable
3. Intercept table value changes
4. Store baseline ROM when adding to project
5. Create `Change` class for modifications
6. Implement change recording to database
7. Add optional comment dialog on save
8. Store original ROM bytes in project

**Files to Create:**
- `src/project/change_tracker.py` - ChangeTracker class
- `src/project/change.py` - Change class
- `src/ui/comment_dialog.py` - Add comment dialog

**Files to Modify:**
- `src/ui/table_viewer_window.py` - Make cells editable
- `src/ui/table_viewer.py` - Enable editing
- `src/core/rom_reader.py` - Track changes when writing

**Deliverables:**
- Table cells are editable
- Changes automatically recorded
- Optional comment dialog on save
- Baseline ROM preserved

---

### Phase 3: Change History UI (Week 5-6)
**Goal:** View and manage change history

**Tasks:**
1. Create `ChangeHistoryWindow` widget
2. Display all changes in table view
3. Add filters: date range, table name, comment search
4. Show old value → new value for each change
5. Allow editing comments on existing changes
6. Add "Revert Change" functionality
7. Add Tools > Change History menu item
8. Group changes by date/table

**Files to Create:**
- `src/ui/change_history_window.py` - Change history viewer
- `src/ui/change_filters.py` - Filter widgets

**Deliverables:**
- Full change history viewer
- Filterable by date, table, comment
- Can add/edit comments
- Can revert individual changes

---

### Phase 4: Comparison Features (Week 7-8)
**Goal:** Compare ROMs and visualize differences

**Tasks:**
1. Create comparison engine
2. Compare current ROM to baseline
3. Compare two ROMs in project
4. Add visual indicators in table browser for modified tables
5. Highlight changed cells in table viewer
6. Show delta values (±X, ±Y%)
7. Create diff viewer window
8. Add Tools > Compare ROMs menu

**Files to Create:**
- `src/project/comparator.py` - ROM comparison engine
- `src/ui/diff_viewer.py` - Visual diff window
- `src/ui/comparison_dialog.py` - Select ROMs to compare

**Files to Modify:**
- `src/ui/table_browser.py` - Add modification indicators
- `src/ui/table_viewer.py` - Highlight changed cells

**Deliverables:**
- Compare any two ROMs
- Visual indicators for modified tables
- Diff viewer with side-by-side comparison
- Delta calculations

---

### Phase 5: Export & Polish (Week 9-10)
**Goal:** Export reports and polish UX

**Tasks:**
1. Export change log to PDF (using reportlab)
2. Export change log to CSV
3. Export change log to text file
4. Add project templates (New Project wizard)
5. Add Recent Projects to File menu
6. Add project backup/archive feature
7. Add settings: auto-comment frequency
8. Polish all dialogs and windows
9. Add comprehensive error handling
10. Write user documentation

**Files to Create:**
- `src/project/exporters/` - Export modules
- `src/project/exporters/pdf_exporter.py`
- `src/project/exporters/csv_exporter.py`
- `src/project/exporters/text_exporter.py`
- `docs/USER_GUIDE_PROJECTS.md`

**New Dependencies:**
- `reportlab` - PDF generation
- Add to `requirements.txt`

**Deliverables:**
- Export to PDF/CSV/text
- Recent projects menu
- Project templates
- Complete documentation

---

## Detailed Task Breakdown

### Phase 1 Subtasks

#### Task 1.1: Create Project Data Model
**Estimated Time:** 4 hours

```python
# src/project/project.py
class Project:
    """Represents a ROM tuning project"""

    def __init__(self, path: str):
        self.path = Path(path)
        self.db = Database(self.path)
        self.roms = []

    @classmethod
    def create(cls, path: str, name: str, description: str):
        """Create new project"""

    def add_rom(self, file_path: str, alias: str) -> Rom:
        """Add ROM to project with alias"""

    def save(self):
        """Save project to disk"""

    def load(self):
        """Load project from disk"""
```

#### Task 1.2: Implement SQLite Backend
**Estimated Time:** 6 hours

- Create database schema
- Implement CRUD operations
- Add migrations support
- Error handling

#### Task 1.3: Create UI Dialogs
**Estimated Time:** 8 hours

- New Project dialog
- Open Project dialog
- Add ROM dialog
- Project settings dialog

---

### Phase 2 Subtasks

#### Task 2.1: Make Tables Editable
**Estimated Time:** 10 hours

```python
# Modify table_viewer.py
class TableViewer(QWidget):
    # Signal emitted when cell is edited
    cell_edited = Signal(int, int, float, float)  # row, col, old, new

    def _display_3d(self, values, x_axis, y_axis):
        # Make cells editable
        for row in range(rows):
            for col in range(cols):
                value_item = QTableWidgetItem(f"{values[row, col]:.4f}")
                value_item.setFlags(value_item.flags() | Qt.ItemIsEditable)
                self.table_widget.setItem(row, col + 1, value_item)

        # Connect cell changed signal
        self.table_widget.cellChanged.connect(self._on_cell_changed)
```

#### Task 2.2: Implement Change Tracking
**Estimated Time:** 12 hours

```python
# src/project/change_tracker.py
class ChangeTracker:
    """Tracks and records ROM modifications"""

    def __init__(self, project: Project, rom: Rom):
        self.project = project
        self.rom = rom

    def record_change(self, table: Table, row: int, col: int,
                     old_value: float, new_value: float,
                     comment: str = None):
        """Record a table modification"""
        change = Change(
            rom_id=self.rom.id,
            timestamp=datetime.now(),
            table_name=table.name,
            table_address=table.address,
            cell_row=row,
            cell_col=col,
            old_value=old_value,
            new_value=new_value,
            comment=comment
        )
        self.project.db.save_change(change)
```

---

## UI Mockups

### New Project Dialog
```
┌─────────────────────────────────────────┐
│ Create New Project                      │
├─────────────────────────────────────────┤
│                                         │
│ Project Name: [___________________]    │
│                                         │
│ Description:                            │
│ [_________________________________]    │
│ [_________________________________]    │
│ [_________________________________]    │
│                                         │
│ Location:                               │
│ [C:\Users\...\MyProjects  ] [Browse]   │
│                                         │
│ Add initial ROM (optional):             │
│ [C:\ROMs\stock.bin        ] [Browse]   │
│ Alias: [Stock Original    ]             │
│                                         │
│              [Cancel]  [Create Project] │
└─────────────────────────────────────────┘
```

### Change History Window
```
┌─────────────────────────────────────────────────────────────────┐
│ Change History - NC1 Stage 1 Tune                              │
├─────────────────────────────────────────────────────────────────┤
│ ROM: [Working Tune v2 ▼]                                       │
│ Filter: Table [All ▼] Date [Last 30 days ▼] [Search: ______] │
├─────────────────────────────────────────────────────────────────┤
│ Date/Time    │ Table           │ Cell  │ Change      │ Comment │
│──────────────┼─────────────────┼───────┼─────────────┼─────────│
│ 2026-01-06   │ Fuel Main VE    │ (5,3) │ 85.0 → 87.5 │ Rich-   │
│ 14:30:22     │                 │       │ (+2.5)      │ ened    │
│──────────────┼─────────────────┼───────┼─────────────┼─────────│
│ 2026-01-06   │ Ignition Timing │ (2,1) │ 12.0 → 14.0 │ Adv.    │
│ 14:25:10     │                 │       │ (+2.0)      │ timing  │
│──────────────┼─────────────────┼───────┼─────────────┼─────────│
│ [47 more changes...]                                           │
├─────────────────────────────────────────────────────────────────┤
│ Selected change actions:                                       │
│ [Edit Comment] [Revert Change] [Export Selection]             │
└─────────────────────────────────────────────────────────────────┘
```

### Add Comment Dialog
```
┌─────────────────────────────────────────┐
│ Add Comment                             │
├─────────────────────────────────────────┤
│ Table: Fuel Main VE                     │
│ Changed: 3 cells                        │
│                                         │
│ Comment (optional):                     │
│ [_________________________________]    │
│ [_________________________________]    │
│                                         │
│ ☐ Don't ask again this session          │
│                                         │
│       [Skip Comment]  [Save with Comment]│
└─────────────────────────────────────────┘
```

---

## Testing Strategy

### Unit Tests
- Project creation/loading
- ROM addition/removal
- Change recording
- Database operations
- Export functions

### Integration Tests
- Full workflow: create project → add ROM → edit table → view history
- Multi-ROM project management
- Change tracking accuracy
- Comparison calculations

### Manual Tests
- UI usability
- Performance with large change logs (1000+ changes)
- File corruption handling
- Multi-user scenarios (future)

---

## Dependencies & Prerequisites

**Required Features:**
- ✅ Table editing operations (nc-rom-editor-3in.6)
- Basic ROM reading/writing

**Nice to Have:**
- ROM comparison tool (nc-rom-editor-3in.8)
- Import/export functionality (nc-rom-editor-3in.9)

**New Python Dependencies:**
```
reportlab>=4.0.0    # PDF generation
```

---

## Risks & Mitigations

### Risk 1: Database Corruption
**Mitigation:**
- Auto-backup before writes
- WAL mode for SQLite
- Regular integrity checks

### Risk 2: Performance with Large Projects
**Mitigation:**
- Database indexing
- Lazy loading of changes
- Pagination in history view

### Risk 3: User Adoption
**Mitigation:**
- Import from Excel (Phase 5)
- Simple project creation wizard
- Clear documentation

---

## Success Metrics

**Must Have:**
- ✅ Can create projects and add ROMs
- ✅ All table edits are tracked automatically
- ✅ Can view complete change history
- ✅ Can export change log to PDF/CSV

**Nice to Have:**
- Side-by-side ROM comparison
- Visual diff viewer
- Change templates
- Collaboration features (future)

---

## Timeline Estimate

| Phase | Duration | Tasks |
|-------|----------|-------|
| Phase 1: Project Foundation | 2 weeks | 9 tasks |
| Phase 2: Change Tracking | 2 weeks | 8 tasks |
| Phase 3: Change History UI | 2 weeks | 8 tasks |
| Phase 4: Comparison Features | 2 weeks | 8 tasks |
| Phase 5: Export & Polish | 2 weeks | 10 tasks |
| **Total** | **10 weeks** | **43 tasks** |

**Note:** Timeline assumes ~20 hours/week of development time

---

## Future Enhancements (Post-v1)

### Version Control Features
- Branching (try different tunes)
- Tagging (mark milestone versions)
- Cherry-pick changes between ROMs

### Collaboration
- Share projects with others
- Merge changes from multiple users
- Cloud sync (optional)

### Advanced Analysis
- Change impact analysis
- Common modification patterns
- Machine learning suggestions

### Templates & Presets
- Save/load modification templates
- Common tune adjustments (e.g., "Stage 1 AFR")
- Share templates with community

---

## Questions to Consider

1. **Storage Location:** Where should .ncproj files be stored by default?
   - User's Documents folder?
   - Alongside ROM files?
   - User configurable?

2. **Change Granularity:** Track individual cell changes or whole table saves?
   - Individual cells = more detail, more storage
   - Whole tables = simpler, less detail

3. **Auto-save:** Should changes be auto-saved or require explicit save?
   - Auto-save = safer, always tracked
   - Manual save = more control, may forget

4. **Baseline Strategy:** When to update baseline ROM?
   - Never (baseline is always original)
   - On user request
   - Create snapshots periodically

**Recommended Answers:**
1. User's Documents/NC Flash/Projects/ (configurable)
2. Individual cells (better for detailed tracking)
3. Auto-save with confirmation dialog (best of both)
4. Baseline never changes, use snapshots instead

---

## Implementation Notes

### Key Design Decisions

**Why SQLite?**
- Single file (portable)
- No server required
- Excellent query performance
- Built into Python
- Supports BLOB for ROM storage

**Why Not JSON?**
- Slower for large datasets
- No query capabilities
- Manual indexing required
- File size grows quickly

**Why Track Individual Cells?**
- More granular history
- Better for undo/redo
- Easier to find specific changes
- More professional audit trail

---

## Getting Started (For Implementer)

1. Review this plan thoroughly
2. Start with Phase 1, Task 1.1
3. Create feature branch: `feature/modification-tracking`
4. Implement incrementally, commit often
5. Add tests for each component
6. Update this document with learnings
7. Demo each phase to stakeholders

---

**Document Version:** 1.0
**Last Updated:** 2026-01-06
**Issue:** nc-rom-editor-1kt
**Author:** Claude (with user requirements)
