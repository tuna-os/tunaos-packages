import importlib.util
import subprocess
from pathlib import Path


def load_script(name):
    path = Path(__file__).parent.parent / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_").removesuffix(".py"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


oras_stage = load_script("oras-stage-artifacts.py")


def test_sanitize_tag_strips_rpm_suffix():
    assert oras_stage.sanitize_tag("vte291-0.84.1-1.bfin1.x86_64.rpm") == \
        "vte291-0.84.1-1.bfin1.x86_64"


def test_sanitize_tag_collapses_tilde():
    tag = oras_stage.sanitize_tag("gnome-shell-51~beta-2.bfin1.x86_64.rpm")
    assert "~" not in tag
    assert tag == "gnome-shell-51_beta-2.bfin1.x86_64"


def test_sanitize_tag_collapses_caret():
    tag = oras_stage.sanitize_tag("quickshell-0.2.1^git20260209.dacfa9d.fc43.x86_64.rpm")
    assert "^" not in tag


def test_sanitize_tag_is_valid_oci_tag_charset():
    import re
    valid = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9._-]{0,127}$")
    for name in (
        "gnome-shell-51~beta-2.bfin1.x86_64.rpm",
        "mozjs140-140.13.0-3.bfin1.x86_64.rpm",
        "~starts-with-tilde-1-1.bfin1.noarch.rpm",
    ):
        assert valid.match(oras_stage.sanitize_tag(name)), name


def test_push_noop_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(oras_stage, "ORAS_PUSH_ENABLED", False)
    (tmp_path / "foo-1-1.bfin1.x86_64.rpm").write_bytes(b"rpm bytes")
    assert oras_stage.push(tmp_path) == 0


def test_push_calls_oras_once_per_rpm(tmp_path, monkeypatch):
    monkeypatch.setattr(oras_stage, "ORAS_PUSH_ENABLED", True)
    (tmp_path / "foo-1-1.bfin1.x86_64.rpm").write_bytes(b"a")
    (tmp_path / "bar-2-1.bfin1.x86_64.rpm").write_bytes(b"b")
    (tmp_path / "not-an-rpm.txt").write_text("ignore me")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert oras_stage.push(tmp_path) == 2
    assert len(calls) == 2
    for cmd in calls:
        assert cmd[:3] == ["oras", "push", "--plain-http=false"]
        assert cmd[3].startswith(f"{oras_stage.ORAS_REPO}:")


def test_push_continues_after_one_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(oras_stage, "ORAS_PUSH_ENABLED", True)
    (tmp_path / "foo-1-1.bfin1.x86_64.rpm").write_bytes(b"a")
    (tmp_path / "bar-2-1.bfin1.x86_64.rpm").write_bytes(b"b")

    def fake_run(cmd, **kwargs):
        if "foo" in cmd[3]:
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert oras_stage.push(tmp_path) == 1


def test_list_tags_parses_output(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="tag-one\ntag-two\n\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert oras_stage.list_tags() == ["tag-one", "tag-two"]


def test_list_tags_empty_on_failure(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert oras_stage.list_tags() == []


def test_pull_all_pulls_every_tag(tmp_path, monkeypatch):
    monkeypatch.setattr(oras_stage, "list_tags", lambda: ["a-1-1.bfin1.x86_64", "b-1-1.bfin1.noarch"])
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert oras_stage.pull_all(tmp_path, ".bfin1", "x86_64") == 2
    assert len(calls) == 2
    assert calls[0][:3] == ["oras", "pull", "--plain-http=false"]
    assert calls[0][3] == f"{oras_stage.ORAS_REPO}:a-1-1.bfin1.x86_64"
    assert str(tmp_path) in calls[0]


def test_pull_all_continues_after_one_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(oras_stage, "list_tags", lambda: ["good-1-1.bfin1.x86_64", "bad-1-1.bfin1.x86_64"])

    def fake_run(cmd, **kwargs):
        if "bad-" in cmd[3]:
            raise subprocess.TimeoutExpired(cmd, 120)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert oras_stage.pull_all(tmp_path, ".bfin1", "x86_64") == 1


def test_pull_all_creates_output_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(oras_stage, "list_tags", lambda: [])
    target = tmp_path / "nested" / "output"
    assert oras_stage.pull_all(target, ".bfin1", "x86_64") == 0
    assert target.is_dir()


def test_pull_all_takes_only_the_cells_dist_and_arch(tmp_path, monkeypatch):
    """The ORAS repo is shared by every family; a hummingbird publish must not
    sign .el10 RPMs into its prefix, nor x86_64 ones into aarch64 (#832)."""
    monkeypatch.setattr(oras_stage, "list_tags", lambda: [
        "tuna-desktop-1.0.0_beta.1-1.bfin1.x86_64",
        "tuna-desktop-1.0.0_beta.1-1.bfin1.aarch64",
        "adwaita-fonts-50.0-1.bfin1.noarch",
        "gtk4-4.20.0-1.el10.x86_64",
        "libfoo-1.0-2.bfin1.1.x86_64",
        "libbar-1.0-2.bfin10.x86_64",
    ])
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd[3].rpartition(":")[2])
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert oras_stage.pull_all(tmp_path, ".bfin1", "x86_64") == 3
    assert calls == [
        "tuna-desktop-1.0.0_beta.1-1.bfin1.x86_64",
        "adwaita-fonts-50.0-1.bfin1.noarch",
        "libfoo-1.0-2.bfin1.1.x86_64",
    ]
