# Contributing

TunaOS Packages contains several pipelines for packages.
Before you change a package, identify its pipeline.
Preserve that pipeline's source, build, install, and runtime gates.

## Get started

1. Fork the repository and clone your fork.
2. Create a feature branch from the latest `main`.
3. Read [ARCHITECTURE.md](ARCHITECTURE.md) and the documentation for the pipeline you plan to change.
4. Make the smallest package, manifest, and test changes needed.
5. Run focused checks, then the full repository check where practical.
6. Push the feature branch and open a pull request. Do not push directly to `main`.

Useful pipeline references:

- [Package factory contract](docs/PACKAGE_FACTORY.md) — supported targets, source policy, promotion gates, and `packages/*/package.yaml` recipes
- [Patch policy](docs/PATCH_POLICY.md) — when and how maintainers carry downstream patches
- [GNOME 50 repository publication](docs/GNOME50-REPO-PUBLISH.md) — operator steps for the native EL10 repository
- [XFWL4 port guide](docs/XFWL4-PORTING.md) — XFCE/XFWL4 target specifications
- [Upstream parity register](docs/UPSTREAM_PARITY.md) — track differences in source and behavior

`AGENTS.md` contains additional GNOME 49/50 conventions. Its scope note lists the package families it does and does not cover.

## Choose the correct package layout

Do not create a generic source directory or tarball unless the spec needs one. Existing packages use one of these layouts:

| Path | Use |
| --- | --- |
| `src/<family>/<package>/` | Native RPM spec, patches, and source metadata |
| `packages/<package>/package.yaml` | Package-factory recipe for supported cross-distribution targets |
| `packaging/<format>/` | Shared format-specific packaging and helpers |
| `build-order*.yml` | Native build dependency order |
| `manifests/` | Factory catalog, dependency trees, target queues, and build contracts |

Start from a nearby package in the same family.
If you add a package-factory recipe, use `packages/_template/package.yaml` and declare only targets the recipe can build and verify.
Keep native EL10 compatibility work in the native RPM pipeline until the package factory satisfies the promotion contract.

When you change a native spec:

- keep the package after its build-time dependencies in the applicable `build-order*.yml` file;
- place patches beside the owning spec and follow `docs/PATCH_POLICY.md`;
- record manual changes to GNOME specs or sources in `SRPM-CHANGES.md` where that log applies; and
- add a focused regression test for compatibility or pipeline behavior that is not evident from a successful build alone.

## Validate changes

Install Python test dependencies with:

```bash
python3 -m pip install pytest pyyaml jsonschema
```

Run focused tests during development. For example:

```bash
python3 -m pytest tests/test_parse_build_order.py -v
python3 scripts/parse-build-order.py build-order.yml --validate
```

Run the fast checks of the repository before you submit:

```bash
just check
```

This runs the Python test suite and validates the primary build-order manifest.
CI also checks YAML, shell scripts, every build-order manifest, and the Bats suite.
If your change touches shell workflows, install Bats and run the relevant file under `tests/bats/`.
See [`tests/bats/README.md`](tests/bats/README.md) for setup and examples.

For a local RPM build, use the target declared by the owning pipeline:

```bash
./scripts/build-local.sh <package> <target>
```

Container and Mock builds are slower than the fast checks.
Run the relevant build when practical and state in the pull request when you did not run it.
A build alone is not a release gate: package promotion also needs a clean staged install and applicable runtime validation.

## Pull requests

Include in the pull request description:

- package family and target distributions or architectures affected;
- source or patch provenance and why a downstream change is required;
- manifests or build-order files changed;
- commands run and their results; and
- staged install or runtime checks run, or why they remain for CI or an operator.

Keep updates to different packages in separate PRs.
Automated source updates must open review pull requests and must not publish directly.
Sign keys and publish credentials belong only in trusted GitHub environments.
Never use production commands or commit secrets from a workstation.

## Style

- Shell: use `set -euo pipefail` and satisfy ShellCheck.
- Python: follow PEP 8 and add focused pytest coverage.
- YAML: use two-space indentation and validate the affected manifest.
- Commits: use imperative subjects and explain non-obvious choices in specs.

All contributions must follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
Report vulnerabilities through the private process in [SECURITY.md](SECURITY.md), not through a public issue.
