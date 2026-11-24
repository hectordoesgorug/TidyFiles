# File Safety and Current Status

The current GUI is a shell and does not import or process user files. A separate
read-only discovery service is implemented but is not connected to the GUI. No
file operation renames, deletes, moves, repacks, or otherwise modifies user
files.

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
  Discovery reads metadata but not file contents, and retains incomplete-scan
  diagnostics with any successfully discovered entries. It does not authorize
  later processing.

These are startup and persistence foundations, not a file-recovery system.

## Planned safety components

The following are not implemented:

- Collision-safe batch rename planning and execution.
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

Path-based discovery checks do not provide a handle-based defense against an
external process replacing a directory with a reparse point between inspection
and enumeration. Treat metadata as a snapshot and revalidate all affected paths
before any future filesystem mutation.
