from __future__ import annotations

import pytest

from tidyfiles.filename_evaluator import FileEvaluationContext, evaluate_batch
from tidyfiles.filename_format import parse_format
from tidyfiles.filename_validation import (
    FilenameValidationError,
    sanitize_and_validate_filename,
)


def _validate(
    name: str,
    *,
    stem: str | None = None,
    limit: int = 255,
) -> str:
    return sanitize_and_validate_filename(
        name,
        generated_stem=name if stem is None else stem,
        context=FileEvaluationContext(name, file_identifier="fixture"),
        max_component_length=limit,
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("?", "？"),
        (":", "∶"),
        ("*", "✱"),
        ('"', "″"),
        ("<", "‹"),
        (">", "›"),
        ("|", "∣"),
        ("/", "⁄"),
        ("\\", "⧵"),
    ],
)
def test_approved_replacements_are_exact_and_idempotent(source: str, expected: str) -> None:
    assert _validate(f"a{source}b") == f"a{expected}b"
    assert _validate(f"a{expected}b") == f"a{expected}b"


def test_multiple_replacements_do_not_create_paths_or_reparse_text() -> None:
    assert _validate("../a\\b?c") == "..⁄a⧵b？c"
    assert _validate("<Name>") == "‹Name›"


@pytest.mark.parametrize(
    "name",
    [
        "CON", "con.txt", "NUL.pdf", "AUX.png", "PRN.docx", "COM1.jpg", "LPT9.txt",
        "COM¹.jpg", "COM²", "COM³.txt", "LPT¹.png", "LPT²", "LPT³.txt",
        "CON.backup.jpg", "CON .jpg", "CON..jpg", "CONIN$", "conout$.log",
    ],
)
def test_reserved_windows_device_names_are_rejected(name: str) -> None:
    with pytest.raises(FilenameValidationError, match="reserved Windows device"):
        _validate(name)


@pytest.mark.parametrize("name", ["COM10.jpg", "CONCERT.jpg", "NULL.txt", "AUX_01.png"])
def test_similar_non_reserved_names_are_allowed(name: str) -> None:
    assert _validate(name) == name


def test_valid_unicode_sequences_are_preserved_without_normalization() -> None:
    composed = "café"
    decomposed = "cafe\u0301"
    values = [composed, decomposed, "Ａ９", "x\u0301", "漢字", "🙂", "¿∶✱″‹›∣⁄⧵"]

    assert [_validate(value) for value in values] == values
    assert composed != decomposed


@pytest.mark.parametrize(
    "name",
    ["", " ", "\t", ".", "..", "name ", "name.", "a\x00b", "a\x1fb", "bad\ud800"],
)
def test_invalid_components_are_rejected(name: str) -> None:
    with pytest.raises(FilenameValidationError):
        _validate(name)


def test_empty_generated_stem_is_not_hidden_by_extension() -> None:
    with pytest.raises(FilenameValidationError, match="stem is empty"):
        _validate(".jpg", stem="")


def test_spaces_periods_and_dotfiles_are_preserved_when_valid() -> None:
    for name in (" leading space", "two words", "many.periods.name", ".env"):
        assert _validate(name) == name


def test_component_length_counts_final_text_in_utf16_code_units() -> None:
    assert _validate("a" * 4 + ".x", limit=6) == "a" * 4 + ".x"
    with pytest.raises(FilenameValidationError, match="UTF-16"):
        _validate("a" * 5 + ".x", limit=6)
    assert _validate("🙂" * 2, limit=4) == "🙂" * 2
    with pytest.raises(FilenameValidationError, match="UTF-16"):
        _validate("🙂" * 3, limit=5)


def test_invalid_extension_metadata_is_not_sanitized() -> None:
    context = FileEvaluationContext(
        "photo", file_identifier="photo", source_extension="bad/name"
    )
    with pytest.raises(FilenameValidationError) as caught:
        sanitize_and_validate_filename(
            "photo.bad/name",
            generated_stem="photo",
            context=context,
            extension_metadata=("bad/name",),
        )
    assert caught.value.sanitized_candidate is None


def test_batch_keeps_raw_resolved_and_validated_values_separate() -> None:
    context = FileEvaluationContext("a?b", "a?b.jpg", source_extension="jpg")
    result = evaluate_batch(parse_format("<Name>"), (context,))

    assert result.is_valid
    item = result.files[0]
    assert item.value == "a?b"
    assert item.candidate_filename == "a?b.jpg"
    assert item.validated_filename == "a？b.jpg"
    assert item.error is None
    assert item.validation_error is None


def test_batch_sanitizes_after_manipulation_numbering_and_conversion_resolution() -> None:
    context = FileEvaluationContext(
        "a?b", "a?b.jpg", source_extension="jpg", intended_output_extension="png",
        is_conversion=True,
    )
    result = evaluate_batch(parse_format("<Name>_<IncNr>.<Ext>"), (context,))

    assert result.is_valid
    item = result.files[0]
    assert item.value == "a?b_01.png"
    assert item.candidate_filename == "a?b_01.png"
    assert item.validated_filename == "a？b_01.png"


def test_batch_rejects_malformed_extension_metadata_without_rewriting_it() -> None:
    context = FileEvaluationContext("photo", "photo.jpg", source_extension="bad/name")
    result = evaluate_batch(parse_format("<Name>"), (context,))

    assert not result.is_valid
    error = result.files[0].validation_error
    assert error is not None
    assert error.sanitized_candidate is None


def test_batch_validation_failure_retains_candidate_and_structured_error() -> None:
    context = FileEvaluationContext("CON", "CON.jpg", source_extension="jpg")
    result = evaluate_batch(parse_format("<Name>"), (context,))

    assert not result.is_valid
    item = result.files[0]
    assert item.value == "CON"
    assert item.candidate_filename == "CON.jpg"
    assert item.validated_filename is None
    assert item.error is None
    assert item.validation_error is result.errors[0]
    assert result.errors[0].file_identifier == "CON.jpg"
    assert result.errors[0].candidate_filename == "CON.jpg"
    assert result.errors[0].sanitized_candidate == "CON.jpg"
    assert result.errors[0].token is None
    assert result.errors[0].position is None


def test_batch_accepts_a_destination_specific_component_limit() -> None:
    context = FileEvaluationContext("photo", "photo.jpg", source_extension="jpg")
    result = evaluate_batch(parse_format("<Name>"), (context,), max_component_length=8)

    assert not result.is_valid
    assert result.files[0].candidate_filename == "photo.jpg"
    assert result.files[0].validated_filename is None
    assert "UTF-16" in result.errors[0].reason


def test_batch_rejects_empty_rename_format_but_parser_still_accepts_it() -> None:
    parsed = parse_format("")
    assert parsed.source == ""

    result = evaluate_batch(
        parsed,
        (FileEvaluationContext("photo", "photo.jpg", source_extension="jpg"),),
    )
    assert not result.is_valid
    assert result.files[0].error is not None
    assert result.files[0].error.reason == "Filename format is required for rename"
