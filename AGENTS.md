# TidyFiles Development Guidelines

## Project

TidyFiles is a Windows desktop application for file organization. The approved
platform is Windows 11 x64 with Python 3.13 x64. The current GUI uses
CustomTkinter and a styled `ttk.Treeview`.

## Before changing code

- Inspect the current branch, commit, working tree, source, tests, and relevant
  tracked documentation. Do not assume a prior report describes the current
  checkout.
- Preserve unrelated user changes. Make the smallest focused change that meets
  the approved requirement.
- Keep the implementation consistent with the actual package architecture and
  configuration. Report discrepancies instead of silently changing product
  requirements.

## Architecture

- Keep the GUI, pure filename parsing/evaluation, collection and processing
  services, filesystem operations, and persistence in separate components.
- Keep business rules out of UI event handlers. Use one validated destination
  plan for previews and any future filesystem executor.
- Do not treat a planned component as implemented. See `docs/ARCHITECTURE.md`
  and `docs/PROGRESS.md` for current responsibilities and status.

## Local references

- `/legacy/` and `/planning/` are local-only, Git-ignored reference directories.
  They are strictly read-only: do not edit, rename, delete, or copy their
  contents into tracked project documentation.
- Their contents may describe proposed or historical decisions. They are not
  evidence that a feature exists in the application.

## Filesystem and data safety

- Do not add automatic file processing, unauthorized overwrites, deletion, or
  unsafe recovery behavior.
- Before future filesystem mutations, validate the complete destination plan
  independently, then revalidate affected sources and destinations immediately
  before execution. Never silently resolve collisions by overwriting or
  inventing a new name.
- Test destructive and failure paths only with temporary, isolated fixtures.
  Verify that unrelated files and original sources remain intact.
- Keep configuration errors visible and preserve damaged configuration rather
  than replacing it with defaults.

## Testing and documentation

- Use the repository virtual environment and existing checks:

  ```powershell
  .\.venv\Scripts\python.exe -m ruff check .
  .\.venv\Scripts\python.exe -m pytest
  .\.venv\Scripts\python.exe -m compileall -q src tests
  ```

- Add regression tests for changed behavior and report actual results, skips,
  and failures. Windows CI runs Ruff and pytest on Python 3.13; it complements,
  but does not replace, local verification.
- Keep `README.md` and the documents under `/docs` synchronized with the source.
  Separate implemented, planned, historical, and unverified behavior.

## Approval and Git boundaries

- Do not install dependencies, delete user files, package or publish the
  application, or change real user data without explicit authorization for that
  action.
- Do not stage, commit, push, merge, rebase, tag, or publish without separate
  authorization. Report the final Git state.
- Do not invent a license, release policy, packaging decision, or unapproved
  feature.

## Project writing

Write repository-facing text in a direct, technically accurate developer voice.
Keep internal collaboration terminology out of public documentation. Preserve
dates, commit IDs, test counts, and verification provenance; never present a
historical or reported result as a check performed during the current task.
