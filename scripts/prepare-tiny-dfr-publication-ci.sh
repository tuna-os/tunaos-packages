#!/usr/bin/env bash
# Authenticate captured inputs and stage immutable release assets for review.
# No release mutation or active recipe enablement occurs here.
set -euo pipefail
[[ "${GITHUB_ACTIONS:-}" == true && "${GITHUB_REPOSITORY:-}" == tuna-os/tunaos-packages ]]
REPO=tuna-os/tunaos-packages
RUN=38085707264
SHA=dd0c94685ca05e6232e8e85bc210713c66768155
OUT="$PWD/.factory/tiny-dfr-publication"
mkdir -p "$OUT"
gh api "repos/${REPO}/actions/runs/${RUN}" > "$OUT/producer-run.json"
jq -e --arg sha "$SHA" --arg repo "$REPO" '
  .id == 38085707264 and .run_attempt == 1 and .head_sha == $sha and
  .head_repository.full_name == $repo and .conclusion == "success" and
  .event == "workflow_dispatch" and .path == ".github/workflows/tiny-dfr-source-inputs.yml"
' "$OUT/producer-run.json"
gh run download "$RUN" --repo "$REPO" --name "tiny-dfr-inputs-${RUN}-1" --dir "$OUT/assets"
gh attestation verify "$OUT/assets/input-contract.json" --repo "$REPO" \
	--signer-workflow "${REPO}/.github/workflows/tiny-dfr-source-inputs.yml" \
	--source-digest "$SHA" --format json > "$OUT/attestation-verification.json"
python3 - "$OUT/assets" <<'PY'
import hashlib, json, pathlib, sys
p = pathlib.Path(sys.argv[1])
receipt = json.loads((p / 'input-contract.json').read_text())
expected = json.loads(pathlib.Path('packaging/tiny-dfr/input-contract.json').read_text())
if receipt != expected:
    raise SystemExit('captured receipt differs from reviewed exact input contract')
for name, record in receipt['artifacts'].items():
    if pathlib.PurePath(name).name != name:
        raise SystemExit('unsafe artifact path')
    artifact = p / name
    if artifact.is_symlink() or not artifact.is_file():
        raise SystemExit('missing or symlink source artifact: ' + name)
    data = artifact.read_bytes()
    if len(data) != record['size'] or hashlib.sha256(data).hexdigest() != record['sha256']:
        raise SystemExit('source artifact digest/size mismatch: ' + name)
if receipt['readiness'] is not False or receipt['target']['cpuCompatibilityVerified'] is not False:
    raise SystemExit('source inputs cannot assert package or CPU readiness')
print('Authenticated source inputs only; publication and recipe enablement remain pending.')
PY
cp packaging/tiny-dfr/package.yaml "$OUT/package.yaml.pending"
printf '%s\n' 'tiny-dfr-vendor-6267754535c16bd2c1006b946aa032561d8432b3' > "$OUT/proposed-release-tag.txt"
