# File Safety and Current Status

The GUI presents one shared collection across three modes. Add Files and Add
Folder import selected paths using read-only discovery metadata; file contents
are not opened. Cancellation rolls back only entries from the active import
and does not change source files. Sorting, manual ordering, Remove from List,
and Clear All manage the shared in-memory collection. Remove and Clear All leave
source files unchanged; they do not delete, move, or Trash files. Opening a row
or its containing folder is an explicit Windows shell action. Known executable
and script extensions are disabled for Open File; suffix checks cannot identify
every potentially launchable file. Bulk Rename shows read-only filename
previews. The **Valid preview** label means filename-level validation passed;
it does not verify destination collisions, source identity or execution
readiness. The desktop rename, deduplication, repacking, and cleanup controls
are disabled.

The Auto-clean switch is disabled until its permanent-deletion disclosure and
eligible-folder workflow are implemented. Its approved future deletion scope
is limited to `Thumbs.db`, `.DS_Store`, `desktop.ini`, and empty eligible
subdirectories. Counter is an off-by-default UI option only; no validation
algorithm or post-processing action runs. Neither switch can start a filesystem
operation.

## Implemented foundations

- **Single-instance protection:** a Windows named mutex prevents a second
  application instance from initializing storage or the GUI. An abandoned
  mutex can be acquired after abnormal process termination.
- **Configuration preservation:** invalid or unreadable configuration raises a
  visible startup error. Startup does not replace it with defaults. Saving
  validates the existing file and uses a temporary file followed by atomic
  replacement.
- **Storage selection:** ordinary configuration and logs use the application
  directory when writable and fall back to `%LOCALAPPDATA%\TidyFiles`.
- **Logging:** rotating logs use a 5 MB active file and three backups. If file
  logging cannot be opened, setup falls back to a no-op handler.
- **Discovery:** selected files and recursively scanned folders are inspected
  with no-follow metadata checks. Discovery excludes `Thumbs.db`, `.DS_Store`,
  and `desktop.ini` case-insensitively, `$RECYCLE.BIN`, `System Volume
  Information`, nonregular entries, and observed symlinks, junctions, and other
  reparse points. The three clutter files and protected directories are not
  deleted. Ordinary hidden and dot-prefixed files remain eligible. Actual
  application-managed Trash, staging, and recovery paths must be supplied to
  the discovery service explicitly; storage currently defines no such paths.
  The GUI displays accepted rows progressively and reports discovery progress,
  intentional exclusions, and errors separately. Discovery reads metadata but
  not file contents, and retains incomplete-scan diagnostics with successfully
  discovered entries. These warnings remain visible after later successful
  imports. Import does not authorize later processing.
- **Collection management:** Move and sort operations change only the working
  list. Remove from List drops selected entries from memory and leaves their
  source files and explicit Add Folder root records intact. Incomplete-scan
  warnings remain until Clear All. Clear All removes collection entries,
  import-root records, and collection-specific diagnostics from memory while
  retaining saved sorting preferences. These operations are disabled while an
  import is active.
- **Filename evaluation:** The adapter reads only the immutable collection
  snapshot and uses the existing pure ordered evaluator. It enumerates an
  immediate parent directory only when `<NumFiles>` or `<NumDirs>` is present.
  Counts include ordinary files excluded from import, count hard-link names as
  separate entries, and skip symlinks, junctions, other reparse points and
  special entries. Counts are nonrecursive; `<NumItems>` comes from collection
  size. An inaccessible, missing, replacement-detected or partially enumerated
  parent produces unavailable counts and diagnostics; it never becomes zero.
  No file contents are opened, and evaluation changes neither collection membership
  nor source files. Results are current only for the matching collection
  revision, request ID, expression and settings. A valid evaluation is not a
  processing-ready state and does not satisfy incomplete-import
  acknowledgment, duplicate analysis, destination collision checks, execution
  preflight or explicit user authorization.
- **Filename previews:** The Bulk Rename tab displays filenames evaluated from
  the complete finalized collection order. The editable format is session-only.
  Preview results are invalidated by expression, collection, import, or metadata
  refresh changes, and stale results are rejected before rendering. Directory
  counts are acquired only for count tokens; Refresh Preview discards cached
  observations and requests fresh counts for the existing collection. It does
  not rescan folders or change collection membership. Parser errors invalidate
  every row; file-specific errors affect only their rows visually but invalidate
  the full rename batch. A valid preview is not permission or readiness to
  rename files.
  The Process, Deduplicate, and Repack controls remain disabled, and preview
  evaluation neither opens file contents nor changes source files.

## Destination planning observations

The standalone planner checks sources and candidate destinations without
opening file contents or creating directories. Exactly unchanged paths are
no-ops. Case-only changes remain rename operations. An existing regular file
can be a vacating dependency only when its observed volume, long parent
spelling, and directory-entry name match a batch source. Distinct hard-link
names remain independent entries even when they refer to the same file object.
A hard-link name outside the batch can occupy and block a destination.

The native observer uses literal Win32 metadata lookups and caches shared
ancestor and volume observations for one plan. It refuses reparse entries and
their descendants, non-file destinations, protected system paths, the shared
clutter names, and supplied managed locations. Volume mapping also protects
managed paths across drive letters that point to the same local volume.
Absence is accepted only for a missing leaf below an observed eligible parent.
Permission errors, disconnected volumes and other failed queries are
unverifiable. [FindFirstFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-findfirstfilew)

The observer supports ordinary absolute drive-letter paths on directly mapped
local disk volumes. It treats UNC, mapped network, SUBST, device and extended
paths as unverifiable. It detects an existing short-name alias when the lookup
returns a different long name. A vacant name shaped like a generated 8.3 alias
is also unverifiable because another operation could create that alias. The
planner groups filename case variants conservatively within each observed
parent, preserves Unicode spelling, and refuses case-sensitive directories.
This can block names a particular filesystem would distinguish.
[Windows path naming](https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file)
[Windows case sensitivity](https://learn.microsoft.com/en-us/windows/wsl/case-sensitivity)
[QueryDosDeviceW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-querydosdevicew)

The observer checks the reported volume component limit in UTF-16 units,
applies the ordinary Win32 path limit of 259 units plus the terminator, and
blocks modifications on read-only volumes. Case-only renames require a volume
that preserves case. A successful plan does not establish rename permission,
source identity continuity, or freedom from later changes and races.
[GetVolumeInformationW](https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-getvolumeinformationw)
[Windows path length limits](https://learn.microsoft.com/en-us/windows/win32/fileio/maximum-file-path-limitation)

## Preflight identity and safety

Preflight augments the planner's literal lookups with `OPEN_EXISTING` handles
using `FILE_READ_ATTRIBUTES`, shared read/write/delete, and
`FILE_FLAG_OPEN_REPARSE_POINT`. It checks disk object type, regular-file or
directory status, and reparse and delete-pending state. Each handle closes
before its observation returns. Metadata queries are limited to sources and
relevant parent or occupant objects. Handles request metadata access only.
File contents remain unopened.

File and parent identities use `FileIdInfo` volume serial and the 128-bit file
ID. A volume GUID and the DOS device mapping bind the observed drive. An
occupied name also records the actual long parent and entry names, which
distinguish hard-link paths. Size, last-write time, creation time when
available, change time and attributes come from the same opened object.
Access time is excluded. Windows CPython 3.13 represents its modern stat
volume serial and file ID in the same integer form. Preflight requires a
matching discovery identity and compares its available metadata, including
size and modification time. An absent or incomparable import or review
baseline blocks validation. [Windows file identity](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_id_info)
[CPython 3.13 stat conversion](https://github.com/python/cpython/blob/3.13/Python/fileutils.c)

The review baseline must be captured before review. A native preflight uses
fresh observers on both sides of filename evaluation and caches only within
each pass. It rechecks the exact scope and compares the current parents,
objects, relevant metadata, and included occupants with the review.
Parent and volume-mapping continuity begins at review-time capture because
the import snapshot carries file identities and metadata only.
The existing UNC, mapped drive, SUBST, device, long-path and case-sensitive
directory restrictions apply.

Successful validation describes a point-in-time observation. Ancestor checks
and subsequent opens are path-based, so external changes can still occur
within a pass and after preflight. Metadata access establishes neither rename
permission nor no-overwrite behavior. Execution needs its own immediate
identity and occupancy checks, handle-based race protection, authorization
and durable transaction safeguards.

## Journal persistence and retention

DD-086 Option A keeps journals portable-first and uses LocalAppData fallback
only before preparation publishes a journal. The fixed recovery locator in
LocalAppData registers the journal's original absolute path. The initial
journal and its registration must both succeed before the modifying phase can
begin. A failed registration can leave a prepared journal visible to direct
discovery. A journal session pins its location and blocks further modification
after a failed required write. Missing or damaged records stay visible.

Persistence validates the JSON, writes a temporary sibling, flushes and
synchronizes its contents, then publishes it with a same-directory atomic
replacement. On Windows, publication uses `MoveFileExW` with write-through.
The old contents remain available when a write or replacement fails before
publication. Journal and locator updates are separate filesystem operations.
These mechanisms do not guarantee directory-entry persistence on every
filesystem, device, or abrupt power loss. [MoveFileExW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexw)

Before a checkpoint, the session verifies its locator registration and
compares the current journal to the exact bytes it last published.
In-progress checkpoints reuse validated representations of unchanged entries,
validate changed entries and record-wide constraints, and publish a complete
version 1 record. The session adopts the new representations only after
successful publication. A failed write leaves the session at its last
successful checkpoint and blocks further modification.

Finalization requires an independently verified outcome for every entry and
resolution of each recorded staging or temporary recovery dependency. It
persists the terminal record first. It then retires the locator entry and
removes the terminal journal. If removal fails, it retains the journal and
attempts to restore its locator entry while the locator remains unchanged.
A cleanup failure is surfaced, with any remaining evidence preserved.
Discovery can find a terminal journal
left between those steps through a known journal directory. Source, staging,
and temporary processing paths remain untouched by journal finalization.

Directory and record checks refuse observed links and reparse points and
recheck expected journal contents before cleanup. These are path-based
checks against external changes. Recovery discovery reports evidence only.
The executor must perform its own immediate source and destination checks
and wait for required checkpoints before crossing modification boundaries.

## Rename execution safety

The standalone Windows executor binds `NtSetInformationFile(FileRenameInformation)`
to the verified source handle and destination-directory handle. Windows
rejects an occupied target because replacement is disabled. Directory
handles are opened component by component without traversing reparse points
and held without delete sharing during the move. The source handle denies
write and delete sharing. The executor also reobserves volume mapping, both
endpoints, and vacancy after the journal checkpoint and before moving.
[NtCreateFile](https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntcreatefile)
[FILE_RENAME_INFORMATION](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_file_rename_information)

Every staged move first records its unique sibling path in the journal.
The path must be vacant and separate from planned paths on the same volume.
The executor owns an artifact only when it verifies its original identity
at the staged path. A successful installation consumes that entry by rename
and verifies the staging name is vacant. A surviving or unverifiable stage
remains recorded. The executor creates no destination directories and
performs neither file copying nor deletion to make room.

NTFS name tunneling can inherit a creation time from a recently vacated
name. Source handles request `FILE_WRITE_ATTRIBUTES` so the executor can
preserve the original creation time on the same object when this occurs.
It verifies identity, spelling, creation and last-write times, size and
attributes. Change time may advance through the authorized move, including
at other hard links to the same object. [NTFS name tunneling](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/fltkernel/nf-fltkernel-fltgettunneledname)
[FILE_BASIC_INFORMATION](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/wdm/ns-wdm-_file_basic_information)

A failure stops further moves and leaves completed files and any staged
originals in place. The executor preserves the last durable checkpoint if
an after-move write fails. It attempts verified finalization only when the
journal remains healthy and all entries and artifact vacancies can be
established. An interruption can leave a prepared or in-progress record.
The executor performs no automatic rollback, restoration, or startup
cleanup.

These safeguards apply to supported local Windows paths and sharing
semantics. Metadata, drive mappings, and names can still change between
separate observations or after handles close, including after final
verification. Permission and sharing failures block a move when it cannot
be safely established. A power loss can occur between the move and its
after-move record.

## Planned safety components

The following are not implemented:

- GUI confirmation of authorized rename scopes and collision-only subsets.
- Protected staging for other processing workflows.
- Startup recovery notification, staging inspection and overlap enforcement.
- Safe original retention and verified installation during repacking.
- Duplicate verification before any deletion action.
- Central application Trash, Trash capacity/error handling, and explicit cleanup
  behavior.
- Operator inspection and recovery of interrupted transactions.

Future filesystem work must independently validate a complete destination plan,
revalidate before mutation, avoid unauthorized overwrites and deletion, and use
temporary isolated fixtures for destructive-operation tests. Logs must not be
used as the sole recovery record. No file operation should run automatically.

Path-based discovery and count-acquisition checks do not provide a handle-based
defense against an external process replacing a directory with a reparse point
between inspection and enumeration. Directory-count cache entries are
performance aids, not proof of current contents. Treat discovery and evaluation
metadata as snapshots and revalidate all affected paths before any future
filesystem mutation.
