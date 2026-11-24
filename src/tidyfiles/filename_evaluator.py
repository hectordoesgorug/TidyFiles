"""Pure evaluation of supported filename-format tokens."""

from __future__ import annotations

import ntpath
from dataclasses import dataclass, replace
from typing import Mapping, NoReturn, Sequence

from tidyfiles.filename_format import (
    LiteralSpan,
    ParsedFormat,
    TokenKind,
    TokenOccurrence,
)


@dataclass(frozen=True, slots=True)
class FileEvaluationContext:
    """Immutable per-file inputs; indexes are zero-based and counts are sizes."""

    original_stem: str
    file_identifier: str | None = None
    import_root_name: str | None = None
    parent_name: str | None = None
    parent_identity: str | None = None
    global_index: int | None = None
    parent_index: int | None = None
    collection_size: int | None = None
    parent_collection_size: int | None = None
    physical_file_count: int | None = None
    physical_directory_count: int | None = None


class FilenameEvaluationError(ValueError):
    """A token evaluation failure with source and per-file context."""

    def __init__(
        self,
        reason: str,
        *,
        context: FileEvaluationContext,
        position: int,
        token: str,
    ) -> None:
        self.reason = reason
        self.file_identifier = context.file_identifier
        self.original_stem = context.original_stem
        self.position = position
        self.token = token
        file_label = context.file_identifier or context.original_stem
        super().__init__(f"{file_label}: {reason} at offset {position} in {token!r}")


@dataclass(frozen=True, slots=True)
class FileEvaluationResult:
    """One batch item's diagnostic preview result."""

    context: FileEvaluationContext
    value: str | None = None
    error: FilenameEvaluationError | None = None


@dataclass(frozen=True, slots=True)
class BatchEvaluationResult:
    """Ordered per-file results; an empty or failed batch is never valid."""

    files: tuple[FileEvaluationResult, ...]

    @property
    def errors(self) -> tuple[FilenameEvaluationError, ...]:
        return tuple(file.error for file in self.files if file.error is not None)

    @property
    def is_valid(self) -> bool:
        return bool(self.files) and not self.errors


def _raise_error(
    reason: str, context: FileEvaluationContext, token: TokenOccurrence
) -> NoReturn:
    raise FilenameEvaluationError(
        reason,
        context=context,
        position=token.start,
        token=token.raw,
    )


def _number(
    value: str, label: str, context: FileEvaluationContext, token: TokenOccurrence
) -> int:
    try:
        return int(value)
    except ValueError:
        _raise_error(f"{label} is too large to evaluate", context, token)


def _arguments(
    token: TokenOccurrence,
    count: int,
    context: FileEvaluationContext,
) -> tuple[str, ...]:
    if len(token.arguments) != count:
        _raise_error("Malformed token arguments", context, token)
    return token.arguments


def _context_number(
    value: int | None,
    label: str,
    context: FileEvaluationContext,
    token: TokenOccurrence,
) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _raise_error(f"{label} is unavailable or invalid", context, token)
    return value


def _token_start(
    token: TokenOccurrence,
    default: int,
    label: str,
    context: FileEvaluationContext,
) -> int:
    if len(token.arguments) > 1:
        _raise_error("Malformed token arguments", context, token)
    return default if not token.arguments else _number(token.arguments[0], label, context, token)


def _alpha(value: int) -> str:
    letters: list[str] = []
    while value >= 0:
        value, remainder = divmod(value, 26)
        letters.append(chr(ord("a") + remainder))
        value -= 1
    return "".join(reversed(letters))


def _padded_number(value: int, width: int) -> str:
    digits = f"{abs(value):0{width}d}"
    return f"-{digits}" if value < 0 else digits


def _directory_key(identity: str) -> str:
    """Normalize Windows separators and case without consulting the filesystem."""
    return ntpath.normcase(ntpath.normpath(identity))


def _numeric_widths_for_file(
    parsed: ParsedFormat, context: FileEvaluationContext
) -> dict[tuple[TokenKind, str | None], int]:
    widths: dict[tuple[TokenKind, str | None], int] = {}
    for part in parsed.parts:
        if not isinstance(part, TokenOccurrence) or part.kind not in (
            TokenKind.INC_NR,
            TokenKind.INC_NR_DIR,
            TokenKind.DECR_NR,
        ):
            continue
        if part.kind is TokenKind.INC_NR_DIR:
            if context.parent_index is None or not context.parent_identity:
                continue
            index = context.parent_index
            group = _directory_key(context.parent_identity)
            sequence_size = context.parent_collection_size
        else:
            if context.global_index is None:
                continue
            index = context.global_index
            group = None
            sequence_size = context.collection_size
        default = (
            context.collection_size
            if part.kind is TokenKind.DECR_NR and not part.arguments
            else 1
        )
        if default is None:
            continue
        try:
            start = _token_start(part, default, "Sequence start", context)
        except FilenameEvaluationError:
            continue
        if part.kind is TokenKind.DECR_NR:
            values = (start, start - (sequence_size - 1)) if sequence_size else (start - index,)
        else:
            values = (start, start + sequence_size - 1) if sequence_size else (start + index,)
        key = (part.kind, group)
        widths[key] = max(widths.get(key, 2), *(len(str(abs(value))) for value in values))
    return widths


def _sequence_index(
    context: FileEvaluationContext,
    token: TokenOccurrence,
    *,
    per_directory: bool,
) -> tuple[int, str | None]:
    if per_directory:
        if not context.parent_identity:
            _raise_error("Directory identity is unavailable", context, token)
        index = _context_number(context.parent_index, "Parent sequence index", context, token)
        size_value = context.parent_collection_size
        size_label = "Parent collection size"
        group = _directory_key(context.parent_identity)
    else:
        index = _context_number(context.global_index, "Global sequence index", context, token)
        size_value = context.collection_size
        size_label = "Collection size"
        group = None
    if size_value is not None:
        size = _context_number(size_value, size_label, context, token)
        if index >= size:
            _raise_error(f"{size_label} does not include this sequence index", context, token)
    return index, group


def _batch_numeric_widths(
    parsed: ParsedFormat,
    contexts: tuple[FileEvaluationContext, ...],
) -> dict[tuple[TokenKind, str | None], int]:
    widths: dict[tuple[TokenKind, str | None], int] = {}
    groups: dict[str, int] = {}
    for context in contexts:
        if context.parent_identity:
            key = _directory_key(context.parent_identity)
            groups[key] = groups.get(key, 0) + 1

    for part in parsed.parts:
        if not isinstance(part, TokenOccurrence) or part.kind not in (
            TokenKind.INC_NR,
            TokenKind.INC_NR_DIR,
            TokenKind.DECR_NR,
        ):
            continue
        if not contexts:
            continue
        default = len(contexts) if part.kind is TokenKind.DECR_NR else 1
        try:
            start = _token_start(part, default, "Sequence start", contexts[0])
        except FilenameEvaluationError:
            continue

        if part.kind is TokenKind.INC_NR_DIR:
            for group, size in groups.items():
                largest = start + size - 1
                key = (part.kind, group)
                widths[key] = max(widths.get(key, 2), len(str(abs(start))), len(str(abs(largest))))
        else:
            last = (
                start - len(contexts) + 1
                if part.kind is TokenKind.DECR_NR
                else start + len(contexts) - 1
            )
            key = (part.kind, None)
            widths[key] = max(widths.get(key, 2), len(str(abs(start))), len(str(abs(last))))
    return widths


def evaluate_format(
    parsed: ParsedFormat,
    context: FileEvaluationContext,
    *,
    numeric_widths: Mapping[tuple[TokenKind, str | None], int] | None = None,
) -> str:
    """Evaluate supported tokens and return raw text without filename validation."""
    if not isinstance(context.original_stem, str):
        raise TypeError("original_stem must be a string")

    output: list[str] = []
    working_stem = context.original_stem
    widths = numeric_widths or _numeric_widths_for_file(parsed, context)

    for part in parsed.parts:
        if isinstance(part, LiteralSpan):
            output.append(part.text)
            continue

        if not isinstance(part, TokenOccurrence):
            raise TypeError("parsed format contains an unsupported part")

        kind = part.kind
        if kind is TokenKind.NAME:
            _arguments(part, 0, context)
            output.append(context.original_stem)
        elif kind is TokenKind.WORD:
            if len(part.arguments) > 1:
                _raise_error("Malformed token arguments", context, part)
            arguments = part.arguments
            words = working_stem.split()
            index = 1 if not arguments else _number(arguments[0], "Word index", context, part)
            if index < 1 or index > len(words):
                _raise_error("Word index is outside the current stem", context, part)
            output.append(words[index - 1])
        elif kind is TokenKind.SUBSTR:
            _arguments(part, 0, context)
            output.append(working_stem[:5])
        elif kind is TokenKind.RSUBSTR:
            _arguments(part, 0, context)
            output.append(working_stem[-5:])
        elif kind in (TokenKind.REMOVE_L, TokenKind.REMOVE_R):
            (value,) = _arguments(part, 1, context)
            count = _number(value, "Removal count", context, part)
            if count > len(working_stem):
                _raise_error("Removal count exceeds the current stem", context, part)
            working_stem = (
                working_stem[count:]
                if kind is TokenKind.REMOVE_L
                else working_stem[: len(working_stem) - count]
            )
            output.append(working_stem)
        elif kind is TokenKind.MOVE:
            x_value, y_value = _arguments(part, 2, context)
            count = _number(x_value, "Move count", context, part)
            if count > len(working_stem):
                _raise_error("Move count exceeds the current stem", context, part)
            remaining = working_stem[count:]
            position = _number(y_value, "Move position", context, part)
            if position > len(remaining):
                _raise_error("Move position exceeds the remaining stem", context, part)
            working_stem = remaining[:position] + working_stem[:count] + remaining[position:]
            output.append(working_stem)
        elif kind is TokenKind.REP_S:
            position_value, replacement = _arguments(part, 2, context)
            position = _number(position_value, "RepS position", context, part)
            if position < 1 or position > len(working_stem):
                _raise_error("RepS position is outside the current stem", context, part)
            index = position - 1
            working_stem = working_stem[:index] + replacement + working_stem[index + 1 :]
            output.append(working_stem)
        elif kind is TokenKind.REP_M:
            count_value, position_value, replacement = _arguments(part, 3, context)
            count = _number(count_value, "RepM count", context, part)
            position = _number(position_value, "RepM position", context, part)
            if position < 1 or position > len(working_stem):
                _raise_error("RepM position is outside the current stem", context, part)
            index = position - 1
            if count > len(working_stem) - index:
                _raise_error("RepM range exceeds the current stem", context, part)
            working_stem = working_stem[:index] + replacement + working_stem[index + count :]
            output.append(working_stem)
        elif kind is TokenKind.INS:
            position_value, insertion = _arguments(part, 2, context)
            position = _number(position_value, "Ins position", context, part)
            if position > len(working_stem):
                _raise_error("Ins position exceeds the current stem", context, part)
            working_stem = working_stem[:position] + insertion + working_stem[position:]
            output.append(working_stem)
        elif kind is TokenKind.DIR_NAME:
            _arguments(part, 0, context)
            if context.import_root_name is None:
                _raise_error("Import root name is unavailable", context, part)
            output.append(context.import_root_name)
        elif kind is TokenKind.SUBDIR_NAME:
            _arguments(part, 0, context)
            if context.parent_name is None:
                _raise_error("Immediate parent name is unavailable", context, part)
            output.append(context.parent_name)
        elif kind is TokenKind.NUM_FILES:
            _arguments(part, 0, context)
            count = _context_number(
                context.physical_file_count, "Physical file count", context, part
            )
            output.append(str(count))
        elif kind is TokenKind.NUM_DIRS:
            _arguments(part, 0, context)
            count = _context_number(
                context.physical_directory_count,
                "Physical directory count",
                context,
                part,
            )
            output.append(str(count))
        elif kind is TokenKind.NUM_ITEMS:
            _arguments(part, 0, context)
            output.append(
                str(_context_number(context.collection_size, "Collection size", context, part))
            )
        elif kind in (TokenKind.INC_NR, TokenKind.INC_NR_DIR, TokenKind.DECR_NR):
            index, group = _sequence_index(
                context, part, per_directory=kind is TokenKind.INC_NR_DIR
            )
            default = (
                _context_number(context.collection_size, "Collection size", context, part)
                if kind is TokenKind.DECR_NR and not part.arguments
                else 1
            )
            start = _token_start(part, default, "Sequence start", context)
            value = start - index if kind is TokenKind.DECR_NR else start + index
            width = widths.get((kind, group), max(2, len(str(abs(value)))))
            if not isinstance(width, int) or isinstance(width, bool) or width < 2:
                _raise_error("Numeric sequence width is invalid", context, part)
            output.append(_padded_number(value, width))
        elif kind is TokenKind.INC_ALPHA:
            index, _ = _sequence_index(context, part, per_directory=False)
            start = _token_start(part, 0, "Alphabetic start", context)
            output.append(_alpha(start + index))
        else:
            _raise_error("Token is not supported by this evaluator", context, part)

    return "".join(output)


def evaluate_batch(
    parsed: ParsedFormat,
    contexts: Sequence[FileEvaluationContext],
) -> BatchEvaluationResult:
    """Evaluate an ordered collection snapshot without authorizing partial work."""
    snapshot = tuple(contexts)
    group_sizes: dict[str, int] = {}
    for context in snapshot:
        if context.parent_identity:
            key = _directory_key(context.parent_identity)
            group_sizes[key] = group_sizes.get(key, 0) + 1

    group_positions: dict[str, int] = {}
    indexed_contexts: list[FileEvaluationContext] = []
    for index, context in enumerate(snapshot):
        group = _directory_key(context.parent_identity) if context.parent_identity else None
        parent_index = None
        parent_size = None
        if group is not None:
            parent_index = group_positions.get(group, 0)
            group_positions[group] = parent_index + 1
            parent_size = group_sizes[group]
        indexed_contexts.append(
            replace(
                context,
                global_index=index,
                parent_index=parent_index,
                collection_size=len(snapshot),
                parent_collection_size=parent_size,
            )
        )

    widths = _batch_numeric_widths(parsed, tuple(indexed_contexts))
    results: list[FileEvaluationResult] = []
    for context in indexed_contexts:
        try:
            value = evaluate_format(parsed, context, numeric_widths=widths)
        except FilenameEvaluationError as error:
            results.append(FileEvaluationResult(context, error=error))
        else:
            results.append(FileEvaluationResult(context, value=value))
    return BatchEvaluationResult(tuple(results))
