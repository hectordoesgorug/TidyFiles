"""Pure sanitization and validation of Windows filename components."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tidyfiles.filename_evaluator import FileEvaluationContext


_REPLACEMENTS = str.maketrans(
    {
        "?": "？",
        ":": "∶",
        "*": "✱",
        '"': "″",
        "<": "‹",
        ">": "›",
        "|": "∣",
        "/": "⁄",
        "\\": "⧵",
    }
)
_INVALID_WINDOWS_CHARACTERS = frozenset('<>:"/\\|?*')
_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {f"com{digit}" for digit in "123456789¹²³"}
    | {f"lpt{digit}" for digit in "123456789¹²³"}
)


class FilenameValidationError(ValueError):
    """A filename-component failure without a fabricated expression location."""

    def __init__(
        self,
        reason: str,
        *,
        context: FileEvaluationContext,
        candidate_filename: str,
        sanitized_candidate: str | None,
    ) -> None:
        self.reason = reason
        self.category = "filename_validation"
        self.file_identifier = context.file_identifier
        self.original_stem = context.original_stem
        self.candidate_filename = candidate_filename
        self.sanitized_candidate = sanitized_candidate
        self.token = None
        self.position = None
        label = context.file_identifier or context.original_stem
        super().__init__(f"{label}: {reason}")


def _fail(
    reason: str,
    context: FileEvaluationContext,
    candidate: str,
    sanitized: str | None,
) -> None:
    raise FilenameValidationError(
        reason,
        context=context,
        candidate_filename=candidate,
        sanitized_candidate=sanitized,
    )


def sanitize_and_validate_filename(
    candidate_filename: str,
    *,
    generated_stem: str,
    context: FileEvaluationContext,
    extension_metadata: tuple[str, ...] = (),
    max_component_length: int = 255,
) -> str:
    """Apply the approved substitutions, then validate one filename component."""
    if not isinstance(max_component_length, int) or isinstance(max_component_length, bool):
        raise TypeError("max_component_length must be an integer")
    if max_component_length < 1:
        raise ValueError("max_component_length must be positive")

    for extension in extension_metadata:
        metadata = extension[1:] if extension.startswith(".") else extension
        if extension and not metadata:
            _fail("Extension metadata is malformed", context, candidate_filename, None)
        if any(
            character in _INVALID_WINDOWS_CHARACTERS or ord(character) < 32
            for character in metadata
        ):
            _fail("Extension metadata contains an invalid Windows filename character", context,
                  candidate_filename, None)
        try:
            metadata.encode("utf-16-le", errors="strict")
        except UnicodeEncodeError:
            _fail("Extension metadata contains an invalid Unicode sequence", context,
                  candidate_filename, None)

    sanitized = candidate_filename.translate(_REPLACEMENTS)
    if generated_stem == "":
        _fail("Generated filename stem is empty", context, candidate_filename, sanitized)
    if generated_stem.isspace() or sanitized.isspace():
        _fail("Filename cannot contain only whitespace", context, candidate_filename, sanitized)
    if sanitized in {"", ".", ".."}:
        _fail("Filename component is empty or a directory component", context,
              candidate_filename, sanitized)
    if sanitized.endswith((" ", ".")):
        _fail("Filename cannot end with an ASCII space or period", context,
              candidate_filename, sanitized)
    if any(ord(character) < 32 for character in sanitized):
        _fail("Filename contains a NUL or forbidden control character", context,
              candidate_filename, sanitized)
    if any(character in _INVALID_WINDOWS_CHARACTERS for character in sanitized):
        _fail("Filename contains an invalid Windows filename character", context,
              candidate_filename, sanitized)

    try:
        component_length = len(sanitized.encode("utf-16-le", errors="strict")) // 2
    except UnicodeEncodeError:
        _fail("Filename contains an invalid Unicode sequence", context,
              candidate_filename, sanitized)
    if component_length > max_component_length:
        _fail(
            f"Filename exceeds the {max_component_length}-unit UTF-16 component limit",
            context,
            candidate_filename,
            sanitized,
        )

    device_basename = sanitized.split(".", 1)[0].rstrip(" .").casefold()
    if device_basename in _DEVICE_NAMES:
        _fail("Filename uses a reserved Windows device name", context,
              candidate_filename, sanitized)
    return sanitized
