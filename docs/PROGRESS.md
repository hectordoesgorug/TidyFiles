# Project Progress

## Current status

The M0 foundation and minimal GUI shell are implemented. The application opens
with an empty collection and exposes no file-processing or destructive actions.
M1 parser, evaluator, extension resolution, and pure filename validation are
complete, including M1-D.12's DD-061 parser diagnostic correction. They remain
unconnected to the GUI and filesystem operations. See
`docs/FILENAME_ENGINE_COVERAGE.md` for the decision-by-decision audit and
historical verification record. M2-A adds the GUI-independent shared working
collection and import-session lifecycle. Scanning, GUI import integration,
filename-evaluator adaptation, and filesystem operations are not implemented.

| Milestone | Status | Scope |
| --- | --- | --- |
| M0-A | Complete | Baseline repository and legacy-reference inspection. |
| M0-B | Complete | Python project foundation, tests, portable storage, configuration, rotating logging, single-instance protection, and Windows CI. |
| M0-C | Complete | Minimal CustomTkinter shell with three tabs and an empty shared Treeview. |
| M1 | Complete | Pure parser/evaluator, extension resolution, and filename validation, including the M1-D.12 DD-061 diagnostic correction. Application and filesystem integration remain outside M1. |
| M2 | In progress | M2-A shared collection domain model and import-session lifecycle implemented. File/folder scanning, GUI import, and evaluator adaptation remain planned. |
| M3 | Planned | Destination planning, filesystem safety, Trash, transaction journals, and recovery. |
| M4 | Planned | Duplicate analysis and verification. |
| M5 | Planned | Media repacking and external-tool integration. |
| M6 | Planned | Complete GUI workflows, settings, profiles, shortcuts, themes, and persistent layout. |
| M7 | Planned | End-to-end integration, packaging, release readiness, and coverage closure. |

The later milestone descriptions follow the local implementation roadmap; they
are plans, not implementation evidence.

## Verification record

At the M0-C checkpoint, Windows GitHub Actions run #9 was reported as passing
Ruff and all 37 pytest tests. That is historical milestone evidence. The current
CI workflow runs Ruff and pytest on Windows with Python 3.13.

During M1-A, local Ruff, pytest (105 tests), compileall, and diff checks passed
on the working tree based on commit `442bb8ec92768835c394d4ed71a9b21cc04001ad`.
These local results do not represent a GitHub Actions run for the parser change.

For M1-B1, local Ruff, all 152 pytest tests, compileall, and `git diff --check`
passed on the working tree based on `4c0c48378087b7d77e04eb8d10897ea30bb35ff0`.
The focused parser/evaluator run passed all 115 tests. These are local results;
no GitHub Actions run or IDE type-checker result is claimed. Evaluator tests use
strings only and do not access user files.

For M1-B2, local Ruff, all 179 pytest tests, compileall, and `git diff --check`
passed on the working tree based on `67af79a222473a27f9b1e9d7f46ffb3d0ed8ad75`.
The focused parser/evaluator run passed all 142 tests. These are local results;
no GitHub Actions run or IDE type-checker result is claimed. Batch fixtures use
immutable synthetic file metadata and do not access user files.

For M1-C1, local Ruff, all 206 pytest tests, compileall, and `git diff --check`
passed on the working tree based on `13cf6a6646177bbc68746fbeda090b7dc9c3e6a0`.
The focused parser/evaluator/extension run passed all 169 tests. These are local
results; no GitHub Actions run or IDE type-checker result is claimed. Extension
tests use synthetic names and metadata and do not access user files.

For M1-C2, local verification was run on the uncommitted working tree based on
`e9a6491d1bb033f295c88eb8934d76929ca37e`: Ruff passed, pytest passed all 259
tests, compileall passed, and `git diff --check` passed. No `pyright` or `mypy`
executable was available. The new validator tests use synthetic names and
metadata; no target-volume or filesystem behavior is verified.

For M1-D, the local audit and three regression tests ran on `main` based on
`50a97a929130c5a6502ac9fc751d9f15700b79bb`. The checkout matched `origin/main`;
Windows CI run #14 passed Ruff and pytest for that commit. Locally, Ruff,
compileall, and `git diff --check` passed; pytest passed all 262 tests, including
three new audit regressions. No static type checker or coverage package was
available; test coverage was reviewed manually. The audit does not verify
target-volume behavior or filesystem execution.

For M1-D.12, the parser now retains the full original string in
`FilenameFormatParseError.expression`; non-string input records `None`. Local
verification passed Ruff, pytest (267 tests), compileall, and `git diff --check`;
the focused parser/evaluator run passed all 119 tests. The constructor audit
confirmed expression context on every string-input parser error path. No
`pyright`, `mypy`, or `basedpyright` executable was available. The audit's
DD-061 finding is resolved; no other M1
pure-engine closure blocker was identified. M2/M3/M5/M6 integration remains
planned, and no Windows destination-volume behavior is claimed.

For M2-A, local verification ran on Windows with Python 3.13.17, on the
working tree based on `6c8c81d6286aa1e887330ebed754de15b02b03fd`. The focused
collection suite passed all 11 tests; Ruff passed; the full pytest suite passed
all 278 tests with no skips; `compileall` passed; and `git diff --check` passed
with line-ending conversion warnings for the two edited Markdown files. No
GitHub Actions result is claimed. The tests cover lexical Windows path keys,
path-based deduplication, context preservation, import-session finalization and
rollback, ordered immutable snapshots, optional metadata, and source-file
preservation. Scanning, asynchronous worker behavior, GUI integration, and
filesystem identity acquisition remain unverified and unimplemented.

## Open questions and deferred decisions

- DD-061 comma clarification is approved: commas inside the final string
  argument of `RepS`, `RepM`, and `Ins` are literal content. The parser preserves
  them, and the partial evaluator uses them verbatim.
- M1-B1 defines `MoveX:Y` against the remaining stem, supports zero-length
  `RepM` insertion, and allows empty replacement strings. M1-C2 preserves
  Unicode sequences without normalization and validates Windows device names.
- M1-B2 batch evaluation derives effective indexes from the supplied ordered
  snapshot, uses per-file import roots and physical-count metadata, and blocks
  batch validity when any file fails. It does not enumerate directories or
  authorize filesystem processing. M1-C1 adds source/output extension context
  and extension-resolved candidates; M1-C2 validates filename components but
  does not plan destination collisions.
- `<Ext>` emits the source extension except when it is the terminal `.<Ext>`
  declaration, which emits the intended output extension. Terminal literal
  suffixes matching the source or output extension are recognized case
  insensitively; extension-like text from tokens remains stem content. Candidate
  assembly preserves supplied extension spelling and is not final validation.
  Extension metadata accepts an optional leading dot; an empty string represents
  a known extensionless name, while `None` represents unavailable metadata.
- Numeric widths use a two-digit minimum. Repeated tokens share width per
  numeric family; `<IncNrDir>` width is calculated separately for each parent
  group, while Windows-normalized parent identity distinguishes groups.
- DD-068 legacy-profile import remains deferred and unapproved.
- The project license has not been selected. Packaging and publication are not
  authorized by the implementation roadmap.
