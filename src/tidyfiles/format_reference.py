"""Approved filename token insertion examples and user-facing reference."""

from __future__ import annotations

from tidyfiles.filename_format import TokenKind, parse_format

TOKEN_GROUPS = (
    ("Filename and extension", ((TokenKind.NAME, "<Name>"), (TokenKind.EXT, "<Ext>"))),
    (
        "Directory information",
        (
            (TokenKind.DIR_NAME, "<DirName>"),
            (TokenKind.SUBDIR_NAME, "<SubDirName>"),
            (TokenKind.NUM_FILES, "<NumFiles>"),
            (TokenKind.NUM_DIRS, "<NumDirs>"),
            (TokenKind.NUM_ITEMS, "<NumItems>"),
        ),
    ),
    (
        "Numbering",
        (
            (TokenKind.INC_NR, "<IncNr>"),
            (TokenKind.INC_NR_DIR, "<IncNrDir>"),
            (TokenKind.DECR_NR, "<DecrNr>"),
            (TokenKind.INC_ALPHA, "<IncAlpha>"),
        ),
    ),
    (
        "Text extraction",
        (
            (TokenKind.WORD, "<Word>"),
            (TokenKind.SUBSTR, "<Substr>"),
            (TokenKind.RSUBSTR, "<RSubstr>"),
        ),
    ),
    (
        "Text manipulation",
        (
            (TokenKind.REMOVE_L, "<RemoveL:1>"),
            (TokenKind.REMOVE_R, "<RemoveR:1>"),
            (TokenKind.MOVE, "<Move1:2>"),
            (TokenKind.REP_S, "<RepS:1,X>"),
            (TokenKind.REP_M, "<RepM:2,1,X>"),
            (TokenKind.INS, "<Ins0:X>"),
        ),
    ),
)

TOKEN_TEMPLATES = {
    token: template for _category, tokens in TOKEN_GROUPS for token, template in tokens
}

FORMAT_REFERENCE = """Filename format reference

Tokens are case-sensitive and use angle brackets. Literal text may be mixed with
tokens. Tokens are evaluated from left to right. <Name> always reads the original
filename stem. Text extraction and manipulation tokens read the evolving working
stem; each manipulation changes that stem for later text tokens.

FILENAME AND EXTENSION

<Name> — original filename stem; no arguments. Example: <Name> for Report.pdf
produces Report.
<Ext> — source extension without its dot; no arguments. Example: <Name>.<Ext>
produces Report.pdf. The extension is also preserved automatically when the
format does not declare one. In bulk rename, a terminal .<Ext> preserves the
source extension.

DIRECTORY INFORMATION

<DirName> — stored import-root name; no arguments. Example: <DirName>_<Name>
uses the root selected for the import, even for a nested file.
<SubDirName> — actual immediate parent-directory name; no arguments. Example:
<SubDirName>_<Name> uses the folder containing the file.
<NumFiles> — number of ordinary file entries in the immediate parent directory;
no arguments. It is a physical, nonrecursive count, not the imported-row count.
<NumDirs> — number of ordinary subdirectories in the immediate parent;
no arguments. The count is physical and nonrecursive.
<NumItems> — number of entries in the complete collection; no arguments.

Number tokens acquire shared physical counts only for <NumFiles> or <NumDirs>.
Symlinks, junctions, other reparse points and special entries are excluded.
Unavailable counts cause an error; they are never silently treated as zero.

NUMBERING

<IncNr[:start]> — global increasing number. The optional unsigned decimal start
defaults to 1. Example: Photo <IncNr> over three files produces Photo 01,
Photo 02 and Photo 03 in effective collection order.
<IncNrDir[:start]> — increasing number per immediate parent directory; optional
unsigned decimal start defaults to 1. Each parent group follows collection order.
<DecrNr[:start]> — global decreasing number; optional unsigned decimal start
defaults to collection size. Without an explicit start, a three-entry collection
produces 03, 02, 01.
<IncAlpha[:offset]> — global lowercase alphabetic sequence; optional unsigned
decimal zero-based offset defaults to 0. Example: <IncAlpha> produces a, b, c;
offset 25 starts at z, then rolls over to aa.

Numeric tokens use a minimum width of two digits. Width is shared across each
numbering family for the complete batch; per-directory numbering calculates
width separately for each parent. Negative results can occur with explicit
decreasing starts and are rendered with a leading minus sign.

TEXT EXTRACTION

<Word[:index]> — whitespace-separated word from the current working stem; the
optional unsigned decimal index is one-based and defaults to 1. Example:
<Word:2> applied to The Blue Ocean produces Blue.
<Substr> — first five characters of the current working stem; no arguments.
Example: <Substr> on Oceanview produces Ocean.
<RSubstr> — last five characters of the current working stem; no arguments.
Example: <RSubstr> on Oceanview produces nview.

TEXT MANIPULATION

<RemoveL:n> — remove n characters from the left of the working stem and emit the
result. n is required and unsigned. Example: <RemoveL:2> on Report produces port.
<RemoveR:n> — remove n characters from the right; n is required and unsigned.
Example: <RemoveR:2> on Report produces Repo.
<MoveX:Y> — move the first X characters to after Y characters in the remainder;
both unsigned values are required. Example: <Move1:2> on abcdef produces bcadef.
<RepS:position,text> — replace one character at a one-based position; the
replacement may be empty or contain commas. Example: <RepS:1,X> on abc produces
Xbc.
<RepM:count,position,text> — replace count characters from a one-based position;
count zero inserts at that position. The replacement may be empty or contain
commas. Example: <RepM:2,1,X> on abcd produces Xcd.
<Insposition:text> — insert text at a zero-based position. Example: <Ins0:X> on
abc produces Xabc. Text may contain colons and commas.

VALIDATION AND BATCH BEHAVIOR

The source extension is preserved automatically. Existing approved substitutions
replace characters that Windows prohibits in filename components. The engine
still rejects empty names, reserved device names, control characters, invalid
Unicode, trailing spaces or periods, and names exceeding the Windows component
limit. It does not invent collision-avoidance names.

A syntax error invalidates the complete batch. A valid expression can also fail
for individual files when a token's input is unavailable or an index is outside
that file's working stem, or when a generated filename fails validation. Valid
rows remain visible for review, but the complete rename batch is invalid. A
preview never renames, moves or deletes files and does not establish destination
availability or execution readiness.
"""


def validate_insertion_templates() -> None:
    """Keep every insertion example accepted by the existing parser."""
    if len(TOKEN_TEMPLATES) != len(TokenKind):
        raise AssertionError("Every supported token must have one insertion template")
    for token, template in TOKEN_TEMPLATES.items():
        parsed = parse_format(template)
        if len(parsed.parts) != 1 or getattr(parsed.parts[0], "kind", None) is not token:
            raise AssertionError(f"Invalid insertion template for {token.value}: {template}")
