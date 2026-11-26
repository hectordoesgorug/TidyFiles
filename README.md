# TidyFiles

TidyFiles is a Windows desktop application intended to organize files through
bulk renaming, duplicate review, and media repacking.

## Development status

The project is in early development. The application provides a single-instance
guard, configuration and logging foundations, and a GUI with Bulk Rename,
Duplicate Finder, and Repacker tabs sharing one collection table. Add Files and
recursive Add Folder import selected paths progressively using read-only
filesystem metadata; incomplete scans keep their warnings, and cancellation
restores the collection state from before that import. Column visibility and
widths are customizable per mode; appearance and window placement are saved in
the existing configuration. The shared collection supports seven sorting
criteria, saved sort preferences, manual movement and row dragging, selection,
context-menu actions, Remove from List, and Clear All. These controls change only
the in-memory collection; sorting uses stored metadata, and removing or clearing
entries leaves source files unchanged. Bulk Rename provides live filename
previews, three built-in formats, token insertion, a scrollable token reference,
and an explicit refresh for physical directory-count metadata. The format is
session-only. Expression and file-specific errors are shown in the preview;
Process uses the live filename preview as the review interface and runs fresh
preflight before renaming. Duplicate analysis and repacking are not available.
The application is not production-ready.

Collection controls are shared across Bulk Rename, Duplicate Finder, and
Repacker. Use Ctrl+A in the table to select all rows and Delete to remove the
selection from the list. Right-click a row to open or copy its path or name,
change its position, or remove it from the list. Common executable and script
filename extensions are disabled in Open File; opening other files uses their
Windows association and remains an explicit user action.

Bulk Rename labels individually valid filename results **Valid preview**; it
does not check destination collisions, source identity or execution readiness.
Refresh Preview recalculates names for the existing collection and refreshes
required directory counts without rescanning folders or changing collection
membership.

Clicking Process authorizes the displayed operation scope. For an ordinary,
conflict-free plan, background preflight proceeds directly to journaled Windows
renaming when it passes unchanged. Process supports ordinary rename and Single
Folder destinations. Collision-only partial processing and changed plans
require review; filename-format errors block the full batch, and incomplete
imports require acknowledgment. Preparation runs outside Tk and can be
canceled. Active filesystem modification cannot be canceled, and normal
application closure waits until it finishes. Processing status reports completed
files and elapsed time, with an estimated remaining time after enough progress.

Startup displays unresolved transaction recovery details without restoring,
moving, or deleting files. Clear Recovery Records requires confirmation and
removes selected recovery information without changing affected files. Clear
Trash requires confirmation and is limited to recognized managed batches.
Auto-clean and Auto-Clear Trash are separate,
default-off settings. Auto-clean runs only after complete rename success from
explicit Add Folder roots; Auto-Clear Trash runs only after normal exit and
warns only if it encounters errors or leaves content behind.
Duplicate analysis and media repacking are not implemented, and their controls
remain disabled.

## Run locally

Use Python 3.13 x64 on Windows 11:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m tidyfiles
```

See [Development](docs/DEVELOPMENT.md) for checks and manual review, and
[Architecture](docs/ARCHITECTURE.md), [Progress](docs/PROGRESS.md), and
[File safety](docs/FILE_SAFETY.md) for implementation details and status.
