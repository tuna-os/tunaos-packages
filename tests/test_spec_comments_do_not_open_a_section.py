"""A spec comment must not begin with an unescaped RPM section macro.

RPM expands macros inside `#` comments -- it warns about it and then acts on
the result. So a PREAMBLE comment whose expansion starts with a section
keyword is parsed as that section, and the real one later in the file becomes a
duplicate:

    warning: Macro expanded in comment on line 33: %install died at
             'No module named pip' after logging 'Missing Python
    error: line 79: second %install

That is what killed src/input-remapper in gnome50-el10-aarch64 (run
32584416838). The spec had exactly one `%install`, at line 79 -- the very line
rpmbuild called the second one -- which is why the failure read as impossible
from the source alone.

The irony is instructive: the offending comment was written to document an
EARLIER failure of this same package ("%install died at 'No module named
pip'", gnome50-el10-x86_64 run 32418295773), and documenting the fix
introduced a new break.

Position is what decides it. Once the parser is inside a section body, `#` is
a shell comment and nothing structural happens -- src/deps/glycin (`# %build`
at line 280, first section at 112) and src/gnome-49/snowball (`# %check` at
224, first section at 83) both carry the same text safely. Only the preamble
is dangerous, so that is exactly what this checks; a rule broad enough to flag
the safe ones would be noise, and noise gets suppressed.

Escaping is `%%`, which renders as a single `%` and expands to nothing.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The section keywords whose presence at the start of a line opens a section.
SECTIONS = ("prep", "build", "install", "files", "check", "changelog",
            "package", "description")
SECTION_HEADER = re.compile(r"^%(" + "|".join(SECTIONS) + r")\b")
# A comment whose first non-blank content is an UNESCAPED section macro.
# `%%install` is already escaped and must not be flagged.
COMMENT_OPENS_SECTION = re.compile(r"^\s*#\s*%(?!%)(" + "|".join(SECTIONS) + r")\b")


def specs() -> list[Path]:
    return sorted(ROOT.rglob("*.spec"))


def preamble_offenders(spec: Path) -> list[tuple[int, str]]:
    lines = spec.read_text(encoding="utf-8", errors="replace").splitlines()
    first_section = next(
        (i for i, line in enumerate(lines) if SECTION_HEADER.match(line)), len(lines)
    )
    return [
        (i + 1, line.strip())
        for i, line in enumerate(lines[:first_section])
        if COMMENT_OPENS_SECTION.match(line)
    ]


def test_there_are_specs_to_check():
    """A guard over an empty set passes for the wrong reason."""
    assert len(specs()) > 50


def test_no_preamble_comment_opens_a_section():
    offenders = {
        str(spec.relative_to(ROOT)): found
        for spec in specs()
        if (found := preamble_offenders(spec))
    }
    assert not offenders, offenders


def test_the_input_remapper_comment_stays_escaped():
    """The specific line that broke, pinned by content rather than by number so
    it survives edits above it."""
    text = (ROOT / "src" / "input-remapper" / "input-remapper.spec").read_text(
        encoding="utf-8"
    )
    assert "# %%install died at 'No module named pip'" in text
    assert "# %install died at" not in text


def test_the_rule_would_catch_the_original_defect(tmp_path):
    """Mutation in miniature: the pre-fix spec shape must be flagged."""
    broken = tmp_path / "broken.spec"
    broken.write_text(
        "Name: x\n"
        "# %install died at 'No module named pip'\n"
        "BuildRequires: python3-pip\n"
        "\n"
        "%prep\n"
        "%install\n",
        encoding="utf-8",
    )
    assert preamble_offenders(broken) == [(2, "# %install died at 'No module named pip'")]


def test_a_section_body_comment_is_not_flagged():
    """glycin and snowball carry the same text safely, below their first
    section. Flagging them would be a false positive."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        spec = Path(tmp) / "body.spec"
        spec.write_text(
            "Name: x\n"
            "%build\n"
            "# %build already compiled jxl; re-export so meson does not rebuild\n"
            "true\n",
            encoding="utf-8",
        )
        assert preamble_offenders(spec) == []


def test_an_escaped_macro_is_not_flagged():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        spec = Path(tmp) / "escaped.spec"
        spec.write_text("Name: x\n# %%install is fine\n%prep\n", encoding="utf-8")
        assert preamble_offenders(spec) == []
