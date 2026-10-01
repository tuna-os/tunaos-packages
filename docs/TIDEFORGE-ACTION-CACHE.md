# Tideforge action cache

Tideforge identifies a package build by canonical inputs and not by workflow runs.
The key covers the recipe directory, target, and architecture.
It also includes the target contract, the build image digest, format renderers, dependency keys, and the reproducibility contract.

The key generator hashes only renderer inputs used by the target.
A Debian assembler change does not invalidate RPM, Arch, or openSUSE actions.
The engine resolves every build image to an `image@sha256` digest before key generation.

An `ActionResult` records the action key, safe basename, byte size, and SHA-256 for each artifact.
Restore verifies the requested key, schema, filenames, sizes, and hashes before a build skips compilation.
Lint, clean-install, and smoke validation still run on hits.

The cache in GitHub Actions accelerates runs.
Workflows restore before compilation and save after full validation.
R2 uses `actions/sha256/<action-key>.json` as the index and `blobs/sha256/<artifact-digest>` for immutable blobs.
Only protected jobs on `main` may publish R2 results.
These jobs write blobs first and `ActionResult` last.
