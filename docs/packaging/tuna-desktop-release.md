# Pointing tuna-desktop at a release tag

`packages/tuna-desktop/package.yaml` builds one upstream release tag of
[tuna-os/tuna-desktop](https://github.com/tuna-os/tuna-desktop). Six fields
move together for each tag: `version`, `release`, `source`, the vendored crate
asset, `build.environment.TUNA_VERSION` and the verify smoke's `want=`.
`scripts/prepare-tuna-desktop-release.py` sets all six, and
`tests/test_prepare_tuna_desktop_release.py` fails when they name different
tags.

## Version forms

Tag `vX.Y.Z-pre.N` (for example `v1.0.0-beta.1`) is written in each format as follows:

| Where | Value | Why |
| --- | --- | --- |
| `TUNA_VERSION` | `v1.0.0-beta.1` | The tag. Every binary's `--version` prints it without the `v`. |
| verify `want=` | `1.0.0-beta.1` | What the smoke expects from `--version`. |
| recipe `version`, RPM `Version:` | `1.0.0~beta.1` | RPM sorts `~` before the release (`1.0.0~beta.1 < 1.0.0`). RPM forbids `-` in `Version:`. |
| Debian upstream version | `1.0.0~beta.1` (package `1.0.0~beta.1-1`) | dpkg sorts `~` first in the same way. |
| pacman `pkgver` | `1.0.0beta.1` | pkgver forbids `~` and `-`. libalpm already sorts a trailing alphabetic segment older (`1.0.0beta.1 < 1.0.0`). Upstream's `packaging/arch/PKGBUILD` uses the same value. |

Tideforge renders the pacman value with `tideforge.pacman_version()`.
`validate` only accepts versions that are valid in all three formats. A `~`
must be followed by a letter, because `1.0~1` would become `1.01` for
pacman, which sorts after `1.0`.

## Filling a new tag

Run this after the tag is pushed upstream, from a clean checkout of this repo
with `cargo`, `gh` (authenticated, allowed to create releases) and network
access:

```sh
python3 scripts/prepare-tuna-desktop-release.py v1.0.0-beta.1 --publish
python3 -m pytest -q tests/test_prepare_tuna_desktop_release.py tests/test_tideforge.py
python3 scripts/tideforge.py validate packages/tuna-desktop/package.yaml
```

The script:

1. Downloads `archive/refs/tags/<tag>.tar.gz` and records its sha256. It checks that the top-level directory is `tuna-desktop-<tag without v>`.
2. Runs `cargo vendor --locked --versioned-dirs` in the extracted tag. It packs `vendor/` as a deterministic tar: `--sort=name --format=gnu --owner=0 --group=0 --numeric-owner`, normalized modes, the tag commit's time as `--mtime`, and `gzip -n -9`. It packs twice and stops if the two hashes differ.
3. Extracts the tag archive again into a fresh directory, unpacks the vendor tar there, and writes the recipe's own `cargo_config` to `.cargo/config.toml`. Then it runs `cargo metadata --locked --offline`.
4. Looks for release `tuna-desktop-vendor-<tag>` in this repo. With `--publish` it creates the release when it is missing. In either case it downloads the published asset and requires the same sha256 as the local build. Without `--publish`, a missing release stops the script before the recipe changes.
5. Rewrites only the six fields and deletes the `# PLACEHOLDER:` comments. Each edit must match exactly once. Then it validates the result with Tideforge and the tag-consistency check before it writes the file.

Use `--keep DIR` to keep the downloads and the vendor tar for inspection. Run
the script again with the same tag to check an existing release: it rebuilds
the asset and compares it with the published one.

Commit the recipe change. Then let the factory build the `tideforge-tuna-desktop-arch-x86_64`
cell. Its smoke runs every binary and checks `--version` against `want=`.
