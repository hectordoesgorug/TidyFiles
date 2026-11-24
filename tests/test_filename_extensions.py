import pytest

from tidyfiles.filename_evaluator import (
    FileEvaluationContext,
    FilenameEvaluationError,
    evaluate_batch,
    evaluate_format,
    resolve_extension,
)
from tidyfiles.filename_format import parse_format


def _context(
    *,
    stem: str = "Vacation",
    source: str | None = "jpg",
    output: str | None = None,
    conversion: bool = False,
    identifier: str = "vacation.jpg",
) -> FileEvaluationContext:
    return FileEvaluationContext(
        stem,
        identifier,
        source_extension=source,
        intended_output_extension=output,
        is_conversion=conversion,
    )


def _candidate(expression: str, context: FileEvaluationContext) -> tuple[str | None, str | None]:
    result = evaluate_batch(parse_format(expression), (context,)).files[0]
    return result.value, result.candidate_filename


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("<Name>", "Vacation.jpg"),
        ("<Name>.<Ext>", "Vacation.jpg"),
        ("<Name>_<Ext>", "Vacation_jpg.jpg"),
        ("<Name>.jpg", "Vacation.jpg"),
        ("<Name>.png", "Vacation.png.jpg"),
        ("<Name>.2026", "Vacation.2026.jpg"),
        ("<Name>.backup", "Vacation.backup.jpg"),
        ("<Name>.JPG", "Vacation.jpg"),
        ("<Name>.<Ext>_backup", "Vacation.jpg_backup.jpg"),
    ],
)
def test_ordinary_rename_extension_examples(expression: str, expected: str) -> None:
    _, candidate = _candidate(expression, _context())

    assert candidate == expected


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("<Name>", "Vacation.png"),
        ("<Name>.<Ext>", "Vacation.png"),
        ("<Name>_<Ext>", "Vacation_jpg.png"),
        ("<Name>.jpg", "Vacation.png"),
        ("<Name>.png", "Vacation.png"),
        ("<Name>.2026", "Vacation.2026.png"),
        ("<Name>.backup", "Vacation.backup.png"),
        ("<Name>.JPG", "Vacation.png"),
        ("<Name>.<Ext>_backup", "Vacation.jpg_backup.png"),
    ],
)
def test_conversion_extension_examples(expression: str, expected: str) -> None:
    _, candidate = _candidate(
        expression, _context(output="png", conversion=True)
    )

    assert candidate == expected


def test_ext_token_uses_source_when_embedded_and_output_when_terminal() -> None:
    conversion = _context(output="PNG", conversion=True)

    assert evaluate_format(parse_format("<Ext>_<Name>"), conversion) == "jpg_Vacation"
    assert evaluate_format(parse_format("<Name>.<Ext>"), conversion) == "Vacation.PNG"
    assert resolve_extension(
        parse_format("<Name>.<Ext>"), "Vacation.PNG", conversion
    ) == "Vacation.PNG"


def test_source_and_target_extension_spelling_are_preserved() -> None:
    _, ordinary = _candidate("<Name>", _context(source=".JpG"))
    _, converted = _candidate(
        "<Name>", _context(source="jpg", output=".PnG", conversion=True)
    )

    assert ordinary == "Vacation.JpG"
    assert converted == "Vacation.PnG"


def test_mixed_source_extensions_and_conversion_targets_are_per_file() -> None:
    contexts = (
        _context(stem="One", source="jpg", output="png", conversion=True),
        _context(stem="Two", source="heic", output="webp", conversion=True),
    )

    result = evaluate_batch(parse_format("<Name>_<Ext>"), contexts)

    assert [file.value for file in result.files] == ["One_jpg", "Two_heic"]
    assert [file.candidate_filename for file in result.files] == ["One_jpg.png", "Two_heic.webp"]
    assert result.is_valid


def test_token_generated_extension_like_suffix_is_preserved_as_stem_text() -> None:
    context = _context(stem="Vacation.jpg")
    parsed = parse_format("<Name>")
    raw = evaluate_format(parsed, context)

    assert resolve_extension(parsed, raw, context) == "Vacation.jpg.jpg"

    insertion = parse_format("<Ins8:.jpg>")
    raw = evaluate_format(insertion, _context())
    assert raw == "Vacation.jpg"
    assert resolve_extension(insertion, raw, _context()) == "Vacation.jpg.jpg"


def test_multiple_periods_only_recognize_the_terminal_literal_suffix() -> None:
    _, candidate = _candidate("<Name>.backup.jpg", _context())

    assert candidate == "Vacation.backup.jpg"


def test_extensionless_source_and_explicit_terminal_ext() -> None:
    context = _context(stem="README", source="")

    assert _candidate("<Name>", context) == ("README", "README")
    assert _candidate("<Name>.<Ext>", context) == ("README.", "README")
    assert evaluate_format(parse_format("<Ext>"), context) == ""


def test_missing_extension_metadata_is_a_structured_batch_error() -> None:
    ordinary = evaluate_batch(parse_format("<Name>"), (_context(source=None),))
    conversion = evaluate_batch(
        parse_format("<Name>"),
        (_context(source="jpg", output=None, conversion=True),),
    )

    assert not ordinary.is_valid
    assert ordinary.files[0].value == "Vacation"
    assert ordinary.files[0].candidate_filename is None
    assert ordinary.errors[0].file_identifier == "vacation.jpg"
    assert "Source extension is unavailable" in ordinary.errors[0].reason
    assert not conversion.is_valid
    assert "Intended conversion extension is unavailable" in conversion.errors[0].reason


def test_extension_resolution_failure_invalidates_batch_but_keeps_valid_candidate() -> None:
    contexts = (
        _context(stem="One", source="jpg"),
        _context(stem="Two", source=None),
    )

    result = evaluate_batch(parse_format("<Name>"), contexts)

    assert not result.is_valid
    assert [file.value for file in result.files] == ["One", "Two"]
    assert [file.candidate_filename for file in result.files] == ["One.jpg", None]
    assert [error.file_identifier for error in result.errors] == ["vacation.jpg"]


def test_repeated_extension_resolution_has_no_state_leakage() -> None:
    parsed = parse_format("<Name>.<Ext>")
    context = _context(output="png", conversion=True)

    assert _candidate("<Name>.<Ext>", context) == ("Vacation.png", "Vacation.png")
    assert resolve_extension(parsed, evaluate_format(parsed, context), context) == "Vacation.png"


def test_embedded_ext_requires_source_extension_but_terminal_ext_requires_output() -> None:
    with pytest.raises(FilenameEvaluationError) as embedded:
        evaluate_format(parse_format("<Ext>"), _context(source=None, conversion=True, output="png"))
    with pytest.raises(FilenameEvaluationError) as terminal:
        evaluate_format(parse_format(".<Ext>"), _context(source=None, conversion=True, output=None))

    assert embedded.value.token == "<Ext>"
    assert terminal.value.token == "<Ext>"
