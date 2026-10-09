"""The hummingbird buildroot is Fedora 44 plus Hummingbird, never Rawhide.

Hummingbird builds in Fedora 44 plus its own Pulp repository at higher
priority (redhat/hummingbird/containers: mock/mock.cfg and
images/variables.yml); projectbluefin/utah-packages built GNOME 51 for it in
that root.  This repository built in Fedora RAWHIDE plus Hummingbird until
2026-09-02, and the cost was measured on our own published prefix
(repo.tunaos.org/hummingbird/20251124-x86_64, index sha256 dacef1b3b4c9…):
gstreamer1-plugins-good, libavutil-free and zvbi require
libm.so.6(GLIBC_2.44) -- Rawhide's glibc -- which Hummingbird's 2.43 cannot
provide.  Built for the target, uninstallable on the target.

The three interpreter "pins" the old config carried ([fedora-44-python],
[fedora-44-perl], [fedora-44-mpich]) were each patching Fedora 44 back in
one namespace at a time.  With Fedora 44 as the base they are the base, and
the tests that guarded them are replaced by this file.

docs/HUMMINGBIRD-TARGET.md has the measured ABI table these invariants rest on.
"""

from __future__ import annotations

import configparser
import io
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIGS = [ROOT / "mock" / "hummingbird-ci.cfg", ROOT / "mock" / "hummingbird-ci-aarch64.cfg"]

# What mock's fedora-44 template contributes, reduced to the two sections
# the config rewrites.  The real template carries more ([updates-testing],
# [local], debuginfo repos); the rewrite only touches these two and the
# test only needs them to exist in the shape the template emits.
TEMPLATE_DNF_CONF = """[main]
keepcache=1
metadata_expire=0

[fedora]
name=fedora
metalink=https://mirrors.fedoraproject.org/metalink?repo=fedora-$releasever&arch=$basearch
gpgcheck=1

[updates]
name=updates
metalink=https://mirrors.fedoraproject.org/metalink?repo=updates-released-f$releasever&arch=$basearch
gpgcheck=1
"""


def evaluate(config: pathlib.Path) -> dict:
    """Execute the mock config the way mock does, with the template stubbed.

    mock evaluates its .cfg files as Python with `config_opts` and `include`
    in scope.  Stubbing `include` with the template's dnf.conf is what lets
    the in-place [fedora]/[updates] rewrite be tested rather than trusted.
    """
    config_opts = {
        "dnf.conf": "",
        "plugin_conf": {"bind_mount_enable": False, "bind_mount_opts": {"dirs": []}},
    }
    included: list[str] = []

    def include(path: str) -> None:
        included.append(path)
        config_opts["dnf.conf"] = TEMPLATE_DNF_CONF

    exec(compile(config.read_text(encoding="utf-8"), str(config), "exec"),
         {"config_opts": config_opts, "include": include})
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.read_file(io.StringIO(config_opts["dnf.conf"]))
    return {"included": included, "opts": config_opts, "repos": parser}


@pytest.fixture(params=CONFIGS, ids=lambda p: p.name)
def evaluated(request):
    return evaluate(request.param)


def test_the_base_is_fedora_44_not_rawhide(evaluated):
    included = evaluated["included"]
    assert len(included) == 1, included
    assert "fedora-44-" in included[0], included
    assert "rawhide" not in included[0], (
        "the buildroot template is Rawhide again -- its glibc 2.44 / openssl 4 / "
        "python 3.15 ABI leaks into every RPM built here, none of which "
        "Hummingbird (2.43 / 3.5 / 3.14) can install"
    )


def test_no_rawhide_repository_remains(evaluated):
    text = "\n".join(
        f"{s} {dict(evaluated['repos'][s])}" for s in evaluated["repos"].sections()
    )
    assert "rawhide" not in text.lower(), text


def test_the_interpreter_pins_are_gone(evaluated):
    """Each pin said 'remove when Hummingbird catches up with Rawhide'.  It was
    never going to; Fedora 44 as the base makes them the base."""
    for section in evaluated["repos"].sections():
        assert not section.startswith("fedora-44-"), (
            f"[{section}] is an includepkgs pin against a Rawhide base that no "
            "longer exists; Fedora 44 supplies that namespace natively now"
        )
        assert "includepkgs" not in evaluated["repos"][section], section


def test_fedora_is_rewritten_in_place_with_a_priority_below_hummingbird(evaluated):
    repos = evaluated["repos"]
    for section in ("fedora", "updates"):
        assert section in repos, f"template section [{section}] lost in the rewrite"
        assert repos.getint(section, "priority") == 50, dict(repos[section])
        assert repos.get(section, "metadata_expire") == "6h", (
            "F44 is frozen; metadata_expire=0 re-fetches a 67k-package index per build"
        )
        assert "ruby-default-gems" in repos.get(section, "excludepkgs", fallback=""), (
            "Fedora's ruby-default-gems and Hummingbird's ruby4.0-default-gems "
            "provide the same capability under different names; the by-name "
            "priority filter cannot arbitrate, so Fedora's must be excluded"
        )
        # the template's own keys survive the rewrite
        assert repos.get(section, "metalink"), dict(repos[section])


def test_priorities_order_local_hummingbird_utah_ours_fedora(evaluated):
    repos = evaluated["repos"]
    local = repos.getint("local-build", "priority")
    base = repos.getint("hummingbird", "priority")
    utah = repos.getint("utah", "priority")
    ours = repos.getint("tunaos-hummingbird", "priority")
    fedora = repos.getint("fedora", "priority")
    assert local < base < utah < ours < fedora, (local, base, utah, ours, fedora)
    assert base == 10 and utah == 11 and ours == 12, (
        "utah wins ties so a stale factory RPM can never shadow the build "
        "utah actually did; the golang and factory-output tests pin base/ours"
    )


def test_utah_repo_is_a_pinned_file_repo(evaluated):
    repos = evaluated["repos"]
    assert repos.get("utah", "baseurl") == "file:///run/utah-repo/repository", (
        "utah publishes no HTTP baseurl -- the chain materialises the OCI "
        "image and bind-mounts it here (see the config comment)"
    )
    assert repos.get("utah", "gpgcheck") == "0"
    assert repos.get("utah", "enabled") == "1"
    assert repos.get("utah", "metadata_expire", fallback=None) is None, (
        "file:// repos must not cache metadata "
        "(test_static_repos_are_not_refetched_per_package pins this)"
    )


def test_utah_repo_is_bind_mounted(evaluated):
    dirs = evaluated["opts"]["plugin_conf"]["bind_mount_opts"]["dirs"]
    assert ("/run/utah-repo", "/run/utah-repo") in dirs


def test_the_rewrite_fails_closed_when_the_template_changes_shape(tmp_path):
    """If mock's template stops emitting [fedora]/[updates] as bare headers,
    the priority rewrite must stop the build rather than silently leave
    Fedora at priority 99, where it would win nothing and lose nothing --
    the misconfiguration this file exists to catch."""
    config_opts = {"dnf.conf": "", "plugin_conf": {"bind_mount_enable": False, "bind_mount_opts": {"dirs": []}}}

    def include(path):
        config_opts["dnf.conf"] = "[main]\nkeepcache=1\n[fedora-44]\nname=x\n"

    with pytest.raises(RuntimeError, match=r"\[fedora\] not found"):
        exec(compile(CONFIGS[0].read_text(encoding="utf-8"), str(CONFIGS[0]), "exec"),
             {"config_opts": config_opts, "include": include})


def test_the_reason_travels_with_the_config():
    """A base change with no citation reads as a preference and invites being
    'corrected' back.  The config must carry the measurement."""
    for config in CONFIGS:
        text = config.read_text(encoding="utf-8")
        assert "GLIBC_2.44" in text, f"{config.name}: does not name the leaked symbol"
        assert "dacef1b3b4c9" in text, f"{config.name}: does not cite the index it was measured on"
        assert "images/variables.yml" in text, (
            f"{config.name}: does not cite Hummingbird's own build-root composition"
        )
        assert "HUMMINGBIRD-TARGET.md" in text


def test_the_two_arch_configs_differ_only_in_arch():
    """One buildroot definition, two architectures.  The measurement in the
    header cites the x86_64 index in both files on purpose -- it is the one
    that was measured -- so only the include and the root name may differ."""
    def normalise(path: pathlib.Path) -> str:
        text = path.read_text(encoding="utf-8")
        text = re.sub(r"include\('/etc/mock/fedora-44-(x86_64|aarch64)\.cfg'\)", "include(ARCH)", text)
        return re.sub(r"config_opts\['root'\] = 'hummingbird-ci(-aarch64)?'", "root=ARCH", text)
    assert normalise(CONFIGS[0]) == normalise(CONFIGS[1]), (
        "the aarch64 config drifted from the x86_64 one beyond the include and root name"
    )
