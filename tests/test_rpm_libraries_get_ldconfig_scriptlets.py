"""A shared library in an RPM must come with ldconfig scriptlets.

cpptrace-devel installed cleanly on Tumbleweed and then failed its own smoke
contract, while both el10 cells passed the identical assertion:

    ldconfig -p | grep -F libcpptrace.so.1     -> no match, exit 1

Fedora and EL do not need `%post -p /sbin/ldconfig`, because their glibc
carries RPM FILE TRIGGERS that run ldconfig for anything landing in a library
directory. openSUSE has no such trigger, so the cache never learned the
soname. rpmlint had been reporting the cause on every openSUSE build:

    E: library-without-ldconfig-postin  /usr/lib64/libcpptrace.so.1.0.4
    E: library-without-ldconfig-postun  /usr/lib64/libcpptrace.so.1.0.4

(run 32566033598, tideforge-cpptrace-devel-opensuse-tumbleweed-x86_64.)

Same shape as #478: one renderer emits one spec for every RPM distro, and the
distro that lacks Fedora's implicit behaviour is the one that breaks.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import tideforge  # noqa: E402


def spec_for(recipe, target="opensuse-tumbleweed") -> str:
    rendered = tideforge.render_rpm(recipe, target)
    return rendered[next(k for k in rendered if k.endswith(".spec"))]


def base(**over):
    recipe = {
        "schema": 1,
        "name": "demo",
        "version": "1.0",
        "release": 1,
        "summary": "s",
        "description": "d",
        "license": "MIT",
        "source": {"url": "https://example.invalid/demo-1.0.tar.gz", "sha256": "0" * 64},
        "build_system": "cmake",
        "build": {},
        "targets": ["el10", "opensuse-tumbleweed"],
        "files": {"common": ["/usr/bin/demo"]},
    }
    recipe.update(over)
    return recipe


def test_a_versioned_library_gets_both_scriptlets():
    r = base(outputs={"rpm": {"files": ["usr/lib64/libdemo.so.1.0.0"]}})
    text = spec_for(r)
    assert "%post -p /sbin/ldconfig" in text
    assert "%postun -p /sbin/ldconfig" in text


def test_the_glob_recipes_actually_write_is_recognised():
    """cpptrace-devel writes `usr/lib64/libcpptrace.so*`, not a literal
    version. Matching only the expanded form would have missed the exact
    recipe that failed."""
    r = base(outputs={"rpm": {"files": ["usr/lib64/libcpptrace.so*"]}})
    assert "%post -p /sbin/ldconfig" in spec_for(r)


def test_a_binary_only_package_gets_none():
    """An unconditional scriptlet would run ldconfig for packages that install
    no library at all -- harmless but dishonest, and it would make the
    assertion below untestable."""
    text = spec_for(base())
    assert "ldconfig" not in text


def test_a_bare_devel_symlink_alone_does_not_count():
    """`libfoo.so` with no version is the -devel link. It is not what
    ldconfig caches, and a -devel subpackage carrying only that needs no
    scriptlet."""
    r = base(outputs={"rpm": {"files": ["usr/include/demo.h", "usr/lib64/libdemo.so"]}})
    assert "ldconfig" not in spec_for(r)


def test_a_subpackage_shipping_a_library_gets_its_own_named_scriptlets():
    """An unnamed %post applies to the MAIN package. A subpackage carrying the
    runtime library needs `%post <name>`, or the scriptlet attaches to the
    wrong package and the cache is still never refreshed."""
    r = base(outputs={"rpm": {
        "files": ["usr/include/demo.h"],
        "subpackages": [{"name": "libs", "summary": "runtime", "files": ["usr/lib64/libdemo.so.1"]}],
    }})
    text = spec_for(r)
    assert "%post libs -p /sbin/ldconfig" in text
    assert "%postun libs -p /sbin/ldconfig" in text


def test_the_scriptlets_precede_files_so_the_spec_parses():
    r = base(outputs={"rpm": {"files": ["usr/lib64/libdemo.so.1"]}})
    text = spec_for(r)
    assert text.index("%post -p /sbin/ldconfig") < text.index("\n%files")


def test_it_is_not_written_as_the_fedora_only_macro():
    """`%ldconfig_scriptlets` is undefined on openSUSE -- the one distro that
    actually needs this -- so it would survive into the spec as literal text
    and do nothing."""
    r = base(outputs={"rpm": {"files": ["usr/lib64/libdemo.so.1"]}})
    assert "%ldconfig_scriptlets" not in spec_for(r)


def test_cpptrace_devel_the_recipe_that_failed_now_gets_them():
    r = yaml.safe_load((ROOT / "packages" / "cpptrace-devel" / "package.yaml").read_text(encoding="utf-8"))
    for target in ("opensuse-tumbleweed", "el10"):
        assert "%post -p /sbin/ldconfig" in spec_for(r, target), target


def test_every_rpm_recipe_shipping_a_library_gets_them():
    """Stated over the whole repo. cpptrace-devel was not special -- any
    recipe shipping a versioned .so had the same latent defect on openSUSE."""
    for path in sorted((ROOT / "packages").glob("*/package.yaml")):
        r = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        rpm_targets = [t for t in (r.get("targets") or []) if t in ("el10", "fedora", "opensuse-tumbleweed")]
        if not rpm_targets:
            continue
        rpm_out = (r.get("outputs") or {}).get("rpm") or {}
        files = list(rpm_out.get("files") or (r.get("files") or {}).get("common") or [])
        if not tideforge.ships_a_shared_library(files):
            continue
        assert "%post -p /sbin/ldconfig" in spec_for(r, rpm_targets[0]), path.parent.name
