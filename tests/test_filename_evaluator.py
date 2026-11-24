from dataclasses import FrozenInstanceError

import pytest

from tidyfiles.filename_evaluator import (
    FileEvaluationContext,
    FilenameEvaluationError,
    evaluate_format,
)
from tidyfiles.filename_format import parse_format


@pytest.mark.parametrize(
    ("stem", "expression", "expected"),
    [
        ("VacationPhoto", "<Name>", "VacationPhoto"),
        ("VacationPhoto", "<RemoveL:8>", "Photo"),
        ("VacationPhoto", "<Name>_<RemoveL:8>", "VacationPhoto_Photo"),
        ("VacationPhoto", "<RemoveL:8>_<Name>", "Photo_VacationPhoto"),
        ("ABCDE", "<RemoveL:1>_<RemoveR:1>", "BCDE_BCD"),
        ("ABCDE", "<Move2:0>", "ABCDE"),
        ("ABCDE", "<Move2:1>", "CABDE"),
        ("ABCDE", "<Move2:3>", "CDEAB"),
        ("ABCDE", "<RepS:2,>", "ACDE"),
        ("ABCDE", "<RepM:2,2,>", "ADE"),
        ("ABCDE", "<RepM:0,3,XY>", "ABXYCDE"),
        ("ABCDE", "<RepS:2,x,y>", "Ax,yCDE"),
        ("ABCDE", "<Ins0:XY>", "XYABCDE"),
        ("ABCDE", "<Ins5:XY>", "ABCDEXY"),
        ("ABCDE", "<Ins2:>", "ABCDE"),
        ("Photo", "<RemoveL:0>", "Photo"),
        ("Photo", "<RemoveR:0>", "Photo"),
        ("Red Blue Green", "<Word>", "Red"),
        ("Red Blue Green", "<Word:2>", "Blue"),
        ("Red Blue Green", "<RemoveL:4>_<Word>", "Blue Green_Blue"),
        ("ABCDEFG", "<Substr>", "ABCDE"),
        ("ABCDEFG", "<RSubstr>", "CDEFG"),
        ("ABC", "<Substr>_<RSubstr>", "ABC_ABC"),
        ("ABCDE", "literal .,_ ", "literal .,_ "),
        ("A", "<RemoveL:1><Ins0:X><Name>", "XA"),
        ("A😀é", "<Name>_<RemoveL:1>", "A😀é_😀é"),
        ("ABCDE", "<RepM:0,5,X>", "ABCDXE"),
    ],
)
def test_evaluates_supported_tokens(
    stem: str, expression: str, expected: str
) -> None:
    assert evaluate_format(parse_format(expression), FileEvaluationContext(stem)) == expected


def test_chained_manipulations_and_extraction_use_evolving_stem() -> None:
    parsed = parse_format("<Substr>|<RemoveL:2>|<Word:2>|<RSubstr>|<Name>")

    assert evaluate_format(parsed, FileEvaluationContext("Red Blue Green")) == (
        "Red B|d Blue Green|Blue|Green|Red Blue Green"
    )


def test_empty_intermediate_stem_is_allowed_and_repeated_calls_do_not_leak() -> None:
    parsed = parse_format("<RemoveL:3><Ins0:X><Name>")
    context = FileEvaluationContext("abc", "file-1")

    assert evaluate_format(parsed, context) == "Xabc"
    assert evaluate_format(parse_format("<Name>"), context) == "abc"


def test_literal_only_empty_expression_and_context_are_immutable() -> None:
    context = FileEvaluationContext("name", "file-1")
    assert evaluate_format(parse_format(""), context) == ""
    assert evaluate_format(parse_format("literal"), context) == "literal"
    with pytest.raises(FrozenInstanceError):
        context.original_stem = "changed"  # type: ignore[misc]


def test_emitted_text_is_not_reinterpreted_as_tokens() -> None:
    assert evaluate_format(parse_format("<Name>"), FileEvaluationContext("<Ext>")) == "<Ext>"


@pytest.mark.parametrize(
    ("stem", "expression", "reason"),
    [
        ("ABCDE", "<Move2:4>", "Move position"),
        ("ABCDE", "<Move6:0>", "Move count"),
        ("ABCDE", "<RemoveL:6>", "Removal count"),
        ("ABCDE", "<RemoveR:6>", "Removal count"),
        ("ABCDE", "<RepS:0,X>", "RepS position"),
        ("ABCDE", "<RepS:6,X>", "RepS position"),
        ("ABCDE", "<RepM:0,0,X>", "RepM position"),
        ("ABCDE", "<RepM:0,6,X>", "RepM position"),
        ("ABCDE", "<RepM:3,4,X>", "RepM range"),
        ("ABCDE", "<Ins6:X>", "Ins position"),
        ("one two", "<Word:0>", "Word index"),
        ("one two", "<Word:3>", "Word index"),
        ("   ", "<Word>", "Word index"),
    ],
)
def test_evaluation_errors_include_file_token_and_offset(
    stem: str, expression: str, reason: str
) -> None:
    context = FileEvaluationContext(stem, "photo-1")
    with pytest.raises(FilenameEvaluationError, match=reason) as error:
        evaluate_format(parse_format(expression), context)

    assert error.value.file_identifier == "photo-1"
    assert error.value.original_stem == stem
    assert error.value.position == 0
    assert error.value.token == expression
    assert "photo-1" in str(error.value)


def test_later_error_does_not_return_partial_evaluation() -> None:
    parsed = parse_format("prefix<RemoveL:2><Word:9>")

    with pytest.raises(FilenameEvaluationError) as error:
        evaluate_format(parsed, FileEvaluationContext("abcdef"))

    assert error.value.position == len("prefix<RemoveL:2>")
    assert error.value.token == "<Word:9>"


def test_extremely_large_argument_is_a_structured_evaluation_error() -> None:
    expression = f"<Ins{'9' * 5000}:x>"
    parsed = parse_format(expression)

    with pytest.raises(FilenameEvaluationError, match="too large"):
        evaluate_format(parsed, FileEvaluationContext("name", "large-number"))
