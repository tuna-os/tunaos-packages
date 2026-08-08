#!/usr/bin/env bash
#
# Build Chain Engine
#
# Builds RPM packages tier-by-tier from build-order.yml.
# Packages within a tier build in parallel (--jobs N); tiers are sequential.
#
# Backends:
#   podman  - runs rpmbuild inside a CentOS Stream 10 container (default)
#   mock    - uses mock chroots (requires mock group membership)
#
# Usage:
#   ./scripts/build-chain.sh [options]
#
# Options:
#   --manifest <path>    Path to build-order.yml (default: build-order.yml)
#   --backend <name>     Build backend: podman, mock, or native (default: podman)
#   --image <ref>        Container image for podman backend
#                        (default: quay.io/centos/centos:stream10)
#   --mock-config <cfg>  Mock config name (default: centos-stream-10-ci)
#   --local-repo <path>  Path to local repo directory (default: ./local-repo)
#   --jobs <N>           Parallel jobs within a tier (default: nproc/2)
#   --tier <name>        Only build a specific tier
#   --package <path>     Only build a specific package path
#   --with-checks        Run the RPM %check section (release-gate mode)
#   --dry-run            Print what would be built without building

# -E so the ERR trap below is inherited by functions, subshells and command
# substitutions. Without it a trap set here never fires for a failure inside
# build_package_podman -- which is where essentially every real failure is --
# and the script dies silently. Verified: with plain `set -e` the trap did not
# print for a `command not found` inside ensure_local_repo.
set -eEuo pipefail

# Say why we died, as the LAST thing in the log.
#
# set -e means any unhandled non-zero command kills this script on the spot,
# before the end-of-run summary that lists failed packages. When that happens
# inside a long tier the reason ends up buried in the middle of a log that can
# be hundreds of MB, and every practical way of reading a CI log -- the GitHub
# API, `gh run view --log-failed`, the web viewer -- gives you the TAIL.
#
# Measured cost: six independent retrieval paths were tried against one failed
# Hummingbird run (job logs three ways, check-run annotations twice, the web
# UI) and not one returned the failing line. The run was reduced to "Process
# completed with exit code 1" with no package named, which is unactionable.
#
# An ERR trap costs nothing on the happy path and makes the failure the last
# thing printed, so the tail always carries it. Everything here is guarded
# with || true: a diagnostic that dies while reporting a death tells you even
# less than no diagnostic.
#
# Two things this got wrong on first contact with a real failure (run
# 31264779379), both visible in its own output:
#
#   command     : main
#
# A DEBUG trap used to record the last command. DEBUG is NOT inherited by
# functions or subshells without `set -T`, so it only ever saw the top-level
# `main "$@"` -- which is every in-function death, i.e. all of them. $BASH_COMMAND
# read as the first thing in the handler is the command that actually tripped
# the trap, with no DEBUG trap and no per-command overhead. `set -T` is not the
# alternative here: functrace also makes RETURN traps inherited, and this script
# hangs `rm -rf "$builddir"` off RETURN, so every nested call would delete the
# build directory out from under the build.
#
# The second was the headline. Packages build in background subshells, and -E
# gives each of them this trap, so an ordinary package failure printed
# "build-chain.sh FAILED" from a worker while the script carried on to the next
# tier. That run printed it three times and never died of any of them. A banner
# that cries abort during normal operation is worse than none, because it
# retrains the reader to ignore it. $BASHPID differs from $$ in a subshell and
# nowhere else, which separates "the script died" from "a package failed".
_on_error() {
    local rc=$? cmd=$BASH_COMMAND
    set +e
    trap - ERR
    echo "" >&2
    if [[ "$BASHPID" == "$$" ]]; then
        echo "=================== build-chain.sh FAILED ===================" >&2
    else
        # A worker subshell. The script is still running; the tier loop records
        # this package as failed and the end-of-run summary names it.
        echo "=============== package build FAILED (worker) ===============" >&2
    fi
    echo "exit status : ${rc}" >&2
    echo "at line     : ${BASH_LINENO[0]:-?}" >&2
    echo "command     : ${cmd}" >&2
    echo "tier filter : ${FILTER_TIER:-<all>}" >&2
    echo "package     : ${pkg_name:-<none in scope>}" >&2
    # The build logs mock leaves behind, if this died during a package build.
    local log
    for log in "${builddir:-/nonexistent}/results/build.log" \
               "${builddir:-/nonexistent}/results/root.log"; do
        if [[ -r "$log" ]]; then
            echo "--- tail of ${log} ---" >&2
            tail -n 40 "$log" >&2 || true
        fi
    done
    echo "=============================================================" >&2
    exit "$rc"
}
trap _on_error ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

MANIFEST="${REPO_ROOT}/build-order.yml"
BACKEND="podman"
BUILD_IMAGE="ghcr.io/tuna-os/mock-runner:centos-stream-10"
MOCK_CONFIG="centos-stream-10-ci"
# RPM %{dist} tag. Empty means "derive from the manifest's target:", so a
# manifest targeting fedora-44 gets .fc44 without every caller passing --dist.
# It used to be hard-coded to .el10 at eight separate call sites, which is why
# this repo could only ever build for EL10.
DIST=""
LOCAL_REPO="${REPO_ROOT}/local-repo"
JOBS=$(( $(nproc) / 2 ))
[[ $JOBS -lt 1 ]] && JOBS=1
FILTER_TIER=""
FILTER_PACKAGE=""
DRY_RUN=false
FORCE=false
WITH_CHECKS=false

usage() {
    echo "Usage: $0 [options]"
    echo ""
    echo "Options:"
    echo "  --manifest <path>    Path to build-order.yml (default: build-order.yml)"
    echo "  --backend <name>     Build backend: podman, mock, or native (default: podman)"
    echo "  --image <ref>        Container image for podman backend"
    echo "  --mock-config <cfg>  Mock config name (default: centos-stream-10-ci)"
    echo "  --dist <tag>         RPM %{dist} tag, e.g. .el10 or .fc44"
    echo "                       (default: derived from the manifest's target:)"
    echo "  --local-repo <path>  Path to local repo directory (default: ./local-repo)"
    echo "  --jobs <N>           Parallel jobs within a tier (default: nproc/2)"
    echo "  --tier <name>        Only build a specific tier"
    echo "  --package <path>     Only build a specific package path"
    echo "  --with-checks        Run the RPM %check section"
    echo "  --dry-run            Print what would be built without building"
    echo "  --force              Force rebuild even if package exists in repo"
    echo "  -h, --help           Show this help message"
}

# --- Argument parsing ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        --manifest)    MANIFEST="$2";    shift 2 ;;
        --backend)     BACKEND="$2";     shift 2 ;;
        --image)       BUILD_IMAGE="$2"; shift 2 ;;
        --mock-config) MOCK_CONFIG="$2"; shift 2 ;;
        --dist)        DIST="$2";        shift 2 ;;
        --local-repo)  LOCAL_REPO="$2";  shift 2 ;;
        --jobs)        JOBS="$2";        shift 2 ;;
        --tier)        FILTER_TIER="$2"; shift 2 ;;
        --package)     FILTER_PACKAGE="$2"; shift 2 ;;
        --with-checks)  WITH_CHECKS=true; shift ;;
        --dry-run)     DRY_RUN=true;     shift ;;
        --force)       FORCE=true;       shift ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

# Without --with-checks the build already passes --nocheck, so %check never
# runs -- but its BuildRequires: are still installed, and they are not free.
# Fedora guards them behind a bcond, and the guarded lines are exactly the
# packages a bootstrap buildroot does not have:
#
#   python-flit-core    %bcond tests   -> python3-pytest, python3-testpath
#   python-poetry-core  %bcond tests   -> python3-pytest-mock, python3-virtualenv,
#                                         python3-build, python3-tomli-w, ...
#
# Those exist only in Rawhide, built for Python 3.15, while Hummingbird is on
# 3.14. dnf5 then cannot resolve the buildroot at all and the build dies
# before rpmbuild starts (run 31262874931, both packages of bootstrap-00):
#
#   package python3-testpath-0.6.0-27.fc45.noarch from fedora requires
#   python(abi) = 3.15, but none of the providers can be installed
#   - cannot install both python3-3.15.0~rc1-1.fc45.x86_64 from fedora and
#     python3-3.14.6-2.2.hum1.x86_64 from hummingbird
#
# So turn the bconds off alongside %check. It goes on the SRPM build because
# that header is what mock's `dnf builddep` reads, and on mock itself so the
# dynamic-BuildRequires pass inside the chroot agrees with it.
#
# `tests` and `check` are the two names Fedora uses. --without on a spec that
# declares neither only defines a macro nothing reads, so this is inert for
# every other package.
SRPM_BCOND_ARGS=()
MOCK_BCOND_ARGS=""
if ! $WITH_CHECKS; then
    for _bcond in tests check; do
        SRPM_BCOND_ARGS+=(--without "$_bcond")
        MOCK_BCOND_ARGS+="--without=${_bcond} "
    done
fi

# --- Helpers ---
log() { echo "==> $*"; }
err() { echo "ERROR: $*" >&2; }
# Used by update_local_repo()'s retry path but never defined, so a
# createrepo_c hiccup became "warn: command not found" (exit 127) and buried
# whatever actually went wrong.
warn() { echo "WARNING: $*" >&2; }

# Derive the %{dist} tag from the manifest's `target:` when --dist was not
# given. Keeps a manifest self-describing: build-order-xfce.yml says
# centos-stream-10-x86_64 and gets .el10, build-order-xfce-fedora.yml says
# fedora-44-x86_64 and gets .fc44 — no caller has to keep the two in sync.
derive_dist() {
    local target
    target="$(sed -n 's/^target:[[:space:]]*//p' "$MANIFEST" 2>/dev/null | head -1)"
    case "$target" in
        # Rawhide's dist tag is whatever Fedora's next release number is, which
        # cannot be read off the target name and must not be guessed from the
        # build host (it is not Fedora in CI). Require it explicitly.
        fedora-rawhide*)
            err "target '${target}' has no derivable %{dist} — rawhide's tag"
            err "tracks Fedora's next release (e.g. .fc45); pass --dist"
            exit 1
            ;;
        # Hummingbird's own packages carry .hum1 — every one of the 16506
        # (name, evr, arch) tuples in its primary.xml does, measured
        # 2026-08-06. .fc43 here is deliberately NOT that tag: it marks a
        # TunaOS rebuild, and because "fc43" sorts below "hum1" for the same
        # version, a rebuild can never shadow a package Hummingbird later
        # starts shipping itself.
        #
        # It is not a claim about ABI. The buildroot ABI comes from
        # mock/hummingbird-ci.cfg, which tracks Fedora Rawhide because that is
        # what the target measurably tracks: glib2 2.89.3-1, systemd 261.2-1
        # and gcc 16.1.1 are Rawhide's versions, not F43's 2.86.5 / 258.10 /
        # 15.3.1. See docs/hummingbird-desktop-gap.md.
        hummingbird-20251124*)   echo ".fc43" ;;
        fedora-*)                 echo ".fc${target#fedora-}" | sed 's/-.*//' ;;
        centos-stream-10*|epel-10*|almalinux*-10*) echo ".el10" ;;
        centos-stream-9*|epel-9*) echo ".el9" ;;
        *)
            err "cannot derive %{dist} from target '${target:-<unset>}' in ${MANIFEST}"
            err "pass --dist explicitly (e.g. --dist .fc44)"
            exit 1
            ;;
    esac
}

if [[ -z "$DIST" ]]; then
    DIST="$(derive_dist)"
fi

ensure_local_repo() {
    mkdir -p "${LOCAL_REPO}"
    if [[ ! -f "${LOCAL_REPO}/repodata/repomd.xml" ]]; then
        log "Initializing local repo at ${LOCAL_REPO}"
        createrepo_c "${LOCAL_REPO}"
    fi
}

update_local_repo() {
    log "Updating local repo metadata"
    # Attempt to update existing metadata first (faster)
    if ! createrepo_c --update "${LOCAL_REPO}"; then
        warn "createrepo_c --update failed, attempting full re-index"
        rm -rf "${LOCAL_REPO}/repodata"
        createrepo_c "${LOCAL_REPO}"
    fi

    if [[ "$BACKEND" == "native" ]] && command -v dnf &>/dev/null; then
        dnf makecache --repo local-build 2>/dev/null || true
    fi
}

find_spec() {
    local pkg_dir="$1"
    local spec_override="$2"

    if [[ -n "$spec_override" ]]; then
        local spec="${REPO_ROOT}/${pkg_dir}/${spec_override}"
        if [[ -f "$spec" ]]; then
            echo "$spec"
            return
        fi
        err "spec_override '${spec_override}' not found in ${pkg_dir}"
        return 1
    fi

    local dir_name
    dir_name="$(basename "$pkg_dir")"
    local default_spec="${REPO_ROOT}/${pkg_dir}/${dir_name}.spec"
    if [[ -f "$default_spec" ]]; then
        echo "$default_spec"
        return
    fi

    # Fallback: any .spec that isn't a bootstrap/rawhide variant
    local specs=()
    while IFS= read -r -d '' f; do
        if [[ ! "$f" =~ -bootstrap\.spec$ ]] && [[ ! "$f" =~ -rawhide\.spec$ ]]; then
            specs+=("$f")
        fi
    done < <(find "${REPO_ROOT}/${pkg_dir}" -maxdepth 1 -name "*.spec" -print0)

    if [[ ${#specs[@]} -eq 1 ]]; then
        echo "${specs[0]}"
        return
    fi

    err "Cannot determine spec for ${pkg_dir} (found ${#specs[@]} candidates)"
    return 1
}

# Prepare a build tree (spec + patches + downloaded sources) in $builddir.
# Does NOT build — just stages everything so a backend can pick it up.
prepare_sources() {
    local builddir="$1"
    local spec="$2"
    local abs_pkg_dir="$3"
    local pkg_name
    pkg_name="$(basename "$spec" .spec)"

    mkdir -p "${builddir}"/{BUILD,BUILDROOT,RPMS,SOURCES,SRPMS,SPECS}

    cp "$spec" "${builddir}/SPECS/"

    # Copy patches and other sources (don't exclude tarballs/zips if they exist locally)
    find "$abs_pkg_dir" -maxdepth 1 -type f \
        ! -name "*.spec" \
        ! -name "sources" \
        ! -name "changelog" \
        ! -name "rpminspect.yaml" \
        ! -name "*.md" \
        -exec cp {} "${builddir}/SOURCES/" \;

    # Download tarballs. Run spectool inside BUILD_IMAGE (has rpmdevtools)
    # so the host doesn't need rpmdevtools — it's Fedora-only.
    echo "==> [${pkg_name}] Downloading sources via spectool..."
    if command -v spectool &>/dev/null; then
        spectool -g -C "${builddir}/SOURCES/" "$spec"
    else
        podman run --rm \
            --pull=always \
            -v "${builddir}:/builddir:Z" \
            "${BUILD_IMAGE}" \
            spectool -g -C /builddir/SOURCES/ "/builddir/SPECS/$(basename "$spec")"
    fi || {
        echo "ERROR: spectool failed for ${pkg_name}" >&2
        return 1
    }

    # Fetch dist-git lookaside sources. A package imported from Fedora dist-git
    # can list artifacts in its `sources` file that have no URL in the spec at
    # all (selinux-policy's container-selinux.tgz is a repacked git snapshot) —
    # spectool cannot download those, and they are not loose files in the
    # package directory. COPR's rpkg tooling fetched them from the lookaside
    # cache natively, which is why this gap only surfaced on the first full
    # GitHub-side chain build (run 30662870608, tier base-tools). Fetch any
    # sources-file entry still missing after the copy and spectool steps, and
    # verify it against the recorded checksum before trusting it.
    local sources_file="${abs_pkg_dir}/sources"
    if [[ -f "$sources_file" ]]; then
        local lookaside_name entry_name entry_hash
        lookaside_name="$(basename "$abs_pkg_dir")"
        while IFS= read -r line; do
            [[ "$line" =~ ^SHA512\ \((.+)\)\ =\ ([0-9a-f]{128})$ ]] || continue
            entry_name="${BASH_REMATCH[1]}"
            entry_hash="${BASH_REMATCH[2]}"
            [[ -f "${builddir}/SOURCES/${entry_name}" ]] && continue
            echo "==> [${pkg_name}] Fetching ${entry_name} from the Fedora lookaside cache..."
            curl -fsSL --retry 3 \
                -o "${builddir}/SOURCES/${entry_name}" \
                "https://src.fedoraproject.org/lookaside/pkgs/rpms/${lookaside_name}/${entry_name}/sha512/${entry_hash}/${entry_name}" || {
                err "lookaside fetch failed for ${entry_name} (${pkg_name})"
                return 1
            }
            echo "${entry_hash}  ${builddir}/SOURCES/${entry_name}" | sha512sum --check --quiet - || {
                err "lookaside checksum mismatch for ${entry_name} (${pkg_name})"
                return 1
            }
        done < "$sources_file"
    fi
}

# Check if the package already exists in the local repo with the same NVR
check_package_exists() {
    local pkg_name="$1"
    local spec="$2"
    local spec_basename
    spec_basename="$(basename "$spec")"

    # Query the spec file inside the container to get the expected NVR
    # We do this inside the container to ensure macro expansion (%autorelease, %dist, etc.)
    local nvr
    nvr=$(podman run --rm \
        --pull=always \
        -v "$(dirname "$spec"):/specdir:Z" \
        "${BUILD_IMAGE}" \
        rpmspec -q "/specdir/${spec_basename}" \
            --define "dist ${DIST}" \
            --queryformat "%{NAME}-%{VERSION}-%{RELEASE}\n" | head -1)

    if [[ -z "$nvr" ]]; then
        return 1
    fi

    # Check if any RPM starting with this NVR exists in the local repo
    # We check for $nvr.rpm or $nvr.*.rpm
    if ls "${LOCAL_REPO}/${nvr}"*.rpm &>/dev/null; then
        echo "==> [${pkg_name}] Skipping: ${nvr} already exists in local repo"
        return 0
    fi

    return 1
}

# --- Podman backend (mock-in-podman) ---
#
# Builds the SRPM on the host, then runs `mock --rebuild` inside a
# privileged Fedora container. Mock handles all CentOS 10 dep resolution
# and package name mappings correctly. The local-repo is bind-mounted into
# the container so mock can see RPMs built in earlier tiers.
build_package_podman() {
    local pkg_dir="$1"
    local spec_override="$2"

    local spec pkg_name abs_pkg_dir
    spec="$(find_spec "$pkg_dir" "$spec_override")"
    pkg_name="$(basename "$spec" .spec)"
    abs_pkg_dir="${REPO_ROOT}/${pkg_dir}"

    if ! $FORCE && check_package_exists "$pkg_name" "$spec"; then
        return 0
    fi

    echo "==> [${pkg_name}] Building (podman+mock) from ${pkg_dir}"

    local builddir
    builddir="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap "rm -rf '${builddir}'" RETURN

    prepare_sources "$builddir" "$spec" "$abs_pkg_dir"

    # Build SRPM inside the container (to ensure macros like %autorelease are available)
    local spec_basename
    spec_basename="$(basename "$spec")"
    echo "==> [${pkg_name}] Building SRPM..."
    podman run --rm \
        --pull=always \
        -v "${builddir}:/builddir:Z" \
        "${BUILD_IMAGE}" \
        rpmbuild -bs "/builddir/SPECS/${spec_basename}" \
            --define "_topdir /builddir" \
            --define "dist ${DIST}" \
            "${SRPM_BCOND_ARGS[@]}"

    local srpm
    srpm="$(find "${builddir}/SRPMS" -name "*.src.rpm" | head -1)"
    if [[ -z "$srpm" ]]; then
        echo "ERROR: No SRPM produced for ${pkg_name}" >&2
        return 1
    fi

    local resultdir="${builddir}/results"
    mkdir -p "$resultdir"

    # The mock-runner image bakes copies of the mock configs into /etc/mock
    # (see mock/Containerfile), but the copies are ONLY a fallback for running
    # the image by hand: the invocation below mounts this checkout's mock/
    # directory and passes --configdir, so the repo's config always wins. It
    # used to be the other way around — the baked copy won — and a fix
    # committed to mock/fedora-44-ci.cfg changed nothing in CI until someone
    # rebuilt the image (#176, run 30652383636). A config present in the repo
    # but read from an image is a trap; do not remove the --configdir wiring.

    # Ensure the local repo metadata is up-to-date before mock starts,
    # locked to prevent parallel jobs from corrupting it.
    flock "${LOCAL_REPO}/repo.lock" -c "createrepo_c --update \"${LOCAL_REPO}\""

    echo "==> [${pkg_name}] Running mock inside podman (${BUILD_IMAGE})..."

    # Optional persistent mock cache (dnf package downloads + chroot state).
    # CI sets MOCK_CACHE_DIR to a host path wrapped in actions/cache, keyed
    # per package, so a rebuild of the SAME package (Renovate bump, retry)
    # reuses its already-resolved BuildRequires instead of re-downloading
    # them from the CentOS/EPEL mirrors. No-op locally when unset.
    MOCK_CACHE_ARGS=()
    if [[ -n "${MOCK_CACHE_DIR:-}" ]]; then
        mkdir -p "${MOCK_CACHE_DIR}"
        MOCK_CACHE_ARGS=(-v "${MOCK_CACHE_DIR}:/var/cache/mock:Z")
    fi

    local mock_check_flag="--nocheck"
    $WITH_CHECKS && mock_check_flag=""

    # Wrapped in a function so the retry below re-runs the IDENTICAL
    # invocation with one extra mock argument, rather than a second copy
    # of it drifting out of sync with this one.
    _run_mock_container() {
        local mock_extra_args="${1:-}"
        podman run --rm --privileged \
            --pull=always \
            -v "${builddir}:/builddir:Z" \
            -v "${LOCAL_REPO}:/local-repo:Z" \
            -v "${REPO_ROOT}/mock:/repo-mock:ro,Z" \
            "${MOCK_CACHE_ARGS[@]}" \
            "${BUILD_IMAGE}" \
            bash -exc "
                # mock refuses to run as root — even 'mock --version' exits with
                # 'Insufficient rights.' It wants an unprivileged user in the mock
                # group and drops privileges itself. This container runs as root,
                # which was fine with older mock but broke every build once the
                # runner image was rebuilt onto a newer one: mock exited before
                # producing build.log or root.log, so the failure looked like an
                # infrastructure glitch rather than a permissions rule.
                #
                # Only /builddir: that is what mock writes results into.
                # /local-repo is a HOST-mounted directory that mock merely reads as
                # a repo, and chowning it to the in-container builder uid locked the
                # runner out of its own workspace, so createrepo_c could not create
                # .repodata there.
                #
                # NOTE: never put a double quote in this string, not even inside a
                # comment. It is passed as bash -exc from the host shell, so a
                # literal double quote closes it early; bash then gets the script
                # as two arguments, treats the second as $0, and silently drops
                # everything after the break. A quoted phrase in this very comment
                # did that and turned the whole container step into a no-op.
                chown -R builder /builddir 2>/dev/null || true
                # Hand /builddir back to root on ANY exit, not just the happy
                # path. Mock runs as builder, so everything it writes under
                # /builddir is builder-owned inside the container; the HOST
                # process is the plain CI runner user, outside this container
                # entirely, and cannot read builder-owned files through the
                # bind mount.
                #
                # This was a plain command after the flock below. Under set -e
                # it never ran when mock failed: the failure branch does exit 1
                # INSIDE the flock string, so the flock command itself fails and
                # aborts this script right there. So on exactly the path where
                # the logs matter most, results stayed unreadable to the host.
                #
                # What that cost: the dnf5 already-installed retry below greps
                # results/root.log to decide whether to retry. On an unreadable
                # file grep -qs fails silently, so the guard fell through to
                # return 1 and the retry never fired -- canary run 31242725235
                # came back built=24 failed=8, bit-identical to its no-fix
                # baseline. Traced directly: this chown appears in the set -x
                # output for pytz and rust-matugen, which built, and is absent
                # for python-wcwidth, which failed.
                #
                # It also predates that: without it a SUCCESSFUL build handed
                # back results the host could not enumerate --
                #   find: /tmp/tmp.XXXXXX/results: Permission denied
                #   ERROR: No RPMs produced for xfce4-dev-tools
                # -- which is what put the command here in the first place. A
                # trap covers both, and cannot be skipped by a later early exit.
                trap 'chown -R root:root /builddir 2>/dev/null || true' EXIT
                # Assemble a config directory where the checked-out mock/ configs
                # override the copies baked into this image: copy the WHOLE
                # /etc/mock tree, then overlay the repo profiles on top.
                #
                # The whole tree, not just site-defaults.cfg and logging.ini. An
                # include() of an ABSOLUTE path such as /etc/mock/fedora-44-x86_64.cfg
                # resolves to the image, but that distro config then does a
                # RELATIVE include of templates/fedora-branched.tpl, and relative
                # includes resolve against --configdir, not /etc/mock — a
                # configdir carrying only .cfg files orphans the templates
                # directory and mock dies with: Could not find included config
                # file: /tmp/mock-configdir/templates/fedora-branched.tpl
                # (run 30654065913).
                mkdir -p /tmp/mock-configdir
                cp -a /etc/mock/. /tmp/mock-configdir/
                # -p, and it is load-bearing. mock invalidates its root cache
                # when any file in config_paths is newer than the cache
                # tarball (plugins/root_cache.py _unpack_root_cache). A plain
                # cp stamps the profile with the CURRENT time in every
                # container, so the config was always newer than a cache any
                # earlier package had written and mock unlinked it before it
                # could ever be read. Measured in run 31268488082, which had
                # MOCK_CACHE_DIR set and mounted correctly and still logged
                #   INFO: /tmp/mock-configdir/hummingbird-ci.cfg newer than
                #   root cache; cache will be rebuilt
                # 18 times, unpacked the cache 0 times, and came out 39.5m
                # against a 39.0m no-cache baseline (31265993115). The mount
                # was right; this one flag was what made it worthless.
                cp -p /repo-mock/*.cfg /tmp/mock-configdir/
                chmod -R a+rX /tmp/mock-configdir
                # SHARED lock: mock only READS /local-repo as a dnf repo, so
                # any number of builds can hold it at once. The exclusive half
                # is the createrepo_c --update on the host, which rewrites the
                # metadata mock is reading -- that is the only thing here that
                # ever needed serializing.
                #
                # No backticks anywhere in this comment, for the same reason
                # the header above bans double quotes: these lines are inside
                # the bash -exc STRING, where a backtick is command
                # substitution, not punctuation. Quoting createrepo_c that way
                # made shellcheck flag SC2006 -- and it was right, the host
                # shell would have run it while building the string.
                #
                # This was an EXCLUSIVE lock, with the comment: the builds
                # \"share mock chroot initialization\". They do not, on three
                # counts, all of which predate this change:
                #   * --uniqueext below gives every package its own chroot
                #     (/var/lib/mock/<config>-<pkg>), which is what the flag is
                #     for;
                #   * /var/lib/mock lives INSIDE this container and is thrown
                #     away with it, so two concurrent builds cannot see each
                #     other's chroots at all;
                #   * /var/cache/mock is only bind-mounted when MOCK_CACHE_DIR
                #     is set, and the Hummingbird workflow does not set it.
                # So the exclusive lock protected nothing, while serialising
                # the single most expensive step in the run: --jobs N started N
                # workers that then took turns compiling one at a time.
                flock -s /local-repo/repo.lock -c \"
                    setpriv --reuid=builder --regid=mock --init-groups \\
                    mock --configdir /tmp/mock-configdir -r '${MOCK_CONFIG}' \\
                        --uniqueext='${pkg_name}' \\
                        --rebuild /builddir/SRPMS/*.src.rpm \\
                        --resultdir=/builddir/results \\
                        --define 'dist ${DIST}' \\
                        ${mock_check_flag} ${MOCK_BCOND_ARGS} \\
                        --no-clean \\
                        --no-cleanup-after ${mock_extra_args} || {
                            echo 'ERROR: mock failed. Printing build.log:';
                            cat /builddir/results/build.log || true;
                            echo 'ERROR: Printing root.log:';
                            cat /builddir/results/root.log || true;
                            exit 1;
                        }
                \"
            "
    }

    # mock 6.7 + dnf5 5.4.2.1: mock's dynamic-BuildRequires loop
    # (backend.py rebuild_package -> installSrpmDeps -> pkg_manager.builddep)
    # runs dnf5 builddep on the generated .buildreqs.nosrc.rpm. When
    # %generate_buildrequires emits requirements the base buildroot ALREADY
    # satisfies, dnf5 fails the whole transaction with "Failed to resolve the
    # transaction: Package \"<nevra>\" is already installed." and mock raises
    # BuildError -- with nothing actually wrong with the package.
    #
    # Measured across all five parallel desktop runs: niri-00 (31231968581)
    # 70 occurrences over 11 distinct packages, kde-00 (31215339645) 31,
    # gnome-00 (31215535607) 25, xfce (31215533814) 4 -- and no other error
    # shape in three of the four (kde's zimg has a genuine stale patch). One
    # toolchain bug wearing 36 package costumes.
    #
    # Retry once with the loop disabled, gated on that exact signature.
    # Disabling it cannot deprive the build of anything in this case: the
    # error being matched is dnf5's own statement that the packages are
    # already present, so the loop has nothing left to install. Every other
    # failure keeps the original behavior and fails loud on the first try.
    if ! _run_mock_container ""; then
        if grep -qs "is already installed" "${builddir}/results/root.log"; then
            echo "==> [${pkg_name}] mock hit the dnf5 already-installed dynamic-BuildRequires bug; retrying with dynamic_buildrequires=False"
            _run_mock_container "--config-opts=dynamic_buildrequires=False"
        else
            return 1
        fi
    fi

    # Collect RPMs from results
    local rpm_count=0
    while IFS= read -r -d '' rpm; do
        cp "$rpm" "${LOCAL_REPO}/"
        echo "==> [${pkg_name}] -> $(basename "$rpm")"
        rpm_count=$(( rpm_count + 1 ))
    done < <(find "$resultdir" -name "*.rpm" ! -name "*.src.rpm" -print0)

    if [[ $rpm_count -eq 0 ]]; then
        echo "ERROR: No RPMs produced for ${pkg_name}" >&2
        return 1
    fi

    echo "==> [${pkg_name}] Built ${rpm_count} RPM(s)"
}

# --- Mock backend ---
build_package_mock() {
    local pkg_dir="$1"
    local spec_override="$2"

    local spec pkg_name abs_pkg_dir builddir
    spec="$(find_spec "$pkg_dir" "$spec_override")"
    pkg_name="$(basename "$spec" .spec)"
    abs_pkg_dir="${REPO_ROOT}/${pkg_dir}"

    if ! $FORCE && check_package_exists "$pkg_name" "$spec"; then
        return 0
    fi

    echo "==> [${pkg_name}] Building (mock) from ${pkg_dir}"

    builddir="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap "rm -rf '${builddir}'" RETURN

    prepare_sources "$builddir" "$spec" "$abs_pkg_dir"

    # Build SRPM inside the container (to ensure macros like %autorelease are available)
    local spec_basename
    spec_basename="$(basename "$spec")"
    echo "==> [${pkg_name}] Building SRPM..."
    podman run --rm \
        --pull=always \
        -v "${builddir}:/builddir:Z" \
        "${BUILD_IMAGE}" \
        rpmbuild -bs "/builddir/SPECS/${spec_basename}" \
            --define "_topdir /builddir" \
            --define "dist ${DIST}" \
            "${SRPM_BCOND_ARGS[@]}"

    local srpm
    srpm="$(find "${builddir}/SRPMS" -name "*.src.rpm" | head -1)"
    if [[ -z "$srpm" ]]; then
        echo "ERROR: No SRPM produced for ${pkg_name}" >&2
        return 1
    fi

    echo "==> [${pkg_name}] Rebuilding with mock (uniqueext=${pkg_name})..."
    local resultdir="${builddir}/results"
    mkdir -p "$resultdir"

    # Ensure the local repo metadata is up-to-date before mock starts,
    # locked to prevent parallel jobs from corrupting it.
    flock "${LOCAL_REPO}/repo.lock" -c "createrepo_c --update \"${LOCAL_REPO}\""

    local mock_check_flag="--nocheck"
    $WITH_CHECKS && mock_check_flag=""

    flock "${LOCAL_REPO}/repo.lock" -c "
        mock -r \"${MOCK_CONFIG}\" \\
            --uniqueext=\"${pkg_name}\" \\
            --rebuild \"$srpm\" \\
            --resultdir=\"$resultdir\" \\
            --define \"dist ${DIST}\" \\
            ${mock_check_flag} ${MOCK_BCOND_ARGS} \\
            --no-clean \\
            --no-cleanup-after || {
                echo 'ERROR: mock failed. Printing build.log:';
                cat \"$resultdir/build.log\" || true;
                echo 'ERROR: Printing root.log:';
                cat \"$resultdir/root.log\" || true;
                exit 1;
            }
        "

    local rpm_count=0
    while IFS= read -r -d '' rpm; do
        cp "$rpm" "${LOCAL_REPO}/"
        echo "==> [${pkg_name}] -> $(basename "$rpm")"
        rpm_count=$(( rpm_count + 1 ))
    done < <(find "$resultdir" -name "*.rpm" ! -name "*.src.rpm" -print0)

    if [[ $rpm_count -eq 0 ]]; then
        echo "ERROR: No RPMs produced for ${pkg_name}" >&2
        return 1
    fi

    echo "==> [${pkg_name}] Built ${rpm_count} RPM(s)"
}

# --- Native rpmbuild backend ---
#
# Runs directly in the current environment (no container). Intended for use
# inside a CentOS Stream 10 GitHub Actions container job where rpmbuild,
# spectool, and dnf are all available natively.
build_package_native() {
    local pkg_dir="$1"
    local spec_override="$2"

    local spec pkg_name abs_pkg_dir
    spec="$(find_spec "$pkg_dir" "$spec_override")"
    pkg_name="$(basename "$spec" .spec)"
    abs_pkg_dir="${REPO_ROOT}/${pkg_dir}"

    if ! $FORCE; then
        local nvr
        nvr=$(rpm -q --specfile "$spec" \
            --define "dist ${DIST}" \
            --queryformat "%{NAME}-%{VERSION}-%{RELEASE}\n" 2>/dev/null | head -1)
        if [[ -n "$nvr" ]] && ls "${LOCAL_REPO}/${nvr}"*.rpm &>/dev/null 2>&1; then
            log "[${pkg_name}] Skipping: ${nvr} already in local repo"
            return 0
        fi
    fi

    log "[${pkg_name}] Building (native rpmbuild) from ${pkg_dir}"

    local builddir
    builddir="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap "rm -rf '${builddir}'" RETURN

    mkdir -p "${builddir}"/{BUILD,BUILDROOT,RPMS,SOURCES,SRPMS,SPECS}

    local spec_basename
    spec_basename="$(basename "$spec")"
    cp "$spec" "${builddir}/SPECS/"

    # Copy local patches/sources
    find "$abs_pkg_dir" -maxdepth 1 -type f \
        ! -name "*.spec" \
        ! -name "sources" \
        ! -name "changelog" \
        ! -name "rpminspect.yaml" \
        ! -name "*.md" \
        -exec cp {} "${builddir}/SOURCES/" \;

    # Download remote sources (use $RPM_SOURCES_CACHE if set for cross-run caching)
    log "[${pkg_name}] Downloading sources..."
    local sources_cache="${RPM_SOURCES_CACHE:-}"
    if [[ -n "$sources_cache" ]]; then
        mkdir -p "$sources_cache"
        spectool -g -C "$sources_cache" "${builddir}/SPECS/${spec_basename}" || {
            err "spectool failed for ${pkg_name}"
            return 1
        }
        # Hard-link cached sources into builddir (fall back to copy)
        find "$sources_cache" -maxdepth 1 -type f \
            -exec ln -f {} "${builddir}/SOURCES/" \; 2>/dev/null \
            || cp "$sources_cache"/* "${builddir}/SOURCES/" 2>/dev/null || true
    else
        spectool -g -C "${builddir}/SOURCES/" "${builddir}/SPECS/${spec_basename}" || {
            err "spectool failed for ${pkg_name}"
            return 1
        }
    fi

    # Install BuildRequires from spec
    log "[${pkg_name}] Installing BuildRequires..."
    dnf builddep -y \
        --define "dist ${DIST}" \
        "${builddir}/SPECS/${spec_basename}" || {
        err "dnf builddep failed for ${pkg_name}"
        return 1
    }

    # Build binary RPMs
    log "[${pkg_name}] Running rpmbuild..."
    rpmbuild -bb \
        --define "_topdir ${builddir}" \
        --define "dist ${DIST}" \
        "${builddir}/SPECS/${spec_basename}" || {
        err "rpmbuild failed for ${pkg_name}"
        return 1
    }

    # Copy resulting RPMs to local repo
    local rpm_count=0
    while IFS= read -r -d '' rpm; do
        cp "$rpm" "${LOCAL_REPO}/"
        log "[${pkg_name}] -> $(basename "$rpm")"
        rpm_count=$(( rpm_count + 1 ))
    done < <(find "${builddir}/RPMS" -name "*.rpm" -print0)

    if [[ $rpm_count -eq 0 ]]; then
        err "No RPMs produced for ${pkg_name}"
        return 1
    fi

    log "[${pkg_name}] Built ${rpm_count} RPM(s)"
}

# Dispatch to the selected backend
build_package() {
    local pkg_dir="$1"
    local spec_override="$2"

    if $DRY_RUN; then
        local spec
        spec="$(find_spec "$pkg_dir" "$spec_override")"
        echo "==> [$(basename "$spec" .spec)] [dry-run] Would build: ${spec}"
        return 0
    fi

    case "$BACKEND" in
        podman) build_package_podman  "$pkg_dir" "$spec_override" ;;
        mock)   build_package_mock    "$pkg_dir" "$spec_override" ;;
        native) build_package_native  "$pkg_dir" "$spec_override" ;;
        *)
            err "Unknown backend '${BACKEND}' — use 'podman', 'mock', or 'native'"
            return 1
            ;;
    esac
}

# How many RPMs the local repo holds. Used to decide whether a tier's failures
# are worth retrying: a retry can only help if something new landed.
_repo_rpm_count() {
    find "$LOCAL_REPO" -maxdepth 1 -name '*.rpm' 2>/dev/null | wc -l
}

# Run all packages in a tier with up to $JOBS parallel workers.
build_tier() {
    local tier_name="$1"
    local -n _tier_pkg_total="$2"
    local -n _tier_failed="$3"

    local logdir
    logdir="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap "rm -rf '${logdir}'" RETURN

    local pids=()
    local pkg_paths=()
    local active=0
    local _tier_start_rpms
    _tier_start_rpms="$(_repo_rpm_count)"

    wait_one() {
        for i in "${!pids[@]}"; do
            local pid="${pids[$i]}"
            local path="${pkg_paths[$i]}"
            if ! kill -0 "$pid" 2>/dev/null; then
                local logfile
                logfile="${logdir}/$(basename "$path").log"
                cat "$logfile"
                if wait "$pid"; then
                    : # success
                else
                    err "Failed: ${path}"
                    _tier_failed+=("${path}")
                fi
                unset 'pids[$i]' 'pkg_paths[$i]'
                active=$(( active - 1 ))
                return
            fi
        done
        sleep 0.5
        wait_one
    }

    while IFS=$'\t' read -r pkg_path spec_override; do
        if [[ -n "$FILTER_PACKAGE" && "$pkg_path" != "$FILTER_PACKAGE" ]]; then
            continue
        fi

        _tier_pkg_total=$(( _tier_pkg_total + 1 ))

        if $DRY_RUN; then
            build_package "$pkg_path" "$spec_override"
            continue
        fi

        while [[ $active -ge $JOBS ]]; do
            wait_one
        done

        local logfile
        logfile="${logdir}/$(basename "$pkg_path").log"
        build_package "$pkg_path" "$spec_override" > "$logfile" 2>&1 &
        pids+=($!)
        pkg_paths+=("$pkg_path")
        active=$(( active + 1 ))
        log "  Queued ${pkg_path} (pid $!)"

    done < <(python3 "${SCRIPT_DIR}/parse-build-order.py" "$MANIFEST" --tier "$tier_name")

    while [[ $active -gt 0 ]]; do
        wait_one
    done

    # Retry this tier's failures once, if the tier produced anything.
    #
    # Tiers are a topological order over BuildRequires, but the ordering is not
    # perfect: measured on the regenerated manifest, 43 one-way BuildRequires
    # edges fall INSIDE a tier -- 27 in cosmic-10, 9 in cosmic-00, 5 in
    # gnome-04, 2 in niri-15. Packages within a tier build concurrently, so
    # those start before the thing they need exists. They are real edges, not
    # artifacts: libepoxy BuildRequires mutter, libdecor BuildRequires gtk3 and
    # libsoup3 BuildRequires glib-networking, and each pair shares gnome-04.
    #
    # A second pass fixes exactly that class by construction -- mutter is in
    # the local repo by the time libepoxy is retried -- without needing to know
    # why tier_sources mis-assigned them. Two hypotheses for that have already
    # been proposed and disproved; this does not depend on the answer.
    #
    # Gated on the repo having GROWN during the tier. If nothing built, nothing
    # a retry could need has appeared, so retrying is just a second identical
    # failure at twice the cost. That gate is what keeps this from being a
    # blanket "try everything twice".
    if ((${#_tier_failed[@]})) && [[ "$(_repo_rpm_count)" -gt "$_tier_start_rpms" ]]; then
        local retry=("${_tier_failed[@]}")
        _tier_failed=()
        log "  ${#retry[@]} package(s) failed but the repo grew during this tier;"
        log "  retrying them once in case they lost an intra-tier race"
        local path
        for path in "${retry[@]}"; do
            if build_package "$path" ""; then
                log "  [retry] ${path} built on the second pass"
            else
                err "Failed: ${path}"
                _tier_failed+=("${path}")
            fi
        done
    fi
}

# --- Main ---
main() {
    log "Build chain starting"
    log "  Manifest:   ${MANIFEST}"
    log "  Backend:    ${BACKEND}"
    [[ "$BACKEND" == "podman" ]] && log "  Image:      ${BUILD_IMAGE}"
    # The podman backend runs mock too, so its config matters either way.
    log "  Mock cfg:   ${MOCK_CONFIG}"
    log "  Dist tag:   ${DIST}"
    log "  Local repo: ${LOCAL_REPO}"
    log "  Jobs:       ${JOBS}"
    [[ -n "$FILTER_TIER" ]]    && log "  Tier filter: ${FILTER_TIER}"
    [[ -n "$FILTER_PACKAGE" ]] && log "  Pkg filter:  ${FILTER_PACKAGE}"
    $WITH_CHECKS && log "  RPM %check: enabled"

    if ! $DRY_RUN; then
        case "$BACKEND" in
            podman) command -v podman   &>/dev/null || { err "podman not found";   exit 1; } ;;
            mock)   command -v mock     &>/dev/null || { err "mock not found";     exit 1; } ;;
            native) command -v rpmbuild &>/dev/null || { err "rpmbuild not found"; exit 1; } ;;
        esac
    fi

    ensure_local_repo

    local tiers
    tiers="$(python3 "${SCRIPT_DIR}/parse-build-order.py" "$MANIFEST" --tiers)"

    local tier_count=0
    local pkg_total=0
    local failed=()

    while IFS= read -r tier_name; do
        if [[ -n "$FILTER_TIER" && "$tier_name" != "$FILTER_TIER" ]]; then
            continue
        fi

        tier_count=$(( tier_count + 1 ))
        log ""
        log "===== Tier: ${tier_name} (backend=${BACKEND}, jobs=${JOBS}) ====="

        build_tier "$tier_name" pkg_total failed

        if ! $DRY_RUN; then
            update_local_repo
        fi

    done <<< "$tiers"

    log ""
    log "===== Summary ====="
    log "Tiers processed: ${tier_count}"
    log "Packages built:  ${pkg_total}"

    if [[ ${#failed[@]} -gt 0 ]]; then
        err "Failed packages (${#failed[@]}):"
        for f in "${failed[@]}"; do
            err "  - ${f}"
        done
        exit 1
    fi

    log "All packages built successfully!"
}

main
