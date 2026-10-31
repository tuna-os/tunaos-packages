import importlib.util
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "tideforge-intermediate.py"


def write_recipe(path: Path) -> None:
    path.write_text(
        """schema: 1
name: payload-canary
version: '1.0'
release: 1
summary: canary
description: canary
license: MIT
source:
  url: https://example.invalid/payload-canary.tar.gz
  sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
build_system: data
files: {common: [usr/share/payload-canary/message]}
dependencies:
  runtime:
    targets:
      ubuntu: [ubuntu-runtime]
      debian: [debian-runtime]
targets: [ubuntu, debian]
"""
    )


def intermediate_module():
    """Import the script by path — its filename is not an identifier."""
    spec = importlib.util.spec_from_file_location("tideforge_intermediate", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT / "scripts"))
    spec.loader.exec_module(module)
    return module


def run(*args: str, umask: int = -1) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=ROOT, check=True, text=True, stdout=subprocess.PIPE, umask=umask)


@pytest.mark.parametrize(
    ("directory_mode", "file_mode", "verify_umask"),
    [
        (0o755, 0o644, 0o077),
        (0o775, 0o664, 0o002),
        (0o750, 0o444, 0o022),
    ],
)
def test_one_payload_digest_is_reused_by_two_target_plans(
    tmp_path: Path, directory_mode: int, file_mode: int, verify_umask: int,
):
    recipe = tmp_path / "package.yaml"
    write_recipe(recipe)
    root = tmp_path / "root"
    message = root / "usr/share/payload-canary/message"
    message.parent.mkdir(parents=True)
    message.write_text("built once\n")
    for directory in (root / "usr", root / "usr/share", message.parent):
        directory.chmod(directory_mode)
    message.chmod(file_mode)
    intermediate = tmp_path / "payload.tfi.tar"

    created = json.loads(run("create", "--recipe", str(recipe), "--root", str(root), "--architecture", "x86_64", "--build-contract", "theory-sdk-v0", "--output", str(intermediate)).stdout)
    verified = json.loads(run("verify", str(intermediate), umask=verify_umask).stdout)
    assert verified["tree_sha256"] == created["tree_sha256"]
    module = intermediate_module()
    extracted = tmp_path / "extracted"
    manifest = module.extract_payload(intermediate, extracted)
    assert module.inventory(extracted / "payload") == manifest["files"]
    ubuntu = json.loads(run("plan", str(intermediate), "--recipe", str(recipe), "--target", "ubuntu").stdout)
    debian = json.loads(run("plan", str(intermediate), "--recipe", str(recipe), "--target", "debian").stdout)

    assert ubuntu["compile"] is debian["compile"] is False
    assert ubuntu["payload_tree_sha256"] == debian["payload_tree_sha256"] == created["tree_sha256"]
    assert ubuntu["declared_target_runtime_dependencies"] == ["ubuntu-runtime"]
    assert debian["declared_target_runtime_dependencies"] == ["debian-runtime"]


@pytest.mark.parametrize(
    ("name", "member_type", "linkname"),
    [
        ("payload/../../outside", tarfile.REGTYPE, ""),
        ("payload/link", tarfile.SYMTYPE, "/etc/passwd"),
        ("payload/link", tarfile.LNKTYPE, "/etc/passwd"),
        ("payload/fifo", tarfile.FIFOTYPE, ""),
    ],
)
def test_payload_extraction_retains_archive_safety_checks(
    tmp_path: Path, name: str, member_type: bytes, linkname: str,
):
    recipe = tmp_path / "package.yaml"
    write_recipe(recipe)
    root = tmp_path / "root"
    root.mkdir()
    intermediate = tmp_path / "payload.tfi.tar"
    run("create", "--recipe", str(recipe), "--root", str(root),
        "--architecture", "noarch", "--build-contract", "theory-sdk-v0",
        "--output", str(intermediate))
    with tarfile.open(intermediate, "a") as archive:
        member = tarfile.TarInfo(name)
        member.type = member_type
        member.linkname = linkname
        archive.addfile(member)
    with pytest.raises((SystemExit, tarfile.FilterError)):
        intermediate_module().extract_payload(intermediate, tmp_path / "extracted")


def test_verifier_rejects_changed_ordinary_permissions(tmp_path: Path):
    recipe = tmp_path / "package.yaml"
    write_recipe(recipe)
    root = tmp_path / "root"
    root.mkdir()
    entry = root / "entry"
    entry.write_text("payload\n")
    entry.chmod(0o644)
    intermediate = tmp_path / "payload.tfi.tar"
    run("create", "--recipe", str(recipe), "--root", str(root),
        "--architecture", "noarch", "--build-contract", "theory-sdk-v0",
        "--output", str(intermediate))
    with tarfile.open(intermediate, "a") as archive:
        archive.add(entry, arcname="payload/entry", recursive=False,
                    filter=lambda member: member.replace(mode=0o640, deep=False))
    with pytest.raises(subprocess.CalledProcessError):
        run("verify", str(intermediate))


@pytest.mark.parametrize("mode", [0o4755, 0o2755, 0o1755])
@pytest.mark.parametrize("is_directory", [False, True])
def test_payload_rejects_privileged_modes(tmp_path: Path, mode: int, is_directory: bool):
    recipe = tmp_path / "package.yaml"
    write_recipe(recipe)
    root = tmp_path / "root"
    entry = root / "entry"
    root.mkdir()
    if is_directory:
        entry.mkdir()
    else:
        entry.write_text("payload\n")
    intermediate = tmp_path / "payload.tfi.tar"
    run("create", "--recipe", str(recipe), "--root", str(root),
        "--architecture", "noarch", "--build-contract", "theory-sdk-v0",
        "--output", str(intermediate))

    entry.chmod(mode)
    rejected = tmp_path / "rejected.tfi.tar"
    with pytest.raises(subprocess.CalledProcessError):
        run("create", "--recipe", str(recipe), "--root", str(root),
            "--architecture", "noarch", "--build-contract", "theory-sdk-v0",
            "--output", str(rejected))
    assert not rejected.exists()

    with tarfile.open(intermediate, "a") as archive:
        member = tarfile.TarInfo("payload/entry")
        member.type = tarfile.DIRTYPE if is_directory else tarfile.REGTYPE
        member.mode = mode
        archive.addfile(member)
    with pytest.raises(SystemExit, match="unsupported payload mode"):
        intermediate_module().extract_payload(intermediate, tmp_path / "extracted")


def test_handler_detects_portable_data_without_manifest_opt_in(tmp_path: Path):
    recipe = tmp_path / "package.yaml"
    write_recipe(recipe)
    contract = json.loads(run("classify", "--recipe", str(recipe)).stdout)
    assert contract == {"architecture": "noarch", "mode": "portable-payload"}


def test_handler_rejects_go_recipe_that_requests_cgo(tmp_path: Path):
    recipe = tmp_path / "package.yaml"
    write_recipe(recipe)
    text = recipe.read_text().replace(
        "build_system: data\n",
        "build_system: go\nbuild:\n  environment: {CGO_ENABLED: '1'}\n",
    )
    recipe.write_text(text)
    assert json.loads(run("classify", "--recipe", str(recipe)).stdout) is None


def test_candidate_planner_uses_handler_and_actual_recipe_targets(tmp_path: Path):
    packages = tmp_path / "packages"
    recipe = packages / "payload-canary" / "package.yaml"
    recipe.parent.mkdir(parents=True)
    write_recipe(recipe)
    result = json.loads(run(
        "candidates", "--root", str(packages), "--architecture", "x86_64",
        "--targets", "el10", "ubuntu", "debian",
    ).stdout)
    assert result["payload_count"] == 1
    assert result["carrier_count"] == 2
    assert result["payloads"]["include"][0]["payload_architecture"] == "noarch"
    assert {row["target"] for row in result["carriers"]["include"]} == {"ubuntu", "debian"}
    assert {row["architecture"] for row in result["carriers"]["include"]} == {"amd64"}
    assert {row["format"] for row in result["carriers"]["include"]} == {"deb"}


def test_intermediate_is_reproducible(tmp_path: Path, monkeypatch):
    recipe = tmp_path / "package.yaml"
    write_recipe(recipe)
    root = tmp_path / "root"
    payload = root / "usr/bin/canary"
    payload.parent.mkdir(parents=True)
    payload.write_text("hello\n")
    payload.chmod(0o755)
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1234")
    outputs = [tmp_path / "one.tar", tmp_path / "two.tar"]
    for output in outputs:
        run("create", "--recipe", str(recipe), "--root", str(root), "--architecture", "x86_64", "--build-contract", "theory-sdk-v0", "--output", str(output))
    assert outputs[0].read_bytes() == outputs[1].read_bytes()
    with tarfile.open(outputs[0]) as archive:
        assert {member.uid for member in archive.getmembers()} == {0}


def test_intermediate_records_dynamic_elf_contract(tmp_path: Path):
    recipe = tmp_path / "package.yaml"
    write_recipe(recipe)
    root = tmp_path / "root"
    binary = root / "usr/bin/payload-canary"
    binary.parent.mkdir(parents=True)
    subprocess.run(
        ["gcc", "-x", "c", "-", "-o", str(binary)],
        input="#include <stdio.h>\nint main(void) { return puts(\"hello\") < 0; }\n",
        text=True,
        check=True,
    )
    intermediate = tmp_path / "payload.tfi.tar"
    run("create", "--recipe", str(recipe), "--root", str(root), "--architecture", "x86_64", "--build-contract", "host-glibc-theory", "--output", str(intermediate))
    plan = json.loads(run("plan", str(intermediate), "--recipe", str(recipe), "--target", "ubuntu").stdout)

    assert plan["elf_contracts"][0]["path"] == "usr/bin/payload-canary"
    assert "libc.so.6" in plan["elf_contracts"][0]["needed"]
    assert any(version.startswith("GLIBC_") for version in plan["elf_contracts"][0]["symbol_versions"])


def test_one_intermediate_packages_for_both_deb_targets_without_recompile(tmp_path: Path):
    recipe = tmp_path / "package.yaml"
    write_recipe(recipe)
    root = tmp_path / "root"
    message = root / "usr/share/payload-canary/message"
    message.parent.mkdir(parents=True)
    message.write_text("built once\n")
    intermediate = tmp_path / "payload.tfi.tar"
    run("create", "--recipe", str(recipe), "--root", str(root), "--architecture", "noarch", "--build-contract", "theory-sdk-v0", "--output", str(intermediate))

    payload_digests = set()
    for target in ("ubuntu", "debian"):
        output = tmp_path / target
        result = json.loads(run("package", str(intermediate), "--recipe", str(recipe), "--target", target, "--output-dir", str(output)).stdout.splitlines()[-1])
        payload_digests.add(result["payload_tree_sha256"])
        package = next(output.glob("*.deb"))
        control = subprocess.run(["dpkg-deb", "-f", package, "Depends"], check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
        assert control == f"{target}-runtime"
    assert len(payload_digests) == 1


# --- the carrier must repackage faithfully, not approximately ----------------


def write_tree_recipe(path: Path) -> None:
    """A recipe that declares a DIRECTORY, the way `dms` does."""
    path.write_text(
        """schema: 1
name: payload-canary
version: '1.0'
release: 1
summary: canary
description: |-
  first line
  second line
license: MIT
source:
  url: https://example.invalid/payload-canary.tar.gz
  sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
build_system: data
files: {common: [usr/share/payload-canary]}
dependencies:
  runtime:
    targets:
      ubuntu: [ubuntu-runtime]
      debian: [debian-runtime]
targets: [ubuntu, debian]
"""
    )


def seal_tree(tmp_path: Path) -> tuple[Path, Path]:
    recipe = tmp_path / "package.yaml"
    write_tree_recipe(recipe)
    root = tmp_path / "root"
    nested = root / "usr/share/payload-canary/nested"
    nested.mkdir(parents=True)
    (nested / "message").write_text("built once\n")
    intermediate = tmp_path / "payload.tfi.tar"
    run("create", "--recipe", str(recipe), "--root", str(root),
        "--architecture", "noarch", "--build-contract", "theory-sdk-v0",
        "--output", str(intermediate))
    return recipe, intermediate


def test_the_rpm_carrier_owns_the_directories_the_native_spec_owns(tmp_path: Path):
    """A declared directory must reach %files as the directory.

    Expanding the payload inventory instead lists each file and owns none of
    the directories holding them, so `rpm -e` leaves the tree behind and the
    portable carrier stops being a faithful repackaging of the native one.
    """
    recipe, _ = seal_tree(tmp_path)
    loaded = yaml.safe_load(recipe.read_text())
    rendered = intermediate_module().rpm_files(loaded)
    assert rendered == "/usr/share/payload-canary"
    assert "nested" not in rendered, (
        "a declared directory must not be expanded into its members"
    )


def test_a_multi_line_description_stays_one_deb_control_field(tmp_path: Path):
    """A bare blank/unprefixed line ENDS a deb822 field.

    Written the obvious way, the second line of a multi-line description
    terminates Description and the rest of the control file is lost.
    """
    recipe, intermediate = seal_tree(tmp_path)
    output = tmp_path / "ubuntu"
    run("package", str(intermediate), "--recipe", str(recipe),
        "--target", "ubuntu", "--output-dir", str(output))
    package = next(output.glob("*.deb"))
    fields = subprocess.run(
        ["dpkg-deb", "-f", package], check=True, text=True,
        stdout=subprocess.PIPE).stdout
    # The field survived in full...
    assert "first line" in fields and "second line" in fields
    # ...and the fields written AFTER it are still there, which is what a
    # truncated Description would have eaten.
    assert "Depends: ubuntu-runtime" in fields
    description = subprocess.run(
        ["dpkg-deb", "-f", package, "Description"], check=True, text=True,
        stdout=subprocess.PIPE).stdout
    assert "second line" in description


def test_portable_arch_carriers_use_the_exact_native_child(tmp_path: Path):
    """CI38081885545: replacing scalar Arch probe_image broke carrier planning."""
    packages = tmp_path / 'packages'
    recipe = packages / 'payload-canary' / 'package.yaml'
    recipe.parent.mkdir(parents=True)
    write_recipe(recipe)
    recipe.write_text(recipe.read_text().replace('targets: [ubuntu, debian]', 'targets: [ubuntu, arch]'))
    expected = {
        'x86_64': 'docker.io/library/archlinux@sha256:2fd1ae548076c67bcd41098ddd44e799058010e8d55f085d453dbb890e706860',
        'aarch64': 'ghcr.io/tuna-os/archlinuxarm@sha256:17ad528bbccd4b59b7ce18c652012916a4bf2e27de53606884ef61eb33c90a0d',
    }
    for architecture, child in expected.items():
        result = json.loads(run('candidates', '--root', str(packages), '--architecture', architecture,
                                '--targets', 'ubuntu', 'arch').stdout)
        carrier = next(row for row in result['carriers']['include'] if row['target'] == 'arch')
        assert carrier['image'] == child
        assert carrier['architecture'] == architecture
        assert carrier['format'] == 'pkg.tar.zst'
