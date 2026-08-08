"""Fedora dist-git has two `sources` formats and both are current.

    SHA512 (foo-1.0.tar.gz) = <128 hex>     the modern one
    <32 hex>  foo-1.0.tar.gz                the legacy md5 one

The lookaside fetch matched only the first. A legacy line hit `continue` --
indistinguishable from a comment or a blank -- so nothing was downloaded, and
the package died much later in rpmbuild with:

    error: Bad file: /builddir/SOURCES/lockdev-1.0.4.20111007git.tar.gz:
      No such file or directory

which names the tarball but not the reason. lockdev and redhat-menus failed
exactly that way in gnome-00 of run 31272392927, and their real sources files
in Rawhide today are:

    c0015d1bcd155b51df688467ed34137f  lockdev-1.0.4.20111007git.tar.gz
    494af105a03e3679505ceb44c3cb6a77  redhat-menus-12.0.2.tar.gz

Old packages nobody has re-uploaded. There are more across 1248 of them.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "build-chain.sh"


def sources_block() -> str:
    src = SCRIPT.read_text()
    start = src.index("local sources_file=")
    end = src.index('done < "$sources_file"', start)
    return src[start:end]


def parse_with_the_real_shell(sources_text: str):
    """Run the script's own two patterns, so the test cannot drift from them."""
    block = sources_block()
    patterns = re.findall(r'\[\[ "\$line" =~ (\^.+?\$) \]\]', block)
    assert len(patterns) == 2, f"expected two sources-line patterns, found {patterns}"
    script = f"""
        set -u
        while IFS= read -r line; do
            if [[ "$line" =~ {patterns[0]} ]]; then
                echo "sha512 ${{BASH_REMATCH[1]}} ${{BASH_REMATCH[2]}}"
            elif [[ "$line" =~ {patterns[1]} ]]; then
                echo "md5 ${{BASH_REMATCH[2]}} ${{BASH_REMATCH[1]}}"
            fi
        done
    """
    out = subprocess.run(["bash", "-c", script], input=sources_text,
                         capture_output=True, text=True, check=True).stdout
    return [line.split() for line in out.strip().splitlines() if line.strip()]


def test_modern_sha512_line_is_parsed():
    got = parse_with_the_real_shell(
        "SHA512 (iso-codes-v4.20.1.tar.gz) = " + "a" * 128 + "\n"
    )
    assert got == [["sha512", "iso-codes-v4.20.1.tar.gz", "a" * 128]]


def test_legacy_md5_line_is_parsed():
    """The regression. Real line from rawhide's lockdev."""
    got = parse_with_the_real_shell(
        "c0015d1bcd155b51df688467ed34137f  lockdev-1.0.4.20111007git.tar.gz\n"
    )
    assert got == [["md5", "lockdev-1.0.4.20111007git.tar.gz",
                    "c0015d1bcd155b51df688467ed34137f"]]


def test_both_formats_in_one_file():
    got = parse_with_the_real_shell(
        "SHA512 (new.tar.gz) = " + "b" * 128 + "\n"
        "494af105a03e3679505ceb44c3cb6a77  redhat-menus-12.0.2.tar.gz\n"
    )
    assert [g[0] for g in got] == ["sha512", "md5"]


def test_junk_lines_are_still_skipped():
    assert parse_with_the_real_shell("\n# a comment\nnot a sources line\n") == []


def test_the_url_carries_the_algorithm():
    """Legacy entries live under .../md5/<hash>/..., not under sha512."""
    block = sources_block()
    assert "${entry_algo}/${entry_hash}" in block, (
        "the lookaside URL hardcodes an algorithm, so legacy md5 entries are "
        "fetched from a sha512 path that does not exist"
    )


def test_the_checksum_is_verified_with_the_matching_tool():
    block = sources_block()
    assert '"${entry_algo}sum"' in block, (
        "the checksum check hardcodes sha512sum, which cannot verify an md5 "
        "entry and would reject every legacy source"
    )
