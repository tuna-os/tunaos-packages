"""rpm expands macros inside spec comments. A comment that names a build
macro unescaped (e.g. "%meson turns auto features on") expands it, and in
%build that pastes "turns auto ..." onto the real meson command line:
thunar 4.21.7 failed with "meson: error: unrecognized arguments: turns auto"
(run 44734376131). Write %%meson in comments, or name the macro in words."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD_MACROS = ("meson", "meson_build", "meson_install", "configure", "make_build",
                "make_install", "cmake", "cmake_build", "cmake_install", "autosetup")
UNESCAPED = re.compile(r"(?<!%)%\{?(" + "|".join(BUILD_MACROS) + r")\b")


def test_no_spec_comment_names_an_unescaped_build_macro() -> None:
    offenders = []
    for spec in sorted((ROOT / "src").rglob("*.spec")):
        for n, line in enumerate(spec.read_text(errors="replace").splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#") and UNESCAPED.search(stripped):
                offenders.append(f"{spec.relative_to(ROOT)}:{n}: {stripped}")
    assert not offenders, "escape as %%name:\n" + "\n".join(offenders)
