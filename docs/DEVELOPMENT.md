# Development

## Requirements

- Windows 11 x64
- Python 3.13 x64
- Git

The project uses CustomTkinter and the standard-library `ttk.Treeview`. The exact
duplicate engine uses `xxhash`. Perceptual hashing uses Pillow, ImageHash, and
pillow-heif. ImageHash supplies its scientific dependencies through normal
package installation. Ruff and pytest are development dependencies.

## Create the environment and install

From the repository root in PowerShell:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

The editable install includes the application and its runtime dependencies, as
well as pytest and Ruff.

## Launch and review the GUI

```powershell
.\.venv\Scripts\python.exe -m tidyfiles
```

The window opens with an empty file table. Add Files and Add Folder populate
the shared collection. Bulk Rename supports live previews, background preflight,
and journaled execution, including Single Folder. Duplicate Checker is on by
default and uses xxHash128 with four hash workers. Changes in Settings apply
to the next import. Repacking is planned. In a source checkout, writable
repository storage is preferred for the log. Git ignores configuration and
log files. Close the window normally to release the single-instance guard.

For desktop acceptance, use disposable folders with equal files and same-size
files with different contents. Import across Add Files and Add Folder, check
the active counts and numbered rename previews, then exercise both Files
Excluded buttons and the flat Duplicate Finder list. Check Cancel during
analysis and retry Deduplicate with a missing keeper or changed copy. Confirm
Deduplicate keeps Files Excluded open and refreshes its counts and outcomes.
Check the layout at the supported display scales
and exercise Clear Trash and normal-exit Auto-Clear Trash only against
disposable batches.

For probable duplicates, use two differently encoded copies of a test image.
Enable both duplicate settings and check the review after an import with and
without exact exclusions. Exercise Skip and the four selection or transfer
controls. Check largest-first ordering, thumbnails, and dialog resizing under
light and dark themes. With a clone, verify that Select All can transfer an
entire group and that a path changed during review is excluded from processing
and reported as a failed transfer. Switch tabs after review and verify the
shared rows and filename preview. Compare media columns with and without
FFprobe and confirm Process becomes available independently of those reads.

For verification acceptance, import a disposable folder containing a valid
photo, a truncated photo, an unsupported media file, and a pair of exact
copies. Check that Pre-verify works with Duplicate Checker disabled. With
both enabled, test Purge and Deduplicate in either order before pressing OK.
Check that controls lock during a transfer and that perceptual hashing starts
after OK when enabled. Verify that a file replaced during review remains
outside processing and is reported when Purge runs. Exercise
Post-verify with Auto-clean and check the completion summary and resulting
files after a verification issue.

## Local checks

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m compileall -q src tests
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

The current GitHub Actions workflow runs on Windows with Python 3.13 and runs
Ruff and pytest after installing `.[dev]`. Local compile checks and manual GUI
review remain separate from those CI results.

The default suite runs without portable media executables. With a local pair
installed as described in the [README](../README.md#optional-media-tools), run
the opt-in integration tests against disposable media:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/media_integration_worker.py
```

The tested pair reported `9.1-essentials_build-www.gyan.dev` for both tools.
The test suite leaves the supplied executables intact and tests copying only
into a disposable application directory.

## Documentation boundaries

Tracked application documentation lives in `/docs`. The local `/legacy/` and
`/planning/` directories are Git-ignored references and are not part of the
tracked documentation set. Keep them read-only; do not copy their archives into
the repository.
