"""Pure evaluation of supported filename-format tokens."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn

from tidyfiles.filename_format import (
    LiteralSpan,
    ParsedFormat,
    TokenKind,
    TokenOccurrence,
)


@dataclass(frozen=True, slots=True)
class FileEvaluationContext:
    """Per-file data needed by the current evaluator slice."""

    original_stem: str
    file_identifier: str | None = None


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


def evaluate_format(
    parsed: ParsedFormat, context: FileEvaluationContext
) -> str:
    """Evaluate supported tokens and return raw text without filename validation."""
    if not isinstance(context.original_stem, str):
        raise TypeError("original_stem must be a string")

    output: list[str] = []
    working_stem = context.original_stem

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
        else:
            _raise_error("Token is not supported by this evaluator", context, part)

    return "".join(output)
