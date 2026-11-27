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
the in-memory collection. File Type + Name sorts non-GIF images, GIFs, audio,
video, and other files in that order. It sorts names naturally and
case-insensitively within each group. Removing or clearing entries leaves
source files unchanged.
Bulk Rename provides live filename
previews, three built-in formats, token insertion, a scrollable token reference,
and an explicit refresh for physical directory-count metadata. The format is
session-only. Expression and file-specific errors are shown in the preview;
Process uses the live filename preview as the review interface and runs fresh
preflight before renaming. Duplicate checking includes exact exclusion and
optional perceptual review. Integrity verification and Purge are available.
Media repacking is planned.
The application is not production-ready.

Use Ctrl+A in the table to select all rows and Delete to remove selected
processing entries from the list. Right-click a row to open or copy its path
or name. Processing entries also support movement and removal. Common
executable and script filename extensions are disabled in Open File. Other
files open through their Windows association on explicit request.

With **Duplicate Checker** enabled in Settings, Add Files and Add Folder
analyze the cumulative processing collection in the background. The first
verified copy in collection order stays in processing. **Files Excluded**
reports the redundant copies and the number of files remaining for processing.
Choose **OK** to keep those copies on disk or **Deduplicate** to authorize a
managed Trash transfer. Progress and results
appear in the main window's status area. The **Duplicate Finder** tab lists
excluded copies with their original filenames for later review and Deduplicate. Use
**Clear All** to clear the session's collection and exclusions.

**Pre-verify** and **Post-verify** in Settings are independent and default off.
Pre-verify checks the processing collection after discovery and before duplicate
analysis. Only files with positive corruption evidence leave processing.
**Files Excluded** shows the breakdown and remaining count. **Purge** there or
in the main window authorizes managed Trash transfer of pending corruption
exclusions. **Deduplicate** authorizes transfer of exact copies. The dialog
stays open, shows the transfer result and updated pending counts, and allows
the other action when files remain eligible. While a transfer runs, all
dialog controls are disabled. **OK** closes the dialog and leaves remaining
excluded files on disk.

With **Perceptual Hashing** also enabled, the import checks the remaining files
for similar images and videos. If Files Excluded appears, the check starts
after **OK** closes the dialog. Process
becomes available after review and preview preparation. The **Probable
Duplicates** dialog shows groups with the largest file first. **Auto-select**
checks every file except the largest in each group. **Delete Selected**
excludes the checked files from processing and sends them to managed Trash.
Transfer failures appear in the status area and log. **Skip** keeps the files
in the processing collection.

The shared table fills in image and video resolutions and video and audio
lengths as background reads finish. Applicable values that could not be read
show as unavailable. The preview and Process can be ready while these reads
continue.

Bulk Rename labels individually valid filename results **Valid preview**; it
does not check destination collisions, source identity or execution readiness.
Refresh Preview recalculates names for the existing collection and refreshes
required directory counts without rescanning folders or changing collection
membership. If the table could not be rebuilt after a canceled or failed import,
Refresh Preview retries that rebuild before Process becomes available.

Clicking Process authorizes the displayed operation scope. For an ordinary,
conflict-free plan, background preflight proceeds directly to journaled Windows
renaming when it passes unchanged. Process supports ordinary rename and Single
Folder destinations. Collision-only partial processing and changed plans
require review; filename-format errors block the full batch, and incomplete
imports require acknowledgment. Preparation runs outside Tk and can be
canceled. Active filesystem modification cannot be canceled, and normal
application closure waits until it finishes. Processing status reports completed
files and elapsed time, with an estimated remaining time after enough progress.

Post-verify checks successful outputs at their final paths before eligible
Auto-clean. The Treeview and rename summary report the outcome. A failed
check leaves the output in place. Verification loads supported images with
Pillow, inspects supported audio and video containers and streams with
FFprobe, and reads up to 512 bytes for other files. **Verified** means those
limited checks passed.
The other outcomes are **Corrupted**, **Unsupported**, **Inaccessible**, and
**Incomplete**.

Startup displays unresolved transaction recovery details without restoring,
moving, or deleting files. Clear Recovery Records requires confirmation and
removes selected recovery information without changing affected files. Clear
Trash requires confirmation for the inspected files and removable directories in
recognized managed batches. Items that arrive after inspection are preserved.
Auto-clean and Auto-Clear Trash are separate,
default-off settings. Auto-clean runs only after complete rename success from
explicit Add Folder roots; Auto-Clear Trash runs only after normal exit and
warns only if it encounters errors or leaves content behind.

## Optional media tools

Video perceptual hashing requires local `ffmpeg.exe` and `ffprobe.exe`. Audio
and video verification and their metadata columns use `ffprobe.exe`.
The recommended Windows
x64 reference is Gyan's FFmpeg 9.1 Essentials ZIP. Other
compatible versions are accepted. [FFmpeg downloads](https://ffmpeg.org/download.html)
[Gyan Windows builds](https://www.gyan.dev/ffmpeg/builds/)
[Essentials ZIP](https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip)

Extract the ZIP and find the `bin` directory. Place the two executables beside
`TidyFiles.exe`, or use **Settings → Locate and Import** and select the
`bin` directory. For a source checkout, the portable directory is the
repository root. Locate and Import copies the executables, retains the
originals, and asks before replacing existing copies.

TidyFiles checks the local versions in the background. You can also run these
commands from the portable directory:

```powershell
.\ffmpeg.exe -version
.\ffprobe.exe -version
```

Image perceptual hashing, exact duplicate checking, and renaming work without
the media tools. TidyFiles releases exclude both executables from archives and
PyInstaller bundles.

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
