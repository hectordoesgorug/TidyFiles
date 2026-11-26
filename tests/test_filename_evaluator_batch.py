import pytest

from tidyfiles.filename_evaluator import (
    FileEvaluationContext,
    evaluate_batch,
    evaluate_format,
)
from tidyfiles.filename_format import parse_format


def _file(
    stem: str,
    identity: str | None = r"C:\root\Photos",
    *,
    root: str | None = "root",
    parent: str = "Photos",
    files: int | None = 4,
    directories: int | None = 0,
    extension: str | None = "",
) -> FileEvaluationContext:
    return FileEvaluationContext(
        stem,
        stem,
        import_root_name=root,
        parent_name=parent,
        parent_identity=identity,
        physical_file_count=files,
        physical_directory_count=directories,
        source_extension=extension,
    )


def test_directory_names_and_physical_counts_use_each_files_context() -> None:
    files = (
        _file("a", r"C:\root\Photos", root="Camera", files=6, directories=0),
        _file("b", r"D:\trip\Photos", root="Trip", files=11, directories=2),
        _file("c", r"D:\trip\Photos\Day1", root="Trip", parent="Day1", files=3, directories=1),
    )

    result = evaluate_batch(
        parse_format("<DirName>|<SubDirName>|<NumFiles>|<NumDirs>|<NumItems>"), files
    )

    assert result.is_valid
    assert [file.value for file in result.files] == [
        "Camera|Photos|6|0|3",
        "Trip|Photos|11|2|3",
        "Trip|Day1|3|1|3",
    ]


def test_directory_tokens_cover_files_in_import_root_and_nested_parent() -> None:
    files = (
        _file("a", r"C:\import", root="import", parent="import"),
        _file("b", r"C:\import\Photos\Day1", root="import", parent="Day1"),
    )

    result = evaluate_batch(parse_format("<DirName>/<SubDirName>"), files)

    assert [file.value for file in result.files] == ["import/import", "import/Day1"]


def test_unavailable_metadata_only_fails_when_a_token_needs_it() -> None:
    context = _file("a", files=None, directories=None)

    assert evaluate_format(parse_format("<Name>"), context) == "a"
    result = evaluate_batch(parse_format("<NumFiles>"), (context,))

    assert not result.is_valid
    assert len(result.errors) == 1
    assert result.errors[0].file_identifier == "a"
    assert result.errors[0].token == "<NumFiles>"


def test_batch_does_not_require_unused_physical_counts() -> None:
    result = evaluate_batch(
        parse_format("<Name>"),
        (_file("a", files=None, directories=None),),
    )

    assert result.is_valid
    assert result.files[0].validated_filename == "a"


def test_directory_identity_is_windows_normalized_but_not_merged_by_name() -> None:
    files = (
        _file("a", r"C:\root\Photos"),
        _file("b", "c:/ROOT/photos"),
        _file("c", r"D:\other\Photos"),
    )

    result = evaluate_batch(parse_format("<IncNrDir>"), files)

    assert [file.value for file in result.files] == ["01", "02", "01"]
    assert [file.context.parent_index for file in result.files] == [0, 1, 0]
    assert [file.context.parent_collection_size for file in result.files] == [2, 2, 1]
    assert all(file.context.collection_size == 3 for file in result.files)


def test_batch_derives_indexes_after_reorder_and_removal() -> None:
    files = (_file("a"), _file("b", r"D:\root\Photos"), _file("c"))

    result = evaluate_batch(parse_format("<IncNr>-<IncNrDir>-<NumItems>"), files[1:])

    assert [file.value for file in result.files] == ["01-01-2", "02-01-2"]
    assert [file.context.global_index for file in result.files] == [0, 1]


def test_numeric_sequences_use_two_digit_minimum_and_shared_occurrence_widths() -> None:
    files = tuple(_file(str(index)) for index in range(5))

    result = evaluate_batch(parse_format("<IncNr:1>_<IncNr:100>"), files)

    assert [file.value for file in result.files] == [
        "001_100",
        "002_101",
        "003_102",
        "004_103",
        "005_104",
    ]


def test_leading_zeroes_in_start_are_not_a_padding_setting() -> None:
    files = tuple(_file(str(index)) for index in range(3))

    result = evaluate_batch(parse_format("<IncNr:0001>"), files)

    assert [file.value for file in result.files] == ["01", "02", "03"]


def test_global_number_width_transitions_at_ninety_nine_and_one_hundred() -> None:
    files = tuple(_file(str(index)) for index in range(100))

    result = evaluate_batch(parse_format("<IncNr>"), files)

    assert result.files[0].value == "001"
    assert result.files[8].value == "009"
    assert result.files[9].value == "010"
    assert result.files[-1].value == "100"


def test_default_and_custom_decrementing_sequences_cross_zero() -> None:
    files = tuple(_file(str(index)) for index in range(5))

    default = evaluate_batch(parse_format("<DecrNr>"), files)
    custom = evaluate_batch(parse_format("<DecrNr:3>"), files)

    assert [file.value for file in default.files] == ["05", "04", "03", "02", "01"]
    assert [file.value for file in custom.files] == ["03", "02", "01", "00", "-01"]


def test_negative_decrement_value_contributes_absolute_digits_to_width() -> None:
    files = tuple(_file(str(index)) for index in range(105))

    result = evaluate_batch(parse_format("<DecrNr:3>"), files)

    assert result.files[0].value == "003"
    assert result.files[3].value == "000"
    assert result.files[4].value == "-001"
    assert result.files[-1].value == "-101"


def test_directory_sequence_width_is_shared_within_but_not_between_parents() -> None:
    files = tuple(_file(f"a{index}", r"C:\A\Photos") for index in range(100)) + (
        _file("b", r"D:\B\Photos"),
    )

    result = evaluate_batch(parse_format("<IncNrDir>"), files)

    assert result.files[0].value == "001"
    assert result.files[8].value == "009"
    assert result.files[9].value == "010"
    assert result.files[99].value == "100"
    assert result.files[100].value == "01"


def test_directory_sequences_keep_independent_widths_when_parent_groups_interleave() -> None:
    files = tuple(
        file
        for index in range(10)
        for file in (_file(f"a{index}", r"C:\A"), _file(f"b{index}", r"D:\B"))
    ) + tuple(_file(f"a{index}", r"C:\A") for index in range(10, 100))

    result = evaluate_batch(parse_format("<IncNrDir>"), files)

    assert result.is_valid
    assert result.files[0].value == "001"
    assert result.files[18].value == "010"
    assert result.files[1].value == "01"
    assert result.files[19].value == "10"
    assert result.files[-1].value == "100"


@pytest.mark.parametrize(
    ("offset", "expected"),
    [(0, "a"), (25, "z"), (26, "aa"), (51, "az"), (52, "ba"), (701, "zz"), (702, "aaa")],
)
def test_alphabetic_sequence_rollover(offset: int, expected: str) -> None:
    files = tuple(_file(str(index)) for index in range(offset + 1))

    result = evaluate_batch(parse_format("<IncAlpha>"), files)

    assert result.files[-1].value == expected


def test_repeated_alpha_starts_and_mixed_numeric_families_are_independent() -> None:
    files = tuple(_file(str(index)) for index in range(5))
    expression = "<IncNr:1>_<IncNr:100>_<DecrNr:3>_<IncAlpha:25>_<IncAlpha>"

    result = evaluate_batch(parse_format(expression), files)

    assert result.files[0].value == "001_100_03_z_a"
    assert result.files[-1].value == "005_104_-01_ad_e"


def test_large_numeric_start_is_reported_per_file() -> None:
    files = (_file("a"), _file("b", r"D:\root\Photos"))

    result = evaluate_batch(parse_format(f"<IncNr:{'9' * 5000}>"), files)

    assert not result.is_valid
    assert [error.file_identifier for error in result.errors] == ["a", "b"]
    assert all("too large" in error.reason for error in result.errors)


def test_batch_collects_all_file_errors_and_keeps_valid_diagnostic_values() -> None:
    files = (_file("one two"), _file("single", r"D:\root\Photos"), _file("three four"))

    result = evaluate_batch(parse_format("<Word:2>"), files)

    assert not result.is_valid
    assert [file.value for file in result.files] == ["two", None, "four"]
    assert [file.candidate_filename for file in result.files] == ["two", None, "four"]
    assert [error.file_identifier for error in result.errors] == ["single"]


def test_batch_reports_multiple_file_errors() -> None:
    files = (_file("one"), _file("two", r"D:\root\Photos"))

    result = evaluate_batch(parse_format("<Word:2>"), files)

    assert not result.is_valid
    assert [file.value for file in result.files] == [None, None]
    assert [error.file_identifier for error in result.errors] == ["one", "two"]


def test_mixed_batch_evaluation_is_deterministic() -> None:
    files = (_file("Alpha one"), _file("Beta two", r"D:\root\Photos"))
    parsed = parse_format("<DirName>_<RemoveL:1>_<Word>_<IncNr>")

    first = evaluate_batch(parsed, files)
    second = evaluate_batch(parsed, files)

    assert first == second
    assert [file.value for file in first.files] == [
        "root_lpha one_lpha_01",
        "root_eta two_eta_02",
    ]


def test_batch_is_empty_and_invalid_for_empty_collection() -> None:
    result = evaluate_batch(parse_format("<IncNr>"), ())

    assert result.files == ()
    assert result.errors == ()
    assert not result.is_valid


def test_directory_context_errors_are_per_file_and_identifiable() -> None:
    files = (_file("a", identity="", root=None), _file("b"))

    result = evaluate_batch(parse_format("<DirName><IncNrDir>"), files)

    assert not result.is_valid
    assert [file.value for file in result.files] == [None, "root01"]
    assert result.errors[0].file_identifier == "a"
    assert result.errors[0].token == "<DirName>"


def test_per_directory_sequence_requires_directory_identity() -> None:
    result = evaluate_batch(parse_format("<IncNrDir>"), (_file("a", identity=None),))

    assert not result.is_valid
    assert result.errors[0].file_identifier == "a"
    assert result.errors[0].token == "<IncNrDir>"


def test_single_evaluation_accepts_explicit_sequence_context() -> None:
    context = FileEvaluationContext(
        "name", global_index=1, collection_size=2,
        parent_identity=r"C:\root\Photos", parent_index=0,
    )

    assert evaluate_format(parse_format("<IncNr><DecrNr><IncNrDir><IncAlpha>"), context) == (
        "020101b"
    )
