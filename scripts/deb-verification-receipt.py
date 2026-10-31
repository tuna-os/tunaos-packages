#!/usr/bin/env python3
"""Bind a served Debian package to its gated ActionResult and install proof.

The verifier consumes evidence collected after a signed flat-APT install. A
matching version is insufficient: the served Packages checksum, installed
payload, runtime dependencies, schema closure and source/action identity must
all refer to the same build.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import yaml

SCRIPT_ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "tideforge_action_cache", SCRIPT_ROOT / "tideforge-action-cache.py"
)
cache = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(cache)

SIGNER = "4E5CC9F8B3B521793D95266E629BE6EA45188366"
TARGETS = {
    "ubuntu": "ubuntu",
    "debian-sid": "debian",
    "debian-trixie": "trixie",
}
TUNA_SCHEMAS = {
    "org.gnome.desktop.interface",
    "org.gnome.desktop.wm.keybindings",
    "org.gnome.desktop.a11y.keyboard",
    "org.gnome.desktop.privacy",
    "org.gnome.desktop.sound",
}
TUNA_BINARIES = (
    "compositor",
    "session",
    "shell-host",
    "shell-gtk",
    "ibus-bridge",
    "wallpaper-svg",
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def relation_name(value: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9+.-]*)", value)
    if not match:
        raise ValueError(f"invalid dependency relation: {value!r}")
    return match.group(1)


def safe_archive_path(value: str, basename: str) -> bool:
    return (
        value
        and not value.startswith("/")
        and Path(value).name == basename
        and all(part not in ("", ".", "..") for part in value.split("/"))
    )


def parse_control(path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    current: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith((" ", "\t")) and current:
            fields[current] += " " + line.strip()
        elif ": " in line:
            current, value = line.split(": ", 1)
            fields[current] = value
        else:
            current = None
    return fields


def parse_payload(path: Path) -> dict[str, list[str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    require(lines and lines[0].startswith("archive\t"), "missing installed archive binding")
    payload: dict[str, list[str]] = {}
    for line in lines[1:]:
        fields = line.split("\t")
        require(len(fields) in (3, 4), "malformed installed payload member")
        kind, member = fields[:2]
        require(
            member and not member.startswith("/") and "\n" not in member and "\t" not in member,
            "unsafe installed payload member",
        )
        require(
            all(part not in ("", ".", "..") for part in member.split("/")),
            "path traversal in installed payload member",
        )
        require(member not in payload, "duplicate installed payload member")
        if kind == "file":
            require(
                len(fields) == 4
                and re.fullmatch(r"[0-7]{3,4}", fields[2]) is not None
                and re.fullmatch(r"[0-9a-f]{64}", fields[3]) is not None,
                "invalid installed regular file record",
            )
        elif kind == "symlink":
            require(len(fields) == 3 and fields[2] != "", "invalid installed symlink record")
        else:
            raise ValueError(f"unsupported installed payload record: {kind}")
        payload[member] = fields
    require(payload, "empty installed payload")
    return payload


def parse_installed(path: Path) -> set[str]:
    result = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split("\t")
        require(len(fields) == 3 and fields[0], "malformed installed package record")
        result.add(fields[0].split(":", 1)[0])
    return result


def verify_signature(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    valid = [line.split() for line in lines if line.startswith("[GNUPG:] VALIDSIG ")]
    require(len(valid) == 1 and len(valid[0]) >= 11, "missing or ambiguous served signature")
    require(valid[0][2] == SIGNER or valid[0][-1] == SIGNER, "unexpected repository signer")
    rejected = {
        "BADSIG",
        "ERRSIG",
        "REVKEYSIG",
        "EXPKEYSIG",
        "EXPSIG",
        "NODATA",
        "NO_PUBKEY",
        "FAILURE",
        "ERROR",
    }
    require(
        not any(len(line.split()) > 1 and line.split()[1] in rejected for line in lines),
        "served repository signature is invalid or expired",
    )


def receipt(
    root: Path,
    provenance: Path,
    observations: Path,
    package: str,
    distro: str,
    arch: str,
    revision: str,
    run_url: str,
    served_url: str,
    run_attempt: int = 1,
) -> dict:
    require(
        re.fullmatch(r"[0-9a-f]{40}", revision) is not None,
        "exact publisher revision required",
    )
    require(type(run_attempt) is int and run_attempt > 0, "positive run attempt required")
    checkout = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    require(
        checkout.returncode == 0 and checkout.stdout.strip() == revision,
        "publisher revision does not match the verification checkout",
    )
    run = urlparse(run_url)
    require(
        run.scheme == "https"
        and run.netloc == "github.com"
        and re.fullmatch(r"/tuna-os/tunaos-packages/actions/runs/[0-9]+", run.path) is not None
        and not run.query
        and not run.fragment,
        "publisher run URL required",
    )
    served = urlparse(served_url)
    require(
        served.scheme == "https"
        and served.netloc == "repo.tunaos.org"
        and served.path.rstrip("/") == f"/tideforge/{distro}"
        and not served.query
        and not served.fragment,
        "unexpected served repository",
    )
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+.-]*", package) is not None, "unsafe package name")

    target = TARGETS[distro]
    recipe_path = root / "packages" / package / "package.yaml"
    recipe = yaml.safe_load(recipe_path.read_text(encoding="utf-8"))
    require(target in (recipe.get("targets") or []), "recipe does not target the published suite")
    identity = json.loads((provenance / "action-inputs.json").read_text(encoding="utf-8"))
    result = json.loads((provenance / "action-result.json").read_text(encoding="utf-8"))
    inputs = identity.get("inputs")
    require(isinstance(inputs, dict), "missing ActionResult input identity")
    expected = cache.action_inputs(
        argparse.Namespace(
            root=str(root),
            recipe=str(recipe_path),
            factory=str(root / "manifests/package-factory.yaml"),
            target=target,
            arch=arch,
            image=inputs.get("build_image", ""),
            source_date_epoch=inputs.get("reproducibility", {}).get("source_date_epoch", 0),
            dependency_key=inputs.get("dependency_action_keys", []),
            consumer_bindings=None,
        )
    )
    require(
        identity.get("action_key") == cache.action_key(expected),
        "build inputs differ from current recipe",
    )
    require(inputs == expected, "build inputs do not match the current recipe/architecture")
    require(result.get("schema") == cache.SCHEMA, "unsupported ActionResult schema")
    require(result.get("action_key") == identity["action_key"], "ActionResult identity mismatch")

    observed_dir = observations / package
    installed_fields = (
        (observed_dir / "installed.tsv").read_text(encoding="utf-8").strip().split("\t")
    )
    require(len(installed_fields) == 6, "missing or malformed installed package observations")
    name, version, installed_arch, filename, observed_hash, observed_size = installed_fields
    expected_version = f"{recipe['version']}-{recipe.get('release', 1)}"
    expected_filename = f"{package}_{expected_version}_{arch}.deb"
    require(
        name == package
        and version == expected_version
        and installed_arch == arch
        and filename == expected_filename,
        "installed package identity mismatch",
    )
    require(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+_.~%-]*\.deb", filename) is not None,
        "unsafe served package filename",
    )
    require(
        re.fullmatch(r"[0-9a-f]{64}", observed_hash) is not None,
        "invalid served package digest",
    )
    require(observed_size.isdigit() and int(observed_size) > 0, "invalid served package size")

    index = parse_control(observed_dir / "index-entry.txt")
    require(
        index.get("Package") == package
        and index.get("Version") == version
        and index.get("Architecture") == arch
        and index.get("Filename")
        and safe_archive_path(index["Filename"], filename),
        "served Packages identity differs from installed package",
    )
    require(
        index.get("SHA256") == observed_hash and index.get("Size") == observed_size,
        "served index checksum mismatch",
    )

    entries = result.get("artifacts")
    require(isinstance(entries, list) and entries, "nonempty ActionResult artifacts required")
    selected = []
    names = set()
    for entry in entries:
        require(isinstance(entry, dict), "malformed ActionResult artifact")
        artifact_name = str(entry.get("name", ""))
        require(
            artifact_name and Path(artifact_name).name == artifact_name,
            "unsafe ActionResult artifact name",
        )
        require(artifact_name not in names, "duplicate ActionResult artifact name")
        names.add(artifact_name)
        require(
            re.fullmatch(r"sha256:[0-9a-f]{64}", str(entry.get("digest", ""))) is not None,
            "invalid ActionResult digest",
        )
        require(type(entry.get("size")) is int and entry["size"] > 0, "invalid ActionResult size")
        if artifact_name == filename:
            selected.append(entry)
    require(len(selected) == 1, "served package is absent or ambiguous in ActionResult")
    artifact = selected[0]
    require(
        "sha256:" + observed_hash == artifact["digest"],
        "served bytes differ from gated ActionResult",
    )
    require(int(observed_size) == artifact["size"], "served size differs from gated ActionResult")

    require((observations / "InRelease").is_file(), "served signed InRelease missing")
    require((observations / "signature-status.txt").is_file(), "served signature evidence missing")
    verify_signature(observations / "signature-status.txt")
    payload = parse_payload(observed_dir / "installed-payload.tsv")
    archive_binding = (
        (observed_dir / "installed-payload.tsv").read_text(encoding="utf-8").splitlines()[0]
    )
    require(
        archive_binding == "archive\t" + observed_hash,
        "installed payload is not bound to served bytes",
    )

    common = set(recipe.get("files", {}).get("common", []))
    require(common <= payload.keys(), "declared package payload member is missing")
    if package == "tuna-desktop":
        for binary in TUNA_BINARIES:
            executable = f"usr/bin/tuna-{binary}"
            compat = f"usr/bin/roost-{binary}"
            require(
                payload.get(executable, [""])[0] == "file",
                f"missing Tuna executable: {executable}",
            )
            require(
                payload[executable][2] == "755",
                f"Tuna executable has wrong mode: {executable}",
            )
            require(
                compat not in payload,
                f"retired Roost binary link still installed: {compat}",
            )
        schemas = set(
            (observed_dir / "installed-schemas.txt").read_text(encoding="utf-8").splitlines()
        )
        require(TUNA_SCHEMAS <= schemas, "compiled GSettings schema closure is incomplete")
        require(
            (observed_dir / "canberra-command.txt").is_file(),
            "canberra event-sound player missing",
        )
        sounds = set((observed_dir / "sound-files.txt").read_text(encoding="utf-8").splitlines())
        require(
            {
                "/usr/share/sounds/freedesktop/stereo/bell.oga",
                "/usr/share/sounds/freedesktop/stereo/audio-volume-change.oga",
            } <= sounds,
            "freedesktop event sound closure is incomplete",
        )

    control = parse_control(observed_dir / "package-info.txt")
    depends = {
        relation_name(alternative)
        for relation in control.get("Depends", "").split(",")
        for alternative in relation.split("|")
        if alternative.strip() and not alternative.strip().startswith("${")
    }
    required = {
        relation_name(str(item))
        for item in recipe.get("dependencies", {}).get("runtime", {}).get("targets", {}).get(
            target, []
        )
    }
    require(required <= depends, "served package lacks declared runtime dependencies")
    installed = parse_installed(observed_dir / "installed-packages.tsv")
    require(required <= installed, "required runtime dependency is not installed")

    sources = [recipe["source"], *(recipe.get("sources") or [])]
    require(
        all(
            re.fullmatch(r"[0-9a-f]{64}", str(source.get("sha256", "")))
            for source in sources
        ),
        "source checksums must be pinned",
    )
    return {
        "schema": 1,
        "status": "pass",
        "revision": revision,
        "run_url": run_url,
        "run_attempt": run_attempt,
        "generated": datetime.now(UTC).isoformat(),
        "target": {"distro": distro, "target": target, "architecture": arch},
        "package": {
            "name": package,
            "version": version,
            "architecture": arch,
            "filename": filename,
            "sha256": observed_hash,
            "size": int(observed_size),
            "url": served_url.rstrip("/") + "/" + index["Filename"],
        },
        "source_inputs": sources,
        "action_key": identity["action_key"],
        "build_inputs": inputs,
        "signature": {
            "primary_fingerprint": SIGNER,
            "inrelease_sha256": hashlib.sha256(
                (observations / "InRelease").read_bytes()
            ).hexdigest(),
        },
        "verification": {
            "served_index": "pass",
            "signed_repository": "pass",
            "exact_artifact": "pass",
            "installed_payload": "pass",
            "runtime_dependencies": "pass",
            "schema_and_sound_closure": "pass" if package == "tuna-desktop" else "not-applicable",
        },
        "evidence": [
            "InRelease",
            "Packages.gz",
            f"{package}/installed.tsv",
            f"{package}/installed-payload.tsv",
            f"{package}/package-info.txt",
            f"{package}/installed-packages.tsv",
            "signature-status.txt",
            "action-inputs.json",
            "action-result.json",
        ],
        "note": (
            "Package publication proof only; installed image session and variant "
            "qualification remain separate gates."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--distro", required=True, choices=sorted(TARGETS))
    parser.add_argument("--arch", required=True, choices=("amd64", "arm64"))
    parser.add_argument("--revision", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--run-attempt", type=int, default=1)
    parser.add_argument("--served-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.unlink(missing_ok=True)
    try:
        value = receipt(
            args.root,
            args.provenance,
            args.observations,
            args.package,
            args.distro,
            args.arch,
            args.revision,
            args.run_url,
            args.served_url,
            args.run_attempt,
        )
    except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        parser.exit(1, f"verification receipt refused: {exc}\n")
    args.output.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
