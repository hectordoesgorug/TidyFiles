# Project Progress

## Current status

The M0 foundation and minimal GUI shell are implemented. The application opens
with an empty collection and exposes no file-processing or destructive actions.
The code checkpoint reviewed for this status is `main` at
`7bef5f9ffb0f0bef408c07802422c039d87c682f`. Documentation changes in this
working tree have not been committed.

| Milestone | Status | Scope |
| --- | --- | --- |
| M0-A | Complete | Baseline repository and legacy-reference inspection. |
| M0-B | Complete | Python project foundation, tests, portable storage, configuration, rotating logging, single-instance protection, and Windows CI. |
| M0-C | Complete | Minimal CustomTkinter shell with three tabs and an empty shared Treeview. |
| M1 | Planned | Pure filename engine. Parser grammar inspection is complete; parser implementation has not started. |
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
Ruff and all 37 pytest tests. That is historical milestone evidence, not a check
performed for this documentation update. The current CI workflow runs Ruff and
pytest on Windows with Python 3.13.

## Open questions and deferred decisions

- DD-061 parser clarification: should commas inside the final string argument
  of `RepS`, `RepM`, and `Ins` be literal content? The recommended compatibility
  policy is to preserve the legacy behavior and allow them. This remains pending
  owner confirmation and has not been implemented.
- The filename evaluator still needs precise behavior for `MoveX:Y`, zero-length
  `RepM`, literal dot/extension interaction, Unicode normalization, and Windows
  device-name variants, as identified in the canonical filename specification.
- DD-068 legacy-profile import remains deferred and unapproved.
- The project license has not been selected. Packaging and publication are not
  authorized by the implementation roadmap.
