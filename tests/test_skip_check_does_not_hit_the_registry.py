"""The already-built check must not cost a registry round-trip per package.

`check_package_exists` is what makes hummingbird's incremental convergence
work: each run seeds `local-repo-hummingbird/` from R2 and this function is
what decides, per package, whether it is already there and can be skipped.

The decision is cheap. Reaching it was not. It resolved the expected NVR by
starting a container with `--pull=always` against a *fixed* tag
(`ghcr.io/tuna-os/mock-runner:centos-stream-10`), which the workflow has
already pulled once in its own "Pull mock runner" step. So every package paid
a GHCR round-trip -- including every package that was about to be skipped.

That scales the wrong way for tunaos-packages#401. Convergence assumes the
per-run overhead stays flat while the useful work shrinks; here the share of
the ~5h05m usable budget spent confirming "already built, skip" grows as the
seed grows. In the limit -- everything built -- a run still round-trips the
registry once per package to conclude it has nothing to do.

`--pull=missing` keeps the image available without re-resolving the tag, and
is also the more correct choice: the NVR is compared against RPMs an earlier
run built, so answering from a freshly-resolved image is a way to get a
different answer than the one that produced them.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts/build-chain.sh"

BACKEND_SOURCES = (
    REPO / "scripts/lib/build-chain/podman.sh",
    REPO / "scripts/lib/build-chain/mock.sh",
    REPO / "scripts/lib/build-chain/native.sh",
)


def all_sources() -> str:
    return "\n".join((SCRIPT, *BACKEND_SOURCES)[i].read_text()
                     for i in range(len(BACKEND_SOURCES) + 1))



def _function_body(name: str, *, code_only: bool = False) -> str:
    """The text of a shell function, from `name() {` to the closing brace.

    `code_only` strips comment lines. The comment above the invocation
    explains the change by naming the flag it replaced, so a naive substring
    check reads the rationale as the thing it forbids -- which is how the
    first version of this test failed against a correct fix.
    """
    src = SCRIPT.read_text()
    start = src.index(f"{name}() {{")
    end = src.index("\n}\n", start)
    body = src[start:end]
    if code_only:
        body = "\n".join(
            line for line in body.splitlines() if not line.lstrip().startswith("#")
        )
    return body


def test_the_skip_check_does_not_re_resolve_the_image_tag():
    body = _function_body("check_package_exists", code_only=True)
    assert "--pull=always" not in body, (
        "check_package_exists re-resolves the image tag against GHCR on every "
        "package, before the skip decision, so the cost is paid for packages "
        "that are about to be skipped"
    )
    assert "--pull=missing" in body, (
        "the skip check should still guarantee the image is present, just not "
        "re-resolve a fixed tag every time"
    )


def test_the_skip_check_still_resolves_the_nvr_inside_the_container():
    """The reason this is expensive is also the reason it is correct.

    `%autorelease` and `%dist` must expand exactly as they will at build time,
    which means asking the build image -- not the host. Making the check
    cheaper must not turn it into a host-side guess.
    """
    body = _function_body("check_package_exists")
    assert "rpmspec -q" in body
    assert "${BUILD_IMAGE}" in body
    assert '--define "dist ${DIST}"' in body


def test_the_skip_is_still_opt_out_able():
    """`--force` must keep rebuilding regardless, or a bad NVR match is unfixable."""
    src = all_sources()
    guards = re.findall(r"if ! \$FORCE && check_package_exists", src)
    assert len(guards) >= 2, (
        f"expected every build backend to guard on FORCE, found {len(guards)}"
    )


def test_the_build_paths_are_left_alone():
    """Deliberately not a blanket sweep of --pull=always.

    The other call sites run once per package that is actually BUILT, so a
    single tag resolution is amortised against minutes of compilation. This
    change targets the one call paid per package *considered*. Pinned so the
    narrow scope is a decision on record rather than something to 'tidy up'
    without measuring.
    """
    src = all_sources()
    assert src.count("--pull=always") >= 1, (
        "the build paths were also changed -- that is a wider change than "
        "tunaos-packages#410 argues for; measure it before widening"
    )
