set dotenv-load := true

export R2_BUCKET := "bluefin"

default:
    @just --list

# Ask the factory for a desktop on a target, in words.
#
#   just want "gnome 51 on hummingbird"
#
# Resolves the ask against the target contract and says what it would build,
# what a release move would touch, and -- with `just want-measured` -- how far
# the published index already is. See docs/rfc/rfc012-request-driven-convergence.md.
want request:
    python3 scripts/request.py "{{request}}"

# The same ask, measured against the live published index: served vs wanted
# over the generated build order. Needs network.
want-measured request:
    python3 scripts/request.py "{{request}}" --measure

# Move every declaration onto a new release track: the roots manifest, the
# cells' source_paths, the catalog and the fan-out's epoch derivation all name
# the old tree, and missing one silently ships the previous release. Fails
# rather than half-renaming, and reports what it deliberately did not touch.
adopt request:
    python3 scripts/request.py "{{request}}" --adopt

# Build a cell on a host that REMEMBERS: the local repo, the mock root cache
# and the served-NVR list persist between runs, so a failed package costs one
# package rather than a run.
#
#   just warm hummingbird-x86_64
#   just warm-status hummingbird-x86_64
#   just warm-forget hummingbird-x86_64 gtk4
#
# See docs/WARM-BUILDER.md for provisioning the host.
warm cell *args:
    bash scripts/warm-builder.sh --cell {{cell}} {{args}}

warm-status cell:
    bash scripts/warm-builder.sh --cell {{cell}} --status

warm-forget cell package:
    bash scripts/warm-builder.sh --cell {{cell}} --forget {{package}} --status

# Fast, deterministic validation that mirrors the non-build portions of CI.
# Keep the distributed Hummingbird/package builds in their dedicated workflows:
# they are intentionally not a developer-machine prerequisite.
check: lint-python
    python3 -m pytest tests/ -v --tb=short
    python3 scripts/parse-build-order.py build-order.yml --validate

# Run the lint policy declared in ruff.toml.
#
# ruff.toml has selected E, F, I and UP at 100 columns since #626, but nothing
# executed ruff, so the policy drifted to 378 violations while reading as
# enforced. This recipe is the developer-side runner; the CI half is a job in
# .github/workflows/lint.yml, which the filing bot cannot push (see #764).
#
# E501 is excluded here and not in ruff.toml on purpose: the line-length
# backlog is 212 violations in 26 files, tracked in #765, and a config-level
# exclusion would silently retire the rule instead of leaving it to be paid off.
lint-python:
    python3 -m ruff check --extend-ignore E501 .

# Build RPM for a single target
build target:
    #!/usr/bin/env bash
    set -euo pipefail
    
    if [ -z "{{target}}" ]; then
        echo "Error: target is required"
        exit 1
    fi
    
    mock -r {{target}} --init
    mock -r {{target}} --build src/*.src.rpm
    mock -r {{target}} --resultdir=./output/{{target}} clean

# Build and sign RPM for a single target
build-and-sign target:
    #!/usr/bin/env bash
    set -euo pipefail
    
    just build {{target}}
    rpmsign --addsign ./output/{{target}}/*.rpm

# Update repository metadata
update-metadata target:
    #!/usr/bin/env bash
    set -euo pipefail
    createrepo_c --update ./output/{{target}}/

# Build all x86_64 targets
build-x86_64: build-fedora-44-x86_64 build-fedora-eln-x86_64 build-almalinux-10-x86_64 build-centos-stream-10-x86_64

# Build all ARM64 targets
build-aarch64: build-fedora-44-aarch64 build-fedora-eln-aarch64 build-almalinux-10-aarch64 build-centos-stream-10-aarch64

# Build all targets
build-all: build-fedora-44-x86_64 build-fedora-eln-x86_64 build-almalinux-10-x86_64 build-almalinux-10-x86_64_v2 build-centos-stream-10-x86_64

build-fedora-44-x86_64:
    @just build fedora-44-x86_64

build-fedora-44-aarch64:
    @just build fedora-44-aarch64

build-fedora-eln-x86_64:
    @just build fedora-eln-x86_64

build-fedora-eln-aarch64:
    @just build fedora-eln-aarch64

build-almalinux-10-x86_64:
    @just build almalinux-10-x86_64

build-almalinux-10-x86_64_v2:
    @just build almalinux-10-x86_64_v2

build-almalinux-10-aarch64:
    @just build almalinux-10-aarch64

build-centos-stream-10-x86_64:
    @just build centos-stream-10-x86_64

build-centos-stream-10-aarch64:
    @just build centos-stream-10-aarch64

# Sign all RPMs in output directory
sign-all:
    #!/usr/bin/env bash
    set -euo pipefail
    find ./output -name "*.rpm" -exec rpmsign --addsign {} \;

# Sync to R2 bucket
sync-to-r2 target:
    #!/usr/bin/env bash
    set -euo pipefail
    rclone --s3-no-check-bucket sync ./output/{{target}}/ "r2:${R2_BUCKET}/repo/{{target}}/"
    # Pull the RPMs but NOT the published repodata, then regenerate from the
    # actual files: `--update` against seeded published metadata carries
    # pre-existing entries forward without re-hashing, so one drifted
    # checksum is republished forever (#358, audited in #421).
    rclone --s3-no-check-bucket sync "r2:${R2_BUCKET}/repo/{{target}}/" ./repodata/{{target}}/ --exclude "repodata/**"
    createrepo_c ./repodata/{{target}}/
    rclone --s3-no-check-bucket sync ./repodata/{{target}}/ "r2:${R2_BUCKET}/repo/{{target}}/"

# Full build and publish pipeline
publish target:
    @just build-and-sign {{target}}
    @just update-metadata {{target}}
    @just sync-to-r2 {{target}}

# Deploy the Cloudflare Worker proxy
deploy-proxy:
    npx wrangler deploy

# Interactively check for required secrets and configuration
check-secrets:
    #!/usr/bin/env bash
    set -euo pipefail
    MISSING=0
    for s in R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY CLOUDFLARE_ACCOUNT_ID GPG_PRIVATE_KEY GPG_PASSPHRASE; do
        if ! grep -q "$s" .env 2>/dev/null; then
            echo "[-] Missing secret in .env: $s"
            MISSING=$((MISSING+1))
        else
            echo "[+] Found secret in .env: $s"
        fi
    done
    if [ $MISSING -eq 0 ]; then
        echo "All secrets found in .env. You can now use 'just publish'."
    else
        echo "Please add the missing secrets to your .env file or GitHub Secrets."
    fi

# Initial setup for the project
setup:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "=== Initializing GitHub Copr-like RPM Repository ==="
    if ! command -v mock &>/dev/null; then echo "Warning: 'mock' not found. Local builds will fail."; fi
    if ! command -v createrepo_c &>/dev/null; then echo "Warning: 'createrepo_c' not found."; fi
    if ! command -v rclone &>/dev/null; then echo "Warning: 'rclone' not found. Syncing to R2 will fail."; fi
    if ! command -v wrangler &>/dev/null; then echo "Hint: install wrangler with 'npm install -g wrangler'"; fi
    
    if [ ! -f .env ]; then
        echo "Creating .env from .env.example..."
        cp .env.example .env
    fi
    
    if [ ! -f public.gpg ]; then
        echo "Warning: public.gpg not found. Follow GPG_SETUP.md to generate your signing key."
    fi
    
    echo "Setup complete. Check GPG_SETUP.md for key generation steps."

# Upload GPG public key and install script to R2 root
publish-static:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ ! -f public.gpg ]; then
        echo "Error: public.gpg not found. Run GPG export first."
        exit 1
    fi
    echo "Uploading public.gpg..."
    rclone --s3-no-check-bucket copyto public.gpg "r2:${R2_BUCKET}/public.gpg"
    echo "Uploading install.sh..."
    rclone --s3-no-check-bucket copyto contrib/install.sh "r2:${R2_BUCKET}/install.sh"

# Build a package in COPR from Fedora Rawhide dist-git (for unmodified packages)
copr-build package project='jreilly1821/c10s-gnome-50' chroot='epel-10-x86_64':
    #!/usr/bin/env bash
    set -euo pipefail
    copr-cli add-package-distgit {{project}} --name {{package}} --distgit fedora --commit rawhide 2>/dev/null || \
    copr-cli edit-package-distgit {{project}} --name {{package}} --distgit fedora --commit rawhide
    copr-cli build-package {{project}} --name {{package}} --chroot {{chroot}} --nowait

# Build a modified package in COPR from our git repo (preferred over copr-srpm-build)
# Usage: just copr-scm-build src/deps/gnome-autoar
copr-scm-build path project='jreilly1821/c10s-gnome-50' chroot='epel-10-x86_64':
    #!/usr/bin/env bash
    set -euo pipefail
    SPEC=$(ls {{path}}/*.spec | grep -v bootstrap | head -n 1)
    NAME=$(rpmspec -q --qf "%{name}\n" "$SPEC" 2>/dev/null | head -n 1)
    REPO_ROOT=$(git rev-parse --show-toplevel)
    REMOTE_URL=$(git remote get-url origin)
    BRANCH=$(git rev-parse --abbrev-ref HEAD)
    SUBDIR=$(realpath --relative-to="$REPO_ROOT" "{{path}}")
    SPECFILE=$(basename "$SPEC")
    echo "Setting up SCM source: $NAME from $REMOTE_URL ($BRANCH) subdir=$SUBDIR spec=$SPECFILE"
    copr-cli edit-package-scm {{project}} \
        --name "$NAME" \
        --clone-url "$REMOTE_URL" \
        --commit "$BRANCH" \
        --subdir "$SUBDIR" \
        --spec "$SPECFILE" \
        --method make_srpm 2>/dev/null || \
    copr-cli add-package-scm {{project}} \
        --name "$NAME" \
        --clone-url "$REMOTE_URL" \
        --commit "$BRANCH" \
        --subdir "$SUBDIR" \
        --spec "$SPECFILE" \
        --method make_srpm
    copr-cli build-package {{project}} --name "$NAME" --chroot {{chroot}} --nowait

# Build a local package in COPR by generating an SRPM first (avoid — use copr-scm-build instead)
copr-srpm-build path project='jreilly1821/c10s-gnome-50' chroot='epel-10-x86_64':
    #!/usr/bin/env bash
    set -euo pipefail
    echo "WARNING: copr-srpm-build uploads a local SRPM. Prefer 'just copr-scm-build {{path}}' for modified specs."
    # Determine spec file (use exact name match to avoid bootstrap specs)
    SPEC=$(ls {{path}}/*.spec | grep -v bootstrap | head -n 1)
    NAME=$(rpmspec -q --qf "%{name}\n" $SPEC | head -n 1)
    echo "Building $NAME from $SPEC..."

    mkdir -p build/{SOURCES,SPECS,SRPMS}
    cp {{path}}/* build/SOURCES/ 2>/dev/null || true
    cp $SPEC build/SPECS/

    rpmbuild -bs $SPEC --define "_topdir $PWD/build" --define "dist .el10"
    SRPM=$(ls build/SRPMS/${NAME}-*.src.rpm | head -n 1)

    copr-cli build {{project}} $SRPM --chroot {{chroot}} --nowait

# Check status of builds in the COPR project
copr-status project='jreilly1821/c10s-gnome-50':
    copr-cli list-builds {{project}} | head -n 20

# Watch the COPR project builds in real-time
watch-copr project='jreilly1821/c10s-gnome-49':
    copr-cli monitor {{project}} --fields name,chroot,state,url_build_log

# Download and open logs for a specific build ID
copr-logs build_id:
    #!/usr/bin/env bash
    set -euo pipefail
    DIR="build-logs/{{build_id}}"
    mkdir -p $DIR
    copr-cli download-build --logs {{build_id}} --dest $DIR
    # Find build.log (might be gzipped)
    LOG=$(find $DIR -name "builder-live.log*" | head -n 1)
    if [[ "$LOG" == *.gz ]]; then
        zless "$LOG"
    else
        less "$LOG"
    fi

# Clean build artifacts
clean:
    rm -rf output/ repodata/ build/

# Pull all RPMs from R2, sign them, and push back
sign-r2:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p tmp-repo
    echo "Downloading existing repository from R2..."
    rclone --s3-no-check-bucket sync "r2:${R2_BUCKET}/repo/" ./tmp-repo/
    echo "Signing RPMs..."
    find ./tmp-repo -name "*.rpm" -exec rpmsign --addsign {} \;
    echo "Updating metadata..."
    for dir in ./tmp-repo/*; do
        if [ -d "$dir" ]; then
            createrepo_c --update "$dir"
        fi
    done
    echo "Uploading signed repository back to R2..."
    rclone --s3-no-check-bucket sync ./tmp-repo/ "r2:${R2_BUCKET}/repo/"
    rm -rf tmp-repo

# Verify GPG setup
verify-gpg:
    #!/usr/bin/env bash
    set -euo pipefail
    if ! gpg --list-secret-keys | grep -q "RPM Signing"; then
        echo "Error: No GPG key found for RPM signing"
        exit 1
    fi
    echo "GPG key found:"
    gpg --list-secret-keys "RPM Signing"

# Run a VM for testing (type: iso or qcow2)
run-vm type variant='centos-stream-10' flavor='gnome' iso_file='':
    @just _run-vm {{type}} {{variant}} {{flavor}} {{iso_file}}

# Download the latest CentOS Stream 10 GNOME Live ISO
download-iso:
    #!/usr/bin/env bash
    set -euo pipefail
    URL="https://mirror.stream.centos.org/SIGs/10-stream/altimages/images/live/x86_64/CentOS-Stream-Image-GNOME-Live.x86_64-10-202601110111.iso"
    DEST="centos-stream-10.iso"
    if [ ! -f "$DEST" ]; then
        echo "Downloading CentOS Stream 10 GNOME Live ISO..."
        curl -Lo "$DEST" "$URL"
    else
        echo "ISO already exists: $DEST"
    fi

[private]
_run-vm type variant flavor='gnome' iso_file='':
    #!/usr/bin/env bash
    set -eoux pipefail

    # Determine the image file based on the type and project conventions
    if [[ -n "{{ iso_file }}" ]]; then
        image_file="{{ iso_file }}"
    elif [[ "{{ type }}" == "iso" ]]; then
        # Check for titanoboa output first
        TITANOBOA_ISO=".build/{{ variant }}-{{ flavor }}/output/install.iso"
        # Check for bootc-image-builder output
        BIB_ISO="{{ variant }}.iso"
        if [[ -f "$TITANOBOA_ISO" ]]; then
            image_file="$TITANOBOA_ISO"
        elif [[ -f "$BIB_ISO" ]]; then
            image_file="$BIB_ISO"
        else
            image_file="{{ variant }}.iso"
        fi
    else
        # QCow2 follows variant[-flavor].qcow2
        if [[ -f "{{ variant }}-{{ flavor }}.qcow2" ]]; then
            image_file="{{ variant }}-{{ flavor }}.qcow2"
        else
            image_file="{{ variant }}.qcow2"
        fi
    fi

    # Build or download the image if it does not exist
    if [[ ! -f "${image_file}" ]]; then
        if [[ -n "{{ iso_file }}" ]]; then
            echo "ISO not found at {{ iso_file }}. Please build it first or specify a valid ISO path."
            exit 1
        fi
        
        # If it's the default centos-stream-10 ISO, offer to download it
        if [[ "{{ type }}" == "iso" && "{{ variant }}" == "centos-stream-10" ]]; then
            echo "Image ${image_file} not found. Downloading latest version..."
            {{ just_executable() }} download-iso
        else
            echo "Image ${image_file} not found. Please ensure the ISO exists or specify a path."
            exit 1
        fi
    fi

    # Determine an available port to use for Web VNC
    port=8006
    while ss -tln | grep -q ":${port} "; do
        port=$(( port + 1 ))
    done
    echo "Using Web Port: ${port}"
    echo "Connect via Web: http://localhost:${port}"

    # Set up the arguments for running the VM
    run_args=()
    run_args+=(--rm --privileged)
    run_args+=(--pull=newer)
    run_args+=(--publish "127.0.0.1:${port}:8006")
    run_args+=(--env "CPU_CORES=4")
    run_args+=(--env "RAM_SIZE=4G")
    run_args+=(--env "DISK_SIZE=64G")
    run_args+=(--env "TPM=Y")
    run_args+=(--env "GPU=Y")
    run_args+=(--device=/dev/kvm)

    # Add SSH port forwarding
    ssh_port=$(( port + 1 ))
    while ss -tln | grep -q ":${ssh_port} "; do
        ssh_port=$(( ssh_port + 1 ))
    done
    echo "Using SSH Port: ${ssh_port}"
    echo "Connect via SSH: ssh centos@localhost -p ${ssh_port}"
    run_args+=(--publish "127.0.0.1:${ssh_port}:22")
    run_args+=(--env "USER_PORTS=22")
    run_args+=(--env "NETWORK=user")

    run_args+=(--volume "${PWD}/${image_file}":"/boot.{{ type }}")
    run_args+=(ghcr.io/qemus/qemu)

    # Run the VM and open the browser to connect
    (sleep 5 && xdg-open "http://localhost:${port}") &
    podman run "${run_args[@]}"

# Test the repository on a remote CentOS 10 machine
test-remote target='root@192.168.122.135':
    #!/usr/bin/env bash
    set -euo pipefail
    echo "=== Testing Tuna OS Repo on {{ target }} ==="
    
    # Upload and run the installation script
    scp contrib/install.sh {{ target }}:/tmp/install-tunaos.sh
    ssh {{ target }} "bash /tmp/install-tunaos.sh"
    
    # Force a refresh and upgrade from the tuna-os repo
    echo "=== Upgrading GNOME components from Tuna OS ==="
    ssh {{ target }} "dnf clean all && dnf makecache && dnf upgrade -y --repo=tuna-os"
    
    echo "=== Upgrade complete. Please reboot the remote machine to apply changes. ==="

# Rsync the local repo to a remote machine and test
rsync-test target='root@192.168.122.135':
    #!/usr/bin/env bash
    set -euo pipefail
    echo "=== Syncing local repo to {{ target }} ==="
    
    # Ensure rsync is installed on target
    ssh {{ target }} "dnf install -y rsync"
    
    # Sync the repo
    rsync -avz --delete local-repo/ {{ target }}:/root/tuna-os-local/
    
    # Create a local repo file on the target
    ssh {{ target }} "printf '[tuna-os-local]\nname=Tuna OS Local\nbaseurl=file:///root/tuna-os-local/\nenabled=1\ngpgcheck=0\npriority=1\n' > /etc/yum.repos.d/tuna-os-local.repo"
    
    echo "=== Upgrading from local rsync repo ==="
    ssh {{ target }} "dnf clean all && dnf upgrade -y --repo=tuna-os-local --allowerasing"
    
    echo "=== Local upgrade complete. Please reboot. ==="
