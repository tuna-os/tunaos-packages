# Debian and Ubuntu packaging

The factory builds DEB packages from maintained `debian/` directories.
It publishes and signs repositories for APT under `apt/ubuntu/` and `apt/debian/`.
`manifests/package-factory.yaml` declares these R2 namespaces.

Do not add a PPA to a TunaOS image. Import the source/package metadata here,
record its provenance in `packages/<name>/source.yaml`, and rebuild it in CI.

## Owned Ubuntu Orca source package

`packages/orca/package.yaml` selects the existing Tideforge target for
Ubuntu 26.04 (`resolute`) on both `amd64` and `arm64`.
The factory planner finds owned recipes automatically.
Do not add a separate list of builds or change the default for publication.
This recipe does not publish packages when you add it.

This recipe uses `outputs.deb.native_source`, not the renderer that generates
package files for Debian. It takes the complete source from the primary Ubuntu archive:
the exact signed `.dsc` for `50.3-0ubuntu0.1`, orig tarball and Debian tarball.
SHA-256 hashes lock each file.

`packages/orca/source.yaml` records the hashes and the downstream version,
`50.3-0ubuntu0.1+tuna1`. Assembly checks the descriptor's identity, source
filenames, sizes and SHA-256 values against those archives.
These checks use checksums to establish provenance.
They do not verify the archive signature.

The complete Ubuntu `debian/` directory remains authoritative.
Assembly does not regenerate `control`, `rules`, copyright, install files,
the wrapper, maintainer behavior or the existing `unity-a11y.patch`.
It only appends the maintained Tuna patch to quilt's series and prepends
an entry to the downstream changelog.

The renderer rejects an invalid mode for native sources before assembly can
replace package files. Recipes that generate package files keep their existing behavior.

### Assembly and native build

Run these commands from the repository root.
Assembly needs Python 3.12 or newer, PyYAML and an empty output directory.
It also needs network access to the primary archive with the locked checksums,
or access to the existing source cache.
These commands are for the build owner.
**They do not prove that a build or installation has passed.**

```bash
work="$PWD/.factory/orca-native/deb"
mkdir -p "$work"
python3 scripts/tideforge.py render packages/orca/package.yaml \
  --target ubuntu --output "$work/rendered"
python3 scripts/assemble-deb-source-tree.py packages/orca/package.yaml "$work" ubuntu
docker run --rm -v "$work:/work" ubuntu:26.04 bash -lc '
  set -euo pipefail
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends build-essential ca-certificates
  cd "$(cat /work/source-dir)"
  apt-get build-dep -y --no-install-recommends "$PWD"
  dpkg-buildpackage -us -uc -S -sa
  dpkg-buildpackage -us -uc -b
'
```

Use the target's native architecture.
Do not use a foreign Python runtime or copy a site-packages tree.
Enable Ubuntu's native `universe` component if the build image does not enable it.
Do not import a PPA or packages from another distribution to satisfy dependencies.

`apt-get build-dep` must read the preserved Ubuntu `debian/control`,
including its versioned AT-SPI requirements.
Do not bypass dependency checks with `-d`.
Build the complete source and binaries with the native
`dpkg-dev`/debhelper/Meson toolchain.

Record the `.dsc`, orig and Debian tarballs, `.changes`, `.buildinfo` and real
`.deb` artifacts in the build receipt.
Include assembly's provenance from `source/original-source/`.
A tree from assembly alone or a list of source files is not a compiled package.

The planner IDs for the existing isolated builds of owned recipes are
`tideforge-orca-ubuntu-amd64` and `tideforge-orca-ubuntu-arm64`.
The build owner uses the normal runner for factory cells and verifies a clean install.
Do not change the publisher's default wave or production repositories for this test.

For `native_source` recipes, the cell runner builds the changed source with
`-S -sa` before it builds binaries with the normal `-b`.
It does not skip native checks.
It keeps the complete changed source and all files that complete that source.
It also keeps `.changes` and `.buildinfo` for both source and binaries.
It stores the locked original source and all its required files under
`artifacts/source/`, beside the real DEB at the artifact root.
Recipes that generate package files still build binaries only.

### Installed package and runtime proof

Install only in a disposable native guest or container with Ubuntu 26.04.
Use its native APT sources to resolve dependencies:

```bash
apt-get update
apt-get install -y ./orca_50.3-0ubuntu0.1+tuna1_all.deb
dpkg-query -W -f='${Package} ${Version} ${Architecture}\n' orca
dpkg-query -S /usr/lib/python3/dist-packages/orca/scripts/apps/tuna-shell-gtk/script.py
```

The package manager must own the installed module.
Do not overlay files, set `PYTHONPATH`, monkeypatch Orca while it runs,
or change user preferences to make the proof pass.
The smoke check for this recipe proves only the version of the package
and who owns the module.
It does not prove spoken feedback.

The maintained patch installs `tuna-shell-gtk/__init__.py` and
`tuna-shell-gtk/script.py` through upstream's `python3.install_sources`.
It does not install a flat `tuna-shell-gtk.py`.
Ubuntu's exact 50.3 package files put existing app scripts under
`usr/lib/python3/dist-packages/orca/scripts/apps/`.
The primary archive's `orca_50.3-0ubuntu0.1_all.deb` contains
`Thunderbird/script.py` at that prefix.
The checks above use this prefix to verify who owns the module.

To qualify speech for passwords, use the actual `tuna-shell-gtk` app on the
native system. Use its identity, its GTK entry for passwords and its stream
of AT-SPI events.
You also need a session accessibility bus, the installed native Orca,
Speech Dispatcher and a path that outputs and captures speech.

Prove counts for committed insert/delete/reinsert events and paste events.
Do not record secret text or raw keys.
Preserve the first failure and any missing identity.
A headless import does not qualify the native runtime path.
An announcement from a synthetic shell does not qualify it either.

Compare how the source from the target handles events for passwords with
the behavior in this maintained patch.
Do this before you bump the version or retire the patch.
Repeat the proof in the native runtime.
A higher version of Ubuntu does not by itself prove that this behavioral gap
has closed.
Retire the owned patch when the native source supplies the correction.
Do not keep a runtime shim or force an unrelated backport.
