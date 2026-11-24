from dataclasses import FrozenInstanceError

import pytest

from tidyfiles.filename_format import (
    FilenameFormatParseError,
    LiteralSpan,
    ParsedFormat,
    TokenKind,
    TokenOccurrence,
    parse_format,
)


@pytest.mark.parametrize(
    ("expression", "kind", "arguments"),
    [
        ("<Name>", TokenKind.NAME, ()),
        ("<Ext>", TokenKind.EXT, ()),
        ("<DirName>", TokenKind.DIR_NAME, ()),
        ("<SubDirName>", TokenKind.SUBDIR_NAME, ()),
        ("<NumFiles>", TokenKind.NUM_FILES, ()),
        ("<NumDirs>", TokenKind.NUM_DIRS, ()),
        ("<NumItems>", TokenKind.NUM_ITEMS, ()),
        ("<IncNr>", TokenKind.INC_NR, ()),
        ("<IncNr:01>", TokenKind.INC_NR, ("01",)),
        ("<IncNrDir>", TokenKind.INC_NR_DIR, ()),
        ("<IncNrDir:12>", TokenKind.INC_NR_DIR, ("12",)),
        ("<DecrNr>", TokenKind.DECR_NR, ()),
        ("<DecrNr:3>", TokenKind.DECR_NR, ("3",)),
        ("<IncAlpha>", TokenKind.INC_ALPHA, ()),
        ("<IncAlpha:0>", TokenKind.INC_ALPHA, ("0",)),
        ("<Word>", TokenKind.WORD, ()),
        ("<Word:2>", TokenKind.WORD, ("2",)),
        ("<Substr>", TokenKind.SUBSTR, ()),
        ("<RSubstr>", TokenKind.RSUBSTR, ()),
        ("<RemoveL:0>", TokenKind.REMOVE_L, ("0",)),
        ("<RemoveR:8>", TokenKind.REMOVE_R, ("8",)),
        ("<Move2:10>", TokenKind.MOVE, ("2", "10")),
        ("<RepS:1,x>", TokenKind.REP_S, ("1", "x")),
        ("<RepM:2,1,xy>", TokenKind.REP_M, ("2", "1", "xy")),
        ("<Ins0:text>", TokenKind.INS, ("0", "text")),
    ],
)
def test_parses_all_token_forms(
    expression: str, kind: TokenKind, arguments: tuple[str, ...]
) -> None:
    parsed = parse_format(expression)

    assert isinstance(parsed, ParsedFormat)
    assert parsed.source == expression
    assert parsed.parts == (TokenOccurrence(kind, arguments, expression, 0, len(expression)),)


@pytest.mark.parametrize("expression", ["", "plain filename", " . name "])
def test_parses_literal_only_and_empty_expressions(expression: str) -> None:
    parsed = parse_format(expression)

    if expression:
        assert parsed.parts == (LiteralSpan(expression, 0, len(expression)),)
    else:
        assert parsed.parts == ()


def test_preserves_literals_repeated_tokens_order_and_offsets() -> None:
    expression = "pre<Name>_<IncNr:02>+<Name> end"

    parsed = parse_format(expression)

    assert parsed.parts == (
        LiteralSpan("pre", 0, 3),
        TokenOccurrence(TokenKind.NAME, (), "<Name>", 3, 9),
        LiteralSpan("_", 9, 10),
        TokenOccurrence(TokenKind.INC_NR, ("02",), "<IncNr:02>", 10, 20),
        LiteralSpan("+", 20, 21),
        TokenOccurrence(TokenKind.NAME, (), "<Name>", 21, 27),
        LiteralSpan(" end", 27, 31),
    )


def test_parsed_representation_is_immutable() -> None:
    parsed = parse_format("<Name>")
    occurrence = parsed.parts[0]

    with pytest.raises(FrozenInstanceError):
        parsed.source = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        occurrence.raw = "<Ext>"  # type: ignore[attr-defined,misc]


@pytest.mark.parametrize(
    ("expression", "kind", "arguments"),
    [
        ("<RepS:1,a,b>", TokenKind.REP_S, ("1", "a,b")),
        ("<RepS:1,a,b,c>", TokenKind.REP_S, ("1", "a,b,c")),
        ("<RepS:1, a,b >", TokenKind.REP_S, ("1", " a,b ")),
        ("<RepS:1,>", TokenKind.REP_S, ("1", "")),
        ("<RepM:2,1,a,b>", TokenKind.REP_M, ("2", "1", "a,b")),
        ("<RepM:2,1, a,b >", TokenKind.REP_M, ("2", "1", " a,b ")),
        ("<RepM:2,1,>", TokenKind.REP_M, ("2", "1", "")),
        ("<Ins0:Hello,world>", TokenKind.INS, ("0", "Hello,world")),
        ("<Ins0: Hello,world >", TokenKind.INS, ("0", " Hello,world ")),
        ("<Ins0:>", TokenKind.INS, ("0", "")),
    ],
)
def test_preserves_commas_in_final_string_arguments(
    expression: str, kind: TokenKind, arguments: tuple[str, ...]
) -> None:
    occurrence = parse_format(expression).parts[0]

    assert occurrence == TokenOccurrence(kind, arguments, expression, 0, len(expression))


@pytest.mark.parametrize(
    "expression",
    [
        "<IncNr:>",
        "<IncNr:-1>",
        "<IncNr:1.5>",
        "<IncNr:١>",
        "<IncNr:1,2>",
        "<Word:abc>",
        "<Word:1:2>",
        "<RemoveL:x>",
        "<RemoveR:1,2>",
        "<Move1>",
        "<Move:2>",
        "<Move1:2:3>",
        "<RepS:1>",
        "<RepS:x,a>",
        "<RepM:2,1>",
        "<RepM:2,x,text>",
        "<Ins:missing-position>",
        "<Insx:text>",
        "<Name:argument>",
    ],
)
def test_rejects_malformed_arguments(expression: str) -> None:
    with pytest.raises(FilenameFormatParseError):
        parse_format(expression)


@pytest.mark.parametrize("expression", ["<Unknown>", "<IncNrr:2>", "<unknown:1>"])
def test_rejects_unknown_token_names(expression: str) -> None:
    with pytest.raises(FilenameFormatParseError, match="Unknown token name"):
        parse_format(expression)


@pytest.mark.parametrize(
    ("expression", "position", "reason"),
    [
        ("prefix Name>", 11, "Unmatched closing delimiter"),
        ("prefix <Name", 7, "Missing closing delimiter"),
        ("pre<<Name>>", 4, "Nested opening delimiter"),
        ("<RepS:1>", 0, "RepS requires an index"),
    ],
)
def test_parse_errors_include_token_or_position_and_reason(
    expression: str, position: int, reason: str
) -> None:
    with pytest.raises(FilenameFormatParseError) as error:
        parse_format(expression)

    assert error.value.position == position
    assert reason in error.value.reason
    assert str(error.value)


def test_parse_error_identifies_offending_token() -> None:
    with pytest.raises(FilenameFormatParseError) as error:
        parse_format("pre<Unknown>")

    assert error.value.position == 3
    assert error.value.token == "<Unknown>"


def test_rejects_non_string_expression_with_structured_error() -> None:
    with pytest.raises(FilenameFormatParseError) as error:
        parse_format(None)  # type: ignore[arg-type]

    assert error.value.position == 0
    assert error.value.token is None
    assert error.value.reason == "Expression must be a string"
