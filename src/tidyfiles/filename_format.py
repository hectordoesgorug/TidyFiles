"""Pure parser for TidyFiles filename-format expressions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn, TypeAlias


class TokenKind(StrEnum):
    NAME = "Name"
    EXT = "Ext"
    DIR_NAME = "DirName"
    SUBDIR_NAME = "SubDirName"
    NUM_FILES = "NumFiles"
    NUM_DIRS = "NumDirs"
    NUM_ITEMS = "NumItems"
    INC_NR = "IncNr"
    INC_NR_DIR = "IncNrDir"
    DECR_NR = "DecrNr"
    INC_ALPHA = "IncAlpha"
    WORD = "Word"
    SUBSTR = "Substr"
    RSUBSTR = "RSubstr"
    REMOVE_L = "RemoveL"
    REMOVE_R = "RemoveR"
    MOVE = "Move"
    REP_S = "RepS"
    REP_M = "RepM"
    INS = "Ins"


@dataclass(frozen=True, slots=True)
class LiteralSpan:
    """Literal expression text with zero-based, end-exclusive source offsets."""

    text: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class TokenOccurrence:
    """One token occurrence; numeric lexemes and string arguments stay verbatim."""

    kind: TokenKind
    arguments: tuple[str, ...]
    raw: str
    start: int
    end: int


FormatPart: TypeAlias = LiteralSpan | TokenOccurrence


@dataclass(frozen=True, slots=True)
class ParsedFormat:
    """A complete expression in source order, without evaluating any token."""

    source: str
    parts: tuple[FormatPart, ...]


class FilenameFormatParseError(ValueError):
    """A syntax error with its original string expression, offset, and token.

    ``expression`` is ``None`` only when parsing a non-string input.
    """

    def __init__(
        self,
        reason: str,
        *,
        position: int,
        token: str | None = None,
        expression: str | None = None,
    ) -> None:
        self.reason = reason
        self.position = position
        self.token = token
        self.expression = expression
        detail = f"{reason} at offset {position}"
        if token is not None:
            detail += f" in {token!r}"
        super().__init__(detail)


_PARAMETERLESS_TOKENS = {
    TokenKind.NAME.value: TokenKind.NAME,
    TokenKind.EXT.value: TokenKind.EXT,
    TokenKind.DIR_NAME.value: TokenKind.DIR_NAME,
    TokenKind.SUBDIR_NAME.value: TokenKind.SUBDIR_NAME,
    TokenKind.NUM_FILES.value: TokenKind.NUM_FILES,
    TokenKind.NUM_DIRS.value: TokenKind.NUM_DIRS,
    TokenKind.NUM_ITEMS.value: TokenKind.NUM_ITEMS,
    TokenKind.SUBSTR.value: TokenKind.SUBSTR,
    TokenKind.RSUBSTR.value: TokenKind.RSUBSTR,
}
_OPTIONAL_NUMBER_TOKENS = {
    TokenKind.INC_NR.value: TokenKind.INC_NR,
    TokenKind.INC_NR_DIR.value: TokenKind.INC_NR_DIR,
    TokenKind.DECR_NR.value: TokenKind.DECR_NR,
    TokenKind.INC_ALPHA.value: TokenKind.INC_ALPHA,
    TokenKind.WORD.value: TokenKind.WORD,
}
_REQUIRED_NUMBER_TOKENS = {
    TokenKind.REMOVE_L.value: TokenKind.REMOVE_L,
    TokenKind.REMOVE_R.value: TokenKind.REMOVE_R,
}
_MOVE_PATTERN = re.compile(r"Move([0-9]+):([0-9]+)", flags=re.ASCII)
_INS_PATTERN = re.compile(r"Ins([0-9]+):(.*)", flags=re.DOTALL | re.ASCII)


def _is_unsigned_decimal(value: str) -> bool:
    return bool(value) and all("0" <= character <= "9" for character in value)


def _raise_token_error(raw: str, start: int, reason: str, expression: str) -> NoReturn:
    raise FilenameFormatParseError(
        reason, position=start, token=raw, expression=expression
    )


def _parse_token(raw: str, start: int, end: int, expression: str) -> TokenOccurrence:
    body = raw[1:-1]

    kind = _PARAMETERLESS_TOKENS.get(body)
    if kind is not None:
        return TokenOccurrence(kind, (), raw, start, end)

    kind = _OPTIONAL_NUMBER_TOKENS.get(body)
    if kind is not None:
        return TokenOccurrence(kind, (), raw, start, end)

    for name, kind in _OPTIONAL_NUMBER_TOKENS.items():
        prefix = f"{name}:"
        if body.startswith(prefix):
            value = body[len(prefix) :]
            if not _is_unsigned_decimal(value):
                _raise_token_error(
                    raw, start, f"{name} requires an unsigned decimal argument", expression
                )
            return TokenOccurrence(kind, (value,), raw, start, end)

    for name, kind in _REQUIRED_NUMBER_TOKENS.items():
        prefix = f"{name}:"
        if body == name:
            _raise_token_error(
                raw, start, f"{name} requires one unsigned decimal argument", expression
            )
        if body.startswith(prefix):
            value = body[len(prefix) :]
            if not _is_unsigned_decimal(value):
                _raise_token_error(
                    raw, start, f"{name} requires one unsigned decimal argument", expression
                )
            return TokenOccurrence(kind, (value,), raw, start, end)

    if body.startswith("Move"):
        match = _MOVE_PATTERN.fullmatch(body)
        if match is None:
            _raise_token_error(
                raw,
                start,
                "Move requires two unsigned decimal arguments in X:Y form",
                expression,
            )
        return TokenOccurrence(TokenKind.MOVE, match.groups(), raw, start, end)

    if body == "RepS" or body.startswith("RepS:"):
        payload = body[len("RepS:") :] if body.startswith("RepS:") else ""
        number, separator, replacement = payload.partition(",")
        if not separator:
            _raise_token_error(
                raw,
                start,
                "RepS requires an index and a comma before its string",
                expression,
            )
        if not _is_unsigned_decimal(number):
            _raise_token_error(
                raw, start, "RepS index must be an unsigned decimal argument", expression
            )
        return TokenOccurrence(TokenKind.REP_S, (number, replacement), raw, start, end)

    if body == "RepM" or body.startswith("RepM:"):
        payload = body[len("RepM:") :] if body.startswith("RepM:") else ""
        count, first_separator, remainder = payload.partition(",")
        position, second_separator, replacement = remainder.partition(",")
        if not first_separator or not second_separator:
            _raise_token_error(
                raw,
                start,
                "RepM requires two unsigned decimal arguments and a comma before its string",
                expression,
            )
        if not _is_unsigned_decimal(count) or not _is_unsigned_decimal(position):
            _raise_token_error(
                raw, start, "RepM arguments must be unsigned decimal values", expression
            )
        return TokenOccurrence(TokenKind.REP_M, (count, position, replacement), raw, start, end)

    if body == "Ins" or re.match(r"Ins[^:]*:", body):
        match = _INS_PATTERN.fullmatch(body)
        if match is None:
            _raise_token_error(
                raw,
                start,
                "Ins requires an unsigned decimal position followed by ':'",
                expression,
            )
        position, text = match.groups()
        return TokenOccurrence(TokenKind.INS, (position, text), raw, start, end)

    for name in _PARAMETERLESS_TOKENS:
        if body.startswith(f"{name}:"):
            _raise_token_error(raw, start, f"{name} does not accept arguments", expression)

    for name in _OPTIONAL_NUMBER_TOKENS:
        if body.startswith(f"{name}:"):
            _raise_token_error(
                raw, start, f"{name} argument must be an unsigned decimal value", expression
            )

    if body.startswith("RemoveL:") or body.startswith("RemoveR:"):
        name = body.split(":", 1)[0]
        _raise_token_error(
            raw, start, f"{name} requires one unsigned decimal argument", expression
        )

    if not body:
        _raise_token_error(raw, start, "Token name is missing", expression)
    _raise_token_error(raw, start, "Unknown token name", expression)


def parse_format(expression: str) -> ParsedFormat:
    """Parse a complete filename-format expression without evaluating tokens.

    Numeric arguments retain their original digit strings. In ``RepS``, ``RepM``,
    and ``Ins``, the final string argument preserves all remaining content,
    including commas and whitespace. Angle brackets delimit tokens and have no
    escape syntax. An empty expression is represented by an empty ``parts`` tuple.
    """
    if not isinstance(expression, str):
        raise FilenameFormatParseError("Expression must be a string", position=0, expression=None)

    parts: list[FormatPart] = []
    cursor = 0
    length = len(expression)

    while cursor < length:
        opening = expression.find("<", cursor)
        closing = expression.find(">", cursor)

        if closing != -1 and (opening == -1 or closing < opening):
            raise FilenameFormatParseError(
                "Unmatched closing delimiter '>'", position=closing, expression=expression
            )

        if opening == -1:
            parts.append(LiteralSpan(expression[cursor:], cursor, length))
            break

        if opening > cursor:
            parts.append(LiteralSpan(expression[cursor:opening], cursor, opening))

        closing = expression.find(">", opening + 1)
        nested_opening = expression.find("<", opening + 1)
        if nested_opening != -1 and (closing == -1 or nested_opening < closing):
            partial = expression[opening:nested_opening]
            raise FilenameFormatParseError(
                "Nested opening delimiter before token closes",
                position=nested_opening,
                token=partial,
                expression=expression,
            )
        if closing == -1:
            raise FilenameFormatParseError(
                "Missing closing delimiter '>'",
                position=opening,
                token=expression[opening:],
                expression=expression,
            )

        raw = expression[opening : closing + 1]
        parts.append(_parse_token(raw, opening, closing + 1, expression))
        cursor = closing + 1

    return ParsedFormat(expression, tuple(parts))
