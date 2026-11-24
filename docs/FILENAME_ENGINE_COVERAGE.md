# Filename Engine Coverage Audit

## Scope and evidence

This audit covers the pure parser, evaluator, extension resolver, and filename
component validator at commit `50a97a929130c5a6502ac9fc751d9f15700b79bb`.
The checkout was clean on `main` and matched `origin/main`. Windows CI run
[#14](https://github.com/hectordoesgorug/TidyFiles/actions/runs/45350946433)
passed its Windows-latest setup, install, lint, and test steps for the same
commit. Local M1-D checks and added regression tests are recorded in
`PROGRESS.md`.

The parser/evaluator is not called by `app.py`; the GUI still has no file
collection, preview, or processing controls. The engine consumes immutable
per-file metadata supplied by a future collection/scanning service and returns
per-file results. It does not construct a destination plan or touch the
filesystem.

## Token coverage

All 20 approved token kinds are present in `filename_format.TokenKind`, parsed
by `parse_format`, and handled in `evaluate_format`.

| Token(s) | Evaluation behavior | Supporting tests |
| --- | --- | --- |
| `<Name>`, `<Ext>` | Original stem; source or conversion-output extension by terminal context | `test_evaluates_supported_tokens`; `test_ext_token_uses_source_when_embedded_and_output_when_terminal` |
| `<DirName>`, `<SubDirName>` | Supplied per-file import-root and immediate-parent names | `test_directory_names_and_physical_counts_use_each_files_context`; `test_directory_tokens_cover_files_in_import_root_and_nested_parent` |
| `<NumFiles>`, `<NumDirs>`, `<NumItems>` | Supplied physical counts and active batch size | `test_directory_names_and_physical_counts_use_each_files_context`; `test_unavailable_metadata_only_fails_when_a_token_needs_it` |
| `<IncNr>`, `<IncNrDir>`, `<DecrNr>` | Global/per-parent positions, independent starts, range-aware padding | `test_numeric_sequences_use_two_digit_minimum_and_shared_occurrence_widths`; `test_directory_sequence_width_is_shared_within_but_not_between_parents`; `test_default_and_custom_decrementing_sequences_cross_zero` |
| `<IncAlpha>` | Zero-based spreadsheet-style lowercase sequence | `test_alphabetic_sequence_rollover`; `test_repeated_alpha_starts_and_mixed_numeric_families_are_independent` |
| `<Word>`, `<Word:n>` | One-based whitespace word from current working stem | `test_evaluates_supported_tokens`; `test_chained_manipulations_and_extraction_use_evolving_stem` |
| `<Substr>`, `<RSubstr>` | First/last five characters of current working stem | `test_evaluates_supported_tokens`; `test_chained_manipulations_and_extraction_use_evolving_stem` |
| `<RemoveL:n>`, `<RemoveR:n>` | Remove from evolving stem; zero is a no-op | `test_evaluates_supported_tokens`; evaluator position-error cases |
| `<MoveX:Y>` | Insert removed prefix after Y characters of remainder | `test_evaluates_supported_tokens`; evaluator position-error cases |
| `<RepS:n,string>`, `<RepM:x,n,string>` | One-based replacement; zero-length `RepM` inserts; empty replacement removes | `test_evaluates_supported_tokens`; parser comma/empty-argument cases |
| `<InsX:string>` | Insert after X characters; empty text is a no-op | `test_evaluates_supported_tokens`; parser comma/empty-argument cases |

`<Word>` and extraction behavior is based on the current working stem where the
approved semantics require it; `<Name>` always uses the original stem. Parser
tests cover all token forms, strict malformed and unknown tokens, offsets,
literal spans, immutability, and comma-preserving final string arguments.

## Decision coverage

“Verified within M1” means the pure engine clause is covered by code and tests.
“Deferred integration by design” means the pure boundary exists, while the
decision also depends on a later collection, GUI, repacker, or filesystem
component. It does not mean the entire decision has been verified.

| Decision | Status | M1 implementation and evidence | Outstanding clause / dependency |
| --- | --- | --- | --- |
| DD-010 | Deferred integration by design | `evaluate_batch` uses supplied order for global indexes; batch reorder/removal test covers a reduced snapshot. | Default/selected sorting and the displayed collection order are M2. |
| DD-026 | Deferred integration by design | Batch indexes are derived from the sequence passed to the evaluator. | Manual reorder controls, stable multi-selection, and live preview are M2/M6. |
| DD-048 | Deferred integration by design | Immutable context carries a per-file `import_root_name` and `parent_name`; directory-token tests use different roots and nested parents. | Import-root establishment/retention and Add Files fallback require M2 collection/import services. |
| DD-049 | Deferred integration by design | Count tokens consume supplied per-file counts; `<NumItems>` uses batch size; missing counts fail only when referenced. | Physical enumeration, cache invalidation, and pre-processing refresh require M2 and later preflight. |
| DD-050 | Verified within M1 | Manipulation and extraction use source-order evolving working stem; chained-token tests cover order. | None in the pure evaluator. |
| DD-051 | Verified within M1 | `<Name>` reads `original_stem` while mutations use a separate working value; tests cover original output after mutations. | None in the pure evaluator. |
| DD-052 | Verified within M1 | `_alpha` implements bijective lowercase sequence, including rollover and custom starts. | None in the pure evaluator. |
| DD-053 | Verified within M1 | Width covers the largest absolute generated value, separately for each parent group. | The final effective order is supplied by M2. |
| DD-054 | Verified within M1 | `<Word>` uses one-based whitespace indexes on the working stem and reports out-of-range errors. | None in the pure evaluator. |
| DD-055 | Verified within M1 | Left/right zero removals preserve the current stem. | None in the pure evaluator. |
| DD-056 | Deferred integration by design | `filename_validation.py` applies the approved substitutions and rejects invalid components, reserved names, controls, trailing ASCII spaces/periods, and over-limit UTF-16 components. | Target-volume limits, complete destination paths, collision checks, and pre-mutation revalidation remain M3. No filesystem behavior was tested. |
| DD-057 | Deferred integration by design | Resolver preserves a source suffix or uses a supplied conversion target; literal suffix matching is case-insensitive. | Repacker-selected extensions, preview/execution agreement, and conversion transaction are M5/M3. |
| DD-058 | Verified within M1 | Decrementing values cross zero; sign is outside zero padding. | None in the pure evaluator. |
| DD-059 | Verified within M1 | Manipulation positions/counts are checked at application time; failures retain token offsets. | UI presentation of diagnostics is M6. |
| DD-060 | Verified within M1 | Repeated numeric occurrences keep their own starts and share family width; repeated alpha starts are independent. | The finalized collection order is supplied by M2. |
| DD-061 | Verified within M1 | Complete expressions are parsed strictly; every parse error retains the exact original string expression, reason, offset, and optional token. Parser tests cover malformed delimiters, arguments, unknown tokens, whitespace, and Unicode. | GUI presentation of the structured diagnostic remains M6. |
| DD-062 | Deferred integration by design | Parser permits empty input; rename batch evaluation rejects an empty format. | The UI must apply this gate only to Bulk Rename and combined Rename + Repack; that workflow is M6. |
| DD-065 | Deferred integration by design | Any per-file evaluation, extension, or validation failure makes `BatchEvaluationResult.is_valid` false; diagnostics remain per file. | Global parse-error gating and Process-button behavior require the future rename workflow/UI. Collision-only partial processing is M3 and remains separate. |
| DD-067 | Verified within M1 | Embedded `<Ext>` uses source; terminal `.<Ext>` uses conversion output; mixed sources/targets are tested. | Actual conversion behavior is M5. |
| DD-069 | Verified within M1 | Move, zero-length `RepM`, empty replacements, and strict ranges are implemented and tested. | None in the pure evaluator. |
| DD-070 | Verified within M1 | Two-digit minimum, range expansion, signs, repeated starts, and per-parent widths are tested. | Effective order still comes from M2. |
| DD-071 | Verified within M1 | Matching terminal literal suffixes are recognized case-insensitively; unmatched and token-generated suffix text stays in the stem. | None in pure extension resolution. |
| DD-072 | Verified within M1 | Validation changes only the nine approved characters; composed/decomposed and supplementary Unicode tests preserve input sequences. | Actual target-volume compatibility remains M3. |
| DD-073 | Verified within M1 | Reserved basename checks include case/suffix variants and COM/LPT superscript digits. | Filesystem-level alias behavior was not physically tested; M3 preflight must revalidate. |

### DD-061 diagnostic finding — historical and resolved

At the M1-D audit on `50a97a929130c5a6502ac9fc751d9f15700b79bb`,
`parse_format("prefix>suffix")` raised
`FilenameFormatParseError("Unmatched closing delimiter '>' at offset 6")` with
`position == 6`, `token is None`, and no `expression` attribute. The parser
identified the offending character but did not retain the complete input.

M1-D.12 resolves the finding: `FilenameFormatParseError.expression` now stores
the exact supplied string on every string-input parse failure. For non-string
input it is `None`, while the existing input-type reason and offset remain.
Regression tests assert complete expressions through both direct parser errors
and errors raised by token-parsing helpers.

## Pipeline, legacy parity, and boundaries

The logical pipeline is represented across parser, immutable context, batch
index/width derivation, token evaluation, extension resolution, and component
validation. Raw expression value, extension-resolved candidate, and validated
name remain separate. Batch iteration preserves input order, derives indexes
without mutating caller contexts, and returns deterministic per-file errors.
No executable destination-plan type exists. No production source calls this
engine from the GUI.

Batch setup and evaluation are linear in file count for ordinary expressions;
per-directory width calculation is proportional to numeric-token occurrences
and distinct parent groups. No quadratic or speculative optimization issue was
found in this review.

Intentional differences from historical legacy behavior follow approved
decisions: complete strict parsing rather than replacement-order token
substitution; `<Name>` staying original while manipulations evolve; strict
position errors rather than clipping; zero-length right removal as a no-op;
bijective alphabetic rollover; output-context terminal `<Ext>`; and no automatic
collision suffixing. DD-047 remains withdrawn and DD-068 remains unapproved.

The validator checks one sanitized filename component. It does not validate a
complete path or discover target-volume limits; it does not compare destination
identity, case sensitivity, or collisions; and it does not execute or recheck
filesystem operations. Those remain M3 responsibilities. Collection ordering,
import context, and physical count sourcing remain M2 responsibilities; actual
repacking integration remains M5; GUI validation and presentation remain M6.

## Test coverage added for this audit

- Interleaved parent-group numeric sequences with different widths.
- Batch evaluation when unreferenced physical counts are unavailable.
- One batch containing an evaluation error, a filename-validation error, and a
  valid row, confirming the batch gate and per-stage diagnostics.

All test data is synthetic. Windows CI and local Windows tests do not prove
compatibility with every destination volume or filesystem configuration.

## Milestone disposition

**Ready for M1 closure.** The DD-061 diagnostic finding is resolved and its
regression cases pass. No other M1 pure-engine closure blocker was identified.
Downstream M2/M3/M5/M6 integration remains intentionally deferred and must be
tracked under those milestones; this audit does not mark M1 administratively
complete.
