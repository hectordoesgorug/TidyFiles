# Development

## Requirements

- Windows 11 x64
- Python 3.13 x64
- Git

The project uses CustomTkinter and the standard-library `ttk.Treeview`. Ruff and
pytest are development dependencies declared in `pyproject.toml`.

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

The current shell displays the three operation tabs, an empty file table, and a
status area. It does not import or process files. In a source checkout, the
storage resolver prefers the repository directory for the log when writable;
configuration and log files are ignored by Git. Close the window normally to
release the single-instance guard.

## Local checks

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m compileall -q src tests
git diff --check
```

The current GitHub Actions workflow runs on Windows with Python 3.13 and runs
Ruff and pytest after installing `.[dev]`. Local compile checks and manual GUI
review remain separate from those CI results.

## Documentation boundaries

Tracked application documentation lives in `/docs`. The local `/legacy/` and
`/planning/` directories are Git-ignored references and are not part of the
tracked documentation set. Keep them read-only; do not copy their archives into
the repository.
