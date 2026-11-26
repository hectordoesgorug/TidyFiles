# Project Progress

## Current status

The M0 foundation and minimal GUI shell are implemented. The application opens
with an empty collection. M1 parser, evaluator, extension resolution, and pure filename validation are
complete, including M1-D.12's DD-061 parser diagnostic correction. They remain
pure filename-engine components; M2-D and M2-E4 connect them to read-only
evaluation and GUI previews. M3-G connects reviewed, journaled Bulk Rename and
managed Trash cleanup to the desktop interface. Duplicate analysis and media
repacking remain unavailable.
See `docs/FILENAME_ENGINE_COVERAGE.md` for the decision-by-decision audit and
historical verification record.

M2-A adds the GUI-independent shared working collection and import-session
lifecycle. M2-B adds bounded read-only discovery, metadata snapshots, exclusions,
cooperative cancellation, and incomplete-import records; M2-E2 connects it to
explicit Add Files and Add Folder requests. M2-C adds deterministic sorting,
manual ordering, non-destructive row management, persistent sort preferences,
and snapshot revision checks. M2-D adds bounded directory counts, ordered
filename evaluation, request-currency checks, progress, and cancellation.

M2-E1 adds the shared main window and per-mode column customization. M2-E2
connects progressive imports, M2-E3 connects collection management, and M2-E4
connects session-only format controls and live read-only filename previews.
M2-E5 improves large-table updates, clarifies preview status and refresh
behavior, and adds lifecycle regression coverage. M2 is complete. M3-A adds a
standalone read-only destination planner. M3-B adds read-only preflight for
reviewed plans. M3-C adds journals, recovery discovery, and exclusive job
coordination. M3-D adds a standalone Windows rename executor, M3-E adds
standalone managed Trash transfers, and M3-F adds read-only recovery inspection,
overlap protection, and guarded cleanup services. M3-G connects Bulk Rename,
startup recovery notices, and managed Trash cleanup. Duplicate and Repack
controls remain disabled.

| Milestone | Status | Scope |
| --- | --- | --- |
| M0-A | Complete | Baseline repository and legacy-reference inspection. |
| M0-B | Complete | Python project foundation, tests, portable storage, configuration, rotating logging, single-instance protection, and Windows CI. |
| M0-C | Complete | Minimal CustomTkinter shell with three tabs and an empty shared Treeview. |
| M1 | Complete | Pure parser/evaluator, extension resolution, and filename validation, including the M1-D.12 DD-061 diagnostic correction. Application and filesystem integration remain outside M1. |
| M2 | Complete | Collection, discovery, management, filename integration and GUI previews accepted by the owner. |
| M2-D | Complete | Collection-backed filename evaluation, physical directory counts, bounded metadata cache, structured diagnostics, request currency, background progress and cancellation. |
| M2-E | Complete | Shared collection, import, management, and preview GUI. |
| M2-E1 | Complete | Main-window layout, shared empty collection presentation, context-specific columns, and persisted appearance and geometry preferences. |
| M2-E2 | Complete | Native import controls, progressive discovery, cancellation, ordering, and incomplete-import reporting. |
| M2-E3 | Complete | Shared sorting controls and preference persistence, manual ordering and drag-and-drop, selection shortcuts, row context actions, non-destructive removal and clearing, and collection-change notifications. |
| M2-E4 | Complete | Session-only live filename-format controls, token insertion and reference, background ordered previews, structured diagnostics, metadata refresh, and stale-result protection. |
| M2-E5 | Complete | Targeted large-table optimization, preview guidance, and lifecycle coverage. Owner manual acceptance passed 21/21. |
| M3 | In progress | Destination planning, preflight, journals, standalone rename and Trash services, recovery inspection, overlap protection, cleanup, and Bulk Rename GUI integration are implemented. Milestone acceptance remains subject to review. |
| M3-A | Complete | Standalone destination planning, collision and dependency classification, and read-only collision-only subset replanning. |
| M3-B | Complete | Review-time identity capture and cancellable read-only execution preflight. |
| M3-C | Complete | Durable journals, persistent recovery locator, recovery discovery, and modifying-job coordination. |
| M3-D | Complete | Standalone, journaled, handle-relative rename and same-volume Single Folder execution. |
| M3-E | Complete | Standalone recoverable deletion, guarded cross-volume copying, and managed Trash discovery exclusions. |
| M3-F | Implemented | Read-only recovery observations, overlap guards, temporary artifact cleanup, Auto-clean, and guarded managed Trash clearing (DD-087). |
| M3-G | Implemented | Confirmed Bulk Rename execution, startup recovery notice, Auto-clean, manual Trash clearing, and normal-exit Auto-Clear Trash. |
| M4 | Planned | Duplicate analysis and verification. |
| M5 | Planned | Media repacking and external-tool integration. |
| M6 | Planned | Complete GUI workflows, settings, profiles, shortcuts, themes, and persistent layout. |
| M6.1 | Planned | Package restructuring and architectural cleanup after M6 and before M7; improve source organization and maintainability without changing established application behavior. |
| M6.2 | Planned | Public documentation sanitization and repository audit after M6.1 and before M7; required before the first stable v1.0.0 release. |
| M7 | Planned | End-to-end integration, packaging, release readiness, and coverage closure. |

The later milestone descriptions follow the local implementation roadmap; they
are plans, not implementation evidence.

## M2-E1 — Main window and shared collection presentation

The main window uses a vertical, responsive layout with a compact Settings
header, prominent Bulk Rename / Duplicate Finder / Repacker tabs, compact
mode-specific panels, a common collection toolbar, a full-width Treeview with
horizontal and vertical scrollbars, movement controls, a collection summary,
and a shared progress/status/action area. The table receives the expandable
window space. Fresh startup selects Bulk Rename and constructs one empty
`WorkingCollection` from saved sort preferences. All tabs use that collection
and the same Treeview; row IDs are stable collection entry IDs. Temporary
collection membership, selection, manual order, and unsaved format edits are
not saved.

DD-079 through DD-085 are recorded as follows:

| Decision | M2-E1 status |
| --- | --- |
| DD-079 — Horizontal window layout with full-width Treeview | Implemented with responsive grid geometry; no permanent sidebar. |
| DD-080 — Context-specific, customizable columns | Implemented with the approved per-mode defaults, fixed order, required-column enforcement, optional visibility, saved widths, native header resizing, Restore Defaults, and header context-menu access. |
| DD-081 — Shared collection toolbar and list interactions | M2-E2 enables Add Files and Add Folder. M2-E3 enables sorting, manual movement, Remove from List, and Clear All against the shared collection. |
| DD-082 — Compact Bulk Rename format controls | The editable format field and three approved built-in presets are present. M2-E4 adds Insert Token, Format Reference, and live evaluation. Profiles remain deferred, and unsaved field edits are not persisted. |
| DD-083 — Optional post-processing Counter | The switch is present, defaults OFF, and has no execution effect. The algorithm remains deferred. |
| DD-084 — Native toggle switches for boolean operation options | Single Folder, Auto-clean, and Counter use CustomTkinter switches. Single Folder changes destination planning; Auto-clean is an explicit default-off setting; Counter remains UI-only. |
| DD-085 — Problematic-row highlighting and expression-wide errors | M2-E1 supplied the subtle problem-row Treeview style. M2-E4 implements live evaluation, row diagnostics, expression-wide invalidation, and preview clearing. |

The default visible columns are:

| Mode | Default visible columns, in order |
| --- | --- |
| Bulk Rename | Row, Original filename, New filename, Size, Status |
| Duplicate Finder | Row, Filename, Size, Group, Status |
| Repacker | Row, Original filename, New filename, Size, Resolution, Length, Status |

Initial widths in logical pixels are Row 50; Original filename 280; New
filename 280; Filename 320; Status 130; Size 100; Group 90; Resolution 120;
Length 100; Extension 85; File type 100; Date created and Date modified 160
each; Parent directory and Import root 200 each; Full path 400. These are
resizable starting values, and per-mode adjusted widths (including hidden
columns) persist independently.

Bulk Rename, Duplicate Finder, and Repacker have separate default column
layouts. Optional columns include Resolution, Length, Extension, File type, Date
created, Date modified, Parent directory, Import root, and Full path; Row and
Size may be hidden. Filename, New filename where applicable, and Status cannot
be hidden. Width preferences, including hidden columns, are independent per
mode. Missing, invalid, and unknown saved preferences fall back safely without
altering unrelated settings. “— (not probed)” identifies applicable media
metadata that is not available yet; blank cells indicate non-applicability.
Resolution and length probing, duplicate group data, proposed names, and
processing statuses are not fabricated. File type is inferred from the
filename extension using Python's MIME database; file contents are not checked.

The header Settings dialog exposes System, Light, and Dark appearance modes.
System is the default and updates the Treeview styling with the detected system
appearance. Window size and position are saved in the existing configuration
and clamped to the current Tk virtual-screen bounds. Physical multi-monitor and
DPI-change verification remains outstanding.

Representative empty-state Windows GUI captures are in
[`screenshots/m2-e1/`](screenshots/m2-e1/): [Bulk Rename](screenshots/m2-e1/bulk-rename.png),
[Duplicate Finder](screenshots/m2-e1/duplicate-finder.png), [Repacker](screenshots/m2-e1/repacker.png),
and [Columns customization](screenshots/m2-e1/columns.png).

Counter's deferred contract is read-only validation after a completed rename,
deduplication, or repacking operation and applicable cleanup. It will validate
resulting directories independently, inspect actual resulting filenames,
infer varying numerical sequence components across files while ignoring
constant numbers, and compare the inferred values with the contiguous range
from 1 through the relevant file count. It will identify missing, repeated, and
out-of-range values; report ambiguous or undetectable sequences as inconclusive;
never guess between equally plausible components; never modify files; never
block, reverse, or invalidate completed processing; and include findings in
the operation summary. Mixed numbered/unnumbered files, partial operation
outcomes, and unrelated files already in result directories remain open until
Counter implementation is specified.

The later DD-085 contract requires expression-wide syntax errors to highlight
the format field, show one diagnostic, clear stale filename previews, and mark
affected rows Invalid format while keeping row backgrounds neutral. File-specific
errors highlight only affected rows subtly, retain textual status, and expose
detailed diagnostics through a row context menu. Normal rows stay neutral;
text supplements color; contrast and selection remain clear in Light and Dark
modes; stale previews are not shown as current; any evaluation error invalidates
the rename batch; and destination-collision checks remain assigned to M3.

At M2-E1 completion, all unconnected controls were disabled and no automatic
import, evaluation, cleanup, or file operation occurred. M2-E2 now starts
discovery only after an explicit Add Files or Add Folder request; it does not
automatically evaluate or change files. The main window exposes the shared
collection as `root.application.collection` and retains
`root.application.refresh_collection()` for infrequent complete refreshes.

## M2-E2 — Import controls and progressive discovery

Native Add Files and recursive Add Folder dialogs delegate directly to the
existing discovery service. Accepted entries appear progressively in the
shared Treeview with stable collection-entry UUIDs and stored metadata only.
The filename-evaluation service remains unconnected; no contents are opened,
media is probed, or filenames are proposed. Existing path recognition prevents
duplicate entries, and first-established import contexts and explicit folder
roots remain collection-owned.

`ImportController` owns one active discovery session. Its Tk-thread poller
consumes at most 32 results per tick, in batches of at most 16, with an 8 ms
queue-consumption budget. The producer retains the existing one-worker,
128-record queue and result batch size of 64. Summary size accounting is
incremental. Final order is prepared from an immutable collection snapshot on a
worker and checked against the session and collection revision before commit.
The Treeview applies order with one native child-order update and refreshes row
values in chunks of 96. Automatic mode sorts all retained entries; manual mode
preserves prior order and appends the newly sorted entries. Manual movement
controls remain disabled.

The shared footer shows indeterminate progress and separate live discovery
counters. Cancel remains visible across tabs when applicable. Cancellation
waits for the producer to stop, removes only the current import's entries and
roots, restores prior order and selection, and retains earlier incomplete
warnings. Import issues opens one consolidated diagnostics view; later
successful imports do not clear older warnings. Closing during discovery
requests cancellation and waits asynchronously for retirement. A blocked
operating-system filesystem call can delay cancellation or close.

Automated GUI/controller, collection, and discovery tests cover dialog
delegation, progressive delivery, control and tab lifecycle, ordering,
cancellation/rollback, persistent warnings, worker failures, and source-file
preservation. Visual manual GUI review could not be performed because the
computer-use native pipe was unavailable after retry; scripted headful Tk
measurements below are not a substitute for visual review.

Headful Tk import measurements used isolated synthetic files, Windows 11 build
26300, Python 3.13.17, and an Intel Core i5-10300H. Values are local
observations, not CI thresholds. Discovery duration includes producer
wall time includes filesystem enumeration/metadata work and time blocked by the
bounded result queue. Queue-drain time runs from producer completion until the
consumer has consumed the final result. Order-preparation time measures the
asynchronous immutable-snapshot sort. Treeview apply/commit time runs from the
first final-order callback until the session becomes idle. Total time is from
import start until all rows are updated and the collection is finalized.

| New files | Rows after import | First visible row | Producer wall time | Queue drain | Order preparation | Treeview apply/commit | Total to finalized | Peak queue / capacity | Largest Tk heartbeat delay |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1,000 | 1,000 | 0.055 s | 0.987 s | 0.137 s | 0.047 s | 0.363 s | 1.533 s | 128 / 128 | 28 ms |
| 10,000 additional | 11,000 | 0.018 s | 9.688 s | 0.115 s | 0.275 s | 3.947 s | 14.024 s | 128 / 128 | 264 ms |

Both runs used one discovery worker, queue capacity 128, and result batches of
64. The 10,000-file run ended with 11,000 cumulative rows. The Tk heartbeat
remained active, with a largest measured delay of 264 ms. A separate controlled
cancellation after 100 rows appeared returned the application to idle in
0.123 s (producer cancellation latency 0.030 s) and rolled back all temporary
rows. The earlier pre-optimization run exceeded four minutes without
completing and was stopped; repeated per-row Treeview moves were replaced by
the native child-order update before collecting the results above.

At the M2-E2 checkpoint, manual acceptance for cancellation and rollback, a
subsequent import after cancellation, and application closure during discovery
remained outstanding. M2-E5 adds automated lifecycle coverage for those paths.
M2-E3 completes the collection-management controls. They use stable entry IDs
and the existing collection APIs, preserve prior incomplete-import warnings
until Clear All, and remain unavailable during conflicting work. M2-E3 does not
enable processing or infer preview validity.

## M2-E3 — Collection management and ordering

M2-E3 connects the shared collection toolbar and Treeview to `WorkingCollection`.
The seven sort criteria and direction control remain available for an empty
collection, persist through the existing atomic config save, and apply across
all three tabs. Sorting uses captured metadata, runs preparation off the Tk
thread, rejects stale collection revisions, and updates row values in chunks of
96 after one Treeview child-order update. Natural sorting, missing-metadata
placement, deterministic tie-breakers, and compound-key behavior come from the
existing sorting module. A successful explicit sort restores automatic mode;
saved preferences determine ordering of later imports.

The four movement controls delegate to collection operations and preserve the
relative order of selected and unselected entries. Dragging moves a selected
group in one complete ordering update, indicates a before/after row target, and
rejects stale revisions. Invalid or outside drops cancel safely. Drag
auto-scrolling is not implemented. Effective manual reorder enters manual mode;
no-op movement does not. The summary shows total files, selected count, known
size or unavailable-size count, and a Manual order indicator when applicable.

Remove from List removes selected IDs from memory, preserves retained order,
folder-root records and prior incomplete-import warnings, and permits paths to
be imported again. Clear All removes entries, import sessions, roots and
collection diagnostics, resets manual mode, and retains sorting and other saved
preferences. Neither operation changes the filesystem. Ctrl+A and Delete are
scoped to the Treeview; Ctrl+A in the rename-format field selects that field's
text and Delete keeps its text-editing behavior. The row context menu opens
files or containing folders, copies selected filenames or full paths in
effective order, removes entries, and moves the selection. Right-clicking a
selected member preserves multi-selection; clicking an unselected row selects
it. Open File and Open Containing Folder require one selected row; copy actions
support multiple rows. Common executable and script extensions are disabled for
Open File; this suffix check cannot identify
every potentially launchable format. Header customization remains separate.

The GUI exposes `root.application.collection` for immutable snapshots and emits
`<<TidyFilesCollectionChanged>>` after effective order or membership changes and
import completion or rollback. `root.collection_revision` carries the revision
associated with the notification. Selection and column changes do not emit
collection-change events. M2-E4 observes this interface and requests current
filename evaluation only after collection work is finalized.

### GUI interaction corrections

The row-drag defect came from the native Treeview press binding collapsing an
existing multi-selection before drag motion read it. The widget now saves the
ordered selection on press and restores it when movement crosses the drag
threshold. An unselected row becomes a one-row drag; release without a valid
target still leaves collection order unchanged. The filename-width defect came
from automatic column stretching overriding deliberate widths during layout.
Filename columns now retain explicit widths, which are saved on divider release
and before a tab change. The Columns window is a single instance, raises when
reopened, closes on a mode change, clears its reference on destruction, and is
disposed of during application shutdown.

These corrections do not change explicit sorting: choosing a sort criterion or
direction returns the collection to automatic order. Imports continue to
preserve an existing manual order.

Automated M2-E3 checks use isolated synthetic files and mock external opening.
They cover sorting criteria and directions, preference restoration in a fresh
process, manual movement, drag group and stale-drop behavior, busy and stale
sort handling, worker and Treeview error recovery, closing during a pending
sort, removal and reimport, warning retention, Clear All preference
preservation, keyboard and context actions, source-file preservation, and
collection-change notifications. Corrective regressions drive native Tk press,
motion and release events for multi-selection drag and column-divider resizing,
and cover width persistence across refresh, import, tabs, and application
restart, plus the Columns window's close and mode-switch lifecycle. Manual
Windows GUI acceptance remains outstanding because the Computer Use native pipe
could not connect; generated Tk events do not replace visual review with a
physical mouse.

On Windows 11 build 26300 with Python 3.13.17, the focused application,
collection-management, sorting, configuration, and discovery-integration run
passed 64 tests; the isolated collection-controls worker suite passed 5 tests.
The complete repository suite passed 412 tests with no skips. Ruff,
`compileall -q src tests`, and `git diff --check` passed. No CI run is claimed
for this working-tree implementation.

The corrective GUI follow-up was checked on 2026-11-25 with the application and
collection-controls entry points (2 passed), the isolated collection-controls
worker suite (8 passed), and the complete repository suite (412 passed). Ruff,
`compileall -q src tests`, and `git diff --check` passed. Windows Computer Use
could not connect, so hands-on visual acceptance remains outstanding; no CI run
is claimed for these local changes.

Synthetic GUI performance runs used Windows 11 build 26300, Python 3.13.17, and
an Intel Core i5-10300H. Collections used synthetic paths and stored metadata;
no source files were created for the sort, move, reorder, or clear measurements.
Sort preparation was timed in the worker; GUI time includes polling and Treeview
updates. Other GUI time includes the synchronous action and any bounded row
updates. Times are seconds converted to milliseconds, rounded to 0.01 ms.

| Operation | 1,000 domain ms | 1,000 GUI ms | 1,000 total ms | 10,000 domain ms | 10,000 GUI ms | 10,000 total ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Natural filename sort | 10.22 | 316.79 | 327.01 | 138.38 | 1,695.36 | 1,833.75 |
| File Type + Name sort | 16.58 | 312.66 | 329.23 | 173.26 | 1,770.61 | 1,943.87 |
| Size sort | 8.87 | 290.37 | 299.24 | 90.26 | 1,790.09 | 1,880.35 |
| Move middle 10% to top | 0.48 | 314.10 | 314.58 | 4.54 | 1,720.62 | 1,725.16 |
| Complete manual reorder | 0.43 | 253.00 | 253.43 | 5.87 | 1,627.98 | 1,633.85 |
| Remove half the entries | 0.62 | 226.73 | 227.35 | 7.60 | 883.48 | 891.08 |
| Clear All | 0.04 | 56.64 | 56.68 | 0.49 | 71.45 | 71.94 |

Clear All was measured against a complete collection at each size; removal
selected half the entries. A 10 ms Tk heartbeat continued during the GUI
measurements. The largest observed heartbeat gap was 80.52 ms at 1,000 entries
and 165.45 ms at 10,000 entries. At 10,000 rows, sorting and full reorder took
about 1.6–1.9 seconds overall, with Treeview synchronization as the main cost.
These runs did not simulate a person clicking during the operation, so they
show event-loop servicing, not complete interactive responsiveness. M2-E2's
10,000-file import observed a 264 ms maximum heartbeat delay; the different
workloads do not establish a direct performance improvement. These measurements
are local observations, not CI thresholds or production-volume guarantees.

## M2-E4 — Live filename previews and format controls

Bulk Rename has an editable, empty-at-startup format field, the three approved
built-in presets, a categorized Insert Token menu for all 20 supported token
types, and a scrollable read-only Format Reference. Edits, preset choices, and
token insertion share a 300 ms debounced preview path. The expression remains
session-only and is not written to configuration. Profiles remain disabled.

The preview integration reuses the existing single-worker evaluation service,
its replaceable pending request, immutable ordered collection snapshot, and
directory-count cache. It listens for finalized collection changes, rejects
stale request or snapshot results, and renders by stable entry ID in chunks of
96. Active imports invalidate prior previews and defer evaluation until
finalization. Progress is passed through a bounded queue and coalesced on the Tk
thread. Selection changes do not request evaluation. Switching away from Bulk
Rename clears its proposal cells in the shared table; a current result is
restored on return without reevaluation. Physical count tokens use the existing
nonrecursive metadata provider, and Refresh Preview forces a new observation.

Parser errors show one inline format diagnostic and leave all rows neutral with
Invalid format status. File-specific token, metadata, and validation failures
highlight only affected rows and expose read-only Details. Any such failure
invalidates the complete rename batch, even when other rows have valid previews.
Exact case-sensitive filename equality determines Unchanged. Process,
Deduplicate, and Repack remain disabled. Preview evaluation reads no file
contents and performs no filesystem mutation.

Automated GUI-worker coverage exercises format controls and parser-valid token
templates, the reference text and representative engine examples, finalized
imports, rapid typing and request debouncing, selection-only and column changes,
numbering and effective-order changes, cumulative imports and removal,
tab restoration, parser/token/validation/metadata failures and recovery,
explicit forced metadata refresh, external count changes, source-content
preservation, and shutdown while a metadata call is stalled. The isolated Tk
worker runs inside the repository verification process; it is not a manual
visual acceptance test.

Synthetic preview measurements used Windows 11 build 26300, Python 3.13.17,
and an Intel Core i5-10300H. The app window was withdrawn while the Tk event
loop and Treeview were measured. Collection paths were synthetic; ten real
temporary parent directories each contained one marker file for physical-count
workloads. The first-row measurement records when a current filename was
written to the Treeview; it is not an on-screen paint measurement. Values are
seconds converted to milliseconds. The 10,000-entry count-token run reused ten
cached directory observations from the preceding 1,000-entry workload.

| Entries | Expression | Edit to submit | Service | Edit to first row | Render | Total | Enumerations / cache hits | Largest heartbeat gap |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1,000 | `<Name>` | 367.52 | 32.09 | 405.50 | 54.27 | 459.77 | 0 / 0 | 33.95 |
| 1,000 | `<Name>_<IncNr>` | 368.45 | 61.63 | 434.59 | 53.89 | 488.48 | 0 / 0 | 44.24 |
| 1,000 | `<SubDirName>_<IncNrDir>` | 361.11 | 44.51 | 411.85 | 55.00 | 466.85 | 0 / 0 | 47.30 |
| 1,000 | `<NumFiles>_<NumDirs>_<Name>` | 363.83 | 47.74 | 416.13 | 53.67 | 469.80 | 10 / 0 | 33.59 |
| 10,000 | `<Name>` | 868.93 | 328.38 | 1,205.93 | 819.25 | 2,025.18 | 0 / 0 | 216.41 |
| 10,000 | `<Name>_<IncNr>` | 888.32 | 380.75 | 1,278.23 | 825.80 | 2,104.03 | 0 / 0 | 287.35 |
| 10,000 | `<SubDirName>_<IncNrDir>` | 971.42 | 431.97 | 1,414.60 | 872.50 | 2,287.11 | 0 / 0 | 208.84 |
| 10,000 | `<NumFiles>_<NumDirs>_<Name>` | 919.50 | 451.96 | 1,380.96 | 1,076.51 | 2,457.47 | 0 / 10 | 107.75 |

The long 10,000-row clear and render passes showed that large Treeview updates
can delay the event loop; the largest measured heartbeat gap was 287.35 ms.
These hidden-window measurements do not establish visual latency or interactive
responsiveness. They prompted the M2-E5 optimization recorded below. No
larger-than-10,000 workload was measured.

Interactive on-screen Windows acceptance was not completed during the earlier
automated review because the native Windows automation channel was unavailable.
The M2-E5 acceptance baseline reports owner acceptance of 17/17 M2-E4 manual
checks; that result was not independently repeated in this local run. Automated
widget tests do not replace visual review of M2-E5 refinements.

## M2-E5 — GUI Acceptance, Performance Optimization and M2 Readiness Review

M2-E5 is complete. The optimization retains one shared
collection and stable Treeview row IDs. A collection-position index avoids
scanning all rows for selection order and movement state. Revision-based preview
currency checks avoid copying a full collection snapshot for routine GUI checks.
Sorting, import finalization, preview rendering and invalidation update only
affected Treeview cells; repeated preview clears and unchanged selection
restoration are skipped. Discovery queue and worker settings were not changed.

Bulk Rename uses **Valid preview** for an individually valid filename result.
This label does not establish destination availability, collision safety,
current source identity, or execution readiness. The Refresh Preview help text
explains that it recalculates filenames and required directory counts for the
existing collection without rescanning folders or changing membership. Forced
refresh continues to bypass the directory-count cache.

### Performance environment and measurements

Measurements ran on Windows 11 Home build 26300, Python 3.13.17 x64, an Intel
Core i5-10300H, and 15.87 GiB installed RAM (about 4.89 GiB available before
the runs). The synthetic discovery fixture was on C:, a fixed NTFS volume. GUI
measurements ran with the Tk window withdrawn, the Bulk Rename mode active, and
synthetic in-memory collection paths. Although the application requests a
1000×680 initial window, the automated runner reported a 200×200 geometry after
virtual-screen clamping. These results describe Treeview operations and event
loop callbacks, not visible paint time or normal-sized on-screen interaction.

The 10,000-entry preview workload is comparable to the earlier M2-E4 run. Times
are milliseconds; the 300 ms debounce is included in Edit to submit. Service
time is measured separately. The count-token expression reused ten cached
physical-directory observations in the earlier and current runs.

| Expression | Edit to submit, before → after | Edit to first row, before → after | Render, before → after | Total, before → after | Max heartbeat, before → after |
| --- | ---: | ---: | ---: | ---: | ---: |
| `<Name>` | 868.93 → 576.97 | 1,205.93 → 925.23 | 819.25 → 262.46 | 2,025.18 → 1,187.68 | 216.41 → 198.89 |
| `<Name>_<IncNr>` | 888.32 → 582.77 | 1,278.23 → 970.09 | 825.80 → 294.58 | 2,104.03 → 1,264.67 | 287.35 → 211.11 |
| `<SubDirName>_<IncNrDir>` | 971.42 → 593.78 | 1,414.60 → 1,004.73 | 872.50 → 260.23 | 2,287.11 → 1,264.95 | 208.84 → 288.69 |
| `<NumFiles>_<NumDirs>_<Name>` | 919.50 → 582.74 | 1,380.96 → 996.50 | 1,076.51 → 254.90 | 2,457.47 → 1,251.40 | 107.75 → 103.45 |

Total preview time fell in these runs, but the maximum heartbeat gap did not
improve for every expression; the SubDirName/IncNrDir run observed 288.69 ms.
Large-list stalls remain and these measurements are not visual or CI thresholds.

A separate 50,000-entry in-memory collection run used a current `<Name>`
preview before sort, movement and removal. Earlier M2-E5 baseline values and
the final local run were:

| Operation | Before ms / max heartbeat ms | After ms / max heartbeat ms |
| --- | ---: | ---: |
| Sort 50,000 entries | 3,829 / 411 | 2,614 / 243 |
| Move 10 rows to top | 3,142 / 267 | 1,771 / 205 |
| Remove 10% of rows | 2,867 / 221 | 1,643 / 249 |
| Clear 45,000 rows | 123 / 122 | 111 / 111 |

The earlier and final collection runs used the same collection scale and action
sizes, but preview state varied; treat these operation deltas as directional.

The final run did not uniformly improve event-loop gaps: removing 10% of rows
was about 43% faster overall but its maximum heartbeat gap rose to 249 ms. A
separate selection-summary update for ten selected rows measured 22 ms. At
50,000 rows, an uninstrumented process used about 197.30 MiB working set and
peak working set, versus a 40.59 MiB empty-window working set. The earlier
baseline was about 193.45 MiB working set and 196.4 MiB peak; the added indexes
therefore cost roughly 4 MiB at this size. No 100,000-row run was performed.

### Progressive import and cancellation

The retained manual fixture contains 12,000 empty `.txt` files in ten immediate
subdirectories at
`C:\Users\marlo\AppData\Local\Temp\TidyFiles-M2-E5-acceptance-7vdc3ru1`.
It is isolated from the repository and user collections; it was not modified by
the application. Use it only with Add Folder or the read-only import benchmark.
Do not move, rename, or delete its contents while reviewing the application.

On the final import run, all 12,000 entries were accepted. The first 32 rows
were visible after 74 ms while traversal was still active. The producer used
one worker, a 128-record queue with peak occupancy 128, and result batches of
64; the Tk consumer processes at most 32 results per tick within its existing
approximately 8 ms budget. Measured producer traversal was 17.81 s, queue drain
to order preparation 0.18 s, immutable-order preparation 0.30 s, and final
Treeview application about 3.89 s. Total controller time was 22.23 s. The
heartbeat median was 16.24 ms and maximum 335.50 ms. Queue backpressure remains
intentional; full queue occupancy did not prompt extra workers.

A focused cancel-and-retry run canceled after 64 new entries were accepted and
65 rows were visible including the earlier sentinel. Rollback returned to one
row and restored its selection; cancel-to-idle was 169.56 ms, including
rollback, and the session-reported cancellation latency was 47.77 ms. A later
one-file import completed successfully in 167.38 ms. Synthetic source contents
were unchanged. The automated controller suite also covers incomplete-warning
persistence, rollback order and selection, and a subsequent import. A GUI worker
test closes the application during active discovery and checks that rollback
finishes before shutdown without Tk callback errors. A separate preview worker
test closes while metadata evaluation is stalled. These controlled checks do
not establish immediate cancellation during a blocked operating-system call.

### GUI, safety and readiness review

Automated tests cover the prior M2-E3 regressions for multi-row drag, resized
filename columns across refresh/tab/restart, and one Columns dialog; they also
cover selection, failed/canceled drops, sorting and preference persistence,
geometry clamping, preview invalidation, forced metadata refresh, stale result
rejection, and session-only filename expressions. Geometry and preference tests
are scripted checks. Physical multi-monitor and DPI checks, minimum-size
usability, visible scrolling, and visual Light/Dark comparison were not
performed. The current local task did not establish interactive Windows
acceptance; a native UI automation channel was unavailable in the recorded
environment. Single-instance tests cover orderly release/reacquisition and
independent competing processes; a second GUI launch after shutdown was not
manually exercised.

The safety review found no M2 processing operation enabled. Import, cancellation,
collection ordering/removal, preview evaluation and directory-count refresh
remain non-destructive; synthetic source content was preserved. Open File
remains an explicit action guarded by a filename-extension denylist, which
cannot identify every executable or launchable file type. That limitation
requires review before any future launch-related workflow.

M2 exposes ordered collection snapshots, stable entry IDs, original/current
source paths, per-file import-root context, filesystem identity observations,
folder-root records, incomplete-import diagnostics, revision tracking, current
evaluation results, structured file diagnostics, forced count refresh,
candidate comparison, and collection-change notifications. These are inputs to
M3 destination planning and preflight. Counter remains deferred.

### M2-E5 verification

On Windows 11 Home build 26300 with Python 3.13.17, the focused application,
collection, discovery, import, filename, instance, and preference suites passed
398 tests. The isolated filename-preview worker passed 5 tests and the isolated
collection-controls worker passed 9 tests. The full repository suite passed
413 tests with no skips. Ruff, `compileall -q src tests`, and `git diff --check`
passed locally during M2-E5 development.

On November 25, 2026, the owner reported 21/21 manual acceptance checks passed
and confirmed the push of
`22c3f96512cfa6251f872e94c5f5d4c6032ae302` and successful Windows CI.
These are historical M2-E5 results. Physical multi-monitor and DPI-change
verification remains outstanding.

## M3-A — Read-only destination planning

The standalone planner consumes finalized snapshots and current filename
evaluations. It uses the immediate source parent for Bulk Rename and the
first-established import root for Single Folder. Immutable plans retain
evaluation identity, options, observations, diagnostics, and dependency
groups. The service recognizes no-ops, case-only renames, competing targets,
unrelated occupants, chains, swaps, and cycles. Collision-only subset
replanning propagates dependency exclusions and observes the remaining
destinations again with the original reviewed names.

This covers the planning clauses of DD-009, DD-021, DD-048, DD-056,
DD-063, DD-065, DD-074, DD-075, DD-076, and DD-085. M3-A established the
planning contract used by M3-B. DD-009 confirmation, DD-075 acknowledgment,
execution-time checks, and transaction and recovery safeguards remain planned.
The GUI continues to display filename-level previews with
Process, Deduplicate, and Repack disabled. See
[Architecture](ARCHITECTURE.md#read-only-destination-planning) and
[File Safety](FILE_SAFETY.md#destination-planning-observations).

The M3-A commit `e1b651302f1037e07472120a13fca5445dda5f50` was pushed and
passed Windows CI, as recorded at the M3-B checkpoint. M3-A's historical
local repository suite passed 447 tests.

The planning benchmark used Windows 11 build 26300, Python 3.13.17 x64, and
an Intel Core i5-10300H. It planned independent renames to vacant names in one
temporary directory from precomputed collection and evaluation results.
Timings exclude fixture setup and filename evaluation.

| Entries | Simulated observations | Native observations | Endpoint observations per run | Native literal lookups |
| ---: | ---: | ---: | ---: | ---: |
| 1,000 | 0.085 s | 0.337 s | 2,000 | 2,006 |
| 10,000 | 0.870 s | 3.316 s | 20,000 | 20,006 |
| 50,000 | 4.463 s | 17.279 s | 100,000 | 100,006 |

The six extra literal lookups checked shared ancestors. The native runs used
volume and directory-case metadata queries and performed no directory
enumerations or content reads. The timings describe this fixture and machine.

On the same Windows environment, the focused planner suite passed 34 tests
and the full repository suite passed 447 tests, both with zero skips.
Standalone GUI workers passed 5 filename-preview tests and 9
collection-controls tests. Ruff and compileall passed. Native planner
fixtures covered hard links, junctions, case-only naming, distinct directory
spellings, and unchanged file contents, identities, and directory membership.
Permission failures and constrained component limits used fault injection.

## M3-B — Execution preflight

Preflight independently checks a reviewed plan, including a collision-only
subset, against source and parent identity baselines, refreshed filenames,
and current destinations and dependencies. A new candidate or destination
requires review. A newly blocked intended row blocks the whole intended set.
The full finalized collection is retained for filename evaluation and import
restrictions. The standalone worker supports cancellation and supersession
during read-only preparation.

M3-B implements preflight clauses of DD-009, DD-021, DD-037, DD-041, DD-048,
DD-049, DD-056, DD-063, DD-065, DD-074, DD-075, DD-076, and DD-085.
DD-041 journal durability, DD-037's modifying-job boundary, DD-009 user
confirmation and execution, and DD-075 acknowledgment remain future work.
The GUI continues to show filename previews and the modifying controls are
disabled. See [Architecture](ARCHITECTURE.md#execution-preflight) and
[File Safety](FILE_SAFETY.md#preflight-identity-and-safety).

The native benchmark ran on Windows 11 build 26300 and Python 3.13.17 x64.
It used independent renames with `<Name>_<NumFiles>` in one temporary
directory. Timings start after review capture and include the forced count
refresh and both source observation passes.

| Entries | Total | Evaluation and refresh | Source observations | Directory observations | Destination observations | Enumerations |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1,000 | 0.939 s | 0.038 s | 2,000 | 14 | 1,000 | 1 |
| 10,000 | 9.328 s | 0.392 s | 20,000 | 14 | 10,000 | 1 |
| 50,000 | 61.543 s | 2.340 s | 100,000 | 14 | 50,000 | 1 |

The directory count includes shared ancestors queried in both passes.
Cancellation requested at the 128-source progress checkpoint returned in
0.051 ms, 0.384 ms, and 6.536 ms, respectively. That measures a cooperative
checkpoint after a query returned.

The focused preflight suite passed 43 tests and related planning, evaluation,
discovery, and collection suites passed 141. The final repository run passed
490 tests with zero failures or skips. Standalone checks passed the GUI smoke
test and 9 collection-controls and 5 filename-preview worker tests. Two
earlier full runs each failed one GUI test because a discovery fixture
released itself before a shutdown assertion. The fixtures now wait for their
explicit release events. Ruff, compileall, and both Git whitespace checks
passed.

The M3-B commit `897c3f7dd12823cda9b9056868a6f689a09ed237` was pushed and
passed Windows CI, as recorded at the M3-C checkpoint.

## M3-C — Transaction infrastructure

Versioned journals persist the exact intended scope, native Windows identity
observations, entry stages, outcomes, and verified finalization evidence.
DD-086 Option A uses portable journals with a LocalAppData fallback and
an independent LocalAppData recovery locator. A failed required write stops
the pinned journal session. Read-only recovery discovery preserves and reports
interrupted, damaged, unavailable, and cleanup-pending records.

The application-level coordinator provides one job slot, cancelable
preparation, and a non-cancelable modifying phase gated by a registered
journal and an in-progress checkpoint. The existing named mutex continues
to guard the process. This covers infrastructure clauses of DD-012, DD-035,
DD-037 through DD-042, and DD-086. Operation-specific checkpoints and
verification belong to M3-D. Startup notification, staging inspection, and
overlap protection belong to M3-F. The GUI controls remain disabled.

On Windows with Python 3.13.17, the focused transaction suite passed 28
tests and the full suite passed 518, with zero failures and skips. Fault
injection covered initial writes, publication, registration, checkpoint
failures, and cleanup. Native fixtures covered journal publication and
retirement, relocation, a redirected journal directory, and identity
round-tripping from preflight. Ruff, compileall, and both Git whitespace
checks passed.

The M3-C commit `04dbb1fde3d8e62c54d06eafa9a71d8190de05d2` was pushed and
passed Windows CI, as recorded at the M3-D checkpoint.

## M3-D — Safe rename executor

The standalone executor runs an authorized, current rename scope against
its reviewed destinations. It reuses planner order, preflight observations,
the modifying-job slot, and the pinned journal. Same-volume moves use
source and destination-directory handles and Windows no-replace semantics.
Chains run in vacating order. Case-only changes, swaps, and cycles use a
single journaled sibling stage per dependency group.

Each move is checked and recorded before and after the filesystem action.
The executor preserves verified completed moves and leaves staged originals
and their journals available for inspection after failure. It verifies
every outcome and stage vacancy before retiring records. The executor
restores the original creation time if NTFS name tunneling changes it.

The executor covers the rename-specific parts of DD-009, DD-037 through
DD-038, DD-040 through DD-043, DD-063, DD-065, DD-074 through DD-076, and
DD-086. Repacker execution remains planned. Recovery workflows and overlap
protection belong to M3-F, and confirmation, activation and collection
updates to M3-G. See [Architecture](ARCHITECTURE.md#standalone-rename-execution) and
[File Safety](FILE_SAFETY.md#rename-execution-safety).

Native Windows fixtures verified independent roots on two physical volumes.
The 100-file independent-rename run took 4.475 seconds. A separate 250-file
run took 15.291 seconds and measured pre-move journal records of
407,012–407,759 bytes. Both measurements exclude preflight and include
journal preparation, before- and after-move checkpoints, and final
verification. Each required checkpoint still publishes the entire record,
which adds persistence cost as the batch grows.

On Windows with Python 3.13.17, 37 executor tests, 105 related regression
tests, and the full 555-test suite passed with zero failures and skips.
Ruff, compileall, and both Git whitespace checks passed. The first executor
run exposed NTFS creation-time changes in a chain and a cycle. The native
tests passed after creation-time preservation was added.

The M3-D commit `c0129f2d1a2579c51d09c152ceb94c9fd53e69e8` was pushed to
`main`. [Windows CI run #30](https://github.com/hectordoesgorug/TidyFiles/actions/runs/45608078017)
passed lint and tests for that commit.

## M3-D1 and M3-D2 — Journal performance

The M3-D1 profile found that building, validating, and encoding journal
records took 158.89 of 190.46 seconds at 1,000 files. A routine checkpoint
usually updated one entry, but the codec rebuilt and validated every entry.

Routine in-progress checkpoints now reuse unchanged entries' validated JSON
representations from the last successful publication. The session preserves
the ordered scope, and the shared codec validates changed entries and
record-wide constraints. State transitions and disk reads use full
validation. Each move still requires durable before- and after-move
checkpoints. The version 1 format, DD-086 Option A placement and locator,
exact-byte journal checks, synchronized atomic publication, and failure and
retirement behavior are preserved.

Both measurements used three instrumented independent-rename runs per batch
size on Windows 11 with Python 3.13.17 x64. Source files, journals, and locator
fixtures used the same local NTFS NVMe volume. Execution timings exclude
preflight and fixture setup.

| Files | D1 execution median | D2 median (range) | D2 files/s | Execution time saved | Record building, validation, and encoding (D1 to D2) |
|---:|---:|---:|---:|---:|---:|
| 100 | 3.62 s | 2.56 s (2.54–2.58) | 39.12 | 29.5% | 1.57 s to 0.33 s |
| 500 | 52.63 s | 19.13 s (18.75–20.64) | 26.14 | 63.7% | 38.65 s to 6.71 s |
| 1,000 | 190.46 s | 59.32 s (58.23–59.69) | 16.86 | 68.9% | 158.89 s to 28.03 s |

At 1,000 files, full JSON encoding took 26.10 seconds and journal and locator
reads and durable writes took 16.00 seconds. Each 1,000-file run performed
2,004 journal reads totaling 3.264 GB and 2,003 journal writes totaling
3.261 GB. Counts and logical byte totals matched D1 at each size. Whole
snapshots still grow with batch size, so cumulative encoding and I/O grow
faster than the number of files.

The D2 benchmarks verified exact destination names, contents, identities,
creation and last-write timestamps, the expected checkpoint, synchronization
and publication counts, and successful journal retirement. The focused
journal and executor tests passed all 78 cases. The full suite passed 568
tests with zero failures and skips. Ruff, compileall, and both Git whitespace
checks passed. The timing results cover independent local renames and do not
measure power-loss behavior or other storage layouts.

The M3-D2 commit `040b7b4fb8e454051713ac6bc104690a2f5dc580` was pushed to
`main`. [Windows CI run #31](https://github.com/hectordoesgorug/TidyFiles/actions/runs/45722683231)
passed for that commit.

## M3-E — Managed Trash

The standalone Trash service accepts an explicitly authorized set of
sources with current Windows identity evidence. It allocates unique
timestamped batches under the sole portable Trash root and preserves
original basenames when possible. Discovery and native processing exclude
that managed path.

Same-volume transfers preserve the original object through guarded native
rename. Cross-volume transfers check capacity, exclusively create and
synchronize the copy, then verify contents and required metadata. A
registered `TRASH` journal records the copy identity and digest before
handle-bound source removal. The first failure stops the batch. Recovery
discovery preserves unresolved copy evidence, and terminal journals retire
only after the complete scope is verified.

This implements the transfer portions of DD-004, DD-005, DD-013, DD-014,
DD-037–038, DD-041–043, DD-074, and DD-086. Explorer provides manual
retrieval. Startup recovery and Trash cleanup, desktop activation, and
duplicate and Purge eligibility remain separate work.

Local Windows verification used Python 3.13.17 and two distinct NTFS
volumes for native cross-volume tests. The 24 Trash cases and the combined
135-case Trash, journal, rename, and discovery run passed. The full suite
passed 592 tests with zero failures and skips. Ruff, compileall, and both
Git whitespace checks passed. Fault injection covered space, copy, source
removal, and journal publication failures. Physical power-loss behavior
and other filesystem types remain unverified.

The M3-E implementation is commit
`587c03a6340fba5db561d94c39cafcad7f0fa93b`, pushed to `main`. Windows CI
run #32 passed for that checkpoint.

## M3-F — Recovery and cleanup

`JournalStore.inspect_recovery()` reports current no-follow observations
alongside recorded transaction expectations. Cross-volume Trash copies are
matched to their recorded copy identity rather than the original source.
`RecoveryGuard` is used by rename and Trash preparation and execution; it blocks
overlapping unresolved paths or identities while allowing unrelated paths when
recovery discovery is complete.

Temporary artifact cleanup accepts only a successful, journal-recorded
temporary path with its creation-time identity retained by the active job.
The native remover deletes one file or a verified empty directory by handle,
without recursion. Journal finalization now checks that every recorded staging
or temporary artifact path is actually absent before it retires the journal.
It does not delete staged originals.

Auto-clean requires the independent setting to be enabled, complete successful
primary processing, no partial or unresolved entries, complete folder
discovery, and explicit Add Folder roots from the active snapshot. It removes
only case-insensitive exact matches for `Thumbs.db`, `.DS_Store`, and
`desktop.ini`, plus empty descendant directories. It preserves roots, unrelated
files, managed and protected system paths, links, and unresolved recovery
dependencies. Callers must pass completed operation output paths; those paths
remain protected even when they match the allowlist. Manual and automatic Trash
clearing share an inspection service. DD-087 makes the dedicated managed Trash
directory and recognized batch directories the deletion boundary, so eligible
batch contents do not require per-file ownership records. M3-E and legacy
timestamp batches are cleared by deleting files individually and removing empty
directories deepest first. Unknown root contents, unresolved recovery
dependencies, links, and failed entries remain. No ownership manifest was
introduced.

The two cleanup settings remain independent and default off. Cleanup errors
are returned separately from primary-operation results. Manual Clear Trash
requires explicit authorization, while Auto-Clear Trash requires a normal exit
and an idle coordinator. At the M3-F checkpoint, GUI confirmation, startup
recovery dialog, Explorer action, and normal-exit invocation remained for
M3-G.

M3-G passes successful output paths with the pre-operation snapshot when it
invokes Auto-clean. This protects a renamed output whose name matches an
allowlisted clutter name.

Focused M3-F verification on the Windows Python 3.13.17 environment ran
`pytest tests/test_recovery.py tests/test_cleanup.py tests/test_config.py
tests/test_transaction_journal.py tests/test_rename_executor.py
tests/test_managed_trash.py -q`: 162 passed, zero skipped. The final complete
suite reported 627 passed, zero skipped. One preceding full run had a transient
timeout in the existing isolated Tk preview test (626 passed, 1 failed); that
test passed when rerun alone, and the complete rerun passed. No assertions were
changed. `ruff check .`, `compileall -q src tests`, `git diff --check`, and
`git diff --cached --check` also passed. The sandboxed virtual-environment
launcher initially could not access its configured base interpreter
(`C:\Users\marlo\AppData\Local\Python\pythoncore-3.13-64\python.exe`; Access is
denied). The same Python 3.13.17 environment ran the checks through the
approved execution path; no dependencies or interpreters were installed or
changed. These checks do not verify M3-G GUI integration or Trash deletion,
which has no GUI or normal-exit invocation yet.

### M3-F1 — Managed Trash clearing under DD-087

DD-087 supersedes M3-F's earlier per-file ownership restriction. Clear now
accepts only recognized batches under the dedicated application Trash directory:
M3-E timestamp-and-UUID batches and legacy timestamp batches. It deletes
eligible files through identity-bound handles and removes empty directories
deepest first. Unknown root entries, unresolved recovery dependencies, links,
and failed or unverifiable items remain. The service reserves the existing
cleanup job slot, requires explicit manual authorization, and gates automatic
clearing on normal exit, the independent default-off setting, and an idle
coordinator. GUI confirmation and normal-exit wiring remain M3-G work.

Verification used Windows Python 3.13.17. The focused cleanup, recovery,
managed Trash, rename executor, transaction journal, and configuration tests
passed: 164 passed, zero skipped. The full suite passed 629 tests, zero skipped.
Ruff, `compileall -q src tests`, `git diff --check`, and
`git diff --cached --check` passed. The tests used isolated temporary fixtures;
no real Trash contents were cleared. This records verification at the M3-F1
checkpoint before M3-G GUI confirmation and normal-exit integration.

### M3-G — GUI execution integration

Bulk Rename Process now consumes the current filename evaluation and finalized
collection snapshot. The live filename preview is the review interface for an
ordinary conflict-free operation: clicking Process authorizes the displayed
scope, and an unchanged passing preflight proceeds directly to journaled
execution. Collision-only subsets and changed plans require review. A confirmed
collision-only subset keeps the reviewed candidates and retains excluded or
failed entries with diagnostics. Incomplete imports require explicit
acknowledgment. Processing runs outside Tk; preparation can be canceled, active
modification cannot, and normal close waits for filesystem changes to finish.

Startup displays read-only unresolved-transaction details and recorded paths,
stages, and Explorer access. Auto-clean runs only after complete success with
the original collection snapshot and successful output paths. Manual Clear
Trash requires confirmation; Auto-Clear Trash runs only on enabled normal exit.
Deduplicate and Repack remain disabled.

M3-F1 checkpoint: commit `fc9393934bd0462c6bddfbb45823a87ea16e1e5d` was pushed
to `main`, and Windows CI run #34 passed, as reported with the M3-G handover.

Verification used Windows Python 3.13.17. The focused GUI, collection,
destination-planning, preflight, and rename-executor run passed 150 tests. The
complete suite passed 640 tests with zero skips. `ruff check .`,
`compileall -q src tests`, `git diff --check`, and `git diff --cached --check`
passed. Two earlier full-suite attempts timed out in the existing physical
directory-count preview worker; its isolated rerun passed, and the final full
suite passed. GUI tests used temporary source, journal, and configuration
fixtures; no production Trash was cleared. Visible hardware and DPI acceptance
was not repeated.

### M3-G1 — Simplify processing workflow

An ordinary conflict-free plan proceeds from Process to background preflight
and directly to execution when the reviewed scope remains unchanged. Collision
subsets and changes detected by preflight still require review; incomplete
imports still require acknowledgment.

Verification on Windows Python 3.13.17: the focused GUI workflow run passed 7
tests, and the complete suite passed 640 tests with zero skips. Ruff, compileall,
and both Git whitespace checks passed. The full suite took 199.32 seconds.

### M3-G2 — Manual acceptance corrections

The collection movement controls retain fixed grid columns during progressive
updates. The Process button now uses theme-aware enabled and disabled colors,
and its existing eligibility rules are unchanged. Processing status reports
completed files and elapsed time, then estimates remaining time after enough
measured progress. Failure dialogs report aggregate counts and the operation
error without listing all unprocessed paths. Missing or empty managed Trash
shows an empty-state message without creating the Trash directory. Settings
continues to open after switching themes in the automated GUI regression test.

Journal checkpoint publication retries Windows access-denied and sharing-
violation errors up to twice, checking the previous durable journal between
attempts. Persistent failures still stop modification and retain the last
durable checkpoint. A synthetic 1,200-file Windows run injected one access-
denied error after 142 successful entries and completed on retry.

The retained interrupted-run journal was inspected read-only and left
unchanged. It records 142 installed entries and 1,058 prepared entries without
a persisted error detail. The user confirmed that the corresponding temporary
source folder was deleted after the test. The precise filesystem sub-operation
that returned the historical access-denied error could not be determined.
Automated verification does not replace visible Light/Dark mode review or
physical layout and DPI acceptance.

### M3-G3 — Recovery record dismissal and Settings visibility

The recovery interface allows explicit selection and confirmation before
removing journal records and their locator entries. It preserves recorded file
paths and refreshes recovery inspection after clearing. Recovery clearing is
unavailable while processing is active. Settings windows are explicitly
deiconified, raised, and focused when opened or reused. A delayed visibility
restore handles CustomTkinter's temporary Windows titlebar withdrawal.

Verification on Windows Python 3.13.17: the focused recovery and Settings run
passed 7 tests; the complete suite passed 656 tests with zero skips. Ruff,
compileall, and both Git whitespace checks passed. The retained journal's size,
timestamp, and SHA-256 matched their pre-test values.

### M3-G4 — Settings widget lifecycle and Auto-Clear exit behavior

Settings widget references are cleared when their dialog is destroyed, and
processing controls update the Auto-Clear switch only while it exists. Auto-
Clear Trash silently handles a missing or empty Trash directory and successful
clearing; it warns when an error occurs or protected, skipped, or failed content
remains. Normal exit does not create a Trash directory. Manual Clear Trash
continues to show its empty-state message. On Windows Python 3.13.17, the
focused Settings, exit-cleanup, and manual empty-Trash regressions passed 4
tests; the full suite passed 657 tests with zero skips. Ruff, compileall, and
both Git whitespace checks passed. Tests used isolated temporary fixtures.

### M3-G5 — Preview completion status race

The intermittent timeout was caused by a late queued progress update replacing
the completed preview status after its current result and rows had already been
rendered. Metadata refresh and evaluation had completed; the request was not
stuck. The GUI now ignores progress updates for a request once its result is
rendering or rendered. A regression injects a late update and verifies that the
completed status remains visible. On Windows Python 3.13.17, the isolated Tk
preview worker passed three separate runs and the full suite passed 657 tests
with zero skips. Ruff, compileall, and both Git whitespace checks passed.

## M6.1 — Package Restructuring and Architectural Cleanup

This planned maintenance checkpoint follows M6 (Complete GUI Integration) and
must be completed before M7 (Packaging and Release). Its objective is to
improve source-code organization and maintainability without changing
established application behavior.

The following package layout is illustrative and preliminary, not a binding
implementation specification:

```text
src/tidyfiles/
├── __init__.py
├── __main__.py
├── app.py
├── core/
├── rename/
├── deduplicator/
├── repacker/
├── filesystem/
├── infrastructure/
└── ui/
```

The final hierarchy and module placement will be determined by inspecting the
implemented feature modules and their dependency relationships after M6, when
those boundaries are substantially established.

The checkpoint will preserve established functionality; organize modules by
functional responsibility; separate feature-specific logic from shared
services; and maintain clear dependency boundaries without circular imports.
It will update affected imports, tests, entry points, and resource paths; review
package discovery and future packaging compatibility; preserve configuration
and portable-storage behavior; and avoid unnecessary abstractions, dependencies,
or algorithm rewrites. Verification will run the complete regression suite and
Windows CI. Restructuring must be completed as a separate, independently
verified commit before packaging begins.

Until M6.1, ordinary feature development continues without comprehensive
package restructuring. Feature-specific subpackages may be created earlier
when implementation complexity justifies them; unrelated functionality should
not be combined into existing modules solely to minimize the number of files.

## M6.2 — Public Documentation Sanitization and Repository Audit

This planned checkpoint follows M6.1, precedes M7, and must be completed before
the first stable v1.0.0 release. Its objective is to make the public repository
professional, self-contained, and free of unnecessary internal development
terminology.

The checkpoint will remove internal design-decision identifiers, milestone
subdivisions, audit identifiers, handover numbers, and agent coordination
terminology from public-facing documentation. It will replace internal
references with descriptive, technically accurate language while preserving
substantive requirements, architecture descriptions, safety policies, and
relevant verification evidence. The review will cover the README, architecture
and file-safety documentation, progress records, and other tracked documents.
Public contributors must be able to build, understand, and contribute to
TidyFiles without access to ignored planning or legacy references. The review
will also check repository organization, obsolete documentation, and broken
references. Appropriate development instructions will remain in `AGENTS.md`,
without mandatory dependencies on inaccessible private documents. Git history
will not be rewritten to remove historical terminology.

During M6.2 release preparation, verify this note's accuracy before publication
and consider including it:

> Historical design records, development planning materials, and legacy implementation references are maintained separately from the public repository.
>
> The tracked documentation describes the current application architecture, supported functionality, safety policies, and development requirements. Access to private reference materials is not required to build, use, or contribute to TidyFiles.

Until M6.2, internal identifiers may remain in tracked technical documentation
where they support requirements traceability and development continuity. Avoid
unnecessary agent coordination terminology in public-facing prose. Preserve
ignored `/planning/` and `/legacy/` directories as read-only references, and do
not prematurely sanitize documentation during active feature development.

## Verification record

At the M0-C checkpoint, Windows GitHub Actions run #9 was reported as passing
Ruff and all 37 pytest tests. That is historical milestone evidence. The current
CI workflow runs Ruff and pytest on Windows with Python 3.13.

During M1-A, local Ruff, pytest (105 tests), compileall, and diff checks passed
on the working tree based on commit `442bb8ec92768835c394d4ed71a9b21cc04001ad`.
These local results do not represent a GitHub Actions run for the parser change.

For M1-B1, local Ruff, all 152 pytest tests, compileall, and `git diff --check`
passed on the working tree based on `4c0c48378087b7d77e04eb8d10897ea30bb35ff0`.
The focused parser/evaluator run passed all 115 tests. These are local results;
no GitHub Actions run or IDE type-checker result is claimed. Evaluator tests use
strings only and do not access user files.

For M1-B2, local Ruff, all 179 pytest tests, compileall, and `git diff --check`
passed on the working tree based on `67af79a222473a27f9b1e9d7f46ffb3d0ed8ad75`.
The focused parser/evaluator run passed all 142 tests. These are local results;
no GitHub Actions run or IDE type-checker result is claimed. Batch fixtures use
immutable synthetic file metadata and do not access user files.

For M1-C1, local Ruff, all 206 pytest tests, compileall, and `git diff --check`
passed on the working tree based on `13cf6a6646177bbc68746fbeda090b7dc9c3e6a0`.
The focused parser/evaluator/extension run passed all 169 tests. These are local
results; no GitHub Actions run or IDE type-checker result is claimed. Extension
tests use synthetic names and metadata and do not access user files.

For M1-C2, local verification was run on the uncommitted working tree based on
`e9a6491d1bb033f295c88eb8934d76929ca37e`: Ruff passed, pytest passed all 259
tests, compileall passed, and `git diff --check` passed. No `pyright` or `mypy`
executable was available. The new validator tests use synthetic names and
metadata; no target-volume or filesystem behavior is verified.

For M1-D, the local audit and three regression tests ran on `main` based on
`50a97a929130c5a6502ac9fc751d9f15700b79bb`. The checkout matched `origin/main`;
Windows CI run #14 passed Ruff and pytest for that commit. Locally, Ruff,
compileall, and `git diff --check` passed; pytest passed all 262 tests, including
three new audit regressions. No static type checker or coverage package was
available; test coverage was reviewed manually. The audit does not verify
target-volume behavior or filesystem execution.

For M1-D.12, the parser now retains the full original string in
`FilenameFormatParseError.expression`; non-string input records `None`. Local
verification passed Ruff, pytest (267 tests), compileall, and `git diff --check`;
the focused parser/evaluator run passed all 119 tests. The constructor audit
confirmed expression context on every string-input parser error path. No
`pyright`, `mypy`, or `basedpyright` executable was available. The audit's
DD-061 finding is resolved; no other M1
pure-engine closure blocker was identified. M2/M3/M5/M6 integration remains
planned, and no Windows destination-volume behavior is claimed.

For M2-A, local verification ran on Windows with Python 3.13.17, on the
working tree based on `6c8c81d6286aa1e887330ebed754de15b02b03fd`. The focused
collection suite passed all 11 tests; Ruff passed; the full pytest suite passed
all 278 tests with no skips; `compileall` passed; and `git diff --check` passed
with line-ending conversion warnings for the two edited Markdown files. No
GitHub Actions result is claimed. The tests cover lexical Windows path keys,
path-based deduplication, context preservation, import-session finalization and
rollback, ordered immutable snapshots, optional metadata, and source-file
preservation. Scanning, asynchronous worker behavior, GUI integration, and
filesystem identity acquisition remain unverified and unimplemented.

For M2-B, the local discovery and collection suites cover the implemented
clauses of DD-007, DD-019 through DD-023, DD-031, and DD-074 through DD-078.
On Windows with Python 3.13.17, Ruff passed, all 306 pytest tests passed with
no skips, compileall passed, and `git diff --check` passed. The focused
collection and discovery suites passed all 39 tests. Temporary synthetic-file
measurements accepted all 1,000 files in 0.2015 seconds and all 10,000 files in
1.4351 seconds; time to first result was 0.0015 and 0.0017 seconds, respectively.
The queue capacity was 128, peak occupancy was 4 and 8, and each scan used one
worker. With a queue capacity of 4, cancellation while the producer was blocked
on a full queue reached the cancelled state in 0.0009 seconds. These are local
observations, not performance thresholds. File/folder discovery remains a
service API; the GUI, processing acknowledgment, physical directory counts,
destination checks, and filesystem mutations are not implemented. GitHub
Actions run #17 passed Ruff and pytest for commit
`8c761a0a786e59a0e500e7cbbbcbf87efade8e6a`; the local M2-B measurements above
remain the recorded performance evidence.

For M2-C, focused collection-management, sorting, configuration, and
discovery-integration suites passed 85 tests. The full local suite passed all
343 tests with no skips; Ruff, compileall, and `git diff --check` also passed on
the working tree based on M2-B commit
`8c761a0a786e59a0e500e7cbbbcbf87efade8e6a`. This working-tree result did not
include CI evidence. After M2-C was committed as
`9f7e182db059285b3662a5d5092acf8fdc0ac838`, GitHub Actions run #18 passed Ruff
and pytest for that commit. Tests cover the implemented domain clauses of
DD-010, DD-021 through DD-026, DD-031, DD-048, DD-075, DD-077, and DD-078.
GUI/settings integration, analysis acknowledgments and readiness, and
filesystem operations remained unimplemented at M2-C.

Synthetic in-memory benchmarks ran on Windows 11 (build 26300), Python 3.13.17,
with an Intel Core i5-10300H. Each cumulative import added 10% new entries
(minimum 100); manual reorder selected 10% and bulk removal selected 50% of the
post-import collection. Elapsed seconds:

| Operation | 1,000 | 10,000 | 100,000 |
| --- | ---: | ---: | ---: |
| Natural filename sort | 0.0116 | 0.1402 | 2.0479 |
| File Type + Name sort | 0.0123 | 0.1463 | 1.9884 |
| Size sort | 0.0078 | 0.0931 | 1.4126 |
| Cumulative import finalization | 0.0153 | 0.1708 | 2.4892 |
| Move 10% selection to top | 0.0003 | 0.0035 | 0.0696 |
| Remove 50% | 0.0007 | 0.0083 | 0.1536 |
| Clear All | 0.0000 | 0.0010 | 0.0090 |

Registration uses average O(1) membership and path-index lookups. Sorting is
O(n log n) with O(n) temporary keys and runs once when an import finalizes;
reordering and removal make one pass over the ordered IDs plus the selected
IDs; Clear All clears collection-owned indexes and sequences in O(n). These
measurements are local observations, not CI thresholds or production-volume
guarantees.

For M2-D, the focused adapter/metadata suite passed 51 tests, the focused
filename-engine suite passed 230 tests, and the focused collection/discovery
suite passed 51 tests, all with no skips. The full local suite passed all 394
tests with no skips; Ruff, `compileall -q src tests`, and `git diff --check`
passed. These are local Windows results on Python
3.13.17; no CI run is claimed for the uncommitted M2-D changes. Native Windows
symlink and hard-link fixtures passed. Junction/reparse-attribute, permission,
partial-enumeration and directory-replacement cases were simulated; they do not
prove behavior on every Windows filesystem or reparse provider. The tests cover
all 20 existing token kinds through the adapter, collection/root context,
ordered numbering, metadata caching/refresh, partial failures, conversion
extensions, batch-wide invalidity, lifecycle, cancellation and stale results.

Synthetic benchmarks used Windows 11 build 26300, Python 3.13.17, and an Intel
Core i5-10300H. Collection entries and source paths were synthetic; only the
temporary parent directories used for count-token benchmarks existed on disk.
In-memory workloads did not enumerate directories. Each evaluation used one
ordered batch; the service uses one worker and at most one replaceable pending
request. Transient context/result memory is O(n), as required by ordered batch
evaluation. The timed runs use a fresh cache per evaluation; a repeated
one-parent evaluation hit the cached observation once with zero enumerations,
while forced refresh performed one new enumeration. Times are seconds, local
observations rather than CI thresholds.

| Entries / parents | Workload | Contexts | Metadata | Engine | Total | Enumerations / cache hits |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1,000 / 10 | Basic | 0.003322 | 0 | 0.023778 | 0.028308 | 0 / 0 |
| 1,000 / 10 | Global numbering | 0.004073 | 0 | 0.025161 | 0.030591 | 0 / 0 |
| 1,000 / 10 | Per-directory numbering | 0.004156 | 0 | 0.024738 | 0.032089 | 0 / 0 |
| 1,000 / 10 | Directory counts | 0.003752 | 0.010811 | 0.025945 | 0.044756 | 10 / 0 |
| 1,000 / 10 | Mixed tokens | 0.003584 | 0 | 0.026149 | 0.031619 | 0 / 0 |
| 1,000 / 1,000 | Basic | 0.003294 | 0 | 0.018981 | 0.023404 | 0 / 0 |
| 1,000 / 1,000 | Global numbering | 0.004879 | 0 | 0.025790 | 0.032075 | 0 / 0 |
| 1,000 / 1,000 | Per-directory numbering | 0.003174 | 0 | 0.026258 | 0.030905 | 0 / 0 |
| 1,000 / 1,000 | Directory counts | 0.003584 | 0.925993 | 0.030478 | 0.964617 | 1,000 / 0 |
| 1,000 / 1,000 | Mixed tokens | 0.004273 | 0 | 0.028771 | 0.034622 | 0 / 0 |
| 10,000 / 10 | Basic | 0.038730 | 0 | 0.205776 | 0.255583 | 0 / 0 |
| 10,000 / 10 | Global numbering | 0.040223 | 0 | 0.258213 | 0.312668 | 0 / 0 |
| 10,000 / 10 | Per-directory numbering | 0.046675 | 0 | 0.264824 | 0.330726 | 0 / 0 |
| 10,000 / 10 | Directory counts | 0.044614 | 0.014322 | 0.271742 | 0.371258 | 10 / 0 |
| 10,000 / 10 | Mixed tokens | 0.041963 | 0 | 0.296710 | 0.353283 | 0 / 0 |
| 10,000 / 1,000 | Basic | 0.037509 | 0 | 0.216203 | 0.264590 | 0 / 0 |
| 10,000 / 1,000 | Global numbering | 0.039025 | 0 | 0.266909 | 0.319764 | 0 / 0 |
| 10,000 / 1,000 | Per-directory numbering | 0.037561 | 0 | 0.281276 | 0.337132 | 0 / 0 |
| 10,000 / 1,000 | Directory counts | 0.037053 | 0.940944 | 0.254274 | 1.278721 | 1,000 / 0 |
| 10,000 / 1,000 | Mixed tokens | 0.039104 | 0 | 0.305581 | 0.358425 | 0 / 0 |
| 100,000 / 10 | Basic | 0.495044 | 0 | 2.135440 | 2.930440 | 0 / 0 |
| 100,000 / 10 | Global numbering | 0.398040 | 0 | 2.849671 | 3.438795 | 0 / 0 |
| 100,000 / 10 | Per-directory numbering | 0.591407 | 0 | 2.729025 | 3.735288 | 0 / 0 |
| 100,000 / 10 | Directory counts | 0.434948 | 0.052528 | 2.872239 | 3.810273 | 10 / 0 |
| 100,000 / 10 | Mixed tokens | 0.568267 | 0 | 2.827922 | 3.814864 | 0 / 0 |
| 100,000 / 1,000 | Basic | 0.483106 | 0 | 2.313722 | 2.932766 | 0 / 0 |
| 100,000 / 1,000 | Global numbering | 0.401517 | 0 | 2.856240 | 3.414309 | 0 / 0 |
| 100,000 / 1,000 | Per-directory numbering | 0.547290 | 0 | 3.019563 | 3.732702 | 0 / 0 |
| 100,000 / 1,000 | Directory counts | 0.445330 | 0.988561 | 2.805015 | 4.689572 | 1,000 / 0 |
| 100,000 / 1,000 | Mixed tokens | 0.553096 | 0 | 3.205438 | 3.929362 | 0 / 0 |

For a 10,000-entry in-memory batch, cancellation during filename evaluation was
requested after 100 completed rows and observed 2.571 ms later; the result was
discarded as canceled. A separate test canceled while directory enumeration
was blocked and completed after the operating-system call returned. Cancellation
is cooperative and has no bound while a filesystem call is blocked.

M2-D integrates and preserves the applicable clauses of DD-007, DD-010,
DD-020 through DD-026, DD-031, DD-048, DD-049, DD-051, DD-057, DD-060, DD-061,
DD-065, DD-067, DD-069 through DD-075, DD-077 and DD-078. DD-076 destination
selection remains for the future planner. M2-E1 provides the main-window shell
and shared collection presentation; M2-E2 connects discovery and import, with
incomplete-import warnings retained. Explicit acknowledgment before future
processing remains pending. Remaining GUI integration includes collection
management and preview evaluation. Duplicate analysis and destination
collision checks remain assigned to their later milestones. Execution
preflight and all filesystem mutations remain pending. M3 must force
fresh required counts and independently revalidate sources, filesystem
metadata, destination availability, target-volume constraints and collisions;
metadata observations do not authorize execution.

## Open questions and deferred decisions

- DD-061 comma clarification is approved: commas inside the final string
  argument of `RepS`, `RepM`, and `Ins` are literal content. The parser preserves
  them, and the partial evaluator uses them verbatim.
- M1-B1 defines `MoveX:Y` against the remaining stem, supports zero-length
  `RepM` insertion, and allows empty replacement strings. M1-C2 preserves
  Unicode sequences without normalization and validates Windows device names.
- M1-B2 batch evaluation derives effective indexes from the supplied ordered
  snapshot, uses per-file import roots and physical-count metadata, and blocks
  batch validity when any file fails. It does not enumerate directories or
  authorize filesystem processing. M1-C1 adds source/output extension context
  and extension-resolved candidates; M1-C2 validates filename components but
  does not plan destination collisions.
- `<Ext>` emits the source extension except when it is the terminal `.<Ext>`
  declaration, which emits the intended output extension. Terminal literal
  suffixes matching the source or output extension are recognized case
  insensitively; extension-like text from tokens remains stem content. Candidate
  assembly preserves supplied extension spelling and is not final validation.
  Extension metadata accepts an optional leading dot; an empty string represents
  a known extensionless name, while `None` represents unavailable metadata.
- Numeric widths use a two-digit minimum. Repeated tokens share width per
  numeric family; `<IncNrDir>` width is calculated separately for each parent
  group, while Windows-normalized parent identity distinguishes groups.
- DD-068 legacy-profile import remains deferred and unapproved.
- The project license has not been selected. Packaging and publication are not
  authorized by the implementation roadmap.
