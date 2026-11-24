# Project Progress

## Current status

The M0 foundation and minimal GUI shell are implemented. The application opens
with an empty collection and exposes no file-processing or destructive actions.
M1 parser and partial pure evaluator work is available in the working tree;
neither is integrated into the GUI or connected to filesystem operations. The
M1-B1 implementation baseline was `main` at
`4c0c48378087b7d77e04eb8d10897ea30bb35ff0`.

| Milestone | Status | Scope |
| --- | --- | --- |
| M0-A | Complete | Baseline repository and legacy-reference inspection. |
| M0-B | Complete | Python project foundation, tests, portable storage, configuration, rotating logging, single-instance protection, and Windows CI. |
| M0-C | Complete | Minimal CustomTkinter shell with three tabs and an empty shared Treeview. |
| M1 | In progress | Strict parser and pure evaluation for original stems, text extraction, and text manipulation implemented; numbering, directory context, extension handling, filename validation, and batch evaluation remain planned. |
| M2 | Planned | Shared collection, import, scanning, ordering, and cancellation. |
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

## Open questions and deferred decisions

- DD-061 comma clarification is approved: commas inside the final string
  argument of `RepS`, `RepM`, and `Ins` are literal content. The parser preserves
  them, and the partial evaluator uses them verbatim.
- M1-B1 defines `MoveX:Y` against the remaining stem, supports zero-length
  `RepM` insertion, and allows empty replacement strings. Literal
  dot/extension interaction, Unicode normalization, and Windows device-name
  variants remain for later filename validation work.
- Numeric and directory-context tokens remain unsupported by the partial
  evaluator. Evaluator failures are per-file; batch-wide aggregation and the
  all-file processing gate remain unimplemented.
- DD-068 legacy-profile import remains deferred and unapproved.
- The project license has not been selected. Packaging and publication are not
  authorized by the implementation roadmap.
