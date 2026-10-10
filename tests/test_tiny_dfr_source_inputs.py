"""CI source capture trust boundaries, with registry/process boundary fakes."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
CAPTURE = ROOT / "scripts/capture-tiny-dfr-inputs-ci.sh"


def capture(tmp_path, *, ci="true", repository="tuna-os/tunaos-packages", host="aarch64",
            child_bad=False, config_bad=False, platform="arm64", duplicate=False):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fixture = tmp_path / "registry"
    fixture.mkdir()
    config = json.dumps({"os": "linux", "architecture": platform}).encode()
    config_digest = "sha256:" + hashlib.sha256(config).hexdigest()
    child = json.dumps({"config": {"digest": config_digest}}).encode()
    child_digest = "sha256:" + hashlib.sha256(child).hexdigest()
    descriptor = {"digest": child_digest, "platform": {"os": "linux", "architecture": "arm64"}}
    (fixture / "index").write_text(json.dumps({"manifests": [descriptor] * (2 if duplicate else 1)}))
    (fixture / "child").write_bytes(child + (b"tampered" if child_bad else b""))
    (fixture / "config").write_bytes(config + (b"tampered" if config_bad else b""))
    log = tmp_path / "calls"
    commands = {
        "uname": f"echo {host}\n",
        "sudo": 'exec "$@"\n',
        "skopeo": (
            f'printf "skopeo %s\\n" "$*" >> {log}\n'
            f'if [[ "$*" == *:latest ]]; then cat {fixture}/index; '
            f'elif [[ "$*" == *--config* ]]; then cat {fixture}/config; '
            f'else cat {fixture}/child; fi\n'
        ),
        "podman": f'printf "podman %s\\n" "$*" >> {log}\nexit 23\n',
    }
    for name, body in commands.items():
        path = bindir / name
        path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body)
        path.chmod(0o755)
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", GITHUB_ACTIONS=ci,
               GITHUB_REPOSITORY=repository, GITHUB_SHA="a" * 40,
               GITHUB_RUN_ID="10", GITHUB_RUN_ATTEMPT="2")
    result = subprocess.run(["bash", str(CAPTURE)], cwd=tmp_path, env=env,
                            capture_output=True, text=True)
    return result, log.read_text() if log.exists() else "", child_digest


def test_exact_native_child_and_raw_config_bound_before_native_execution(tmp_path):
    result, calls, digest = capture(tmp_path)
    assert result.returncode == 23, result.stderr
    assert calls.count(":latest") == 1
    assert f"--config --raw docker://registry.opensuse.org/opensuse/tumbleweed@{digest}" in calls
    assert f"registry.opensuse.org/opensuse/tumbleweed@{digest}" in calls.splitlines()[-1]
    assert "--interactive" in calls.splitlines()[-1]
    assert "--platform linux/arm64" in calls.splitlines()[-1]


@pytest.mark.parametrize("options", [
    {"ci": "false"}, {"repository": "fork/tunaos-packages"}, {"host": "x86_64"},
])
def test_wrong_producer_or_runner_refuses_before_registry(tmp_path, options):
    result, calls, _ = capture(tmp_path, **options)
    assert result.returncode != 0
    assert calls == ""


@pytest.mark.parametrize("options", [
    {"child_bad": True}, {"config_bad": True}, {"platform": "amd64"}, {"duplicate": True},
])
def test_registry_identity_failures_never_start_native_container(tmp_path, options):
    result, calls, _ = capture(tmp_path, **options)
    assert result.returncode != 0
    assert "podman " not in calls


def test_locked_source_and_license_capture_remains_nonready():
    source = CAPTURE.read_text()
    assert "COMMIT=6267754535c16bd2c1006b946aa032561d8432b3" in source
    assert "cargo vendor --locked vendor" in source
    assert "cmp Cargo.lock /output/Cargo.lock" in source
    assert "cargo metadata --locked --offline" in source
    assert "pkg-config --atleast-version=2.59 librsvg-2.0" in source
    assert "cp LICENSE LICENSE.material /output/" in source
    assert "'readiness': False" in source
    assert "'cpuCompatibilityVerified': False" in source
    assert "--no-gpg-checks" not in source
    assert "grep -qx 'host: aarch64-unknown-linux-gnu'" in source
    assert "zypper --non-interactive install --no-recommends" in source


def test_pull_requests_register_only_unprivileged_source_tests():
    workflow = yaml.safe_load((ROOT / ".github/workflows/tiny-dfr-source-inputs.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert "pull_request" in triggers and "workflow_dispatch" in triggers
    unit = workflow["jobs"]["source-boundaries"]
    assert unit["if"] == "github.event_name == 'pull_request'"
    assert "permissions" not in unit
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["capture"]["if"] == "github.event_name == 'workflow_dispatch'"
    assert any("tests/test_tiny_dfr_source_inputs.py" in step.get("run", "") for step in unit["steps"])


def test_native_container_inputs_survive_sudo_environment_filter():
    source = CAPTURE.read_text()
    for name in ("COMMIT", "CHILD", "GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"):
        assert '-e "'+name+'=$'+name+'"' in source
