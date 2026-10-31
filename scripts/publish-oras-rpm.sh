#!/usr/bin/env bash
# Production opt-in transport. Run only under the workflow's publish-rpms lock.
# Build/cache and installed HTTP consumers are separate migrations.
set -euo pipefail
export LC_ALL=C
: "${CELL:?}" "${ARCH:?}" "${BUILD_MANIFEST:?}" "${SEED_DIGEST:?}"
: "${GITHUB_REPOSITORY:?}" "${GITHUB_RUN_ID:?}" "${GITHUB_RUN_ATTEMPT:?}"
: "${ORAS_WORKDIR:?}" "${GITHUB_OUTPUT:?}"
[[ "$SEED_DIGEST" =~ ^sha256:[a-f0-9]{64}$ ]]
[[ "$CELL" =~ ^[a-z0-9][a-z0-9._-]{0,95}$ ]]
[[ "$ARCH" = x86_64 || "$ARCH" = aarch64 ]]
[[ "$(oras version | awk '/^Version:/{print $2}')" = 1.3.4 ]]
image="ghcr.io/${GITHUB_REPOSITORY%/*}/tunaos-packages"
image="${image,,}"
identity="https://github.com/${GITHUB_REPOSITORY}/.github/workflows/publish-build-chain-rpms.yml@refs/heads/main"
seed="${image}@${SEED_DIGEST}"
current_digest() { oras manifest fetch --descriptor "${image}:${CELL}" | jq -er '.digest'; }
# Never treat missing/inaccessible tags as an empty repository or try R2.
[[ "$(current_digest)" = "$SEED_DIGEST" ]]
cosign verify "$seed" --certificate-identity "$identity" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com > seed-signature.json
# Bootstrap compatibility: old production images have no scope labels. The
# current cell's tag + pinned digest + signer + native architecture are required.
podman pull --authfile "$DOCKER_CONFIG/config.json" "$seed"
cid=$(podman create "$seed" /bin/true)
trap 'podman rm "$cid" >/dev/null 2>&1 || true' EXIT
mkdir repo
podman cp "$cid:/repository/." repo/
podman rm "$cid" >/dev/null
trap - EXIT
# Release image layers before building another complete repository layout.
podman rmi "$seed"
bash scripts/verify-native-rpm-repository.sh repo "$ARCH"
dist=$(bash scripts/build-chain.sh --manifest "$BUILD_MANIFEST" --print-dist)
test -n "$dist"
while IFS= read -r -d '' package; do
  package_arch=$(rpm -qp --qf '%{ARCH}' "$package")
  release=$(rpm -qp --qf '%{RELEASE}' "$package")
  [[ "$package_arch" = "$ARCH" || "$package_arch" = noarch ]]
  [[ "$release" = *"$dist" || "$release" = *"$dist".* ]]
done < <(find staged -name '*.rpm' ! -name '*.src.rpm' -print0)
# No foreign eviction in the ORAS lane; retain the verified accumulated tree.
bash scripts/publish-rpm-wave.sh --staged staged --repo repo --subdir build-chain
repo_bytes=$(du -sb repo | cut -f1)
available=$(df -B1 --output=avail "$ORAS_WORKDIR" | tail -n 1)
if ((available <= repo_bytes * 2 + 1073741824)); then
  echo 'Insufficient scratch space: need two repository copies plus 1 GiB' >&2
  exit 1
fi
python3 scripts/oras_distribution.py prepare --repository repo \
  --layout "$ORAS_WORKDIR/layout" --format rpm --target "$CELL" \
  --architecture "$ARCH" > candidate.digest
staging="${CELL}-oras-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
oras cp --from-oci-layout "$ORAS_WORKDIR/layout:repository" "${image}:${staging}"
rm -rf "$ORAS_WORKDIR/layout"
ref="${image}@$(cat candidate.digest)"
cosign sign --yes "$ref"
TMPDIR="$ORAS_WORKDIR" python3 scripts/oras_distribution.py pull \
  --reference "$ref" --destination "$ORAS_WORKDIR/roundtrip" \
  --format rpm --target "$CELL" --architecture "$ARCH" --identity "$identity"
diff -qr repo "$ORAS_WORKDIR/roundtrip"
bash scripts/verify-native-rpm-repository.sh "$ORAS_WORKDIR/roundtrip" "$ARCH"
# Single-manifest atomic tag update, only after full verification and a second
# stale-base check. All repository publishers must honor the same workflow lock.
[[ "$(current_digest)" = "$SEED_DIGEST" ]]
printf '{"reference":"%s","previous":"%s","cell":"%s","state":"promotion-pending"}\n' \
  "$ref" "$seed" "$CELL" > publication-intent.json
oras tag "$ref" "$CELL"
[[ "$(current_digest)" = "$(cat candidate.digest)" ]]
printf 'ref=%s\nprevious=%s\n' "$ref" "$seed" >> "$GITHUB_OUTPUT"
printf '{"reference":"%s","previous":"%s","cell":"%s"}\n' \
  "$ref" "$seed" "$CELL" > publication-receipt.json
