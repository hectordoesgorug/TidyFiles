# Project Progress

## Current status

The M0 foundation and minimal GUI shell are implemented. The application opens
with an empty collection and exposes no file-processing or destructive actions.
M1 parser, evaluator, extension resolution, and pure filename validation are
complete, including M1-D.12's DD-061 parser diagnostic correction. They remain
unconnected to the GUI and filesystem operations. See
`docs/FILENAME_ENGINE_COVERAGE.md` for the decision-by-decision audit and
historical verification record. M2-A adds the GUI-independent shared working
collection and import-session lifecycle. M2-B adds a read-only filesystem
discovery service with bounded background work, metadata snapshots, exclusions,
cooperative cancellation, and incomplete-import records. The service is not
connected to the GUI. M2-C adds deterministic collection sorting, manual
ordering, non-destructive row management, persistent sort preferences, and
snapshot revision checks. M2-D adds a filename-evaluator adapter with bounded
directory counts, ordered evaluation, request-currency checks, progress and
cancellation. These domain services are not connected to the GUI. M2-E1 adds
the main-window, shared empty collection presentation, and per-mode column
customization foundation. Discovery/evaluation integration, collection
management bindings, final processing-readiness checks, destination planning,
and filesystem operations remain unimplemented.

| Milestone | Status | Scope |
| --- | --- | --- |
| M0-A | Complete | Baseline repository and legacy-reference inspection. |
| M0-B | Complete | Python project foundation, tests, portable storage, configuration, rotating logging, single-instance protection, and Windows CI. |
| M0-C | Complete | Minimal CustomTkinter shell with three tabs and an empty shared Treeview. |
| M1 | Complete | Pure parser/evaluator, extension resolution, and filename validation, including the M1-D.12 DD-061 diagnostic correction. Application and filesystem integration remain outside M1. |
| M2 | In progress | M2-A through M2-D domain services implemented; M2-E GUI integration is in progress. Filesystem processing remains planned. |
| M2-D | Complete | Collection-backed filename evaluation, physical directory counts, bounded metadata cache, structured diagnostics, request currency, background progress and cancellation. |
| M2-E | In progress | GUI integration, preview rendering and narrower evaluation-validity presentation. |
| M2-E1 | Complete | Main-window layout, shared empty collection presentation, context-specific columns, and persisted appearance and geometry preferences. |
| M2-E2/E3 | Planned | Discovery/import integration and shared collection management bindings. |
| M3 | Planned | Destination planning, filesystem safety, Trash, transaction journals, and recovery. |
| M4 | Planned | Duplicate analysis and verification. |
| M5 | Planned | Media repacking and external-tool integration. |
| M6 | Planned | Complete GUI workflows, settings, profiles, shortcuts, themes, and persistent layout. |
| M6.1 | Planned | Package restructuring and architectural cleanup after M6 and before M7; improve source organization and maintainability without changing established application behavior. |
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
| DD-081 — Shared collection toolbar and list interactions | Shared controls and movement buttons are present. Import, remove, clear, sort, and movement controls remain disabled pending M2-E2/E3 bindings. |
| DD-082 — Compact Bulk Rename format controls | Editable format field and the three approved built-in presets are present. Insert Token, Profiles, Format Reference, and live evaluation remain disabled/deferred. Unsaved field edits are not persisted. |
| DD-083 — Optional post-processing Counter | The switch is present, defaults OFF, and has no execution effect. The algorithm remains deferred. |
| DD-084 — Native toggle switches for boolean operation options | Single Folder, Auto-clean, and Counter use CustomTkinter switches. Single Folder and Counter only change session-local UI state. Auto-clean is disabled. |
| DD-085 — Problematic-row highlighting and expression-wide errors | A subtle problem-row Treeview style is available for later rendering. Live evaluation, row diagnostics, expression-wide invalidation, and preview clearing remain deferred. |

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
and clamped to the current Tk virtual-screen bounds. Physical monitor-layout and
DPI-change acceptance remains a manual M2-E5 check.

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

All unconnected controls remain disabled. No automatic import, evaluation,
cleanup, or file operation occurs. The main window exposes the shared collection
as `root.application.collection` and provides
`root.application.refresh_collection()` for later GUI-thread integration. Actual
discovery and evaluation services are not started by this slice.

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
and shared collection presentation. Remaining M2-E integration includes
discovery/import acknowledgment, collection management, duplicate analysis,
preview evaluation and destination collision checks. Execution preflight and
all filesystem mutations remain pending. M3 must force
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
