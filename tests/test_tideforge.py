"""Tests for the single-recipe TunaOS package renderer."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("tideforge", ROOT / "scripts" / "tideforge.py")
assert SPEC and SPEC.loader
tideforge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tideforge)


@pytest.fixture
def recipe() -> dict:
    return {
        "schema": 1,
        "name": "hello-tuna",
        "version": "1.2.3",
        "release": 1,
        "summary": "Hello Tuna",
        "description": "A test package.",
        "license": "Apache-2.0",
        "source": {"url": "https://example.com/hello-tuna-1.2.3.tar.gz", "sha256": "a" * 64},
        "build_system": "meson",
        "dependencies": {"build": {"common": ["meson"], "targets": {"el10": ["gcc"], "ubuntu": ["ninja-build"]}}},
        "files": {"common": ["usr/bin/hello-tuna"]},
        "targets": ["el10", "ubuntu", "debian"],
    }


def test_recipe_renders_el10_rpm(recipe: dict) -> None:
    tideforge.validate(recipe, "el10")
    rendered = tideforge.render(recipe, "el10")
    spec = rendered["hello-tuna.spec"]
    assert "BuildRequires: meson" in spec
    assert "BuildRequires: gcc" in spec
    assert "%meson_install" in spec


def test_recipe_renders_ubuntu_debian_metadata(recipe: dict) -> None:
    rendered = tideforge.render(recipe, "ubuntu")
    assert "Build-Depends: debhelper-compat (= 13), meson, ninja-build" in rendered["debian/control"]
    # A single binary package stages straight into debian/hello-tuna, so no
    # .install file is rendered -- see
    # test_single_binary_package_renders_no_install_file.
    assert "debian/hello-tuna.install" not in rendered


def test_recipe_rejects_unknown_target(recipe: dict) -> None:
    recipe["targets"] = ["imaginary"]
    with pytest.raises(SystemExit):
        tideforge.validate(recipe)


def test_recipe_renders_subpackages(recipe: dict) -> None:
    recipe["outputs"] = {
        "rpm": {"subpackages": [{"name": "devel", "summary": "Headers", "files": ["usr/include/demo"]}]},
        "deb": {"packages": [{"name": "libdemo0", "files": ["usr/lib/libdemo.so.0"]}]},
    }
    rpm = tideforge.render(recipe, "el10")["hello-tuna.spec"]
    deb = tideforge.render(recipe, "ubuntu")
    assert "%package devel" in rpm
    # A subpackage that declares no `requires` must not leak a Requires: line.
    devel_stanza = rpm.split("%package devel", 1)[1].split("%description devel", 1)[0]
    assert "Requires:" not in devel_stanza
    assert "Package: libdemo0" in deb["debian/control"]


def test_recipe_subpackage_requires_pulls_in_main_package(recipe: dict) -> None:
    # A -devel subpackage that ships only headers and the unversioned .so is
    # useless without the runtime library and daemons in the main package.
    # Declared `requires` must land inside the %package stanza (before its
    # %description) so `dnf install <name>-devel` resolves the full closure --
    # the libseat regression where installing libseat-devel alone left `seatd`
    # missing and the smoke test exited 127.
    recipe["outputs"] = {
        "rpm": {
            "subpackages": [
                {
                    "name": "devel",
                    "summary": "Headers",
                    "requires": ["%{name}%{?_isa} = %{version}-%{release}"],
                    "files": ["usr/include/demo"],
                }
            ]
        }
    }
    rpm = tideforge.render(recipe, "el10")["hello-tuna.spec"]
    devel_stanza = rpm.split("%package devel", 1)[1].split("%description devel", 1)[0]
    assert "Requires: %{name}%{?_isa} = %{version}-%{release}" in devel_stanza


def test_deb_split_development_half_declares_the_runtime_half(recipe: dict) -> None:
    # dpkg-shlibdeps derives nothing from the unversioned .so symlink a -dev
    # package ships, so without an explicit relation the rendered stanza carries
    # only ${shlibs:Depends}/${misc:Depends} and `apt-get install libdemo-dev`
    # leaves the library behind -- the xfconf split-package contract failure on
    # ubuntu and debian.
    recipe["outputs"] = {
        "deb": {
            "packages": [
                {"name": "libdemo0", "files": ["usr/lib/*/libdemo.so.0"]},
                {
                    "name": "libdemo-dev",
                    "depends": ["libdemo0 (= ${binary:Version})"],
                    "files": ["usr/include/demo", "usr/lib/*/libdemo.so"],
                },
            ]
        }
    }
    tideforge.validate(recipe, "ubuntu")
    control = tideforge.render(recipe, "ubuntu")["debian/control"]
    development_stanza = control.split("Package: libdemo-dev", 1)[1]
    assert "Depends: ${shlibs:Depends}, ${misc:Depends}, libdemo0 (= ${binary:Version})" in development_stanza


def test_deb_split_rejects_a_development_half_with_no_runtime_dependency(recipe: dict) -> None:
    recipe["outputs"] = {
        "deb": {
            "packages": [
                {"name": "libdemo0", "files": ["usr/lib/*/libdemo.so.0"]},
                {"name": "libdemo-dev", "files": ["usr/include/demo", "usr/lib/*/libdemo.so"]},
            ]
        }
    }
    with pytest.raises(SystemExit):
        tideforge.validate(recipe, "ubuntu")


def test_deb_single_package_shipping_headers_needs_no_sibling(recipe: dict) -> None:
    # libcli11-dev and friends are header-only single-package outputs: there is
    # no runtime half for them to depend on, so the split rule must not fire.
    recipe["outputs"] = {"deb": {"packages": [{"name": "libdemo-dev", "files": ["usr/include/demo"]}]}}
    tideforge.validate(recipe, "ubuntu")


def test_single_binary_package_renders_no_install_file(recipe: dict) -> None:
    # dh_auto_install stages into debian/tmp only when the source produces more
    # than one binary package; with a single one it installs straight into
    # debian/<that package>. A .install file then makes dh_install search
    # debian/tmp for files already in place and abort the build:
    #   dh_install: warning: Cannot find (any matches for) "usr/bin/ninja"
    #     (tried in ., debian/tmp)
    #   dh_install: error: missing files, aborting
    # ninja-build (cmake, one binary package) failed exactly this way on ubuntu
    # while building fine on el10.
    recipe["build_system"] = "cmake"
    assert "debian/hello-tuna.install" not in tideforge.render(recipe, "ubuntu")
    # Same for a single package the deb output renames, as cli11-devel does.
    recipe["outputs"] = {"deb": {"packages": [{"name": "libdemo-dev", "files": ["usr/include/demo"]}]}}
    assert "debian/libdemo-dev.install" not in tideforge.render(recipe, "ubuntu")


def test_split_deb_output_keeps_per_package_install_files(recipe: dict) -> None:
    # Two or more binary packages do stage through debian/tmp, so each keeps the
    # .install list that carves its share out of it.
    recipe["outputs"] = {
        "deb": {
            "packages": [
                {"name": "libdemo0", "files": ["usr/lib/*/libdemo.so.0"]},
                {
                    "name": "libdemo-dev",
                    "depends": ["libdemo0 (= ${binary:Version})"],
                    "files": ["usr/include/demo"],
                },
            ]
        }
    }
    rendered = tideforge.render(recipe, "ubuntu")
    assert rendered["debian/libdemo0.install"] == "usr/lib/*/libdemo.so.0\n"
    assert rendered["debian/libdemo-dev.install"] == "usr/include/demo\n"


def test_recipe_renders_arch_pkgbuild(recipe: dict) -> None:
    recipe["build_system"] = "cargo"
    recipe["targets"] = ["arch"]
    recipe["dependencies"]["build"]["targets"]["arch"] = ["pkgconf"]
    rendered = tideforge.render(recipe, "arch")["PKGBUILD"]
    assert "pkgname=hello-tuna" in rendered
    assert "cargo build --release --locked" in rendered
    assert "pkgconf" in rendered


def test_recipe_renders_pinned_auxiliary_source_closure(recipe: dict) -> None:
    recipe["sources"] = [{
        "url": "https://example.com/vendor-1.0.tar.gz",
        "sha256": "b" * 64,
        "filename": "vendor.tar.gz",
        "destination": "third-party/vendor",
        "strip_components": 1,
    }]
    rpm = tideforge.render(recipe, "el10")["hello-tuna.spec"]
    arch = tideforge.render(recipe, "arch")["PKGBUILD"]
    # The `filename:` override differs from the URL basename (vendor-1.0.tar.gz),
    # so the provenance URL is dropped: rpmbuild would otherwise look for the URL
    # basename on disk and never find the file the fetch script wrote.
    assert "Source1:        vendor.tar.gz\n" in rpm
    assert "vendor-1.0.tar.gz" not in rpm
    assert "\\nSource" not in rpm
    assert "tar --extract --file %{SOURCE1} --strip-components=1 --directory third-party/vendor" in rpm
    assert "'vendor.tar.gz::https://example.com/vendor-1.0.tar.gz'" in arch
    assert "tar --extract --file \"$srcdir/vendor.tar.gz\" --strip-components=1 --directory third-party/vendor" in arch


def test_auxiliary_source_requires_safe_destination(recipe: dict) -> None:
    recipe["sources"] = [{"url": "https://example.com/vendor.tar.gz", "sha256": "b" * 64, "destination": "../vendor"}]
    with pytest.raises(SystemExit):
        tideforge.validate(recipe)


def test_dist_git_source_must_pin_a_commit(recipe: dict) -> None:
    base = "https://src.fedoraproject.org/rpms/cosmic-bg/raw/{ref}/f/vendor-config-1.4.0.toml"
    entry = {"sha256": "b" * 64, "destination": ".cargo/config.toml", "extract": False}
    # A branch tip stops resolving the moment Fedora rebases the package: the
    # per-version filename is replaced, and the recipe 404s at fetch time.
    recipe["sources"] = [{"url": base.format(ref="rawhide"), **entry}]
    with pytest.raises(SystemExit):
        tideforge.validate(recipe)
    recipe["sources"] = [{"url": base.format(ref="a" * 40), **entry}]
    tideforge.validate(recipe)


def test_recipe_renders_checksum_locked_auxiliary_file(recipe: dict) -> None:
    recipe["sources"] = [{
        "url": "https://example.com/vendor-config.toml",
        "sha256": "b" * 64,
        "filename": "vendor-config.toml",
        "destination": ".cargo/config.toml",
        "extract": False,
    }]
    rpm = tideforge.render(recipe, "el10")["hello-tuna.spec"]
    arch = tideforge.render(recipe, "arch")["PKGBUILD"]
    # The `filename:` override matches the URL basename here, so the provenance
    # URL is preserved and rpm still resolves %{SOURCE1} to the fetched file.
    assert "Source1:        vendor-config.toml::https://example.com/vendor-config.toml" in rpm
    assert "install -Dm0644 %{SOURCE1} .cargo/config.toml" in rpm
    assert 'install -Dm0644 "$srcdir/vendor-config.toml" .cargo/config.toml' in arch


def test_arch_pkgbuild_includes_runtime_dependencies(recipe: dict) -> None:
    recipe["targets"] = ["arch"]
    recipe["dependencies"]["runtime"] = {"targets": {"arch": ["glibc", "libinput>=1.0"]}}
    rendered = tideforge.render(recipe, "arch")["PKGBUILD"]
    assert "depends=('glibc' 'libinput>=1.0')" in rendered


def test_rpm_and_deb_preserve_runtime_dependencies(recipe: dict) -> None:
    recipe["dependencies"]["runtime"] = {"common": ["dbus"], "targets": {"el10": ["bluez"], "ubuntu": ["bluez"]}}
    rpm = tideforge.render(recipe, "el10")["hello-tuna.spec"]
    deb = tideforge.render(recipe, "ubuntu")["debian/control"]
    assert "Requires:       dbus" in rpm
    assert "Requires:       bluez" in rpm
    assert "Depends: ${shlibs:Depends}, ${misc:Depends}, dbus, bluez" in deb


def test_provides_renders_in_rpm_deb_and_arch(recipe: dict) -> None:
    """A declared `provides:` alias lands in every format (#169)."""
    recipe["provides"] = ["cosmic-icons"]
    rpm = tideforge.render(recipe, "el10")["hello-tuna.spec"]
    deb = tideforge.render(recipe, "ubuntu")["debian/control"]
    assert "Provides:       cosmic-icons" in rpm
    assert "Provides: cosmic-icons" in deb
    recipe["targets"] = ["arch"]
    pkgbuild = tideforge.render(recipe, "arch")["PKGBUILD"]
    assert "provides=('cosmic-icons')" in pkgbuild


def test_recipe_without_provides_renders_no_provides_lines(recipe: dict) -> None:
    """Recipes without the key keep clean spec/control output."""
    rpm = tideforge.render(recipe, "el10")["hello-tuna.spec"]
    deb = tideforge.render(recipe, "ubuntu")["debian/control"]
    assert "Provides:" not in rpm
    assert "Provides:" not in deb


@pytest.mark.parametrize(
    ("recipe_name", "target", "expected"),
    [
        ("kairpods", "el10", ["Requires:       dbus", "Requires:       bluez"]),
        ("dms-cli", "ubuntu", ["dms", "quickshell"]),
    ],
)
def test_real_recipes_keep_declared_runtime_metadata(
    recipe_name: str, target: str, expected: list[str]
) -> None:
    """Guard the concrete recipes that exposed the renderer regression (#117)."""
    recipe = tideforge.load_yaml(ROOT / "packages" / recipe_name / "package.yaml")
    tideforge.validate(recipe, target)
    rendered = tideforge.render(recipe, target)
    metadata = "\n".join(rendered.values())
    for dependency in expected:
        assert dependency in metadata


def test_dms_greeter_is_an_arch_recipe_with_runtime_closure() -> None:
    recipe = tideforge.load_yaml(ROOT / "packages" / "dms-greeter" / "package.yaml")
    tideforge.validate(recipe, "arch")
    pkgbuild = tideforge.render(recipe, "arch")["PKGBUILD"]
    assert "depends=('greetd' 'quickshell')" in pkgbuild
    assert "usr/bin/dms-greeter" in pkgbuild


def test_rpm_changelog_uses_a_valid_rpm_date(recipe: dict) -> None:
    spec = tideforge.render(recipe, "el10")["hello-tuna.spec"]
    assert "* Sat Jul 25 2026 TunaOS Package Factory" in spec


def test_go_rpm_disables_empty_automatic_debug_packages(recipe: dict) -> None:
    recipe["build_system"] = "go"
    spec = tideforge.render(recipe, "el10")["hello-tuna.spec"]
    assert spec.startswith("%global debug_package %{nil}\nName:")


def test_data_rpm_disables_empty_automatic_debug_packages(recipe: dict) -> None:
    recipe["build_system"] = "data"
    spec = tideforge.render(recipe, "el10")["hello-tuna.spec"]
    assert spec.startswith("%global debug_package %{nil}\nName:")


def test_cargo_rpm_retains_native_debug_packages(recipe: dict) -> None:
    recipe["build_system"] = "cargo"
    spec = tideforge.render(recipe, "el10")["hello-tuna.spec"]
    assert not spec.startswith("%global debug_package")


def test_custom_rpm_disables_empty_automatic_debug_packages(recipe: dict) -> None:
    recipe["build_system"] = "custom"
    recipe["build"] = {"commands": ["just build"]}
    recipe["install"] = {"commands": ["just rootdir={destdir} install"]}
    spec = tideforge.render(recipe, "el10")["hello-tuna.spec"]
    assert spec.startswith("%global debug_package %{nil}\nName:")


def test_recipe_renders_go_builds(recipe: dict) -> None:
    recipe["build_system"] = "go"
    rpm = tideforge.render(recipe, "el10")["hello-tuna.spec"]
    deb = tideforge.render(recipe, "debian")["debian/rules"]
    arch = tideforge.render(recipe, "arch")["PKGBUILD"]
    assert "go build -buildmode=pie" in rpm
    assert "go build -buildmode=pie" in deb
    assert "go build -buildmode=pie" in arch
    assert "override_dh_dwz:\n\t:" in deb
    assert "debian/hello-tuna.install" not in tideforge.render(recipe, "debian")


def test_go_recipe_renders_prepare_tags_and_linker_flags(recipe: dict) -> None:
    recipe["build_system"] = "go"
    recipe["build"] = {
        "working_directory": "core",
        "go_package": "./cmd/demo",
        "binary": "demo",
        "prepare": ["make -C core sync-assets"],
        "go_tags": ["embedded", "wayland"],
        "go_ldflags": ["-s", "-w"],
        "go_module_mode": "vendor",
    }
    for target in ("el10", "debian", "arch"):
        rendered = "\n".join(tideforge.render(recipe, target).values())
        assert "make -C core sync-assets" in rendered
        assert "go build -buildmode=pie -trimpath -mod=vendor -tags embedded,wayland -ldflags '-s -w' -o demo ./cmd/demo" in rendered


def test_go_recipe_rejects_invalid_prepare_and_build_options(recipe: dict) -> None:
    recipe["build_system"] = "go"
    recipe["build"] = {"prepare": [""]}
    with pytest.raises(SystemExit):
        tideforge.validate(recipe)
    recipe["build"] = {"go_tags": ["invalid tag"]}
    with pytest.raises(SystemExit):
        tideforge.validate(recipe)
    recipe["build"] = {"go_ldflags": [1]}
    with pytest.raises(SystemExit):
        tideforge.validate(recipe)
    recipe["build"] = {"go_module_mode": "mod"}
    with pytest.raises(SystemExit):
        tideforge.validate(recipe)


def test_recipe_installs_reviewed_source_files(recipe: dict) -> None:
    recipe["install"] = {"files": [{"source": "demo.service", "destination": "usr/lib/systemd/system/demo.service"}]}
    assert "install -Dm0644 demo.service %{buildroot}/usr/lib/systemd/system/demo.service" in tideforge.render(recipe, "el10")["hello-tuna.spec"]
    assert "install -Dm0644 demo.service debian/hello-tuna/usr/lib/systemd/system/demo.service" in tideforge.render(recipe, "debian")["debian/rules"]
    assert "install -Dm0644 demo.service $pkgdir/usr/lib/systemd/system/demo.service" in tideforge.render(recipe, "arch")["PKGBUILD"]


def test_recipe_installs_reviewed_source_directories(recipe: dict) -> None:
    recipe["build_system"] = "data"
    recipe["install"] = {"directories": [{"source": "qml", "destination": "usr/share/demo"}]}
    assert "cp -a qml/. %{buildroot}/usr/share/demo/" in tideforge.render(recipe, "el10")["hello-tuna.spec"]
    assert "cp -a qml/. $pkgdir/usr/share/demo/" in tideforge.render(recipe, "arch")["PKGBUILD"]


def test_recipe_installs_generated_files(recipe: dict) -> None:
    recipe["build_system"] = "data"
    recipe["install"] = {"generated_files": [{"destination": "usr/lib/pkgconfig/demo.pc", "content": "Name: demo\\n"}]}
    rpm = tideforge.render(recipe, "el10")["hello-tuna.spec"]
    deb = tideforge.render(recipe, "ubuntu")["debian/rules"]
    arch = tideforge.render(recipe, "arch")["PKGBUILD"]
    assert "printf '%s\\n' 'Name: demo\\n'" in rpm
    assert "debian/hello-tuna/usr/lib/pkgconfig/demo.pc" in deb
    assert "$pkgdir/usr/lib/pkgconfig/demo.pc" in arch


def test_generated_file_content_stays_on_one_shell_line(recipe: dict) -> None:
    # Every LINE of a debian/rules recipe is its own /bin/sh invocation, so a
    # quoted literal carrying real newlines reached the shell truncated:
    #   printf %s 'prefix=/usr
    #   /bin/sh: 1: Syntax error: Unterminated quoted string
    # That killed the wayland-protocols deb build while el10 (whose %install is
    # one shell script) rendered the same recipe fine.
    recipe["build_system"] = "data"
    recipe["install"] = {
        "generated_files": [
            {
                "destination": "usr/share/pkgconfig/demo.pc",
                "content": "prefix=/usr\ndatarootdir=${prefix}/share\n\nName: demo\n",
            }
        ]
    }
    for rendered in (tideforge.render(recipe, "el10")["hello-tuna.spec"], tideforge.render(recipe, "arch")["PKGBUILD"]):
        printf = next(line for line in rendered.splitlines() if "printf" in line)
        assert printf.endswith("demo.pc")
        assert printf.count("printf") == 1
    printf = next(line for line in tideforge.render(recipe, "ubuntu")["debian/rules"].splitlines() if "printf" in line)
    assert printf.endswith("demo.pc")
    # make expands $ before /bin/sh sees the recipe, so a pkg-config file's
    # ${prefix} reference has to reach the shell as $${prefix}.
    assert "$${prefix}" in printf
    assert "'${prefix}" not in printf


def test_custom_recipe_renders_native_install_contract(recipe: dict) -> None:
    recipe["build_system"] = "custom"
    recipe["build"] = {"commands": ["cargo build --release --offline"], "environment": {"RUSTFLAGS": "-C relocation-model=pic"}}
    recipe["install"] = {"commands": ["just rootdir={destdir} install"]}
    rpm = tideforge.render(recipe, "el10")["hello-tuna.spec"]
    deb = tideforge.render(recipe, "debian")["debian/rules"]
    arch = tideforge.render(recipe, "arch")["PKGBUILD"]
    assert "just rootdir=%{buildroot} install" in rpm
    assert "export RUSTFLAGS='-C relocation-model=pic'" in rpm
    assert "just rootdir=debian/hello-tuna install" in deb
    assert "just rootdir=$pkgdir install" in arch


def test_custom_recipe_requires_build_and_install_commands(recipe: dict) -> None:
    recipe["build_system"] = "custom"
    recipe["build"] = {"commands": []}
    recipe["install"] = {"commands": ["make install DESTDIR={destdir}"]}
    with pytest.raises(SystemExit):
        tideforge.validate(recipe)


def test_deb_rooted_source_directory_excludes_generated_debian_metadata(recipe: dict) -> None:
    recipe["build_system"] = "data"
    recipe["source"]["directory"] = "."
    recipe["install"] = {"directories": [{"source": ".", "destination": "usr/share/demo"}]}
    rules = tideforge.render(recipe, "ubuntu")["debian/rules"]
    assert '[ "$$entry" = "./debian" ] && continue' in rules
    assert "debian/hello-tuna.install" not in tideforge.render(recipe, "ubuntu")


def test_rpm_rooted_release_archive_gets_a_safe_build_directory(recipe: dict) -> None:
    recipe["build_system"] = "data"
    recipe["source"]["directory"] = "."
    spec = tideforge.render(recipe, "el10")["hello-tuna.spec"]
    assert "%setup -q -c -n %{name}-%{version}" in spec
    assert "%autosetup -n ." not in spec


def test_go_recipe_uses_declared_module_and_binary(recipe: dict) -> None:
    recipe["build_system"] = "go"
    recipe["build"] = {"working_directory": "core", "go_package": "./cmd/demo", "binary": "demo"}
    assert "cd core\ngo build" in tideforge.render(recipe, "el10")["hello-tuna.spec"]
    assert "core/demo" in tideforge.render(recipe, "arch")["PKGBUILD"]


def test_cargo_recipe_uses_declared_workspace_and_binary(recipe: dict) -> None:
    recipe["build_system"] = "cargo"
    recipe["build"] = {"working_directory": "service", "cargo_package": "daemon", "binary": "demo-daemon"}
    rpm = tideforge.render(recipe, "el10")["hello-tuna.spec"]
    deb = tideforge.render(recipe, "debian")["debian/rules"]
    arch = tideforge.render(recipe, "arch")["PKGBUILD"]
    assert "cd service\nCARGO_PROFILE_RELEASE_DEBUG=1 cargo build --release --locked --package daemon" in rpm
    assert "service/target/release/demo-daemon" in deb
    assert "cd service\n  CFLAGS+=(' -ffat-lto-objects')\n  CARGO_PROFILE_RELEASE_DEBUG=1 cargo build --release --locked --package daemon" in arch


def test_cargo_arch_emits_fat_lto_objects_for_c_shim_linking(recipe: dict) -> None:
    """Cargo PKGBUILDs must force fat LTO objects for C-compiling crates.

    Arch's makepkg enables LTO by default (OPTIONS=(... lto), LTOFLAGS=-flto=auto).
    A Rust crate that builds C via the cc crate -- niri's libspa-sys PipeWire
    bindings, for instance -- then emits pure thin-LTO bitcode whose wrapper
    symbols vanish at the final ld.lld link ("undefined symbol:
    spa_pod_object_find_prop_libspa_rs"). Emitting fat LTO objects keeps real
    machine code beside the bitcode so those symbols resolve, exactly as Arch's
    own niri PKGBUILD does. The flag is Arch-only and lives on its own line
    before the cargo invocation so it applies to the whole build.
    """
    recipe["build_system"] = "cargo"
    arch = tideforge.render(recipe, "arch")["PKGBUILD"]
    assert "CFLAGS+=(' -ffat-lto-objects')\n  CARGO_PROFILE_RELEASE_DEBUG=1 cargo build" in arch
    # The flag is specific to Arch's LTO-by-default toolchain; RPM and DEB paths
    # must not carry it.
    assert "-ffat-lto-objects" not in tideforge.render(recipe, "el10")["hello-tuna.spec"]
    assert "-ffat-lto-objects" not in "\n".join(tideforge.render(recipe, "debian").values())


def test_cargo_recipe_renders_validated_build_environment(recipe: dict) -> None:
    recipe["build_system"] = "cargo"
    recipe["build"] = {"environment": {"RUSTFLAGS": "-C link-arg=-lexample"}}
    for target in ("el10", "debian", "arch"):
        rendered = "\n".join(tideforge.render(recipe, target).values())
        assert "RUSTFLAGS='-C link-arg=-lexample' CARGO_PROFILE_RELEASE_DEBUG=1 cargo build" in rendered


def test_cargo_recipe_renders_locked_vendor_closure(recipe: dict) -> None:
    recipe["build_system"] = "cargo"
    recipe["build"] = {
        "cargo_no_default_features": True,
        "cargo_features": ["udev", "smithay/renderer_gl"],
        "cargo_offline": True,
        "cargo_config": '[source.crates-io]\nreplace-with = "vendored-sources"\n',
    }
    for target in ("el10", "debian", "arch"):
        rendered = "\n".join(tideforge.render(recipe, target).values())
        assert "mkdir -p .cargo" in rendered
        assert "cargo build --release --locked --no-default-features --features udev,smithay/renderer_gl --offline" in rendered


def test_cargo_recipe_rejects_invalid_closure_configuration(recipe: dict) -> None:
    recipe["build_system"] = "cargo"
    recipe["build"] = {"cargo_features": ["invalid feature"]}
    with pytest.raises(SystemExit):
        tideforge.validate(recipe)
    recipe["build"] = {"cargo_offline": "yes"}
    with pytest.raises(SystemExit):
        tideforge.validate(recipe)


def test_build_environment_rejects_unsafe_names_and_non_string_values(recipe: dict) -> None:
    recipe["build"] = {"environment": {"bad-name": "value"}}
    with pytest.raises(SystemExit):
        tideforge.validate(recipe)
    recipe["build"] = {"environment": {"RUSTFLAGS": 1}}
    with pytest.raises(SystemExit):
        tideforge.validate(recipe)


def test_cargo_recipe_can_explicitly_repair_a_root_lockfile_version(recipe: dict) -> None:
    recipe["build_system"] = "cargo"
    recipe["build"] = {
        "cargo_locked": False,
        "cargo_lock_reason": "Upstream archive lockfile has an outdated root package version.",
    }
    for target in ("el10", "debian", "arch"):
        rendered = "\n".join(tideforge.render(recipe, target).values())
        assert "cargo build --release --locked" not in rendered
        assert "cargo build --release" in rendered


def test_unlocked_cargo_recipe_requires_an_explicit_reason(recipe: dict) -> None:
    recipe["build_system"] = "cargo"
    recipe["build"] = {"cargo_locked": False}
    with pytest.raises(SystemExit):
        tideforge.validate(recipe)


def test_cargo_recipe_can_install_session_assets(recipe: dict) -> None:
    recipe["build_system"] = "cargo"
    recipe["install"] = {"files": [{"source": "resources/demo.desktop", "destination": "usr/share/wayland-sessions/demo.desktop"}]}
    rpm = tideforge.render(recipe, "el10")["hello-tuna.spec"]
    deb = tideforge.render(recipe, "debian")["debian/rules"]
    arch = tideforge.render(recipe, "arch")["PKGBUILD"]
    assert "install -Dm0644 resources/demo.desktop %{buildroot}/usr/share/wayland-sessions/demo.desktop" in rpm
    assert "install -Dm0644 resources/demo.desktop debian/hello-tuna/usr/share/wayland-sessions/demo.desktop" in deb
    assert "install -Dm0644 resources/demo.desktop $pkgdir/usr/share/wayland-sessions/demo.desktop" in arch


def test_cmake_options_render_for_every_native_format(recipe: dict) -> None:
    recipe["build_system"] = "cmake"
    recipe["build"] = {"cmake_options": ["-DUSE_DEMO=OFF"]}
    rpm = tideforge.render(recipe, "el10")["hello-tuna.spec"]
    deb = tideforge.render(recipe, "ubuntu")["debian/rules"]
    arch = tideforge.render(recipe, "arch")["PKGBUILD"]
    assert "%cmake -DUSE_DEMO=OFF" in rpm
    assert "dh_auto_configure -- -DUSE_DEMO=OFF" in deb
    assert "cmake -B build -S . -DCMAKE_INSTALL_PREFIX=/usr -DUSE_DEMO=OFF" in arch


def test_meson_options_render_for_every_native_format(recipe: dict) -> None:
    recipe["build_system"] = "meson"
    recipe["build"] = {"meson_options": ["-Ddemo=enabled"]}
    rpm = tideforge.render(recipe, "el10")["hello-tuna.spec"]
    deb = tideforge.render(recipe, "ubuntu")["debian/rules"]
    arch = tideforge.render(recipe, "arch")["PKGBUILD"]
    assert "%meson -Ddemo=enabled" in rpm
    assert "dh_auto_configure -- -Ddemo=enabled" in deb
    assert "arch-meson build -Ddemo=enabled" in arch


def test_cmake_ninja_generator_renders_for_every_native_format(recipe: dict) -> None:
    recipe["build_system"] = "cmake"
    recipe["build"] = {"cmake_generator": "Ninja", "cmake_options": ["-DUSE_DEMO=OFF"]}
    rpm = tideforge.render(recipe, "el10")["hello-tuna.spec"]
    deb = tideforge.render(recipe, "ubuntu")["debian/rules"]
    arch = tideforge.render(recipe, "arch")["PKGBUILD"]
    assert "%cmake -G Ninja -DUSE_DEMO=OFF" in rpm
    assert "cmake -B build -S . -DCMAKE_INSTALL_PREFIX=/usr -G Ninja -DUSE_DEMO=OFF" in arch
    # deb expresses the generator as the BUILDSYSTEM, not as a flag, and this
    # test used to assert the flag. That was the bug: debhelper's `cmake`
    # buildsystem runs make in dh_auto_build whatever -G says, so the flag
    # configured Ninja and then make ran against build.ninja --
    # "No targets specified and no makefile found" on every deb cell that asked
    # for Ninja (run 32556308211). cmake+ninja passes -GNinja itself and builds
    # with ninja, so passing -G by hand as well would put two -G flags on one
    # command line.
    assert "--buildsystem=cmake+ninja" in deb
    assert "dh_auto_configure -- -DUSE_DEMO=OFF" in deb
    assert "-G Ninja" not in deb


def test_dependency_capabilities_resolve_to_native_target_packages(recipe: dict) -> None:
    recipe["dependencies"]["build"] = {"capabilities": ["rust", "pkg-config"]}
    assert tideforge.target_dependencies(recipe, "el10") == ["rust", "cargo", "pkgconf-pkg-config"]
    assert tideforge.target_dependencies(recipe, "arch") == ["rust", "pkgconf"]


def test_unknown_dependency_capability_is_rejected(recipe: dict) -> None:
    recipe["dependencies"]["build"] = {"capabilities": ["imaginary-sdk"]}
    with pytest.raises(SystemExit):
        tideforge.validate(recipe)


def test_deb_drops_redundant_debhelper_compat_from_recipe_dependencies(recipe: dict) -> None:
    """A bare debhelper-compat in a recipe must not reach debian/control.

    render_deb always emits "debhelper-compat (= 13)". When a recipe also listed
    a bare "debhelper-compat" the relation appeared twice, and dh read the
    unversioned one and aborted: "Could not parse desired debhelper compat level
    from relation: debhelper-compat". Broke xfconf on both debian and ubuntu.
    """
    recipe.setdefault("dependencies", {}).setdefault("build", {}).setdefault("targets", {})[
        "ubuntu"
    ] = ["debhelper-compat", "libglib2.0-dev"]
    control = tideforge.render(recipe, "ubuntu")["debian/control"]
    build_depends = next(
        line for line in control.splitlines() if line.startswith("Build-Depends:")
    )
    assert build_depends.count("debhelper-compat") == 1
    assert "debhelper-compat (= 13)" in build_depends
    assert "libglib2.0-dev" in build_depends


def test_native_deb_skips_upstream_test_suite(recipe: dict) -> None:
    """Native build systems must not auto-run the upstream test suite.

    debhelper's "dh $@" auto-runs dh_auto_test for meson/cmake/autotools builds.
    Those suites routinely need a session D-Bus, a machine-id, a display, or the
    network -- none of which exist in the minimal build container -- so xfconf's
    33 D-Bus integration tests aborted the build with "Cannot spawn a message bus
    without a machine-id". The RPM path emits no %check, so skip them here too for
    RPM/DEB parity.
    """
    for build_system in ("meson", "cmake", "autotools"):
        recipe["build_system"] = build_system
        rules = tideforge.render(recipe, "ubuntu")["debian/rules"]
        assert "override_dh_auto_test:\n\t:\n" in rules


def test_custom_deb_does_not_emit_test_override(recipe: dict) -> None:
    """Non-native builds replace dh_auto_build/install and need no test override.

    The cargo/go/data/custom renderers do not hand debhelper a detectable build
    system, so dh_auto_test is already a no-op; emitting the override would be
    dead metadata.
    """
    recipe["build_system"] = "custom"
    recipe["build"] = {"commands": ["just build"]}
    recipe["install"] = {"commands": ["just rootdir={destdir} install"]}
    rules = tideforge.render(recipe, "ubuntu")["debian/rules"]
    assert "override_dh_auto_test:" not in rules


def test_data_deb_disables_debhelper_build_system_autodetection(recipe: dict) -> None:
    """Data recipes must pin --buildsystem=none.

    Plain "dh $@" auto-detects a build system from whatever upstream ships.
    oversteer-udev installs only udev rules but upstream carries a meson.build,
    so debhelper ran dh_auto_configure under meson and failed with exit code 25
    — while the same recipe built fine as an RPM, which bypasses debhelper.
    """
    recipe["build_system"] = "data"
    rules = tideforge.render(recipe, "ubuntu")["debian/rules"]
    assert "dh $@ --buildsystem=none" in rules


def test_deb_render_emits_machine_readable_copyright(recipe: dict) -> None:
    # lintian: no-copyright-file was an error on every generated deb --
    # nothing emitted debian/copyright. dh_installdocs installs this one into
    # each binary package.
    rendered = tideforge.render(recipe, "ubuntu")
    copyright_file = rendered["debian/copyright"]
    assert "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/" in copyright_file
    assert "Upstream-Name: hello-tuna" in copyright_file
    assert "Source: https://example.com/hello-tuna-1.2.3.tar.gz" in copyright_file
    assert "License: Apache-2.0" in copyright_file


def test_autotools_deb_rules_delete_libtool_archives(recipe: dict) -> None:
    # Mirrors the rpm renderer's post-%make_install cleanup: shipped .la files
    # carry dependency_libs lintian rejects as a policy error
    # (non-empty-dependency_libs-in-la-file, libunwind-dev x5). Only libtool
    # emits them, so only the autotools rules carry the hook.
    recipe["build_system"] = "autotools"
    rules = tideforge.render(recipe, "ubuntu")["debian/rules"]
    assert "execute_after_dh_auto_install:" in rules
    assert "find debian -name '*.la' -delete" in rules


def test_non_autotools_deb_rules_carry_no_libtool_cleanup(recipe: dict) -> None:
    rules = tideforge.render(recipe, "ubuntu")["debian/rules"]
    assert "*.la" not in rules


def test_python_rpm_renders_pyproject_macros(recipe: dict) -> None:
    recipe["build_system"] = "python"
    recipe["dependencies"]["build"]["common"] = ["python3-devel", "python3-setuptools"]
    spec = tideforge.render(recipe, "el10")["hello-tuna.spec"]
    assert "%pyproject_wheel" in spec
    assert "%pyproject_install" in spec
    assert "BuildRequires: python3-devel" in spec


def test_python_rpm_disables_empty_debug_packages(recipe: dict) -> None:
    """Pure-Python packages produce no debuginfo; the build must not abort.

    C-extensions linked into a Python wheel can produce native debuginfo, but
    %pyproject_install does not generate the RPM debugsource payload the
    automatic debug package relies on.  Disable it the same way go/data/custom
    do so rpmbuild does not abort with "Empty %files file debugsourcefiles.list".
    """
    recipe["build_system"] = "python"
    spec = tideforge.render(recipe, "el10")["hello-tuna.spec"]
    assert spec.startswith("%global debug_package %{nil}\nName:")


def test_python_deb_renders_pybuild(recipe: dict) -> None:
    recipe["build_system"] = "python"
    recipe["dependencies"]["build"]["targets"]["ubuntu"] = ["python3", "python3-setuptools", "dh-python"]
    rendered = tideforge.render(recipe, "ubuntu")
    rules = rendered["debian/rules"]
    control = rendered["debian/control"]
    assert "dh $@ --buildsystem=pybuild" in rules
    assert "Build-Depends: debhelper-compat (= 13), meson, python3, python3-setuptools, dh-python" in control
    # Native build system, so auto-test is skipped.
    assert "override_dh_auto_test:" in rules
    # Single binary package stages directly; no .install file.
    assert "debian/hello-tuna.install" not in rendered


def test_python_arch_renders_build_and_installer(recipe: dict) -> None:
    recipe["build_system"] = "python"
    recipe["targets"] = ["arch"]
    recipe["dependencies"]["build"]["targets"]["arch"] = ["python", "python-build", "python-installer", "python-setuptools"]
    pkgbuild = tideforge.render(recipe, "arch")["PKGBUILD"]
    assert "python -m build --wheel --no-isolation" in pkgbuild
    assert 'python -m installer --destdir="$pkgdir" dist/*.whl' in pkgbuild
    assert "python-build" in pkgbuild


def test_python_recipe_respects_prepare_commands(recipe: dict) -> None:
    recipe["build_system"] = "python"
    recipe["build"] = {"prepare": ["make generate-protos"]}
    spec = tideforge.render(recipe, "el10")["hello-tuna.spec"]
    assert "make generate-protos" in spec
    assert "make generate-protos\n%pyproject_wheel" in spec


def test_python_recipe_with_install_files_appends_to_staged_wheel(recipe: dict) -> None:
    recipe["build_system"] = "python"
    recipe["install"] = {"files": [{"source": "demo.service", "destination": "usr/lib/systemd/system/demo.service"}]}
    spec = tideforge.render(recipe, "el10")["hello-tuna.spec"]
    assert "install -Dm0644 demo.service %{buildroot}/usr/lib/systemd/system/demo.service" in spec
    rules = tideforge.render(recipe, "ubuntu")["debian/rules"]
    assert "install -Dm0644 demo.service debian/hello-tuna/usr/lib/systemd/system/demo.service" in rules
    pkgbuild = tideforge.render(recipe, "arch")["PKGBUILD"]
    assert "install -Dm0644 demo.service $pkgdir/usr/lib/systemd/system/demo.service" in pkgbuild
