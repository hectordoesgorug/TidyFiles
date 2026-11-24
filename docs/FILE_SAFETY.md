# File Safety and Current Status

The current application is a GUI shell and does not import, rename, delete,
repack, or otherwise modify user files. The foundations below do not imply that
future file operations are already safe or implemented.

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
- Filesystem revalidation and symlink, junction, and reparse-point safeguards.

Future filesystem work must independently validate a complete destination plan,
revalidate before mutation, avoid unauthorized overwrites and deletion, and use
temporary isolated fixtures for destructive-operation tests. Logs must not be
used as the sole recovery record. No file operation should run automatically.
