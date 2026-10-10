#!/usr/bin/env python3
"""Theory prototype: seal one staged payload for cheap native repackaging.

It explores the boundary between an expensive upstream build/install and the
comparatively cheap, target-specific package metadata step.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile

import yaml

import tideforge
from target_platform import build_context


SCHEMA = 0


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalized_path(path: str) -> str:
    value = str(PurePosixPath(path)).lstrip("/")
    if not value or value == "." or ".." in PurePosixPath(value).parts:
        raise SystemExit(f"unsafe payload path: {path!r}")
    return value


def elf_contract(path: Path) -> dict | None:
    if not path.is_file() or path.is_symlink():
        return None
    with path.open("rb") as stream:
        magic = stream.read(4)
    if magic != b"\x7fELF":
        return None
    proc = subprocess.run(
        ["readelf", "--wide", "--dynamic", "--version-info", "--program-headers", str(path)],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    needed: list[str] = []
    versions: set[str] = set()
    interpreter = ""
    for line in proc.stdout.splitlines():
        if "(NEEDED)" in line and "[" in line:
            needed.append(line.rsplit("[", 1)[1].split("]", 1)[0])
        if "Requesting program interpreter:" in line:
            interpreter = line.split("Requesting program interpreter:", 1)[1].strip().rstrip("]")
        for token in line.replace("(", " ").replace(")", " ").split():
            if token.startswith(("GLIBC_", "GLIBCXX_", "CXXABI_")):
                versions.add(token.rstrip("[]"))
    return {
        "needed": sorted(set(needed)),
        "symbol_versions": sorted(versions),
        "interpreter": interpreter,
        "static": not needed and not interpreter,
    }


def inventory(root: Path) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(root.rglob("*")):
        relative = normalized_path(path.relative_to(root).as_posix())
        mode = stat.S_IMODE(path.lstat().st_mode)
        if path.is_symlink():
            rows.append({"path": relative, "type": "symlink", "mode": f"{mode:04o}", "target": os.readlink(path)})
        elif path.is_dir():
            rows.append({"path": relative, "type": "directory", "mode": f"{mode:04o}"})
        elif path.is_file():
            row = {"path": relative, "type": "file", "mode": f"{mode:04o}", "size": path.stat().st_size, "sha256": sha256(path)}
            elf = elf_contract(path)
            if elf is not None:
                row["elf"] = elf
            rows.append(row)
    return rows


def tree_digest(rows: list[dict]) -> str:
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def add_bytes(archive: tarfile.TarFile, name: str, payload: bytes, epoch: int, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = mode
    info.mtime = epoch
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    archive.addfile(info, io.BytesIO(payload))


def normalized_tarinfo(epoch: int):
    def filter_info(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        info.mtime = epoch
        return info
    return filter_info


def create(args: argparse.Namespace) -> None:
    recipe = yaml.safe_load(args.recipe.read_text())
    rows = inventory(args.root)
    manifest = {
        "schema": SCHEMA,
        "kind": "tideforge-staged-payload",
        "package": recipe["name"],
        "version": str(recipe["version"]),
        "release": int(recipe.get("release", 1)),
        "architecture": args.architecture,
        "build_contract": args.build_contract,
        "source_sha256": recipe["source"]["sha256"],
        "tree_sha256": tree_digest(rows),
        "files": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
    with tarfile.open(args.output, "w", format=tarfile.PAX_FORMAT) as archive:
        add_bytes(archive, "manifest.json", (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(), epoch)
        for path in sorted(args.root.rglob("*")):
            name = f"payload/{normalized_path(path.relative_to(args.root).as_posix())}"
            archive.add(path, arcname=name, recursive=False, filter=normalized_tarinfo(epoch))
    print(json.dumps({"intermediate": str(args.output), "tree_sha256": manifest["tree_sha256"], "files": len(rows)}, sort_keys=True))


def read_manifest(path: Path) -> dict:
    with tarfile.open(path, "r:*") as archive:
        member = archive.getmember("manifest.json")
        stream = archive.extractfile(member)
        if stream is None:
            raise SystemExit("intermediate has no readable manifest.json")
        manifest = json.load(stream)
    if manifest.get("schema") != SCHEMA or manifest.get("kind") != "tideforge-staged-payload":
        raise SystemExit("unsupported Tideforge intermediate")
    return manifest


def verify(args: argparse.Namespace) -> None:
    expected = read_manifest(args.intermediate)
    with tempfile.TemporaryDirectory(prefix="tideforge-intermediate-") as tmp:
        root = Path(tmp)
        with tarfile.open(args.intermediate, "r:*") as archive:
            for member in archive.getmembers():
                if member.name == "manifest.json":
                    continue
                normalized_path(member.name)
            archive.extractall(root, filter="data")
        actual_rows = inventory(root / "payload")
    actual = tree_digest(actual_rows)
    if actual != expected["tree_sha256"]:
        raise SystemExit(f"payload tree mismatch: expected {expected['tree_sha256']}, got {actual}")
    print(json.dumps({"package": expected["package"], "architecture": expected["architecture"], "tree_sha256": actual}, sort_keys=True))


def plan(args: argparse.Namespace) -> None:
    manifest = read_manifest(args.intermediate)
    recipe = yaml.safe_load(args.recipe.read_text())
    if manifest["package"] != recipe["name"] or manifest["version"] != str(recipe["version"]):
        raise SystemExit("recipe identity does not match intermediate")
    elf_files = [row for row in manifest["files"] if "elf" in row]
    target_deps = recipe.get("dependencies", {}).get("runtime", {}).get("targets", {}).get(args.target, [])
    print(json.dumps({
        "package": manifest["package"],
        "target": args.target,
        "architecture": manifest["architecture"],
        "payload_tree_sha256": manifest["tree_sha256"],
        "compile": False,
        "target_work": ["translate dependency names", "split files into native subpackages", "generate native metadata/scriptlets", "lint", "clean-install and smoke-test"],
        "declared_target_runtime_dependencies": target_deps,
        "elf_contracts": [{"path": row["path"], **row["elf"]} for row in elf_files],
    }, indent=2, sort_keys=True))


def reuse_contract(recipe: dict) -> dict:
    contract = tideforge.portable_payload_contract(recipe)
    if contract is None:
        raise SystemExit("Tideforge does not classify this recipe as a portable payload")
    return contract


def copy_directory(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        target = destination / child.name
        if child.is_dir() and not child.is_symlink():
            shutil.copytree(child, target, symlinks=True, dirs_exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, target, follow_symlinks=False)


def stage_declared_installs(recipe: dict, source_root: Path, stage: Path) -> None:
    for item in recipe.get("install", {}).get("directories", []):
        source_name = item["source"]
        source = source_root if source_name == "." else source_root / normalized_path(source_name)
        copy_directory(source, stage / normalized_path(item["destination"]))
    for item in recipe.get("install", {}).get("files", []):
        source = source_root / normalized_path(item["source"])
        destination = stage / normalized_path(item["destination"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination, follow_symlinks=False)
        destination.chmod(int(str(item.get("mode", "0644")), 8))
    for item in recipe.get("install", {}).get("generated_files", []):
        destination = stage / normalized_path(item["destination"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(item["content"])
        destination.chmod(int(str(item.get("mode", "0644")), 8))


def build(args: argparse.Namespace) -> None:
    recipe = yaml.safe_load(args.recipe.read_text())
    contract = reuse_contract(recipe)
    architecture = "noarch" if contract["architecture"] == "noarch" else args.architecture
    if architecture not in {"noarch", "x86_64", "aarch64"}:
        raise SystemExit(f"unsupported portable architecture: {architecture}")
    with tempfile.TemporaryDirectory(prefix="tideforge-portable-build-") as tmp:
        root = Path(tmp)
        downloads = root / "downloads"
        subprocess.run(
            [sys.executable, str(Path(__file__).with_name("fetch-tideforge-sources.py")), str(args.recipe), str(downloads), "--cache-dir", str(args.cache_dir)],
            check=True,
        )
        extracted = root / "source"
        extracted.mkdir()
        archive_path = downloads / tideforge.source_filename(recipe["source"], 0)
        with tarfile.open(archive_path, "r:*") as archive:
            archive.extractall(extracted, filter="data")
        source_directory = recipe["source"].get("directory", f"{recipe['name']}-{recipe['version']}")
        source_root = extracted if source_directory == "." else extracted / normalized_path(source_directory)
        if not source_root.is_dir():
            raise SystemExit(f"source directory was not extracted: {source_directory}")
        stage = root / "stage"
        stage.mkdir()
        if recipe["build_system"] == "go":
            prepare = tideforge.prepare_commands(recipe)
            if prepare:
                subprocess.run(["bash", "-euo", "pipefail", "-c", prepare], cwd=source_root, check=True)
            workdir_name = tideforge.build_option(recipe, "working_directory", ".")
            workdir = source_root if workdir_name == "." else source_root / normalized_path(workdir_name)
            binary = tideforge.build_option(recipe, "binary", recipe["name"])
            package = tideforge.build_option(recipe, "go_package", ".")
            # The normal native packages use PIE. With Go's internal linker,
            # that still records the host ELF interpreter and is therefore not
            # a fully static payload. The portable contract deliberately uses
            # the default executable mode and then rejects any DT_NEEDED or
            # interpreter entry below. Native lint/install gates decide
            # whether that hardening trade-off is acceptable for promotion.
            command = tideforge.go_build_command(recipe, binary, package, buildmode="")
            environment = os.environ.copy()
            environment.update({str(key): str(value) for key, value in recipe.get("build", {}).get("environment", {}).items()})
            environment.update({"CGO_ENABLED": "0", "GOOS": "linux", "GOARCH": {"x86_64": "amd64", "aarch64": "arm64"}[architecture]})
            subprocess.run(["bash", "-euo", "pipefail", "-c", command], cwd=workdir, env=environment, check=True)
            destination = stage / "usr/bin" / binary
            destination.parent.mkdir(parents=True)
            shutil.copy2(workdir / binary, destination)
            destination.chmod(0o755)
        stage_declared_installs(recipe, source_root, stage)
        create(argparse.Namespace(
            recipe=args.recipe,
            root=stage,
            architecture=architecture,
            build_contract=args.build_contract,
            output=args.output,
        ))
    manifest = read_manifest(args.output)
    if recipe["build_system"] == "go":
        elfs = [row for row in manifest["files"] if "elf" in row]
        if not elfs or any(not row["elf"]["static"] for row in elfs):
            raise SystemExit("portable Go payload is not fully static")


def extract_payload(intermediate: Path, destination: Path) -> dict:
    manifest = read_manifest(intermediate)
    with tarfile.open(intermediate, "r:*") as archive:
        for member in archive.getmembers():
            normalized_path(member.name)
        archive.extractall(destination, filter="data")
    return manifest


def portable_package_name(recipe: dict, target: str) -> str:
    packages = recipe.get("outputs", {}).get("deb", {}).get("packages", [])
    if target in {"ubuntu", "debian"} and packages:
        return packages[0]["name"]
    return recipe["name"]


def deb_description(text: str) -> str:
    """Render the extended description as deb822 continuation lines.

    Every line after the synopsis carries a leading space, and an empty one
    becomes " ." — a bare blank line ENDS the field, so a multi-line
    description written the obvious way silently truncates the control file.
    """
    return "".join(f" {line}\n" if line.strip() else " .\n" for line in text.splitlines())


def package_deb(recipe: dict, target: str, manifest: dict, payload: Path, output: Path) -> Path:
    name = portable_package_name(recipe, target)
    architecture = {"x86_64": "amd64", "aarch64": "arm64", "noarch": "all"}[manifest["architecture"]]
    root = output / f"{name}-deb-root"
    copy_directory(payload, root)
    control = root / "DEBIAN/control"
    control.parent.mkdir(parents=True)
    dependencies = tideforge.target_runtime_dependencies(recipe, target)
    control.write_text(
        f"Package: {name}\nVersion: {recipe['version']}-{recipe.get('release', 1)}\n"
        f"Architecture: {architecture}\nMaintainer: TunaOS Package Factory <packages@tunaos.org>\n"
        f"Description: {recipe['summary']}\n{deb_description(recipe['description'])}"
        + (f"Depends: {', '.join(dependencies)}\n" if dependencies else "")
    )
    artifact = output / f"{name}_{recipe['version']}-{recipe.get('release', 1)}_{architecture}.deb"
    subprocess.run(
        ["dpkg-deb", "--root-owner-group", "--uniform-compression", "-Zxz", "--build", str(root), str(artifact)],
        check=True,
    )
    return artifact


def rpm_files(recipe: dict) -> str:
    """The %files body, rendered from the recipe's DECLARED paths."""
    rpm_output = recipe.get("outputs", {}).get("rpm", {})
    return "\n".join(
        f"/{path.lstrip('/')}"
        for path in rpm_output.get("files", recipe["files"]["common"])
    )


def package_rpm(recipe: dict, target: str, manifest: dict, intermediate: Path, output: Path) -> Path:
    top = output / "rpmbuild"
    for name in ("BUILD", "BUILDROOT", "RPMS", "SOURCES", "SPECS", "SRPMS"):
        (top / name).mkdir(parents=True, exist_ok=True)
    source = top / "SOURCES" / intermediate.name
    shutil.copy2(intermediate, source)
    dependencies = "\n".join(f"Requires: {name}" for name in tideforge.target_runtime_dependencies(recipe, target))
    # The SAME expression the native renderer uses (scripts/tideforge.py), so
    # the two carriers cannot disagree about what the package owns. Expanding
    # the payload inventory instead would list every file individually and own
    # none of the directories holding them: `dms` declares the directory
    # /usr/share/dankmaterialshell, which RPM reads as "this tree is mine", and
    # a file-by-file %files would leave those directories behind on erase.
    # It also buys a real check for free — rpmbuild fails the build on a file
    # staged into the buildroot that %files does not cover, so a payload that
    # drifts from the recipe's declaration cannot be packaged silently.
    files = rpm_files(recipe)
    rpm_arch = manifest["architecture"]
    release_suffix = {"el10": "tfi.el10", "opensuse-tumbleweed": "tfi.tw"}.get(target, "tfi")
    spec = top / "SPECS" / f"{recipe['name']}.spec"
    spec.write_text(f"""Name: {recipe['name']}
Version: {recipe['version']}
Release: {recipe.get('release', 1)}.{release_suffix}
Summary: {recipe['summary']}
License: {recipe['license']}
Source0: {intermediate.name}
BuildArch: {rpm_arch}
{dependencies}

%description
{recipe['description']}

%prep
:

%build
:

%install
mkdir -p %{{buildroot}}
tar -xf %{{SOURCE0}} --strip-components=1 -C %{{buildroot}} payload

%files
{files}

%changelog
* Wed Aug 26 2026 TunaOS Package Factory <packages@tunaos.org> - {recipe['version']}-{recipe.get('release', 1)}
- Repacked from a verified Tideforge portable payload.
""")
    subprocess.run(["rpmbuild", "-bb", "--define", f"_topdir {top}", "--target", rpm_arch, str(spec)], check=True)
    artifacts = list((top / "RPMS").rglob("*.rpm"))
    if len(artifacts) != 1:
        raise SystemExit(f"expected one RPM artifact, got {len(artifacts)}")
    destination = output / artifacts[0].name
    shutil.copy2(artifacts[0], destination)
    return destination


def package_arch(recipe: dict, target: str, manifest: dict, payload: Path, output: Path) -> Path:
    name = recipe["name"]
    architecture = {"x86_64": "x86_64", "aarch64": "aarch64", "noarch": "any"}[manifest["architecture"]]
    root = output / f"{name}-arch-root"
    copy_directory(payload, root)
    installed_size = sum(row.get("size", 0) for row in manifest["files"])
    dependencies = "".join(f"depend = {dep}\n" for dep in tideforge.target_runtime_dependencies(recipe, target))
    (root / ".PKGINFO").write_text(
        f"pkgname = {name}\npkgbase = {name}\npkgver = {recipe['version']}-{recipe.get('release', 1)}\n"
        f"pkgdesc = {recipe['summary']}\nurl = {recipe['source']['url']}\nbuilddate = {os.environ.get('SOURCE_DATE_EPOCH', '0')}\n"
        f"packager = TunaOS Package Factory <packages@tunaos.org>\nsize = {installed_size}\n"
        f"arch = {architecture}\nlicense = {recipe['license']}\n{dependencies}"
    )
    artifact = output / f"{name}-{recipe['version']}-{recipe.get('release', 1)}-{architecture}.pkg.tar.zst"
    members = " ".join(shlex.quote(path.name) for path in sorted(root.iterdir()))
    command = f"tar --sort=name --mtime=@${{SOURCE_DATE_EPOCH:-0}} --owner=0 --group=0 --numeric-owner -C {shlex.quote(str(root))} -cf - {members} | zstd -19 -T0 -o {shlex.quote(str(artifact))}"
    subprocess.run(["bash", "-euo", "pipefail", "-c", command], check=True)
    return artifact


def package(args: argparse.Namespace) -> None:
    recipe = yaml.safe_load(args.recipe.read_text())
    reuse_contract(recipe)
    if args.target not in recipe["targets"]:
        raise SystemExit(f"recipe does not enable target: {args.target}")
    target = tideforge.load_targets()[args.target]
    args.output_dir = args.output_dir.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tideforge-portable-package-") as tmp:
        extracted = Path(tmp)
        manifest = extract_payload(args.intermediate, extracted)
        payload = extracted / "payload"
        if target["format"] == "deb":
            artifact = package_deb(recipe, args.target, manifest, payload, args.output_dir)
        elif target["format"] == "rpm":
            artifact = package_rpm(recipe, args.target, manifest, args.intermediate, args.output_dir)
        elif target["format"] == "pkg.tar.zst":
            artifact = package_arch(recipe, args.target, manifest, payload, args.output_dir)
        else:
            raise SystemExit(f"unsupported target package format: {target['format']}")
    print(json.dumps({"target": args.target, "artifact": str(artifact), "payload_tree_sha256": manifest["tree_sha256"]}, sort_keys=True))


def candidates(args: argparse.Namespace) -> None:
    """Emit the multi-target recipes selected by Tideforge's handler."""
    requested_targets = set(args.targets)
    target_contracts = tideforge.load_targets()
    payloads = []
    carriers = []
    for recipe_path in sorted(args.root.glob("*/package.yaml")):
        if recipe_path.parent.name.startswith("_"):
            continue
        recipe = yaml.safe_load(recipe_path.read_text())
        contract = tideforge.portable_payload_contract(recipe)
        # Shared compiled payloads have no Alma baseline proof. Keep these
        # consumers on their actual native compiler/buildroot path.
        targets = [target for target in recipe.get("targets", []) if target in requested_targets
                   and target not in {"alma10", "alma10-kitten"}]
        # A one-target recipe cannot save a duplicate build, so leave it on
        # the native path even if its bytes happen to satisfy the handler.
        if contract is None or len(targets) < 2:
            continue
        package = recipe["name"]
        payloads.append({
            "package": package,
            "recipe": str(recipe_path),
            "architecture": args.architecture,
            "payload_architecture": contract["architecture"],
        })
        for target in targets:
            target_contract = target_contracts[target]
            native_architecture = args.architecture
            if target_contract["format"] == "deb":
                native_architecture = {"x86_64": "amd64", "aarch64": "arm64"}[args.architecture]
            carriers.append({
                "package": package,
                "recipe": str(recipe_path),
                "architecture": native_architecture,
                "payload_architecture": args.architecture,
                "target": target,
                "format": target_contract["format"],
                "image": build_context(target_contract, args.architecture)["image"],
            })
    result = {
        "payloads": {"include": payloads},
        "carriers": {"include": carriers},
        "payload_count": len(payloads),
        "carrier_count": len(carriers),
    }
    print(json.dumps(result, sort_keys=True))
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"payload_matrix={json.dumps(result['payloads'], separators=(',', ':'))}\n")
            output.write(f"carrier_matrix={json.dumps(result['carriers'], separators=(',', ':'))}\n")
            output.write(f"payload_count={result['payload_count']}\n")
            output.write(f"carrier_count={result['carrier_count']}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create_parser = sub.add_parser("create")
    create_parser.add_argument("--recipe", type=Path, required=True)
    create_parser.add_argument("--root", type=Path, required=True)
    create_parser.add_argument("--architecture", required=True)
    create_parser.add_argument("--build-contract", required=True)
    create_parser.add_argument("--output", type=Path, required=True)
    create_parser.set_defaults(func=create)
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("intermediate", type=Path)
    verify_parser.set_defaults(func=verify)
    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("intermediate", type=Path)
    plan_parser.add_argument("--recipe", type=Path, required=True)
    plan_parser.add_argument("--target", required=True)
    plan_parser.set_defaults(func=plan)
    build_parser = sub.add_parser("build")
    build_parser.add_argument("--recipe", type=Path, required=True)
    build_parser.add_argument("--architecture", required=True)
    build_parser.add_argument("--build-contract", required=True)
    build_parser.add_argument("--cache-dir", type=Path, required=True)
    build_parser.add_argument("--output", type=Path, required=True)
    build_parser.set_defaults(func=build)
    package_parser = sub.add_parser("package")
    package_parser.add_argument("intermediate", type=Path)
    package_parser.add_argument("--recipe", type=Path, required=True)
    package_parser.add_argument("--target", required=True)
    package_parser.add_argument("--output-dir", type=Path, required=True)
    package_parser.set_defaults(func=package)
    classify_parser = sub.add_parser("classify")
    classify_parser.add_argument("--recipe", type=Path, required=True)
    classify_parser.set_defaults(
        func=lambda parsed: print(json.dumps(
            tideforge.portable_payload_contract(yaml.safe_load(parsed.recipe.read_text())),
            sort_keys=True,
        ))
    )
    candidates_parser = sub.add_parser("candidates")
    candidates_parser.add_argument("--root", type=Path, default=Path("packages"))
    candidates_parser.add_argument("--architecture", required=True)
    candidates_parser.add_argument("--targets", nargs="+", required=True)
    candidates_parser.add_argument("--github-output", type=Path)
    candidates_parser.set_defaults(func=candidates)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
