"""Native recipe/source contracts only; runtime proofs are CI-owned."""

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts import tideforge

ROOT = Path(__file__).parents[1]
RECIPE = ROOT / "packages/gtk4-layer-shell/package.yaml"


def recipe():
    return yaml.safe_load(RECIPE.read_text())


def test_official_source_pin_and_native_target_are_explicit():
    data = recipe()
    tideforge.validate(data, "el10")
    assert data["source"] == {
        "url": "https://github.com/wmww/gtk4-layer-shell/archive/refs/tags/v1.3.0.tar.gz",
        "sha256": "1ebb01ab14e98afd1727f68f64981c37bd23305b1f131f5667c02b94cf593192",
        "directory": "gtk4-layer-shell-1.3.0",
    }
    assert data["version"] == "1.3.0" and data["license"] == "MIT"
    assert data["targets"] == ["el10"]
    assert {"alma10", "alma10-kitten"} <= data["dependencies"]["build"]["targets"].keys()
    assert tideforge.portable_payload_contract(data) is None
    with pytest.raises(SystemExit):
        tideforge.validate(data, "alma10")


def test_rpm_owns_both_apis_and_keeps_runtime_devel_matched():
    data = recipe()
    spec = tideforge.render_rpm(data, "el10")["gtk4-layer-shell.spec"]
    assert "%{set_build_flags}" in spec
    assert "Requires: %{name}%{?_isa} = %{version}-%{release}" in spec
    assert "%post -p /sbin/ldconfig" in spec and "%postun -p /sbin/ldconfig" in spec
    assert "DESTDIR=%{buildroot} meson install -C build --no-rebuild" in spec
    assert (
        "install -Dm0644 LICENSE %{buildroot}/usr/share/licenses/gtk4-layer-shell/LICENSE" in spec
    )
    main, devel = spec.split("%files\n", 1)[1].split("%files devel\n", 1)
    for path in data["outputs"]["rpm"]["files"]:
        assert "/" + path in main
    for path in data["outputs"]["rpm"]["subpackages"][0]["files"]:
        assert "/" + path in devel
    assert "Gtk4SessionLock-1.0.typelib" in main
    assert "gtk4-session-lock.h" in devel
    assert "pkgconfig(gtk4) >= 4.12" in spec
    assert "gtk3-devel" not in spec and "libgtk-layer-shell.so" not in spec
    # The existing GTK3 library, headers and metadata remain independently owned.
    gtk3 = yaml.safe_load((ROOT / "packages/gtk-layer-shell/package.yaml").read_text())
    gtk3_files = set(gtk3["outputs"]["rpm"]["files"])
    for package in gtk3["outputs"]["rpm"]["subpackages"]:
        gtk3_files.update(package["files"])
    gtk4_files = set(data["outputs"]["rpm"]["files"])
    for package in data["outputs"]["rpm"]["subpackages"]:
        gtk4_files.update(package["files"])
    assert not gtk3_files & gtk4_files


def admission_script(data):
    command = next(c for c in data["build"]["commands"] if c.startswith("meson introspect"))
    argv = shlex.split(command.rsplit("|", 1)[1])
    assert argv[:2] == ["python3", "-c"]
    return argv[2]


@pytest.mark.parametrize(
    "names,accepted",
    [
        (["layer-probe", "lock-probe", "unit-test-get-version"], True),
        ([], False),
        (["layer-probe", "unit-test-get-version"], False),
        (["lock-probe", "unit-test-get-version"], False),
        (["layer-probe", "lock-probe"], False),
    ],
)
def test_actual_recipe_test_admission_rejects_missing_suites(names, accepted):
    result = subprocess.run(
        [sys.executable, "-c", admission_script(recipe())],
        input=json.dumps([{"name": name} for name in names]),
        text=True,
        capture_output=True,
        check=False,
    )
    assert (result.returncode == 0) is accepted


def test_upstream_tests_and_installed_smoke_are_not_replaced_by_presence():
    data = recipe()
    commands = data["build"]["commands"]
    setup = next(c for c in commands if c.startswith("meson setup"))
    for option in ("-Dtests=true", "-Dintrospection=true", "-Dvapi=true", "-Dsmoke-tests=false"):
        assert option in setup
    assert (
        commands[5]
        == "GDK_BACKEND=wayland GSK_RENDERER=cairo meson test -C build --no-rebuild --print-errorlogs"
    )
    assert "ext-session-lock-v1.xml" in commands[1]
    smoke = data["verify"]["smoke"]
    assert "--atleast-version=1.1 gtk4-layer-shell-0" in smoke
    assert "version==(1,3,0)" in smoke
    for symbol in (
        "gtk_session_lock_instance_lock",
        "gtk_session_lock_instance_unlock",
        "gtk_session_lock_instance_assign_window_to_monitor",
        "gtk_layer_init_for_window",
    ):
        assert symbol in smoke
    assert "is_supported()" not in smoke  # Does not attach to the caller's desktop.
    assert subprocess.run(["sh", "-n"], input=smoke, text=True, check=False).returncode == 0


def result_script(data):
    command = next(c for c in data["build"]["commands"] if c.startswith("python3 -c"))
    argv = shlex.split(command)
    assert argv[:2] == ["python3", "-c"]
    return argv[2]


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "skip",
        "missing",
        "duplicate",
        "wrong_result",
        "timeout",
        "error",
        "rc",
        "command",
        "start",
        "duration",
    ],
)
def test_actual_result_admission(tmp_path, bad):
    build = tmp_path / "build"
    logs = build / "meson-logs"
    logs.mkdir(parents=True)
    names = ["layer-probe", "lock-probe", "unit-test-get-version"]
    (build / "provider-admitted-tests.json").write_text(json.dumps([{"name": n} for n in names]))
    results = [
        {
            "name": n,
            "result": "OK",
            "returncode": 0,
            "command": ["/owned/test", "--auto"],
            "starttime": 12345.0,
            "duration": 0.1,
        }
        for n in names
    ]
    if bad == "missing":
        results.pop()
    elif bad == "duplicate":
        results[-1] = results[0].copy()
    elif bad in {"skip", "wrong_result", "timeout", "error"}:
        results[0]["result"] = {
            "skip": "SKIP",
            "wrong_result": "EXPECTEDFAIL",
            "timeout": "TIMEOUT",
            "error": "ERROR",
        }[bad]
    elif bad == "rc":
        results[0]["returncode"] = 77
    elif bad == "command":
        results[0]["command"] = []
    elif bad == "start":
        results[0]["starttime"] = None
    elif bad == "duration":
        results[0]["duration"] = -1
    (logs / "testlog.json").write_text("\n".join(json.dumps(r) for r in results) + "\n")
    result = subprocess.run(
        [sys.executable, "-O", "-c", result_script(recipe())],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is (bad is None)
    receipt = build / "provider-test-receipt.json"
    assert receipt.exists() is (bad is None)
    if bad is None:
        data = json.loads(receipt.read_text())
        assert data["passed"] == sorted(names)
        assert not data["hardware_qualified"]
        import hashlib

        assert (
            data["testlog_sha256"]
            == hashlib.sha256((logs / "testlog.json").read_bytes()).hexdigest()
        )


def test_logs_reach_existing_factory_artifact():
    data = recipe()
    commands = "\n".join(data["build"]["commands"])
    for filename in (
        "provider-admitted-tests.json",
        "provider-test-receipt.json",
        "testlog.json",
        "testlog.txt",
    ):
        assert filename in commands
    assert "%{_topdir}/provider-test-evidence/gtk4-layer-shell/" in commands
    runner = (ROOT / "scripts/run-package-factory-cell.sh").read_text()
    assert (
        'cp -a "$root/rpmbuild/provider-test-evidence/." "$out/artifacts/provider-tests/"' in runner
    )
    workflow = (ROOT / ".github/workflows/package-factory-cell.yml").read_text()
    assert ".factory/${{ matrix.id }}/artifacts/" in workflow
