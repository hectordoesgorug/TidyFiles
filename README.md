# TidyFiles

TidyFiles is a Windows desktop application intended to organize files through
bulk renaming, duplicate review, and media repacking.

## Development status

The project is in early development. The current application provides a
single-instance guard, configuration and logging foundations, and a minimal GUI
shell with Bulk Rename, Duplicate Finder, and Repacker tabs. File import,
renaming, duplicate analysis, repacking, and filesystem changes are not yet
available. The application is not production-ready.

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
