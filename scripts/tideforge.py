#!/usr/bin/env python3
"""Tideforge: render one TunaOS recipe into native package metadata.

This deliberately owns the repetitive packaging boilerplate.  Recipes retain
small target-specific dependency overrides, because distro package names and
toolchain availability are real compatibility constraints.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]
TARGETS = ROOT / "manifests" / "package-factory.yaml"
VALID_BUILD_SYSTEMS = {"meson", "autotools", "cmake", "cargo", "go", "data", "custom", "python"}
DIST_GIT_RAW_REF = re.compile(r"https://src\.fedoraproject\.org/rpms/[^/]+/raw/([^/]+)/f/")


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def load_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        fail(f"{path}: expected a mapping")
    return data


def load_targets() -> dict:
    return load_yaml(TARGETS)["targets"]


def load_dependency_catalog() -> dict:
    return load_yaml(TARGETS).get("dependency_catalog", {})


def resolve_capabilities(capabilities: list[str], target: str) -> list[str]:
    catalog = load_dependency_catalog()
    packages: list[str] = []
    for capability in capabilities:
        if capability not in catalog:
            fail(f"unknown dependency capability: {capability}")
        target_packages = catalog[capability].get(target)
        if not isinstance(target_packages, list) or not target_packages:
            fail(f"dependency capability {capability} has no mapping for {target}")
        packages.extend(target_packages)
    return packages


def implied_capabilities(recipe: dict) -> list[str]:
    """Capabilities a recipe's build settings require without naming them.

    `build.cmake_generator: Ninja` makes ninja a build-time requirement on
    every target, but nothing made that automatic: each target list had to
    remember it by hand, and openSUSE's did not (#478). Its cells installed
    no ninja, configured a Ninja tree anyway, and died in %cmake_build.
    Deriving the dependency from the setting that causes it stops the two
    drifting apart again -- and the catalog supplies the per-distro spelling
    (`ninja-build` on EL and Debian, `ninja` on openSUSE and Arch), which is
    the other half of what each list had to get right by hand.
    """
    if recipe.get("build_system") != "cmake":
        return []
    return ["ninja"] if recipe.get("build", {}).get("cmake_generator") == "Ninja" else []


def deduplicate(names: list[str]) -> list[str]:
    """Drop repeats, keeping first occurrence.

    An implied capability can name a package a target list already carries
    explicitly. Emitting it twice is harmless to every package manager here
    but noisy in the rendered spec/control/PKGBUILD, and a duplicated
    Build-Depends reads like a mistake to anyone auditing one.
    """
    seen: set[str] = set()
    return [name for name in names if not (name in seen or seen.add(name))]


def target_dependencies(recipe: dict, target: str) -> list[str]:
    build = recipe.get("dependencies", {}).get("build", {})
    capabilities = list(build.get("capabilities", [])) + implied_capabilities(recipe)
    return deduplicate(
        list(build.get("common", []))
        + resolve_capabilities(capabilities, target)
        + list(build.get("targets", {}).get(target, []))
    )


def target_runtime_dependencies(recipe: dict, target: str) -> list[str]:
    runtime = recipe.get("dependencies", {}).get("runtime", {})
    return list(runtime.get("common", [])) + resolve_capabilities(list(runtime.get("capabilities", [])), target) + list(runtime.get("targets", {}).get(target, []))


def provides_entries(recipe: dict) -> list[str]:
    """Names this recipe's artifact also provides.

    A recipe may declare `provides:` to alias an upstream name that runtime
    Requires are written against -- cosmic-icon-theme provides `cosmic-icons`
    (#169). Rendered into the RPM spec, the deb control file, and the Arch
    PKGBUILD so the alias holds on every format.
    """
    provides = recipe.get("provides") or []
    if isinstance(provides, str):
        provides = [provides]
    return [str(item) for item in provides]


def generated_file_content_argument(content: str) -> str:
    """Return a declared file's content as one-line `printf` arguments.

    Quoting the content as a single literal embeds real newlines in the emitted
    command. RPM's %install and Arch's package() are shell scripts, so that
    worked there, but every LINE of a debian/rules recipe is its own /bin/sh
    invocation: the first line arrived as `printf %s 'prefix=/usr` and the deb
    build died with "Syntax error: Unterminated quoted string" (wayland-protocols
    on ubuntu and debian). One argument per line keeps the command on a single
    line for all three renderers while writing byte-identical content.
    """
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return " ".join(shlex.quote(line) for line in lines)


def install_commands(recipe: dict, destination_root: str, *, make_escape: bool = False) -> str:
    commands: list[str] = []
    for item in recipe.get("install", {}).get("files", []):
        commands.append(f"install -Dm{item.get('mode', '0644')} {item['source']} {destination_root}/{item['destination']}")
    for item in recipe.get("install", {}).get("generated_files", []):
        destination = f"{destination_root}/{item['destination']}"
        commands.append(f"install -d {Path(destination).parent}")
        commands.append(f"printf '%s\\n' {generated_file_content_argument(item['content'])} > {destination}")
        commands.append(f"chmod {item.get('mode', '0644')} {destination}")
    rendered = "\n".join(commands)
    # debian/rules is a Makefile, so make expands $ before /bin/sh ever sees it:
    # the `${prefix}` and `${pc_sysrootdir}` references in a generated pkg-config
    # file would reach the shell empty. Doubling restores the literal $.
    return rendered.replace("$", "$$") if make_escape else rendered


def install_directories(recipe: dict, destination_root: str, *, exclude_generated_debian: bool = False) -> str:
    commands: list[str] = []
    for item in recipe.get("install", {}).get("directories", []):
        commands.append(f"install -d {destination_root}/{item['destination']}")
        # Debian's package staging directory lives below the unpacked source.
        # A release archive rooted at `.` would otherwise recursively copy that
        # generated directory into its own destination during dh_auto_install.
        if exclude_generated_debian and item["source"] == ".":
            commands.append(
                f'for entry in ./* ./.??*; do [ "$$entry" = "./debian" ] && continue; '
                f'[ -e "$$entry" ] || continue; cp -a "$$entry" {destination_root}/{item["destination"]}/; done'
            )
        else:
            commands.append(f"cp -a {item['source']}/. {destination_root}/{item['destination']}/")
    return "\n".join(commands)


def build_option(recipe: dict, option: str, default: str) -> str:
    return str(recipe.get("build", {}).get(option, default))


def cmake_options(recipe: dict) -> str:
    options = recipe.get("build", {}).get("cmake_options", [])
    if not isinstance(options, list) or not all(isinstance(option, str) and option.startswith("-D") for option in options):
        fail("build.cmake_options must be a list of CMake -D options")
    return " ".join(options)


def meson_options(recipe: dict) -> str:
    options = recipe.get("build", {}).get("meson_options", [])
    if not isinstance(options, list) or not all(isinstance(option, str) and option.startswith("-D") for option in options):
        fail("build.meson_options must be a list of Meson -D options")
    return " ".join(options)


def cmake_generator(recipe: dict) -> str:
    generator = recipe.get("build", {}).get("cmake_generator", "")
    if generator not in {"", "Ninja"}:
        fail("build.cmake_generator must be Ninja when set")
    return f"-G {generator}" if generator else ""


def debug_package_enabled(recipe: dict) -> bool:
    enabled = recipe.get("build", {}).get("debug_package", True)
    if not isinstance(enabled, bool):
        fail("build.debug_package must be a boolean")
    return enabled


def autoreconf_enabled(recipe: dict) -> bool:
    enabled = recipe.get("build", {}).get("autoreconf", False)
    if not isinstance(enabled, bool):
        fail("build.autoreconf must be a boolean")
    return enabled


def configure_options(recipe: dict) -> str:
    options = recipe.get("build", {}).get("configure_options", [])
    if not isinstance(options, list) or not all(isinstance(option, str) and option.startswith("--") for option in options):
        fail("build.configure_options must be a list of configure -- options")
    return " ".join(options)


def cargo_options(recipe: dict) -> tuple[str, str, str]:
    """Return the Cargo workspace directory, package selector, and binary."""
    return (
        build_option(recipe, "working_directory", "."),
        build_option(recipe, "cargo_package", ""),
        build_option(recipe, "binary", recipe["name"]),
    )


def cargo_lock_flag(recipe: dict) -> str:
    """Return Cargo's lockfile enforcement flag for a recipe.

    Source releases should contain a lockfile that matches their manifest. A
    small number of upstream archives have only their root package version out
    of sync; those must declare an explicit reason before Tideforge permits
    Cargo to repair that metadata while retaining the pinned dependency set.
    """
    locked = recipe.get("build", {}).get("cargo_locked", True)
    if not isinstance(locked, bool):
        fail("build.cargo_locked must be a boolean")
    if not locked:
        reason = recipe.get("build", {}).get("cargo_lock_reason", "")
        if not isinstance(reason, str) or not reason.strip():
            fail("build.cargo_locked=false requires build.cargo_lock_reason")
        return ""
    return " --locked"


def cargo_build_flags(recipe: dict) -> str:
    """Return validated feature and offline flags for a Cargo source closure."""
    build = recipe.get("build", {})
    features = build.get("cargo_features", [])
    if not isinstance(features, list) or not all(isinstance(feature, str) and re.fullmatch(r"[A-Za-z0-9_./-]+", feature) for feature in features):
        fail("build.cargo_features must be a list of Cargo feature names")
    no_default = build.get("cargo_no_default_features", False)
    offline = build.get("cargo_offline", False)
    if not isinstance(no_default, bool) or not isinstance(offline, bool):
        fail("build.cargo_no_default_features and build.cargo_offline must be booleans")
    return (
        (" --no-default-features" if no_default else "")
        + (f" --features {shlex.quote(','.join(features))}" if features else "")
        + (" --offline" if offline else "")
    )


def cargo_config_commands(recipe: dict) -> str:
    """Render a reviewed Cargo source-replacement configuration when needed."""
    config = recipe.get("build", {}).get("cargo_config", "")
    if not isinstance(config, str):
        fail("build.cargo_config must be a string")
    if not config:
        return ""
    return f"mkdir -p .cargo\nprintf %s {shlex.quote(config)} > .cargo/config.toml"


def prepare_commands(recipe: dict) -> str:
    """Return source-root preparation commands declared by a trusted recipe."""
    commands = recipe.get("build", {}).get("prepare", [])
    if not isinstance(commands, list) or not all(isinstance(command, str) and command.strip() for command in commands):
        fail("build.prepare must be a list of non-empty commands")
    return "\n".join(commands)


def custom_commands(recipe: dict, key: str, destination_root: str = "") -> str:
    """Render reviewed commands for source projects with nonstandard tooling.

    Tideforge still owns source provenance, dependencies, and native package
    metadata. This narrow escape hatch is for upstream projects whose install
    contract is legitimately `just`/`make`, rather than pretending they are a
    simple one-binary Cargo package. `{destdir}` is expanded by each renderer.
    """
    commands = recipe.get("build" if key == "build" else "install", {}).get("commands", [])
    if not isinstance(commands, list) or not commands or not all(isinstance(command, str) and command.strip() for command in commands):
        fail(f"{key}.commands must be a non-empty list of commands")
    return "\n".join(command.replace("{destdir}", destination_root) for command in commands)


def go_options(recipe: dict) -> tuple[str, str]:
    """Return validated Go build tag and linker-flag arguments."""
    tags = recipe.get("build", {}).get("go_tags", [])
    if not isinstance(tags, list) or not all(isinstance(tag, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", tag) for tag in tags):
        fail("build.go_tags must be a list of Go build tags")
    ldflags = recipe.get("build", {}).get("go_ldflags", [])
    if not isinstance(ldflags, list) or not all(isinstance(flag, str) and flag for flag in ldflags):
        fail("build.go_ldflags must be a list of non-empty linker flags")
    tag_arg = f" -tags {shlex.quote(','.join(tags))}" if tags else ""
    ldflags_arg = f" -ldflags {shlex.quote(' '.join(ldflags))}" if ldflags else ""
    return tag_arg, ldflags_arg


def go_module_mode(recipe: dict) -> str:
    mode = recipe.get("build", {}).get("go_module_mode", "readonly")
    if mode not in {"readonly", "vendor"}:
        fail("build.go_module_mode must be readonly or vendor")
    return mode


def go_build_command(recipe: dict, binary: str, package: str) -> str:
    """Render the portable Go build invocation for a recipe."""
    tags, ldflags = go_options(recipe)
    return f"go build -buildmode=pie -trimpath -mod={go_module_mode(recipe)}{tags}{ldflags} -o {binary} {package}"


def with_build_environment(recipe: dict, command: str) -> str:
    environment = build_environment(recipe)
    return f"{environment} {command}" if environment else command


def build_environment(recipe: dict) -> str:
    """Return validated shell assignments declared by a recipe.

    This is intentionally small: it covers upstream build workarounds without
    turning a declarative recipe into an arbitrary shell-script escape hatch.
    """
    environment = recipe.get("build", {}).get("environment", {})
    if not isinstance(environment, dict):
        fail("build.environment must be a mapping")
    assignments: list[str] = []
    for name, value in environment.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name):
            fail("build.environment keys must be uppercase shell variable names")
        if not isinstance(value, str):
            fail("build.environment values must be strings")
        assignments.append(f"{name}={shlex.quote(value)}")
    return " ".join(assignments)


def build_environment_exports(recipe: dict) -> str:
    """Render shell exports when a custom build has several commands."""
    environment = recipe.get("build", {}).get("environment", {})
    # Reuse the normal validator before retaining quoted values intact.
    build_environment(recipe)
    return "\n".join(f"export {name}={shlex.quote(value)}" for name, value in environment.items())


def source_entries(recipe: dict) -> list[dict]:
    """Return the primary source followed by pinned auxiliary source trees.

    ``source`` remains the build root for compatibility.  ``sources`` is for
    source closures such as upstream git submodules: every entry is an archive
    with a checksum and an explicit destination below that build root.
    """
    entries = [dict(recipe["source"])]
    extras = recipe.get("sources", [])
    if not isinstance(extras, list):
        fail("sources must be a list")
    entries.extend(dict(item) if isinstance(item, dict) else item for item in extras)
    return entries


def source_filename(source: dict, index: int) -> str:
    filename = source.get("filename")
    if filename is None:
        filename = Path(source["url"].split("?", 1)[0]).name
    if not isinstance(filename, str) or not filename or Path(filename).name != filename:
        fail("source.filename must be a plain filename")
    return filename


def rpm_source_field(source: dict, index: int) -> str:
    # rpmbuild ignores the dist-git `name::url` rename convention: it always
    # resolves a source to its URL basename, both for %{SOURCEn} in %prep and
    # for the SOURCES files it packs into the SRPM. The fetch script writes each
    # source under its `filename:` override, so a source whose override differs
    # from the URL basename is unreachable. Keep the provenance URL only when
    # its basename already matches the fetched filename; otherwise reference the
    # local filename directly so rpm can find it on disk.
    filename = source_filename(source, index)
    url = source["url"]
    if Path(url.split("?", 1)[0]).name == filename:
        return f"{filename}::{url}"
    return filename


def validate_source(source: dict, *, auxiliary: bool) -> None:
    if not isinstance(source, dict) or not source.get("url", "").startswith("https://"):
        fail("source.url must use HTTPS")
    if not re.fullmatch(r"[0-9a-f]{64}", str(source.get("sha256", ""))):
        fail("source.sha256 must be a 64-character lowercase SHA-256")
    # Fedora dist-git serves `raw/<ref>/f/<file>`, and a branch name like
    # `rawhide` is a moving tip: when Fedora rebases the package to a new
    # upstream version the old per-version filename disappears and every recipe
    # pinned to the branch starts 404ing at fetch time. Require an immutable
    # commit id so a recipe's sources stay fetchable for as long as its
    # checksums claim they are.
    ref = DIST_GIT_RAW_REF.match(source["url"])
    if ref and not re.fullmatch(r"[0-9a-f]{40}", ref.group(1)):
        fail(f"dist-git source.url must pin a commit, not the mutable ref {ref.group(1)!r}")
    source_filename(source, 0)
    if auxiliary:
        destination = source.get("destination")
        if not isinstance(destination, str) or not destination or destination.startswith("/") or ".." in Path(destination).parts:
            fail("auxiliary sources require a relative destination")
        extract = source.get("extract", True)
        if not isinstance(extract, bool):
            fail("auxiliary source.extract must be a boolean")
        strip_components = source.get("strip_components", 1)
        if not isinstance(strip_components, int) or strip_components < 0:
            fail("auxiliary source.strip_components must be a non-negative integer")


def validate_deb_packages(recipe: dict) -> None:
    """A split DEB's development half has to declare the runtime half itself.

    RPM's automatic dependency generator follows the unversioned `.so` symlink a
    -devel subpackage ships and derives a soname requirement on the package that
    owns the real library, so the el10 half of the split contract holds without
    the recipe saying anything. dpkg has no equivalent: dpkg-shlibdeps inspects
    ELF objects only, and a symlink is not one, so a rendered -dev package
    carries `${shlibs:Depends}, ${misc:Depends}` and those expand to nothing.
    `apt-get install libxfconf-0-dev` then installed headers with no library and
    no xfconf-query behind them:

        assert-xfconf-split: installing libxfconf-0-dev did not pull in libxfconf-0-4

    Debian expects the relation to be spelled out, so require it here instead of
    letting the next split package rediscover the same silence.
    """
    packages = recipe.get("outputs", {}).get("deb", {}).get("packages", [])
    names = {package.get("name") for package in packages}
    for package in packages:
        depends = package.get("depends", [])
        if not isinstance(depends, list) or not all(isinstance(item, str) for item in depends):
            fail(f"outputs.deb.packages[{package.get('name')}].depends must be a list of Debian relations")
        if len(packages) < 2:
            continue
        if not any(str(path).lstrip("/").startswith("usr/include") for path in package.get("files", [])):
            continue
        declared = {relation.split("(")[0].strip() for relation in depends}
        if not declared & (names - {package.get("name")}):
            fail(
                f"outputs.deb.packages[{package.get('name')}] ships headers but depends on no sibling "
                "package: a -dev half must declare the runtime half, e.g. "
                "depends: [\"<runtime-package> (= ${binary:Version})\"]"
            )


def verify_metadata(recipe: dict, target: str) -> dict[str, str]:
    """Return how this recipe proves itself installed, for one target.

    The gate used to carry `smoke` and `install_name` as hand-written matrix
    keys, which is why a recipe could declare a target that no cell ever built
    (#139): nothing tied the two together. A recipe now states how to verify
    itself, so the cell can be derived rather than remembered.

    `install_name` defaults to the recipe name. A target may override it where
    the rendered binary package genuinely differs -- xfconf produces `xfconf`
    on el10 and `libxfconf-0-4` on ubuntu and debian.
    """
    block = recipe.get("verify") or {}
    resolved = {
        "smoke": block.get("smoke"),
        "install_name": block.get("install_name", recipe["name"]),
    }
    resolved.update(
        {
            key: value
            for key, value in (block.get("targets") or {}).get(target, {}).items()
            if key in {"smoke", "install_name"}
        }
    )
    return resolved


def validate_verify(recipe: dict) -> None:
    """Schema-check the `verify` block.

    Nothing rejected unknown top-level keys, so a mistyped block would simply
    be ignored -- a silently absent assertion, which is the same defect class
    #139 was about. Check it explicitly instead.
    """
    block = recipe.get("verify")
    if block is None:
        return
    if not isinstance(block, dict):
        fail("verify must be a mapping")
    unknown = set(block) - {"smoke", "install_name", "targets"}
    if unknown:
        fail(f"unknown verify key(s): {sorted(unknown)}")
    if not isinstance(block.get("smoke", ""), str) or not block.get("smoke", "").strip():
        fail("verify.smoke must be a non-empty string")
    if "install_name" in block and not isinstance(block["install_name"], str):
        fail("verify.install_name must be a string")
    overrides = block.get("targets", {})
    if not isinstance(overrides, dict):
        fail("verify.targets must be a mapping of target to overrides")
    for name, override in overrides.items():
        if name not in recipe["targets"]:
            fail(f"verify.targets names {name!r}, which the recipe does not enable")
        if not isinstance(override, dict):
            fail(f"verify.targets.{name} must be a mapping")
        unknown = set(override) - {"smoke", "install_name"}
        if unknown:
            fail(f"unknown verify.targets.{name} key(s): {sorted(unknown)}")


def validate(recipe: dict, target: str | None = None) -> None:
    if recipe.get("schema") != 1:
        fail("schema must be 1")
    for field in ("name", "version", "summary", "description", "license", "source", "build_system", "files", "targets"):
        if not recipe.get(field):
            fail(f"{field} is required")
    if not re.fullmatch(r"[a-z0-9][a-z0-9+._-]*", str(recipe["name"])):
        fail("name must be a lowercase package identifier")
    for index, source in enumerate(source_entries(recipe)):
        validate_source(source, auxiliary=index > 0)
    if recipe["build_system"] not in VALID_BUILD_SYSTEMS:
        fail(f"build_system must be one of {sorted(VALID_BUILD_SYSTEMS)}")
    if recipe["build_system"] == "cmake":
        cmake_options(recipe)
        cmake_generator(recipe)
    if recipe["build_system"] == "meson":
        meson_options(recipe)
    if recipe["build_system"] == "cargo":
        cargo_lock_flag(recipe)
        cargo_build_flags(recipe)
        cargo_config_commands(recipe)
    if recipe["build_system"] == "go":
        go_options(recipe)
        go_module_mode(recipe)
    if recipe["build_system"] == "custom":
        custom_commands(recipe, "build")
        custom_commands(recipe, "install", "{destdir}")
        cargo_config_commands(recipe)
    prepare_commands(recipe)
    build_environment(recipe)
    validate_verify(recipe)
    debug_package_enabled(recipe)
    autoreconf_enabled(recipe)
    configure_options(recipe)
    targets = load_targets()
    requested = recipe["targets"]
    if not isinstance(requested, list) or not requested:
        fail("targets must be a non-empty list")
    for item in requested:
        if item not in targets:
            fail(f"unknown target: {item}")
    if target and target not in requested:
        fail(f"recipe does not enable target: {target}")
    for dependency_kind in ("build", "runtime"):
        dependency_data = recipe.get("dependencies", {}).get(dependency_kind, {})
        capabilities = dependency_data.get("capabilities", [])
        if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
            fail(f"dependencies.{dependency_kind}.capabilities must be a list of capability names")
        for requested_target in requested:
            resolve_capabilities(capabilities, requested_target)
    if not recipe["files"].get("common"):
        fail("files.common must list installed paths")
    validate_deb_packages(recipe)
    for item in recipe.get("install", {}).get("files", []) + recipe.get("install", {}).get("directories", []):
        if not isinstance(item, dict) or not isinstance(item.get("source"), str) or not isinstance(item.get("destination"), str):
            fail("install.files entries need source and destination")
        if item["source"].startswith("/") or item["destination"].startswith("/") or ".." in Path(item["source"]).parts or ".." in Path(item["destination"]).parts:
            fail("install.files paths must stay relative")
    for item in recipe.get("install", {}).get("generated_files", []):
        if not isinstance(item, dict) or not isinstance(item.get("destination"), str) or not isinstance(item.get("content"), str):
            fail("install.generated_files entries need destination and content")
        if item["destination"].startswith("/") or ".." in Path(item["destination"]).parts:
            fail("install.generated_files destinations must stay relative")


def rpm_build_lines(build_system: str, recipe: dict | None = None) -> tuple[str, str]:
    if build_system == "meson":
        options = meson_options(recipe or {})
        return f"%meson {options}\n%meson_build".rstrip(), "%meson_install"
    if build_system == "autotools":
        # build.environment was wired into the cargo and go paths only, so an
        # autotools recipe had no way to influence its own link line:
        # configure_options rejects anything not starting with "--", and
        # nothing prefixed %configure. libunwind needed LIBS=-lgcc_s and could
        # express it nowhere (#469), shipping an aarch64 library with four
        # undefined outline-atomics symbols.
        #
        # Exported on their own lines rather than prefixed onto %configure:
        # that macro expands to a multi-line script, so `VAR=x %configure`
        # would apply the assignment to its first command only.
        exports = build_environment_exports(recipe or {})
        prefix = f"{exports}\n" if exports else ""
        prefix += "autoreconf -fi\n" if autoreconf_enabled(recipe or {}) else ""
        options = configure_options(recipe or {})
        # Delete libtool archives explicitly. EL10's rpm strips them in
        # brp-remove-la-files, so the el10 gate never sees them; openSUSE's
        # rpm keeps them and fails the build with "Installed (but unpackaged)
        # file(s) found: *.la" -- exactly how the libunwind-devel Tumbleweed
        # cell died on its first run.
        return (
            f"{prefix}%configure {options}\n%make_build".rstrip(),
            "%make_install\nfind %{buildroot} -type f -name '*.la' -delete",
        )
    if build_system == "cargo":
        return "%cargo_build", "%cargo_install"
    if build_system == "go":
        return go_build_command(recipe or {}, "%{name}", "."), "install -Dm0755 %{name} %{buildroot}%{_bindir}/%{name}"
    if build_system == "data":
        return ":", ":"
    if build_system == "python":
        return "%pyproject_wheel", "%pyproject_install"
    if build_system == "custom":
        return custom_commands(recipe or {}, "build"), custom_commands(recipe or {}, "install", "%{buildroot}")
    options = " ".join(filter(None, [cmake_generator(recipe or {}), cmake_options(recipe or {})]))
    return f"%cmake {options}\n%cmake_build".rstrip(), "%cmake_install"


def ships_a_shared_library(paths: list[str]) -> bool:
    """Whether a file list installs a versioned shared library.

    Matches `libfoo.so.1`, `libfoo.so.1.0.4` and the globs recipes write for
    them (`usr/lib64/libcpptrace.so*`), while ignoring a bare `libfoo.so`
    development symlink on its own -- that alone needs no ldconfig.
    """
    for path in paths:
        name = path.rsplit("/", 1)[-1]
        if ".so" not in name:
            continue
        tail = name.split(".so", 1)[1]
        if tail.startswith(".") or tail.startswith("*"):
            return True
    return False


def rpm_ldconfig_scriptlets(recipe: dict) -> str:
    """`%post`/`%postun` ldconfig for every RPM subpackage shipping a library.

    Fedora and EL do not need these: their glibc carries RPM FILE TRIGGERS
    that run ldconfig for anything landing in a library directory, so a spec
    omitting them still ends up with a correct cache. openSUSE has no such
    trigger. cpptrace-devel installed cleanly on Tumbleweed and then failed
    its own smoke contract:

        ldconfig -p | grep -F libcpptrace.so.1     -> no match, exit 1

    while both el10 cells passed the identical assertion. rpmlint had been
    reporting the cause on every openSUSE build all along:

        E: library-without-ldconfig-postin  /usr/lib64/libcpptrace.so.1.0.4
        E: library-without-ldconfig-postun  /usr/lib64/libcpptrace.so.1.0.4

    Written as `%post -p /sbin/ldconfig` rather than Fedora's
    `%ldconfig_scriptlets`, because that macro is not defined on openSUSE --
    using it would leave the literal text in the spec on the one distro that
    actually needs the scriptlet.
    """
    rpm_output = recipe.get("outputs", {}).get("rpm", {})
    blocks: list[str] = []
    if ships_a_shared_library(list(rpm_output.get("files", recipe["files"]["common"]))):
        blocks.append("%post -p /sbin/ldconfig")
        blocks.append("%postun -p /sbin/ldconfig")
    for subpackage in rpm_output.get("subpackages", []):
        if ships_a_shared_library(list(subpackage.get("files", []))):
            blocks.append(f"%post {subpackage['name']} -p /sbin/ldconfig")
            blocks.append(f"%postun {subpackage['name']} -p /sbin/ldconfig")
    return "\n".join(blocks)


def rpm_subpackage_block(subpackage: dict) -> str:
    # A -devel (or similarly split-out) subpackage ships only the unversioned
    # .so symlink, headers, and pkg-config metadata; the runtime library and any
    # daemons live in the main package. Installing the subpackage alone is
    # therefore useless -- and the supported-target smoke test proved it: pulling
    # in libseat-devel by itself left `seatd` absent, so `seatd -h` exited 127.
    # Emit any recipe-declared `requires` (e.g. the main package with %{?_isa})
    # so the dependency closure resolves the way RPM convention expects.
    header = [f"%package {subpackage['name']}", f"Summary: {subpackage['summary']}"]
    for dependency in subpackage.get("requires", []):
        header.append(f"Requires: {dependency}")
    header.append("")
    header.append(f"%description {subpackage['name']}")
    header.append(subpackage.get("description", subpackage["summary"]))
    header.append("")
    return "\n".join(header)


def render_rpm(recipe: dict, target: str) -> dict[str, str]:
    build, install = rpm_build_lines(recipe["build_system"], recipe)
    prepare = prepare_commands(recipe)
    if prepare:
        build = f"{prepare}\n{build}"
    if recipe["build_system"] == "go":
        workdir = build_option(recipe, "working_directory", ".")
        binary = build_option(recipe, "binary", recipe["name"])
        package = build_option(recipe, "go_package", ".")
        build = f"{prepare + chr(10) if prepare else ''}cd {workdir}\n{with_build_environment(recipe, go_build_command(recipe, binary, package))}"
        install = f"install -Dm0755 {workdir}/{binary} %{{buildroot}}%{{_bindir}}/{binary}"
    elif recipe["build_system"] == "cargo":
        workdir, cargo_package, binary = cargo_options(recipe)
        selector = f" --package {cargo_package}" if cargo_package else ""
        environment = " ".join(filter(None, [build_environment(recipe), "CARGO_PROFILE_RELEASE_DEBUG=1"]))
        prelude = "\n".join(filter(None, [prepare_commands(recipe), cargo_config_commands(recipe)]))
        build = f"cd {workdir}\n{prelude + chr(10) if prelude else ''}{environment} cargo build --release{cargo_lock_flag(recipe)}{cargo_build_flags(recipe)}{selector}"
        install = f"install -Dm0755 {workdir}/target/release/{binary} %{{buildroot}}%{{_bindir}}/{binary}"
    elif recipe["build_system"] == "custom":
        build = "\n".join(filter(None, [prepare_commands(recipe), build_environment_exports(recipe), cargo_config_commands(recipe), custom_commands(recipe, "build")]))
        install = custom_commands(recipe, "install", "%{buildroot}")
    requires = "\n".join(f"BuildRequires: {dep}" for dep in target_dependencies(recipe, target))
    runtime_requires = "\n".join(f"Requires:       {dep}" for dep in target_runtime_dependencies(recipe, target))
    provides = "\n".join(f"Provides:       {dep}" for dep in provides_entries(recipe))
    rpm_output = recipe.get("outputs", {}).get("rpm", {})
    files = "\n".join(f"/{path.lstrip('/')}" for path in rpm_output.get("files", recipe["files"]["common"]))
    subpackage_definitions = "\n".join(
        rpm_subpackage_block(subpackage)
        for subpackage in rpm_output.get("subpackages", [])
    )
    subpackage_files = "\n".join(
        f"%files {subpackage['name']}\n" + "\n".join(f"/{path.lstrip('/')}" for path in subpackage["files"]) + "\n"
        for subpackage in rpm_output.get("subpackages", [])
    )
    source_directory = recipe["source"].get("directory", f"%{{name}}-%{{version}}")
    # Release assets sometimes contain files directly at archive root rather
    # than a conventional name-version directory.  RPM's %autosetup cannot
    # safely use `-n .` (it attempts `rm -rf .`).  Create an isolated build
    # directory before unpacking such sources instead.
    prep = (
        "%setup -q -c -n %{name}-%{version}"
        if source_directory == "."
        else f"%autosetup -n {source_directory}"
    )
    auxiliary_sources = source_entries(recipe)[1:]
    if auxiliary_sources:
        unpack_auxiliary = "\n".join(
            (
                f"mkdir -p {shlex.quote(source['destination'])}\n"
                f"tar --extract --file %{{SOURCE{index}}} --strip-components={source.get('strip_components', 1)} --directory {shlex.quote(source['destination'])}"
                if source.get("extract", True)
                else f"install -Dm0644 %{{SOURCE{index}}} {shlex.quote(source['destination'])}"
            )
            for index, source in enumerate(auxiliary_sources, start=1)
        )
        prep = f"{prep}\n{unpack_auxiliary}"
    extra_install = "\n".join(filter(None, [install_commands(recipe, "%{buildroot}"), install_directories(recipe, "%{buildroot}")]))
    # Tideforge's Go and data renderers do not produce RPM-compatible
    # debug-source payloads, and custom builds compile through opaque upstream
    # tooling (e.g. `just`/`cargo build --release`) that tideforge cannot force
    # to retain debuginfo -- an automatic debug package would be empty and abort
    # rpmbuild with "Empty %files file debugsourcefiles.list". Cargo builds
    # retain debuginfo so native RPM debug packages can be generated normally.
    rpm_preamble = ""
    if recipe["build_system"] in {"go", "data", "custom", "python"} or not debug_package_enabled(recipe):
        rpm_preamble = "%global debug_package %{nil}\n"
    # openSUSE derives the CMake generator FROM %__builder rather than from
    # anything the spec passes: its %cmake emits -G"Unix Makefiles" unless
    # %__builder differs from %__make, and its %cmake_build expands to plain
    # `%__builder ... %{?_smp_mflags}`. A recipe asking for Ninja therefore
    # got a Ninja tree (our -G wins, being last) that %cmake_build then drove
    # with make -- "No targets specified and no makefile found. Stop." (#478).
    #
    # Setting %__builder fixes the whole chain at once: %cmake emits -GNinja
    # itself, %__builder_verbose becomes -v, %cmake_build runs `ninja -v`, and
    # %cmake_install runs `ninja install -C build`.
    #
    # Emitted for every RPM target rather than gated on the target name.
    # Fedora and EL never read %__builder -- zero references in both
    # cmake's macros.cmake.in and redhat-rpm-config/macros, checked against
    # rawhide -- so it is inert there, and a mechanism-driven emit cannot
    # regress the way a name list would when the next openSUSE-family target
    # is added. %__ninja is the sanctioned spelling and both distributions'
    # ninja packages define it; implied_capabilities guarantees one is
    # installed whenever this line is emitted.
    if recipe["build_system"] == "cmake" and cmake_generator(recipe):
        rpm_preamble += "%global __builder %__ninja\n"
    ldconfig_scriptlets = rpm_ldconfig_scriptlets(recipe)
    auxiliary_sources_str = "".join(f"Source{index}:        {rpm_source_field(source, index)}\n" for index, source in enumerate(auxiliary_sources, start=1))
    spec = f"""{rpm_preamble}Name:           {recipe['name']}
Version:        {recipe['version']}
Release:        {recipe.get('release', 1)}%{{?dist}}
Summary:        {recipe['summary']}
License:        {recipe['license']}
Source0:        {rpm_source_field(recipe['source'], 0)}
{auxiliary_sources_str}{requires}
{runtime_requires}
{provides}

%description
{recipe['description']}

{subpackage_definitions}

%prep
{prep}

%build
{build}

%install
{install}
{extra_install}

{ldconfig_scriptlets}

%files
{files}

{subpackage_files}

%changelog
* Sat Jul 25 2026 TunaOS Package Factory <packages@tunaos.org> - {recipe['version']}-{recipe.get('release', 1)}
- Generated from package.yaml
"""
    return {f"{recipe['name']}.spec": spec}


def render_deb(recipe: dict, target: str) -> dict[str, str]:
    # The Build-Depends line below always emits "debhelper-compat (= 13)", so a
    # recipe that also lists a bare "debhelper-compat" produces the relation
    # twice — once versioned, once not. dh reads the UNVERSIONED one and aborts
    # the build before it starts:
    #   dh: error: Could not parse desired debhelper compat level from
    #   relation: debhelper-compat
    # Dropped here rather than only in the recipes because packages/_template
    # carries the same entry, so every new recipe would inherit the bug.
    build_deps = ", ".join(
        dependency
        for dependency in target_dependencies(recipe, target)
        if dependency.split("(")[0].strip() != "debhelper-compat"
    )
    deb_output = recipe.get("outputs", {}).get("deb", {})
    binary_packages = deb_output.get("packages", [{"name": recipe["name"], "summary": recipe["summary"], "description": recipe["description"], "files": recipe["files"]["common"]}])
    recipe_runtime_dependencies = target_runtime_dependencies(recipe, target)
    provides_field = "".join(f"Provides: {provided}\n" for provided in provides_entries(recipe))
    package_stanzas = "\n".join(
        f"""Package: {package['name']}
Architecture: any
Depends: {', '.join(['${shlibs:Depends}', '${misc:Depends}', *recipe_runtime_dependencies, *package.get('depends', [])])}
{provides_field}Description: {package.get('summary', recipe['summary'])}
 {package.get('description', recipe['description'])}
"""
        for package in binary_packages
    )
    control = f"""Source: {recipe['name']}
Section: misc
Priority: optional
Maintainer: TunaOS Package Factory <packages@tunaos.org>
Build-Depends: debhelper-compat (= 13){', ' if build_deps else ''}{build_deps}
Standards-Version: 4.7.0
Rules-Requires-Root: no

{package_stanzas}
"""
    buildsystem = {"meson": "meson", "autotools": "autoconf", "cmake": "cmake", "python": "pybuild"}.get(recipe["build_system"])
    # debhelper's `cmake` buildsystem runs `make` in dh_auto_build regardless of
    # the generator. Passing -G Ninja through dh_auto_configure therefore wrote
    # build.ninja and then ran make against it, for every deb cell of a recipe
    # that asked for Ninja (run 32556308211):
    #
    #     cd obj-x86_64-linux-gnu && make -j4 ...
    #     make[1]: *** No targets specified and no makefile found.  Stop.
    #
    # cmake+ninja is debhelper's own generator-aware variant: it passes -GNinja
    # at configure time AND builds with ninja. The generator must then NOT be
    # passed by hand as well -- the configure line already carried debhelper's
    # "-GUnix Makefiles" plus our "-G Ninja", and two -G flags is exactly the
    # ambiguity that produced this.
    if recipe["build_system"] == "cmake" and cmake_generator(recipe):
        buildsystem = "cmake+ninja"
    if recipe["build_system"] == "cargo":
        workdir, cargo_package, binary = cargo_options(recipe)
        selector = f" --package {cargo_package}" if cargo_package else ""
        environment = " ".join(filter(None, [build_environment(recipe), "CARGO_PROFILE_RELEASE_DEBUG=1"]))
        prelude = "\n".join(filter(None, [prepare_commands(recipe), cargo_config_commands(recipe)]))
        prelude = "\n".join(f"\t{command}" for command in prelude.splitlines())
        if prelude:
            prelude += "\n"
        rules = f"#!/usr/bin/make -f\n\n%:\n\tdh $@\n\n# dh_clean deletes *.orig as patch cruft, which strips Cargo.toml.orig out\n# of every vendored crate and breaks cargo checksum verification. This\n# override MUST NOT be the last target in the file: render_deb appends the\n# recipe's install.files commands onto whatever target comes last, and when\n# #207 put this at the end, niri and kairpods installed their session files\n# at CLEAN time and shipped binary-only debs (run 30698387751).\noverride_dh_clean:\n\tdh_clean -Xvendor/\n\n# dh_auto_clean auto-detects UPSTREAM build systems: cosmic-comp ships a\n# Makefile whose clean target deletes .cargo/ and vendor/, so the first\n# staged deb build lost its offline-source config between assembly and\n# cargo (run 30714289211; reproduced in a container -- rules clean left\n# .cargo GONE). Every deb build starts from a freshly assembled tree;\n# upstream clean targets must never run.\noverride_dh_auto_clean:\n\t:\n\noverride_dh_auto_build:\n\tcd {workdir} && :\n{prelude}\tcd {workdir} && {environment} cargo build --release{cargo_lock_flag(recipe)}{cargo_build_flags(recipe)}{selector}\n\noverride_dh_auto_install:\n\tinstall -Dm0755 {workdir}/target/release/{binary} debian/{recipe['name']}/usr/bin/{binary}\n\noverride_dh_dwz:\n\t:\n"
    elif recipe["build_system"] == "custom":
        build = "\n".join(filter(None, [prepare_commands(recipe), build_environment_exports(recipe), cargo_config_commands(recipe), custom_commands(recipe, "build")]))
        install = custom_commands(recipe, "install", f"debian/{recipe['name']}")
        rules = f"#!/usr/bin/make -f\n\n%:\n\tdh $@\n\n# dh_clean deletes *.orig as patch cruft, which strips Cargo.toml.orig out\n# of every vendored crate and breaks cargo checksum verification. This\n# override MUST NOT be the last target in the file: render_deb appends the\n# recipe's install.files commands onto whatever target comes last, and when\n# #207 put this at the end, niri and kairpods installed their session files\n# at CLEAN time and shipped binary-only debs (run 30698387751).\noverride_dh_clean:\n\tdh_clean -Xvendor/\n\n# dh_auto_clean auto-detects UPSTREAM build systems: cosmic-comp ships a\n# Makefile whose clean target deletes .cargo/ and vendor/, so the first\n# staged deb build lost its offline-source config between assembly and\n# cargo (run 30714289211; reproduced in a container -- rules clean left\n# .cargo GONE). Every deb build starts from a freshly assembled tree;\n# upstream clean targets must never run.\noverride_dh_auto_clean:\n\t:\n\noverride_dh_auto_build:\n\t{build.replace(chr(10), chr(10) + chr(9))}\n\noverride_dh_auto_install:\n\t{install.replace(chr(10), chr(10) + chr(9))}\n"
    elif recipe["build_system"] == "go":
        workdir = build_option(recipe, "working_directory", ".")
        binary = build_option(recipe, "binary", recipe["name"])
        package = build_option(recipe, "go_package", ".")
        prepare = prepare_commands(recipe)
        prelude = "\n".join(f"\t{command}" for command in prepare.splitlines())
        if prelude:
            prelude += "\n"
        rules = f"#!/usr/bin/make -f\n\n%:\n\tdh $@\n\noverride_dh_auto_build:\n{prelude}\tcd {workdir} && {with_build_environment(recipe, go_build_command(recipe, binary, package))}\n\noverride_dh_auto_install:\n\tinstall -Dm0755 {workdir}/{binary} debian/{recipe['name']}/usr/bin/{binary}\n\noverride_dh_dwz:\n\t:\n"
    elif recipe["build_system"] == "data":
        # --buildsystem=none is load-bearing. A data recipe ships files straight
        # out of the tarball, but plain "dh $@" still AUTO-DETECTS a build system
        # from whatever the upstream source happens to contain. oversteer-udev
        # ships only udev rules, yet upstream carries a meson.build, so debhelper
        # picked meson and ran dh_auto_configure — which this rule set does not
        # override — failing with "meson --version returned exit code 25". The
        # RPM path never hit this because it does not go through debhelper, so
        # the same recipe built on el10 and failed on debian/ubuntu.
        rules = "#!/usr/bin/make -f\n\n%:\n\tdh $@ --buildsystem=none\n\noverride_dh_auto_build:\n\t:\n\noverride_dh_auto_install:\n\t:\n"
    else:
        cmake_configure_options = [cmake_options(recipe)] if buildsystem == "cmake+ninja" else [cmake_generator(recipe), cmake_options(recipe)]
        options = " ".join(filter(None, cmake_configure_options)) if recipe["build_system"] == "cmake" else meson_options(recipe) if recipe["build_system"] == "meson" else ""
        if options:
            configure = f"\noverride_dh_auto_configure:\n\tdh_auto_configure -- {options}\n"
        elif recipe["build_system"] == "autotools" and autoreconf_enabled(recipe):
            configure = f"\noverride_dh_auto_configure:\n\tautoreconf -fi\n\tdh_auto_configure -- {configure_options(recipe)}\n"
        elif recipe["build_system"] == "autotools" and configure_options(recipe):
            configure = f"\noverride_dh_auto_configure:\n\tdh_auto_configure -- {configure_options(recipe)}\n"
        else:
            configure = ""
        rules = f"#!/usr/bin/make -f\n\n%:\n\tdh $@ --buildsystem={buildsystem}\n{configure}"
    extra_install = "\n".join(filter(None, [install_commands(recipe, f"debian/{recipe['name']}", make_escape=True), install_directories(recipe, f"debian/{recipe['name']}", exclude_generated_debian=True)]))
    if extra_install:
        rules = rules.rstrip() + "\n\t" + extra_install.replace("\n", "\n\t") + "\n"
    # For native build systems debhelper auto-runs the upstream test suite via
    # dh_auto_test. Those suites routinely need a session D-Bus, a machine-id, a
    # display, or network -- none of which exist in the minimal build container
    # -- e.g. xfconf's 33 D-Bus integration tests aborted the build with "Cannot
    # spawn a message bus without a machine-id". The RPM path never runs them
    # (tideforge emits no %check), so skip them here too for RPM/DEB parity. The
    # cargo/go/data/custom branches already replace dh_auto_build/install and do
    # not auto-detect a testable build system, so they need no override.
    if recipe["build_system"] in {"meson", "cmake", "autotools", "python"}:
        rules = rules.rstrip() + "\n\noverride_dh_auto_test:\n\t:\n"
    # Delete libtool archives after staging, mirroring the rpm renderer's
    # cleanup after %make_install. A libtool build ships .la files whose
    # dependency_libs lintian flags as a policy error
    # (non-empty-dependency_libs-in-la-file — libunwind-dev carried five of
    # them), and Debian's answer is to not ship .la at all. Only autotools
    # uses libtool; the other build systems never emit them.
    if recipe["build_system"] == "autotools":
        rules = rules.rstrip() + "\n\nexecute_after_dh_auto_install:\n\tfind debian -name '*.la' -delete\n"
    changelog = f"{recipe['name']} ({recipe['version']}-{recipe.get('release', 1)}) {target}; urgency=medium\n\n  * Generated from package.yaml.\n\n -- TunaOS Package Factory <packages@tunaos.org>  Thu, 01 Jan 1970 00:00:00 +0000\n"
    # DEP-5 machine-readable copyright, derived from the recipe. dh_installdocs
    # installs it into every binary package, which is what clears lintian's
    # no-copyright-file error on the generated debs. The full license text
    # lives in the upstream source distribution; the recipe's SPDX identifier
    # names it.
    copyright_file = f"""Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: {recipe['name']}
Source: {recipe['source']['url']}

Files: *
Copyright: the {recipe['name']} upstream authors
License: {recipe['license']}
 See the upstream source distribution for the full license text.
"""
    rendered = {
        "debian/control": control,
        "debian/rules": rules,
        "debian/changelog": changelog,
        "debian/copyright": copyright_file,
        "debian/source/format": "3.0 (quilt)\n",
    }
    # Tideforge's Go, Cargo, and data renderers install directly into
    # debian/<binary-package>.  A .install file would make dh_install search
    # debian/tmp for those same files and fail the package build.  Native
    # build-system packages retain .install metadata for dh_auto_install.
    #
    # debhelper's dh_auto_install only stages into debian/tmp when the source
    # produces MORE THAN ONE binary package; with a single one it installs
    # straight into debian/<that package>.  So a single-package native recipe
    # is in the same position as the direct-install renderers above, and its
    # .install file sends dh_install looking for files that are already in
    # place:
    #   dh_install: warning: Cannot find (any matches for) "usr/bin/ninja"
    #     (tried in ., debian/tmp)
    #   dh_install: error: missing files, aborting
    # ninja-build (cmake, one binary package) failed exactly this way on
    # ubuntu and debian while building fine on el10.  Split recipes such as
    # xfconf and libseat do stage through debian/tmp, so they keep their
    # .install lists to carve it up between the binary packages.
    direct_install = (
        recipe["build_system"] in {"cargo", "go", "data", "custom", "python"}
        or bool(recipe.get("install"))
        or len(binary_packages) == 1
    )
    if not direct_install:
        for package in binary_packages:
            rendered[f"debian/{package['name']}.install"] = "\n".join(package.get("files", recipe["files"]["common"])) + "\n"
    return rendered


def render_pkgbuild(recipe: dict, target: str) -> dict[str, str]:
    """Render an Arch PKGBUILD for the straightforward single-binary case.

    Complex split packages and packaging hooks stay native until the recipe
    schema can model them without hiding Arch-specific behaviour.
    """
    source_directory = recipe["source"].get("directory", f"{recipe['name']}-{recipe['version']}")
    makedepends = " ".join(f"'{dependency}'" for dependency in target_dependencies(recipe, target))
    depends = " ".join(f"'{dependency}'" for dependency in target_runtime_dependencies(recipe, target))
    provides = " ".join(f"'{dependency}'" for dependency in provides_entries(recipe))
    sources = source_entries(recipe)
    source = recipe["source"]["url"]
    if recipe["build_system"] == "cargo":
        workdir, cargo_package, binary = cargo_options(recipe)
        selector = f" --package {cargo_package}" if cargo_package else ""
        environment = " ".join(filter(None, [build_environment(recipe), "CARGO_PROFILE_RELEASE_DEBUG=1"]))
        prelude = "\n  ".join(filter(None, [prepare_commands(recipe), cargo_config_commands(recipe)]))
        # Arch's makepkg enables LTO by default. A Rust crate that compiles C via
        # the cc crate (niri's libspa-sys PipeWire bindings, for example) then
        # emits pure thin-LTO bitcode whose wrapper symbols vanish at the final
        # ld.lld Rust link ("undefined symbol: spa_pod_object_find_prop_libspa_rs").
        # Emitting fat LTO objects keeps real machine code beside the bitcode so
        # those symbols resolve. Inert for pure-Rust crates. Arch's own niri
        # PKGBUILD applies the identical flag.
        cflags = "CFLAGS+=(' -ffat-lto-objects')"
        build = f"cd {workdir}\n  {prelude + chr(10) + '  ' if prelude else ''}{cflags}\n  {environment} cargo build --release{cargo_lock_flag(recipe)}{cargo_build_flags(recipe)}{selector}"
        install = f"install -Dm0755 {workdir}/target/release/{binary} \"$pkgdir/usr/bin/{binary}\""
    elif recipe["build_system"] == "go":
        workdir = build_option(recipe, "working_directory", ".")
        binary = build_option(recipe, "binary", recipe["name"])
        package = build_option(recipe, "go_package", ".")
        prepare = prepare_commands(recipe)
        build = f"{prepare + chr(10) if prepare else ''}cd {workdir}\n  {with_build_environment(recipe, go_build_command(recipe, binary, package))}"
        install = f"install -Dm0755 {workdir}/{binary} \"$pkgdir/usr/bin/{binary}\""
    elif recipe["build_system"] == "data":
        build = ":"
        install = ":"
    elif recipe["build_system"] == "python":
        build = "python -m build --wheel --no-isolation"
        install = 'python -m installer --destdir="$pkgdir" dist/*.whl'
    elif recipe["build_system"] == "custom":
        build = "\n  ".join(filter(None, [prepare_commands(recipe), build_environment_exports(recipe), cargo_config_commands(recipe), custom_commands(recipe, "build")]))
        install = custom_commands(recipe, "install", "$pkgdir")
    elif recipe["build_system"] == "meson":
        build = f"arch-meson build {meson_options(recipe)}\n  meson compile -C build".rstrip()
        install = "DESTDIR=\"$pkgdir\" meson install -C build"
    elif recipe["build_system"] == "autotools":
        prefix = "autoreconf -fi\n  " if autoreconf_enabled(recipe) else ""
        build = f"{prefix}./configure --prefix=/usr {configure_options(recipe)}\n  make".rstrip()
        install = "make DESTDIR=\"$pkgdir\" install"
    else:
        options = " ".join(filter(None, [cmake_generator(recipe), cmake_options(recipe)]))
        build = f"cmake -B build -S . -DCMAKE_INSTALL_PREFIX=/usr {options}\n  cmake --build build".rstrip()
        install = "DESTDIR=\"$pkgdir\" cmake --install build"
    extra_install = "\n".join(filter(None, [install_commands(recipe, "$pkgdir"), install_directories(recipe, "$pkgdir")]))
    if extra_install:
        install = f"{install}\n  {extra_install.replace(chr(10), chr(10) + '  ')}"
    source_lines = "\n".join(
        f"  '{source_filename(item, index)}::{item['url']}'" for index, item in enumerate(sources)
    )
    checksum_lines = "\n".join(f"  '{item['sha256']}'" for item in sources)
    auxiliary_prepare = "\n  ".join(
        (
            f"mkdir -p {shlex.quote(item['destination'])}\n  tar --extract --file \"$srcdir/{source_filename(item, index)}\" --strip-components={item.get('strip_components', 1)} --directory {shlex.quote(item['destination'])}"
            if item.get("extract", True)
            else f"install -Dm0644 \"$srcdir/{source_filename(item, index)}\" {shlex.quote(item['destination'])}"
        )
        for index, item in enumerate(sources[1:], start=1)
    )
    pkgbuild = f"""# Generated by Tideforge; target-specific dependencies remain in package.yaml.
pkgname={recipe['name']}
pkgver={recipe['version']}
pkgrel={recipe.get('release', 1)}
pkgdesc={recipe['summary']!r}
arch=('x86_64' 'aarch64')
url={source!r}
license=({recipe['license']!r})
makedepends=({makedepends})
depends=({depends})
provides=({provides})
source=(
{source_lines}
)
sha256sums=(
{checksum_lines}
)

build() {{
  cd \"$srcdir/{source_directory}\"
  {auxiliary_prepare}
  {build}
}}

package() {{
  cd \"$srcdir/{source_directory}\"
  {install}
}}
"""
    return {"PKGBUILD": pkgbuild}


def render(recipe: dict, target: str) -> dict[str, str]:
    target_data = load_targets()[target]
    if target_data["format"] == "rpm":
        return render_rpm(recipe, target)
    if target_data["format"] == "deb":
        return render_deb(recipe, target)
    if target_data["format"] == "pkg.tar.zst":
        return render_pkgbuild(recipe, target)
    fail(f"{target}: renderer for {target_data['format']} is scaffold-only")


def main() -> None:
    parser = argparse.ArgumentParser(prog="tideforge")
    commands = parser.add_subparsers(dest="command", required=True)
    validate_parser = commands.add_parser("validate")
    validate_parser.add_argument("recipe", type=Path)
    plan_parser = commands.add_parser("plan")
    plan_parser.add_argument("recipe", type=Path)
    plan_parser.add_argument("--target", required=True)
    render_parser = commands.add_parser("render")
    render_parser.add_argument("recipe", type=Path)
    render_parser.add_argument("--target", required=True)
    render_parser.add_argument("--output", required=True, type=Path)
    # `verify` prints how the recipe proves itself installed, so the gate can
    # read smoke/install_name from the recipe instead of carrying hand-written
    # matrix copies of them (#139: nothing connected the cell to the recipe).
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("recipe", type=Path)
    verify_parser.add_argument("--target", required=True)
    verify_parser.add_argument("--field", required=True, choices=["smoke", "install_name"])
    args = parser.parse_args()
    recipe = load_yaml(args.recipe)
    target = getattr(args, "target", None)
    validate(recipe, target)
    if args.command == "validate":
        print(f"{args.recipe}: valid")
    elif args.command == "plan":
        print(json.dumps({"package": recipe["name"], "target": target, "build_dependencies": target_dependencies(recipe, target), "format": load_targets()[target]["format"]}, indent=2))
    elif args.command == "verify":
        value = verify_metadata(recipe, target).get(args.field)
        if not value or not str(value).strip():
            # An empty smoke must be a loud failure, not an empty string a
            # shell would happily exec as a no-op success.
            fail(f"{args.recipe}: no verify.{args.field} for target {target}")
        print(value)
    else:
        for relative_path, content in render(recipe, target).items():
            destination = args.output / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content)
            if destination.name == "rules":
                destination.chmod(0o755)
        print(f"Rendered {recipe['name']} for {target} into {args.output}")


if __name__ == "__main__":
    main()
