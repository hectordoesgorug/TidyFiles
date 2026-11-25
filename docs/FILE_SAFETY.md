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
readiness. No rename, deduplication, repacking, or cleanup operation is
available.

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

## Planned safety components

The following are not implemented:

- Execution of rename plans and explicit confirmation of collision-only subsets.
- Protected staging for multi-step filesystem changes.
- Durable transaction journals and interrupted-operation recovery inspection.
- Safe original retention and verified installation during repacking.
- Duplicate verification before any deletion action.
- Central application Trash, Trash capacity/error handling, and explicit cleanup
  behavior.
- Execution-time filesystem revalidation and race-safe mutation protections for
  symlinks, junctions, and reparse points.

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
