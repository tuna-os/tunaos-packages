<!-- ste-disable-file: historical implementation plan dated 2026-03-15 preserved as an archival record -->
# GNOME 49 Self-Hosted GitHub Actions Pipeline Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build GNOME 49 RPM packages via GitHub Actions (mock/podman), publish to Cloudflare R2, and serve a functioning DNF/YUM repository at `https://repo.tunaos.org/gnome49/` — without touching any existing COPR infrastructure.

**Architecture:** All new work lives in the `gnome-49-pipeline` branch. A separate `.copr/build-order-gnome49.yml` manifest drives a per-package distributed GitHub Actions workflow that mirrors the pattern of the existing GNOME 50 workflow. Packages are uploaded to a dedicated R2 path (`gnome49/10-stream-x86_64/`) that the existing Cloudflare Worker serves without modification. Renovate watches `src/gnome-49/**/*.spec` Version fields for automated update PRs, each of which triggers an incremental single-package rebuild workflow.

**Tech Stack:** GitHub Actions, mock (via podman container), createrepo_c, rpmsign, rclone → Cloudflare R2, Cloudflare Worker (existing), GitHub CLI (`gh`), Renovate Bot, Lima (VM testing), Python 3 (workflow generator).

---

## Constraints (DO NOT BREAK)

- **Never modify** `build-order.yml`, `build-distributed.yml`, `build.yml`, or any COPR justfile commands.
- **Never change** the existing R2 path layout (`repo/10-x86_64/`, `repo/10-stream-x86_64/`).
- **Never change** the Cloudflare Worker (`workers/repo-proxy.ts`) for this work — the new R2 path `/gnome49/...` is served without transformation.
- Work in branch `gnome-49-pipeline` until ready to merge.

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `.copr/build-order-gnome49.yml` | **Create** | GNOME 49 tier manifest (separate from GNOME 50) |
| `scripts/generate-distributed-workflow.py` | **Modify (tiny)** | Accept manifest + output as CLI args (currently hardcoded) |
| `.github/workflows/build-gnome49-distributed.yml` | **Generate** | Full bootstrap workflow (all tiers, per-package matrix jobs) |
| `.github/workflows/build-gnome49-package.yml` | **Create** | Incremental single-package workflow (Renovate PRs, path filters) |
| `.github/workflows/build-gnome49-verify.yml` | **Create** | Triggers after successful publish; runs install test in container |
| `scripts/watch-pipeline.sh` | **Create** | Local script: watch GHA run progress, show per-tier/package status |
| `contrib/install-gnome49.sh` | **Create** | User-facing repo install script for GNOME 49 |
| `renovate.json` | **Create** | Renovate config: RPM spec Version: watcher for F43 dist-git |
| `tests/gnome49-repo-test.yaml` | **Create** | Lima VM config: boots CS10, adds repo.tunaos.org/gnome49, installs GNOME 49 |
| `AGENTS.md` | **Update** | Document new pipeline, R2 paths, Renovate setup |
| `GEMINI.md` | **Update** | Add new pipeline rules so Gemini doesn't touch COPR |

---

## Chunk 1: Branch + Infrastructure Verification

### Task 1.1: Create working branch

- [ ] **Create branch and verify existing R2 state**

```bash
git checkout -b gnome-49-pipeline
git push -u origin gnome-49-pipeline
```

- [ ] **Verify repo.tunaos.org is functional** (already confirmed, document findings):
  - `GET https://repo.tunaos.org/public.gpg` → HTTP 200 (GPG key served ✓)
  - `GET https://repo.tunaos.org/install.sh` → HTTP 200 ✓
  - `GET https://repo.tunaos.org/repo/10/x86_64/repodata/repomd.xml` → HTTP 200 ✓ (GNOME 50 data)
  - `GET https://repo.tunaos.org/gnome49/10-stream-x86_64/repodata/repomd.xml` → HTTP 404 (expected — no data yet)

- [ ] **Verify the Cloudflare Worker correctly serves `/gnome49/` paths without transformation**

  The Worker's `transformPath()` only touches paths starting with `/repo/`. Paths starting with `/gnome49/` pass through unmodified → R2 key = `gnome49/<rest>`. No Worker changes needed.

  Test after first publish:
  ```bash
  curl -sI https://repo.tunaos.org/gnome49/10-stream-x86_64/repodata/repomd.xml
  # Expected: HTTP 200
  ```

- [ ] **Commit** (empty commit to mark branch start)
  ```bash
  git commit --allow-empty -m "chore: start gnome-49-pipeline branch"
  ```

---

## Chunk 2: GNOME 49 Build Order Manifest

### Task 2.1: Create `.copr/build-order-gnome49.yml`

The manifest drives both the bootstrap and incremental workflows. Packages that don't exist in `src/gnome-49/` (e.g., `mozjs128`, which comes from EPEL 10 base) are NOT listed — mock's buildroot resolves them from the base distro.

**Files:**
- Create: `.copr/build-order-gnome49.yml`

- [ ] **Create the manifest**

```yaml
# .copr/build-order-gnome49.yml
# Build Order Manifest for GNOME 49 on CentOS Stream 10 / EPEL 10
#
# All packages under src/gnome-49/ only.
# Packages within a tier build in parallel; tiers execute sequentially.
# mock buildroot: centos-stream-10-x86_64 + our gnome49 local repo

target: centos-stream-10-x86_64
r2_path: gnome49/10-stream-x86_64

tiers:
  # Tier 0: EL10 compat + build tools (no internal deps)
  - name: compat-tools
    packages:
      - path: src/gnome-49/gnome49-el10-compat
      - path: src/gnome-49/meson
        build_tool: true
      - path: src/gnome-49/ninja-build
        build_tool: true

  # Tier 1: Font/text foundations (need meson, no GLib yet)
  - name: text-foundation
    packages:
      - path: src/gnome-49/snowball
      - path: src/gnome-49/harfbuzz
      - path: src/gnome-49/fontconfig

  # Tier 2: GLib bootstrap (needs harfbuzz for docs, but no GI yet)
  - name: glib2-bootstrap
    packages:
      - path: src/gnome-49/glib2
        spec_override: glib2-bootstrap.spec

  # Tier 3: GObject Introspection bootstrap (needs glib2-bootstrap)
  - name: gi-bootstrap
    packages:
      - path: src/gnome-49/gobject-introspection
        spec_override: gobject-introspection-bootstrap.spec

  # Tier 4: Full GLib (needs gi-bootstrap to generate .gir files)
  - name: glib2-full
    packages:
      - path: src/gnome-49/glib2

  # Tier 5: Full GI + high-level text/search libs (need full glib2)
  - name: gi-full-libs
    packages:
      - path: src/gnome-49/gobject-introspection
      - path: src/gnome-49/pango
      - path: src/gnome-49/tinysparql

  # Tier 6: GJS + GTK layer (need full GI, mozjs128 from EPEL base)
  - name: gtk-gjs-layer
    packages:
      - path: src/gnome-49/gjs
      - path: src/gnome-49/gtk4
      - path: src/gnome-49/gsettings-desktop-schemas
      - path: src/gnome-49/gnome-desktop3
      - path: src/gnome-49/libgexiv2
      - path: src/gnome-49/libcloudproviders

  # Tier 7: libadwaita + portal (need gtk4)
  - name: adwaita-portal
    packages:
      - path: src/gnome-49/libadwaita
      - path: src/gnome-49/xdg-desktop-portal
      - path: src/gnome-49/localsearch

  # Tier 8: Settings daemon (needs gsettings-desktop-schemas, gnome-desktop3)
  - name: settings-daemon
    packages:
      - path: src/gnome-49/gnome-settings-daemon

  # Tier 9: Compositor (needs settings daemon, mutter needs libadwaita, gjs, etc.)
  - name: mutter
    packages:
      - path: src/gnome-49/mutter

  # Tier 10: Shell + Control Center + Nautilus (need mutter)
  - name: shell-cc
    packages:
      - path: src/gnome-49/gnome-shell
      - path: src/gnome-49/gnome-session
      - path: src/gnome-49/gnome-control-center
      - path: src/gnome-49/nautilus

  # Tier 11: Display Manager + Remote Desktop (need gnome-session, mutter)
  - name: session-display
    packages:
      - path: src/gnome-49/gdm
      - path: src/gnome-49/gnome-remote-desktop
```

- [ ] **Verify all `src/gnome-49/<name>` directories exist**

```bash
python3 -c "
import yaml, os
m = yaml.safe_load(open('.copr/build-order-gnome49.yml'))
missing = []
for tier in m['tiers']:
    for pkg in tier['packages']:
        if not os.path.isdir(pkg['path']):
            missing.append(pkg['path'])
print('Missing:', missing if missing else 'none')
"
```

Expected: `Missing: none`

- [ ] **Commit**
```bash
git add .copr/build-order-gnome49.yml
git commit -m "feat: add GNOME 49 build order manifest with 11 dependency tiers"
```

---

## Chunk 3: Extend Workflow Generator

### Task 3.1: Parameterize `generate-distributed-workflow.py`

Currently the generator hardcodes the manifest and output paths. Add CLI args so it can generate workflows for both GNOME 50 and GNOME 49.

**Files:**
- Modify: `scripts/generate-distributed-workflow.py`

- [ ] **Add argparse to the generator**

Replace the bottom `__main__` block (lines 116-120) with:

```python
if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Generate distributed build workflow from manifest')
    parser.add_argument('manifest', nargs='?',
                        default=os.path.join(os.path.dirname(__file__), '..', 'build-order.yml'),
                        help='Path to build-order YAML manifest')
    parser.add_argument('output', nargs='?',
                        default=os.path.join(os.path.dirname(__file__), '..', '.github', 'workflows', 'build-distributed.yml'),
                        help='Output workflow YAML path')
    parser.add_argument('--name', default='Distributed Build and Publish RPMs',
                        help='Workflow name')
    parser.add_argument('--r2-path', default='10-stream-x86_64',
                        help='R2 repo path suffix (e.g. gnome49/10-stream-x86_64)')
    args = parser.parse_args()
    generate_workflow(args.manifest, args.output, workflow_name=args.name, r2_path=args.r2_path)
    print(f"Generated {args.output}")
```

Also update `generate_workflow()` signature to accept `workflow_name` and `r2_path` kwargs and use them.

- [ ] **Verify the existing GNOME 50 workflow regenerates identically**

```bash
cp .github/workflows/build-distributed.yml /tmp/gnome50-before.yml
python3 scripts/generate-distributed-workflow.py
diff /tmp/gnome50-before.yml .github/workflows/build-distributed.yml
```

Expected: no diff (or only harmless whitespace changes).

- [ ] **Commit**
```bash
git add scripts/generate-distributed-workflow.py
git commit -m "feat: parameterize workflow generator to support multiple manifests"
```

---

## Chunk 4: Bootstrap GitHub Actions Workflow

### Task 4.1: Generate GNOME 49 distributed workflow

**Files:**
- Create: `.github/workflows/build-gnome49-distributed.yml` (generated)

- [ ] **Generate the workflow**

```bash
python3 scripts/generate-distributed-workflow.py \
  .copr/build-order-gnome49.yml \
  .github/workflows/build-gnome49-distributed.yml \
  --name "GNOME 49 Distributed Build and Publish" \
  --r2-path "gnome49/10-stream-x86_64"
```

- [ ] **Verify the generated workflow structure**

```bash
python3 -c "
import yaml
w = yaml.safe_load(open('.github/workflows/build-gnome49-distributed.yml'))
jobs = list(w['jobs'].keys())
print('Jobs:', jobs)
print('Total:', len(jobs))
"
```

Expected output: `seed-repo`, `build-compat-tools`, `consolidate-compat-tools`, ..., `build-session-display`, `consolidate-session-display`, `publish` — roughly 25 jobs.

- [ ] **Manual review of key sections in generated file:**
  - `seed-repo` seeds from `r2:bluefin/gnome49/10-stream-x86_64/`
  - `publish` uploads to `r2:bluefin/gnome49/10-stream-x86_64/`
  - `publish` copies `contrib/install-gnome49.sh` (not install.sh)
  - GPG signing uses `secrets.GPG_PRIVATE_KEY` and `secrets.GPG_PASSPHRASE`

- [ ] **If the generator doesn't produce gnome49-specific R2 paths, patch the generated file manually then update the generator to fix it**

- [ ] **Commit**
```bash
git add .github/workflows/build-gnome49-distributed.yml
git commit -m "feat: add GNOME 49 distributed bootstrap workflow (11 tiers, per-package matrix jobs)"
```

---

## Chunk 5: Incremental Per-Package Workflow

This workflow runs when a PR or push modifies `src/gnome-49/<package>/`. It builds only the changed package against the *current state of R2* (not rebuilding all deps). Used for Renovate update PRs.

### Task 5.1: Create `build-gnome49-package.yml`

**Files:**
- Create: `.github/workflows/build-gnome49-package.yml`

- [ ] **Create the incremental workflow**

```yaml
name: GNOME 49 Package Build (Incremental)

on:
  pull_request:
    paths:
      - 'src/gnome-49/**'
  push:
    branches:
      - gnome-49-pipeline
      - main
    paths:
      - 'src/gnome-49/**'
  workflow_dispatch:
    inputs:
      package_path:
        description: 'Package path (e.g. src/gnome-49/gdm)'
        required: true

env:
  R2_BUCKET: bluefin
  R2_REPO_PATH: gnome49/10-stream-x86_64
  MOCK_RUNNER_IMAGE: ghcr.io/tuna-os/mock-runner:centos-stream-10

jobs:
  detect-changes:
    runs-on: ubuntu-latest
    outputs:
      packages: ${{ steps.detect.outputs.packages }}
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Detect changed packages
        id: detect
        run: |
          if [[ -n "${{ github.event.inputs.package_path }}" ]]; then
            # Manual dispatch: use explicit path
            PACKAGES='["${{ github.event.inputs.package_path }}"]'
          else
            # Detect from changed files
            BASE="${{ github.event.pull_request.base.sha || github.event.before }}"
            HEAD="${{ github.sha }}"
            CHANGED=$(git diff --name-only "${BASE}" "${HEAD}" | \
              grep '^src/gnome-49/' | \
              sed 's|/[^/]*$||' | sort -u | \
              python3 -c "import sys,json; dirs=sys.stdin.read().splitlines(); print(json.dumps(dirs))")
            PACKAGES="${CHANGED}"
          fi
          echo "packages=${PACKAGES}" >> $GITHUB_OUTPUT
          echo "Will build: ${PACKAGES}"

  build:
    needs: detect-changes
    if: needs.detect-changes.outputs.packages != '[]'
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        package: ${{ fromJson(needs.detect-changes.outputs.packages) }}
    steps:
      - uses: actions/checkout@v4
        with:
          submodules: recursive

      - name: Install host dependencies
        run: |
          sudo apt-get update -q
          sudo apt-get install -y -q podman createrepo-c rpm gpg gpgconf
          curl -fsSL https://rclone.org/install.sh | sudo bash

      - name: Cache CentOS Stream 10 image
        uses: actions/cache@v4
        with:
          path: /tmp/cs10-image.tar
          key: cs10-image-${{ hashFiles('mock/centos-stream-10-ci.cfg') }}

      - name: Load or pull container image
        run: |
          if [[ -f /tmp/cs10-image.tar ]]; then
            podman load -i /tmp/cs10-image.tar
          else
            podman pull quay.io/centos/centos:stream10
            podman save -o /tmp/cs10-image.tar quay.io/centos/centos:stream10
          fi
          podman pull ${{ env.MOCK_RUNNER_IMAGE }}

      - name: Configure rclone
        run: |
          mkdir -p ~/.config/rclone
          cat > ~/.config/rclone/rclone.conf << EOF
          [r2]
          type = s3
          provider = Cloudflare
          access_key_id = ${{ secrets.R2_ACCESS_KEY_ID }}
          secret_access_key = ${{ secrets.R2_SECRET_ACCESS_KEY }}
          endpoint = https://${{ secrets.CLOUDFLARE_ACCOUNT_ID }}.r2.cloudflarestorage.com
          EOF

      - name: Seed local repo from R2 (current published state)
        run: |
          mkdir -p local-repo
          rclone sync "r2:${R2_BUCKET}/${R2_REPO_PATH}/" local-repo/ --exclude "repodata/**" || true
          createrepo_c local-repo

      - name: Build package
        run: |
          ./scripts/build-chain.sh \
            --backend podman \
            --local-repo local-repo \
            --package "${{ matrix.package }}" \
            --force

      - name: Import GPG key and sign
        if: github.event_name != 'pull_request'
        uses: crazy-max/ghaction-import-gpg@v6
        id: import-gpg
        with:
          gpg_private_key: ${{ secrets.GPG_PRIVATE_KEY }}
          passphrase: ${{ secrets.GPG_PASSPHRASE }}

      - name: Sign new RPMs
        if: github.event_name != 'pull_request'
        run: |
          echo "%_signature gpg" > ~/.rpmmacros
          echo "%_gpg_name ${{ steps.import-gpg.outputs.keyid }}" >> ~/.rpmmacros
          find local-repo -name "*.rpm" -exec rpmsign --addsign {} \;
          createrepo_c --update local-repo

      - name: Upload updated package to R2
        if: github.event_name != 'pull_request'
        run: |
          rclone sync local-repo/ "r2:${R2_BUCKET}/${R2_REPO_PATH}/"

      - name: Upload RPMs as artifact (PR builds)
        if: github.event_name == 'pull_request'
        uses: actions/upload-artifact@v4
        with:
          name: rpms-${{ matrix.package != '' && hashFiles(matrix.package) || 'pkg' }}
          path: local-repo/*.rpm
          retention-days: 3
```

- [ ] **Commit**
```bash
git add .github/workflows/build-gnome49-package.yml
git commit -m "feat: add incremental per-package GNOME 49 workflow for Renovate PRs"
```

---

## Chunk 6: Install Script & Verification Workflow

### Task 6.1: Create GNOME 49 install script

**Files:**
- Create: `contrib/install-gnome49.sh`

- [ ] **Create the install script**

```bash
#!/usr/bin/env bash
#
# TunaOS GNOME 49 RPM Repository
#
# Usage:
#   curl -sSL https://repo.tunaos.org/gnome49/install.sh | sudo bash
#
set -euo pipefail

REPO_URL="https://repo.tunaos.org/gnome49/10-stream-x86_64"
REPO_NAME="tunaos-gnome49"
GPG_KEY_URL="https://repo.tunaos.org/public.gpg"
GPG_KEY_PATH="/etc/pki/rpm-gpg/RPM-GPG-KEY-tunaos"

install_gpg_key() {
    echo "Installing GPG key..."
    curl -sSLo "${GPG_KEY_PATH}" "${GPG_KEY_URL}"
    rpm --import "${GPG_KEY_PATH}" 2>/dev/null || true
}

install_repo_file() {
    cat > "/etc/yum.repos.d/${REPO_NAME}.repo" << EOF
[${REPO_NAME}]
name=TunaOS GNOME 49 for CentOS Stream 10
baseurl=${REPO_URL}/
enabled=1
gpgcheck=1
gpgkey=${GPG_KEY_URL}
repo_gpgcheck=0
metadata_expire=3600
priority=10
EOF
    echo "Repository file installed: /etc/yum.repos.d/${REPO_NAME}.repo"
}

verify() {
    echo "Verifying repository..."
    dnf repolist "${REPO_NAME}" 2>/dev/null || true
    echo "Testing package availability..."
    dnf info gdm 2>/dev/null | grep -E "Version|Repo" || echo "gdm not yet available (repo may be empty)"
}

main() {
    echo "Installing TunaOS GNOME 49 repository..."
    install_gpg_key
    install_repo_file
    verify
    echo ""
    echo "Done! Install GNOME 49 with:"
    echo "  dnf install gnome-shell mutter gdm gnome-session gnome49-el10-compat"
}

main "$@"
```

- [ ] **Commit**
```bash
git add contrib/install-gnome49.sh
git commit -m "feat: add GNOME 49 repo install script with gpgcheck=1"
```

### Task 6.2: Create post-publish verification workflow

After the distributed publish succeeds, automatically verify the repo is accessible:

**Files:**
- Create: `.github/workflows/build-gnome49-verify.yml`

- [ ] **Create the verification workflow**

```yaml
name: Verify GNOME 49 Repo

on:
  workflow_run:
    workflows: ["GNOME 49 Distributed Build and Publish"]
    types: [completed]
  workflow_dispatch:

jobs:
  verify:
    runs-on: ubuntu-latest
    if: ${{ github.event.workflow_run.conclusion == 'success' || github.event_name == 'workflow_dispatch' }}
    steps:
      - name: Check repomd.xml is accessible
        run: |
          STATUS=$(curl -sI https://repo.tunaos.org/gnome49/10-stream-x86_64/repodata/repomd.xml | head -1 | grep -o '[0-9]*')
          echo "HTTP status: ${STATUS}"
          [[ "${STATUS}" == "200" ]] || (echo "ERROR: Repo not accessible!" && exit 1)

      - name: Verify in CentOS Stream 10 container
        run: |
          cat > /tmp/verify.sh << 'EOF'
          #!/bin/bash
          set -e
          dnf -y install dnf-plugins-core curl
          curl -sSL https://repo.tunaos.org/gnome49/install.sh | bash
          dnf -y --nogpgcheck install gnome49-el10-compat gdm
          rpm -q gnome49-el10-compat gdm
          echo "VERIFICATION PASSED"
          EOF
          chmod +x /tmp/verify.sh
          podman run --rm -v /tmp/verify.sh:/verify.sh:ro \
            quay.io/centos/centos:stream10 bash /verify.sh
```

- [ ] **Commit**
```bash
git add .github/workflows/build-gnome49-verify.yml
git commit -m "feat: add automated post-publish repo verification workflow"
```

---

## Chunk 7: Local Watch Script

### Task 7.1: Create `scripts/watch-pipeline.sh`

**Files:**
- Create: `scripts/watch-pipeline.sh`

- [ ] **Create the watch script**

```bash
#!/usr/bin/env bash
#
# watch-pipeline.sh — Monitor and manage GNOME 49 GitHub Actions pipeline
#
# Usage:
#   scripts/watch-pipeline.sh               # Show latest run status
#   scripts/watch-pipeline.sh run           # Trigger a full bootstrap run and watch
#   scripts/watch-pipeline.sh watch [id]    # Watch an existing run (latest if no id)
#   scripts/watch-pipeline.sh package <path>  # Trigger a single-package build
#   scripts/watch-pipeline.sh status        # Show all recent runs (last 10)
#
set -euo pipefail

WORKFLOW_BOOTSTRAP="build-gnome49-distributed.yml"
WORKFLOW_PACKAGE="build-gnome49-package.yml"
REPO="tuna-os/tunaos-packages"
BRANCH="gnome-49-pipeline"

cmd="${1:-status}"
shift || true

case "${cmd}" in
  run)
    echo "Triggering full GNOME 49 bootstrap build..."
    gh workflow run "${WORKFLOW_BOOTSTRAP}" \
      --repo "${REPO}" \
      --ref "${BRANCH}"
    sleep 3
    RUN_ID=$(gh run list --repo "${REPO}" \
      --workflow "${WORKFLOW_BOOTSTRAP}" \
      --limit 1 \
      --json databaseId \
      --jq '.[0].databaseId')
    echo "Run ID: ${RUN_ID}"
    echo "Watching... (Ctrl+C to stop watching without cancelling)"
    gh run watch "${RUN_ID}" --repo "${REPO}"
    ;;

  watch)
    RUN_ID="${1:-}"
    if [[ -z "${RUN_ID}" ]]; then
      RUN_ID=$(gh run list --repo "${REPO}" \
        --workflow "${WORKFLOW_BOOTSTRAP}" \
        --limit 1 \
        --json databaseId \
        --jq '.[0].databaseId')
    fi
    echo "Watching run ${RUN_ID}..."
    gh run watch "${RUN_ID}" --repo "${REPO}"
    ;;

  package)
    PKG_PATH="${1:?Usage: watch-pipeline.sh package <path>}"
    echo "Triggering incremental build for ${PKG_PATH}..."
    gh workflow run "${WORKFLOW_PACKAGE}" \
      --repo "${REPO}" \
      --ref "${BRANCH}" \
      --field "package_path=${PKG_PATH}"
    sleep 3
    RUN_ID=$(gh run list --repo "${REPO}" \
      --workflow "${WORKFLOW_PACKAGE}" \
      --limit 1 \
      --json databaseId \
      --jq '.[0].databaseId')
    echo "Run ID: ${RUN_ID}"
    gh run watch "${RUN_ID}" --repo "${REPO}"
    ;;

  status)
    echo "=== Recent GNOME 49 Pipeline Runs ==="
    echo ""
    echo "--- Bootstrap (Full Build) ---"
    gh run list --repo "${REPO}" \
      --workflow "${WORKFLOW_BOOTSTRAP}" \
      --limit 5 \
      --json databaseId,status,conclusion,createdAt,displayTitle \
      --template '{{range .}}{{.databaseId}} {{.status}} {{.conclusion}} {{.createdAt}} {{.displayTitle}}{{"\n"}}{{end}}'
    echo ""
    echo "--- Incremental (Package) Builds ---"
    gh run list --repo "${REPO}" \
      --workflow "${WORKFLOW_PACKAGE}" \
      --limit 5 \
      --json databaseId,status,conclusion,createdAt,displayTitle \
      --template '{{range .}}{{.databaseId}} {{.status}} {{.conclusion}} {{.createdAt}} {{.displayTitle}}{{"\n"}}{{end}}'
    ;;

  *)
    echo "Unknown command: ${cmd}"
    echo "Usage: watch-pipeline.sh [run|watch [id]|package <path>|status]"
    exit 1
    ;;
esac
```

- [ ] **Make executable and commit**
```bash
chmod +x scripts/watch-pipeline.sh
git add scripts/watch-pipeline.sh
git commit -m "feat: add local pipeline watch/trigger script"
```

---

## Chunk 8: Renovate Configuration

Renovate will watch `src/gnome-49/**/*.spec` files for `Version:` changes relative to Fedora F43 dist-git. When a new version is found, it opens a PR updating the spec. The PR triggers `build-gnome49-package.yml`.

### Task 8.1: Create `renovate.json`

**Files:**
- Create: `renovate.json`

- [ ] **Create Renovate config**

```json
{
  "$schema": "https://docs.renovatebot.com/renovate-schema.json",
  "description": "TunaOS GNOME 49 package version tracking against Fedora F43",
  "extends": [
    "config:base"
  ],
  "enabled": true,
  "labels": ["renovate", "package-update"],
  "prHourlyLimit": 5,
  "prConcurrentLimit": 10,
  "automerge": false,
  "reviewers": ["jreilly1821"],
  "customManagers": [
    {
      "customType": "regex",
      "description": "Track RPM spec Version: fields",
      "fileMatch": ["^src/gnome-49/[^/]+/[^/]+\\.spec$"],
      "matchStrings": [
        "^Version:\\s+(?<currentValue>[0-9][^\\s]+)\\s*$"
      ],
      "depNameTemplate": "{{{packageName}}}",
      "packageNameTemplate": "{{replace '.*/' '' packageFileDir}}",
      "datasourceTemplate": "custom.fedora-f43",
      "versioningTemplate": "loose"
    }
  ],
  "customDatasources": {
    "fedora-f43": {
      "defaultRegistryUrlTemplate": "https://src.fedoraproject.org/api/0/rpms/{{packageName}}/git/tags?pattern=f43*",
      "format": "json",
      "transformTemplates": [
        "{\"releases\": $map($.tags, function($t) { {\"version\": $replace($t, /^.*-/, \"\")} }) }"
      ]
    }
  },
  "packageRules": [
    {
      "matchFileNames": ["src/gnome-49/**/*.spec"],
      "groupName": "GNOME 49 package updates",
      "commitMessagePrefix": "chore(gnome49):",
      "additionalBranchPrefix": "gnome49-"
    },
    {
      "matchPackageNames": ["gdm", "gnome-shell", "mutter"],
      "automerge": false,
      "reviewersFromCodeOwners": true,
      "labels": ["renovate", "major-component"]
    }
  ]
}
```

**Note:** The Renovate datasource for Fedora dist-git is a best-effort approximation. The Pagure REST API (`src.fedoraproject.org/api/0/`) can return package tags. If the API format doesn't work, switch to a simpler approach: a scheduled GHA workflow that runs `scripts/fetch_rawhide_specs.py` and commits version bumps directly.

- [ ] **Commit**
```bash
git add renovate.json
git commit -m "feat: add Renovate config to track GNOME 49 versions against Fedora F43"
```

### Task 8.2: Enable Renovate on the GitHub repository

- [ ] Install the [Renovate GitHub App](https://github.com/apps/renovate) on the `tuna-os/tunaos-packages` repository (or verify it's already installed).
- [ ] Trigger a Renovate run: go to the Renovate App dashboard or push a commit to the branch.
- [ ] Verify Renovate finds the spec files and creates (or would create) PRs.

---

## Chunk 9: VM Verification

### Task 9.1: Create Lima VM config for repo.tunaos.org testing

**Files:**
- Create: `tests/gnome49-repo-test.yaml`

- [ ] **Create the VM config**

```yaml
# tests/gnome49-repo-test.yaml
# Lima VM config: verify GNOME 49 packages install correctly from repo.tunaos.org
#
# Usage:
#   limactl start tests/gnome49-repo-test.yaml
#   limactl shell gnome49-repo-test

vmType: qemu
arch: x86_64
cpus: 4
memory: 8GiB
disk: 40GiB

video:
  display: "vnc"
  vga: "std"

images:
  - location: "https://cloud.centos.org/centos/10-stream/x86_64/images/CentOS-Stream-GenericCloud-x86_64-10-latest.x86_64.qcow2"
    arch: x86_64

mounts:
  - location: "~"
    writable: true

ssh:
  localPort: 0
  loadDotSSHPubKeys: true

provision:
  - mode: system
    script: |
      #!/bin/bash
      set -euxo pipefail

      # Install from repo.tunaos.org (self-hosted, not COPR)
      dnf -y install dnf-plugins-core curl
      curl -sSL https://repo.tunaos.org/gnome49/install.sh | bash

      # Install GNOME 49 components
      dnf -y install \
        gnome-shell \
        gnome-control-center \
        mutter \
        gdm \
        gnome-session-wayland-session \
        nautilus \
        glib2 \
        gnome49-el10-compat

      # Enable graphical boot
      dnf -y group install "Fonts"
      systemctl enable gdm
      systemctl set-default graphical.target

      echo "GNOME 49 from repo.tunaos.org installation complete."
```

- [ ] **Commit**
```bash
git add tests/gnome49-repo-test.yaml
git commit -m "feat: add Lima VM config for end-to-end repo.tunaos.org verification"
```

---

## Chunk 10: Documentation Updates

### Task 10.1: Update AGENTS.md

- [ ] **Replace the current AGENTS.md content** with an updated version that reflects the new self-hosted pipeline. Keep all existing sections and add a new section for the GHA pipeline.

Key additions to AGENTS.md:
- New section: "Self-Hosted GitHub Actions Pipeline"
  - Describes `.copr/build-order-gnome49.yml` and the two new workflow files
  - Documents R2 path layout: `gnome49/10-stream-x86_64/`
  - Lists the `scripts/watch-pipeline.sh` commands
  - Notes: never modify `build-order.yml` or existing GNOME 50 workflows
- Updated "Current Status" section for GNOME 49 COPR and GHA pipeline
- Updated "Work Items" checklist

### Task 10.2: Update GEMINI.md

- [ ] **Append a new section to GEMINI.md** with rules specific to the GHA pipeline:

```markdown
## 5. Self-Hosted Pipeline Rules (GitHub Actions + R2)

### CRITICAL: What NOT to touch
- **NEVER** modify `build-order.yml` (GNOME 50 manifest) or any GNOME 50 workflow files.
- **NEVER** modify the existing `repo/10-x86_64/` or `repo/10-stream-x86_64/` R2 paths (GNOME 50 data).
- **NEVER** change `workers/repo-proxy.ts` unless explicitly asked — the GNOME 49 path `/gnome49/...` works without Worker changes.
- **NEVER** change COPR build commands (`just copr-build`, `just copr-scm-build`) — COPR and GHA pipelines are parallel, not replacements.

### GNOME 49 GHA Pipeline
- Manifest: `.copr/build-order-gnome49.yml` (separate from GNOME 50's `build-order.yml`)
- Bootstrap workflow: `.github/workflows/build-gnome49-distributed.yml` (GENERATED — regenerate with `python3 scripts/generate-distributed-workflow.py .copr/build-order-gnome49.yml ...`)
- Incremental workflow: `.github/workflows/build-gnome49-package.yml` (manually maintained)
- R2 upload path: `r2:bluefin/gnome49/10-stream-x86_64/`
- Public URL: `https://repo.tunaos.org/gnome49/10-stream-x86_64/`
- All new work lives in `gnome-49-pipeline` branch until stable.

### Renovate
- `renovate.json` tracks `src/gnome-49/**/*.spec` Version: fields against Fedora F43.
- When Renovate opens a PR, the `build-gnome49-package.yml` workflow auto-triggers.
- Do NOT automerge Renovate PRs for major components (gdm, mutter, gnome-shell).
```

- [ ] **Commit**
```bash
git add AGENTS.md GEMINI.md
git commit -m "docs: update AGENTS.md and GEMINI.md for GNOME 49 GHA pipeline"
```

---

## Chunk 11: First End-to-End Run

### Task 11.1: Trigger the first full bootstrap build

- [ ] **Push the branch**
```bash
git push -u origin gnome-49-pipeline
```

- [ ] **Trigger the bootstrap workflow**
```bash
scripts/watch-pipeline.sh run
```

- [ ] **Watch progress tier by tier** — each tier should take 5-15 minutes. If a tier fails:
  - Check the job logs in GitHub Actions UI
  - Fix the spec or build-order
  - Re-run just the failed tier: `gh workflow run build-gnome49-distributed.yml --field tier=<tier-name>`
  - Or use the incremental workflow: `scripts/watch-pipeline.sh package src/gnome-49/<package>`

- [ ] **After publish succeeds, verify the repo**
```bash
curl -sI https://repo.tunaos.org/gnome49/10-stream-x86_64/repodata/repomd.xml
# Expected: HTTP 200

curl -s https://repo.tunaos.org/gnome49/10-stream-x86_64/repodata/repomd.xml | grep revision
```

### Task 11.2: VM end-to-end test

- [ ] **Start the test VM**
```bash
limactl start tests/gnome49-repo-test.yaml
```

- [ ] **Verify packages install from repo.tunaos.org** (not COPR)
```bash
limactl shell gnome49-repo-test -- sudo dnf info gdm | grep Repo
# Expected: tunaos-gnome49 (not COPR)
```

- [ ] **Verify GDM starts and socket exists**
```bash
limactl shell gnome49-repo-test -- \
  sudo systemctl start gdm && \
  sleep 5 && \
  ls /run/systemd/userdb/org.gnome.DisplayManager && \
  echo "SOCKET_EXISTS"
```

- [ ] **Stop test VM**
```bash
limactl stop gnome49-repo-test
limactl delete gnome49-repo-test
```

---

## Chunk 12: Merge Strategy

When the pipeline is stable and the VM test passes:

- [ ] **Open a PR**: `gnome-49-pipeline` → `main`
- [ ] **PR checklist before merge**:
  - [ ] All new files only (`.copr/build-order-gnome49.yml`, new `.github/workflows/build-gnome49-*.yml`, `scripts/watch-pipeline.sh`, `contrib/install-gnome49.sh`, `renovate.json`, `tests/gnome49-repo-test.yaml`)
  - [ ] `build-order.yml` is unchanged (confirm with `git diff main -- build-order.yml`)
  - [ ] `build-distributed.yml` is unchanged
  - [ ] `build.yml` is unchanged
  - [ ] COPR justfile commands work: `just copr-status`
  - [ ] repo.tunaos.org/gnome49/ returns HTTP 200 for repomd.xml
  - [ ] GPG check passes: `rpm --checksig <package>.rpm`
- [ ] **After merge**: verify `main` branch workflows still work for GNOME 50

---

## Known Issues & Decisions

| Issue | Decision |
|-------|----------|
| `.copr/build-order-gnome49.yml` tier ordering may need adjustment once actual builds run | Adjust tiers iteratively; re-generate workflow after each change |
| Renovate's Fedora Pagure datasource may not match API format | Fall back to scheduled `scripts/fetch_rawhide_specs.py` script if needed |
| `spec_override` in gnome49 manifest (glib2-bootstrap, gi-bootstrap) needs generator support | Verify generator already handles `spec_override` field (it does for GNOME 50) |
| GPG key: `install-gnome49.sh` enables `gpgcheck=1` but RPMs must be signed | GPG signing happens in the `publish` job; first run signs all RPMs |
| `mozjs128` for gjs — available in EPEL 10 base or needs building? | Check `dnf info mozjs128` on CS10; if missing, add to tier 0 |

---

## Quick Reference

```bash
# Generate GNOME 49 workflow (after changing manifest)
python3 scripts/generate-distributed-workflow.py \
  .copr/build-order-gnome49.yml \
  .github/workflows/build-gnome49-distributed.yml \
  --name "GNOME 49 Distributed Build and Publish" \
  --r2-path "gnome49/10-stream-x86_64"

# Trigger full rebuild
scripts/watch-pipeline.sh run

# Watch latest run
scripts/watch-pipeline.sh watch

# Build a single package
scripts/watch-pipeline.sh package src/gnome-49/gdm

# Check run status
scripts/watch-pipeline.sh status

# Verify repo health
curl -sI https://repo.tunaos.org/gnome49/10-stream-x86_64/repodata/repomd.xml
```
