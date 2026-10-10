#!/usr/bin/env python3
"""Check Tideforge recipe dependencies against real distro repositories."""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
FACTORY = ROOT / "manifests" / "package-factory.yaml"
SPEC = importlib.util.spec_from_file_location("tideforge", ROOT / "scripts" / "tideforge.py")
assert SPEC and SPEC.loader
tideforge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tideforge)


QUERY_SCRIPTS = {
    "el10": """dnf -qy makecache
for package in "$@"; do
  if dnf -q repoquery --available "$package" | grep -q .; then
    status=available
  else
    status=missing
  fi
  printf 'RESULT\\t%s\\t%s\\n' "$package" "$status"
done""",
    "ubuntu": """apt-get update -qq
for package in "$@"; do
  apt-cache show "$package" >/dev/null 2>&1 && status=available || status=missing
  printf 'RESULT\\t%s\\t%s\\n' "$package" "$status"
done""",
    "debian": """apt-get update -qq
for package in "$@"; do
  apt-cache show "$package" >/dev/null 2>&1 && status=available || status=missing
  printf 'RESULT\\t%s\\t%s\\n' "$package" "$status"
done""",
    "opensuse-tumbleweed": """zypper --non-interactive refresh >/dev/null
for package in "$@"; do
  zypper --non-interactive --no-refresh info "$package" >/dev/null 2>&1 && status=available || status=missing
  printf 'RESULT\\t%s\\t%s\\n' "$package" "$status"
done""",
    # Hummingbird's script answers from the repos the image itself ships and
    # nothing else. The target sat at status: scaffold precisely because the
    # only probe image available answered from Fedora Rawhide, which says
    # "available" for packages Hummingbird does not ship — a probe that lies
    # (see manifests/package-factory.yaml). The honesty of the image's own
    # repos is MEASURED, not assumed: inside the pinned bootc-os image, tunaOS
    # run 32813311729 got `dnf5 install dbus-daemon` -> installed and
    # `dnf5 install flatpak` -> "No match for argument: flatpak", which is the
    # truthful answer for a distribution that does not package flatpak.
    #
    # dnf5-first: the image ships dnf5; plain `dnf` may not exist there.
    "hummingbird": """DNF=dnf5
command -v dnf5 >/dev/null 2>&1 || DNF=dnf
"$DNF" -qy makecache >/dev/null 2>&1 || true
for package in "$@"; do
  if "$DNF" -q repoquery "$package" 2>/dev/null | grep -q .; then
    status=available
  else
    status=missing
  fi
  printf 'RESULT\t%s\t%s\n' "$package" "$status"
done""",
    "arch": """pacman -Sy --noconfirm >/dev/null
for package in "$@"; do
  pacman -Si "$package" >/dev/null 2>&1 && status=available || status=missing
  printf 'RESULT\\t%s\\t%s\\n' "$package" "$status"
done""",
}


def load_factory() -> dict:
    return yaml.safe_load(FACTORY.read_text())


def native_dependencies(recipe: dict, target: str) -> list[str]:
    dependencies = tideforge.target_dependencies(recipe, target) + tideforge.target_runtime_dependencies(recipe, target)
    return list(dict.fromkeys(dependencies))


def repository_setup(target: str, repositories: list[str]) -> str:
    """Return target-native setup for repositories used only while building."""
    if target != "el10":
        return ""
    commands: list[str] = []
    if "epel" in repositories:
        commands.append("dnf -qy install epel-release")
    if "crb" in repositories:
        commands.append("dnf -qy install dnf-plugins-core")
        commands.append("dnf config-manager --set-enabled crb")
    return "\n".join(commands) + ("\n" if commands else "")


def podman_command(image: str, target: str, packages: list[str], repositories: list[str] | None = None) -> list[str]:
    script = repository_setup(target, repositories or []) + QUERY_SCRIPTS[target]
    return ["podman", "run", "--rm", image, "bash", "-euc", script, "tideforge-probe", *packages]


def probe(image: str, target: str, packages: list[str], repositories: list[str]) -> tuple[dict[str, str], str]:
    completed = subprocess.run(podman_command(image, target, packages, repositories), text=True, capture_output=True, check=False)
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or f"podman exited {completed.returncode}")
    result: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if line.startswith("RESULT\t"):
            _, package, status = line.split("\t", 2)
            result[package] = status
    return result, completed.stderr


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recipe", type=Path, help="Tideforge package.yaml")
    parser.add_argument("--target", action="append", choices=sorted(QUERY_SCRIPTS), help="probe one target; repeatable")
    parser.add_argument("--dry-run", action="store_true", help="print resolved names without starting containers")
    parser.add_argument("--json", action="store_true", help="emit machine-readable results")
    args = parser.parse_args()

    recipe = tideforge.load_yaml(args.recipe)
    tideforge.validate(recipe)
    factory = load_factory()
    targets = args.target or recipe["targets"]
    report: dict[str, dict] = {}
    failed = False
    for target in targets:
        packages = native_dependencies(recipe, target)
        target_data = factory["targets"][target]
        image = target_data["probe_image"]
        repositories = target_data.get("build_repositories", [])
        if args.dry_run:
            report[target] = {"image": image, "dependencies": packages, "status": "not-run"}
            continue
        try:
            results, stderr = probe(image, target, packages, repositories)
        except RuntimeError as error:
            report[target] = {"image": image, "dependencies": packages, "status": "probe-error", "error": str(error)}
            failed = True
            continue
        missing = [package for package in packages if results.get(package) != "available"]
        report[target] = {"image": image, "dependencies": packages, "results": results, "missing": missing, "status": "ok" if not missing else "missing"}
        failed = failed or bool(missing)
        if stderr:
            report[target]["stderr"] = stderr

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for target, result in report.items():
            print(f"{target}: {result['status']} ({result['image']})")
            for package in result["dependencies"]:
                state = result.get("results", {}).get(package, "not-run")
                print(f"  {state:>9}  {package}")
            if result.get("error"):
                print(f"  error: {result['error']}")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
