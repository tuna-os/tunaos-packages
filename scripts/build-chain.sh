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
    #
    # A bare tail is not enough. root.log ends with dnf's transaction summary,
    # and the line that explains a buildroot failure -- the "Problem:" chain,
    # or "nothing provides X needed by Y" -- sits hundreds of lines above it.
    # Retrieving those hundreds of lines from CI meant downloading the whole
    # run's log archive, which for one Hummingbird run was a multi-megabyte
    # zip that failed to transfer twice. So grep the reasons out first and
    # print them before the tail, where the tail is all anyone gets.
    #
    # WHY_FAILED_PATTERN is a local, not a file-scope global: the tests for
    # this handler run its body under the script's own `set -u`, and a global
    # declared further down the file reads as unbound there.
    local log why WHY_FAILED_PATTERN
    WHY_FAILED_PATTERN='nothing provides|but none of the providers|conflicting requests|Problem: |Failed to resolve|No match for argument|Unable to find a match|is already installed|hunk FAILED|No such file or directory|Bad exit status from|error:'
    for log in "${builddir:-/nonexistent}/results/build.log" \
               "${builddir:-/nonexistent}/results/root.log"; do
        if [[ -r "$log" ]]; then
            why="$(grep -E "$WHY_FAILED_PATTERN" "$log" | tail -n 20)" || true
            if [[ -n "$why" ]]; then
                echo "--- why ${log} says it failed ---" >&2
                printf '%s\n' "$why" >&2
            fi
            echo "--- tail of ${log} ---" >&2
            tail -n 40 "$log" >&2 || true
            # KEEP the log, do not only print it. A 40-line tail in a job log
            # is not a diagnosis: on a long chain the run prints one of these
            # per failed package, hours apart, and the API only serves the
            # tail of a multi-hundred-kilobyte log -- so the 25 failures of
            # the GNOME 51.beta bump were unreadable after the fact, exactly
            # the archaeology BUILDROOT_MANIFESTS was added to end (#480).
            # Copied beside the buildroot manifests, inside artifacts/, so
            # the cell's artifact carries every failure's full build.log and
            # root.log. Never fatal: a log that cannot be copied must not
            # change how the build failed.
            if [[ -n "${FAILURE_LOGS:-}" ]]; then
                mkdir -p "$FAILURE_LOGS" 2>/dev/null \
                    && cp "$log" \
                       "${FAILURE_LOGS}/${pkg_name:-chain}.$(basename "$log")" \
                    || echo "--- ${log} not kept (non-fatal) ---" >&2
            fi
        fi
    done
    echo "=============================================================" >&2
    exit "$rc"
}
trap _on_error ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# The local repository and spec/source staging services, and the backend
# implementations that use them, are sourced as modules so they can be
# reviewed, tested, and shellchecked independently from orchestration.
#
# ("shellchecked" must not start this comment line: shellcheck parses any
# comment beginning with "shellcheck" as a directive and fails with SC1073.)
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/build-chain/repo.sh
source "${SCRIPT_DIR}/lib/build-chain/repo.sh"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/build-chain/sources.sh
source "${SCRIPT_DIR}/lib/build-chain/sources.sh"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/build-chain/podman.sh
source "${SCRIPT_DIR}/lib/build-chain/podman.sh"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/build-chain/mock.sh
source "${SCRIPT_DIR}/lib/build-chain/mock.sh"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/build-chain/native.sh
source "${SCRIPT_DIR}/lib/build-chain/native.sh"

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
# UTAH_REPO_DIR: host directory holding the materialised utah-packages repo
# (scripts/materialize-consumed-repo.py --out), bind-mounted at
# /run/utah-repo for the [utah] repo in mock/hummingbird-ci*.cfg. Required
# whenever MOCK_CONFIG is a hummingbird config -- a hummingbird build
# without utah in the root silently resolves utah-shipped BuildRequires
# from Fedora instead, which is the rebuild-everything behaviour this
# exists to end. The chain-band workflow sets it from its materialise
# step; local runs must materialise first (see the error below).
JOBS=$(( $(nproc) / 2 ))
[[ $JOBS -lt 1 ]] && JOBS=1
FILTER_TIER=""
FILTER_PACKAGE=""
FILTER_TIERS=""
# --packages-file: build ONLY the paths listed (one src/... path per line,
# blank lines and #-comments ignored). The shard runner in
# build-chain-fanout.yml is the intended caller: packages within a tier are
# dependency-independent, so disjoint shards of one tier can build on
# separate runners against the same inputs. Unset = no behavior change.
FILTER_PACKAGES_FILE=""
declare -A FILTER_PACKAGES_SET=()
# --served-nvrs: NVRs the published index already serves (one per line).
# check_package_exists skips a package whose computed NVR is listed, exactly
# as it skips one whose RPM sits in the local repo. Without this, "already
# built" was ONLY the local repo -- seeded from action-key-matched partials
# -- so any key move (a mock cfg edit rebuilds the image, the image digest
# is a key input) sent the next leg back to tier 0 to rebuild packages the
# repo has SERVED for days. Legs 32914264044/32991216265/33022688689 each
# rebuilt the same ~85 packages for exactly this reason (#544). The served
# index is the durable record of what exists; keys only guard partials.
SERVED_NVRS_FILE=""
declare -A SERVED_NVRS_SET=()
DRY_RUN=false
FORCE=false
WITH_CHECKS=false
STREAM=false
PRINT_DIST=false

# --- Soft deadline -----------------------------------------------------------
# CHAIN_BUDGET_SECONDS, when set, is a clean stopping point INSIDE the job's
# hard ceiling. The nightly hummingbird-desktops cells die at
# `timeout-minutes: 360` -- reported as CANCELLED -- with every step after the
# build skipped, so six hours of mock output reaches only the resume partial
# and the SERVED repo never gains a package (every scheduled run 08-19..08-24,
# e.g. job 97441135486: killed at 5h59m29s still inside the python bootstrap
# tiers). A chain that stops itself BEFORE the ceiling finishes its current
# packages, drains, and exits normally -- so validation, checksums, SBOM,
# attestation and the publish artifact all run on what DID build.
#
# Deferring is not failing: remaining packages are counted and named in the
# summary, and the caller learns about it through CHAIN_DEFERRED_MARKER (a
# file written on deadline, carrying the count) -- which the workflow uses to
# keep a partial OUT of the action cache. Recording a deferred chain as a
# completed ActionResult would make every later run cache-hit on the partial
# and freeze the chain at it forever.
CHAIN_START_EPOCH=$(date +%s)
DEADLINE_HIT=false
DEFERRED_COUNT=0
_past_deadline() {
    [[ -n "${CHAIN_BUDGET_SECONDS:-}" ]] || return 1
    (( $(date +%s) - CHAIN_START_EPOCH >= CHAIN_BUDGET_SECONDS ))
}

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
    echo "  --tiers <list>       Comma-separated tiers to build (used with --stream)"
    echo "  --package <path>     Only build a specific package path"
    echo "  --stream             Stream all tiers as one wavefront (no tier barriers)"
    echo "  --with-checks        Run the RPM %check section"
    echo "  --dry-run            Print what would be built without building"
    echo "  --force              Force rebuild even if package exists in repo"
    echo "  --print-dist         Print the manifest's %{dist} and exit"
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
        --tiers)       FILTER_TIERS="$2"; shift 2 ;;
        --package)     FILTER_PACKAGE="$2"; shift 2 ;;
        --packages-file) FILTER_PACKAGES_FILE="$2"; shift 2 ;;
        --served-nvrs)   SERVED_NVRS_FILE="$2"; shift 2 ;;
        --with-checks)  WITH_CHECKS=true; shift ;;
        --dry-run)     DRY_RUN=true;     shift ;;
        --force)
            # Consumed by sourced backend modules.
            # shellcheck disable=SC2034
            FORCE=true
            shift
            ;;
        --stream)      STREAM=true;      shift ;;
        --print-dist)  PRINT_DIST=true;  shift ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

if [[ -n "$FILTER_PACKAGES_FILE" ]]; then
    if [[ ! -f "$FILTER_PACKAGES_FILE" ]]; then
        echo "ERROR: --packages-file '$FILTER_PACKAGES_FILE' does not exist" >&2
        exit 1
    fi
    while IFS= read -r _line; do
        _line="${_line%%#*}"
        _line="${_line//[$'\t\r ']/}"
        [[ -n "$_line" ]] && FILTER_PACKAGES_SET["$_line"]=1
    done < "$FILTER_PACKAGES_FILE"
    if [[ ${#FILTER_PACKAGES_SET[@]} -eq 0 ]]; then
        echo "ERROR: --packages-file '$FILTER_PACKAGES_FILE' lists no packages" >&2
        exit 1
    fi
fi

# Consumed by the sourced repo, sources and podman modules.
# shellcheck disable=SC2034
ALMA_CANDIDATE=false
# shellcheck disable=SC2034
ALMA_CANDIDATE_STATE=""
# shellcheck disable=SC2034
ALMA_KEY_MOUNT_ARGS=()
if [[ "$MOCK_CONFIG" == alma10-* ]]; then
    # shellcheck disable=SC2034
    ALMA_CANDIDATE=true
    if [[ "$BACKEND" != podman ]]; then
        echo 'ERROR: signed Alma bootstrap currently requires the podman mock backend' >&2
        exit 1
    fi
    # Neither restored roots nor an NVR from another publication proves this
    # target's baseline. Initial supply is built from signed native inputs.
    SERVED_NVRS_FILE=""
    # Alma candidate mode must not inherit a host mock cache.
    # shellcheck disable=SC2034
    MOCK_CACHE_DIR=""
    export BUILDROOT_MANIFESTS="${BUILDROOT_MANIFESTS:-${LOCAL_REPO}/buildroots}"
fi

if [[ -n "$SERVED_NVRS_FILE" ]]; then
    if [[ ! -f "$SERVED_NVRS_FILE" ]]; then
        echo "ERROR: --served-nvrs '$SERVED_NVRS_FILE' does not exist" >&2
        exit 1
    fi
    while IFS= read -r _line; do
        _line="${_line%%#*}"
        _line="${_line//[$'\t\r ']/}"
        # Consumed by check_package_exists in the sourced sources module.
        # shellcheck disable=SC2034
        [[ -n "$_line" ]] && SERVED_NVRS_SET["$_line"]=1
    done < "$SERVED_NVRS_FILE"
    # An empty list is legal: a first publish has nothing served yet.
fi

# The hummingbird mock configs carry a [utah] file:// repo; without the
# bind-mounted tree dnf fails the whole builddep on a missing baseurl.
# Fail here, naming the missing step, rather than inside mock's root.log.
UTAH_MOUNT_ARGS=()
if [[ "$MOCK_CONFIG" == hummingbird-* ]]; then
    if [[ -z "${UTAH_REPO_DIR:-}" ]]; then
        echo "ERROR: MOCK_CONFIG=$MOCK_CONFIG needs UTAH_REPO_DIR: run" >&2
        echo "  python3 scripts/materialize-consumed-repo.py --ref <consumed_indexes pin>" >&2
        echo "    --arch <x86_64|aarch64> --out <dir> [--allow-empty]" >&2
        echo "and export UTAH_REPO_DIR=<dir> first." >&2
        exit 1
    fi
    if [[ ! -f "${UTAH_REPO_DIR}/repository/repodata/repomd.xml" ]]; then
        echo "ERROR: UTAH_REPO_DIR=$UTAH_REPO_DIR has no repository/repodata/repomd.xml" >&2
        exit 1
    fi
    # Passed to the sourced podman backend.
    # shellcheck disable=SC2034
    UTAH_MOUNT_ARGS=(-v "${UTAH_REPO_DIR}:/run/utah-repo:Z")
elif [[ -n "${UTAH_REPO_DIR:-}" ]]; then
    # Passed to the sourced podman backend.
    # shellcheck disable=SC2034
    UTAH_MOUNT_ARGS=(-v "${UTAH_REPO_DIR}:/run/utah-repo:Z")
fi

# True when filters say this package is NOT ours to build. Deferral/skip
# accounting deliberately does not count these: another shard owns them.
_package_filtered_out() {
    local pkg_path="$1"
    [[ -n "$FILTER_PACKAGE" && "$pkg_path" != "$FILTER_PACKAGE" ]] && return 0
    [[ ${#FILTER_PACKAGES_SET[@]} -gt 0 && -z "${FILTER_PACKAGES_SET[$pkg_path]:-}" ]] && return 0
    return 1
}

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

# --- Structured per-package result recording ---
#
# Every outcome this script knows about -- which package, which tier, how
# long it ran, pass or fail -- already exists transiently inside wait_one:
# started_at[i] and `now` compute elapsed, and the wait/err branch knows
# pass or fail. All of that was previously only ever echoed as prose
# (`log`/`err`), which is exactly what the _on_error narrative above
# documents as unreadable after the fact once a log is hundreds of MB and
# every retrieval path only serves the tail.
#
# BUILD_RESULTS_FILE, when set by the caller, gets one JSON object per
# line per package outcome (JSONL, so a crash mid-run still leaves prior
# lines parseable). Unset = no behavior change, same convention as
# CHAIN_DEFERRED_MARKER and FAILURE_LOGS above. Fields are fixed and
# bounded (package path, tier, backend, outcome, duration, retry flag,
# timestamp) -- no free-form text, so this cannot grow unbounded labels
# the way a raw log tail already can.
_record_result() {
    local pkg_path="$1" tier_name="$2" outcome="$3" duration_s="$4" is_retry="$5"
    [[ -n "${BUILD_RESULTS_FILE:-}" ]] || return 0
    python3 - "$pkg_path" "$tier_name" "$outcome" "$duration_s" "$is_retry" "$BACKEND" \
        >> "${BUILD_RESULTS_FILE}" 2>/dev/null <<'PYEOF' || true
import json, sys, datetime
pkg_path, tier_name, outcome, duration_s, is_retry, backend = sys.argv[1:7]
print(json.dumps({
    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "package": pkg_path,
    "tier": tier_name,
    "backend": backend,
    "outcome": outcome,
    "duration_s": int(duration_s),
    "retry": is_retry == "true",
}))
PYEOF
}

# Used by update_local_repo()'s retry path but never defined, so a
# createrepo_c hiccup became "warn: command not found" (exit 127) and buried
# whatever actually went wrong.
warn() { echo "WARNING: $*" >&2; }

# Derive the %{dist} tag from the manifest's `target:` when --dist was not
# given. Keeps a manifest self-describing: build-order-xfce.yml says
# centos-stream-10-x86_64 and gets .el10, build-order-xfce-fedora.yml says
# fedora-44-x86_64 and gets .fc44 — no caller has to keep the two in sync.
derive_dist() {
    if [[ "${MOCK_CONFIG:-}" == fedora-eln* ]]; then
        echo ".eln159"
        return 0
    fi
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
        # 2026-08-06. .bfin1 here is deliberately NOT that tag: it marks a
        # TunaOS/Bluefin rebuild, and because "bfin1" sorts below "hum1" for
        # the same version ('b' < 'h'), a rebuild can never shadow a package
        # Hummingbird later starts shipping itself. (Was .fc43 through
        # 2026-08-29 -- changed because it read as a real Fedora 43 tie-in,
        # which it never was; any alpha prefix sorting before "hum" is
        # equally safe, this just says what the rebuild actually is.)
        #
        # It is not a claim about ABI. The buildroot ABI comes from
        # mock/hummingbird-ci.cfg: Fedora 44 plus Hummingbird's own repository
        # at higher priority, the composition Hummingbird itself builds in.
        # (It tracked Rawhide until 2026-09-02, on the strength of glib2 /
        # systemd / gcc matching Rawhide's versions; glibc, openssl, python
        # and perl -- the ABI -- are Fedora 44's, and three served packages
        # carried Rawhide's GLIBC_2.44 as a result. docs/HUMMINGBIRD-TARGET.md.)
        hummingbird-20251124*)   echo ".bfin1" ;;
        # Fedora ELN does not put the EL major (11) in %{dist}: its
        # fedora-eln-release macros.dist sets %eln to a counter and
        # %distcore to .eln%{eln}. Verified 2026-10-02 against
        # quay.io/fedora/eln:latest and registry.fedoraproject.org/eln-bootc
        # (fedora-eln-release-11.0-0.20.eln159): `rpm --eval %dist` = .eln159.
        # The old .eln11 sorted below ELN's own build of the same version
        # ("eln11" < "eln159"), so ELN's package always won. %eln moves
        # forward over time; re-check with the command above and bump this
        # when ELN does.
        fedora-eln*|eln*)        echo ".eln159" ;;
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
# The publisher scopes what it pulls from the shared ORAS staging repo to
# this %{dist} (publish-build-chain-rpms.yml), from this one derivation.
if $PRINT_DIST; then
    echo "$DIST"
    exit 0
fi

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
    # Worker output stays in a per-package file until the package exits, so
    # parallel builds do not interleave. That made a single long build look
    # indistinguishable from a hung one in Actions: after "Queued", nothing at
    # all reached the live log until mock and rpmbuild both finished. Keep the
    # clean final log, but let the parent scheduler prove every live worker is
    # still alive once a minute.
    local started_at=()
    local last_heartbeat=()
    local heartbeat_interval=60
    local active=0
    local _tier_start_rpms
    _tier_start_rpms="$(_repo_rpm_count)"

    wait_one() {
        # This used to recurse every 0.5 seconds while all workers were alive.
        # A TeX package can run for hours, so use a loop: progress reporting
        # must not grow the shell call stack just because a build is slow.
        while true; do
            local now
            now=$(date +%s)
            for i in "${!pids[@]}"; do
                local pid="${pids[$i]}"
                local path="${pkg_paths[$i]}"
                if ! kill -0 "$pid" 2>/dev/null; then
                    local logfile
                    logfile="${logdir}/$(basename "$path").log"
                    cat "$logfile"
                    local _dur=$(( now - started_at[i] ))
                    if wait "$pid"; then
                        _record_result "${path}" "${tier_name}" "success" "${_dur}" "false"
                    else
                        err "Failed: ${path}"
                        _record_result "${path}" "${tier_name}" "failure" "${_dur}" "false"
                        _tier_failed+=("${path}")
                    fi
                    unset 'pids[$i]' 'pkg_paths[$i]' 'started_at[$i]' 'last_heartbeat[$i]'
                    active=$(( active - 1 ))
                    return
                fi

                if (( now - last_heartbeat[i] >= heartbeat_interval )); then
                    local elapsed=$(( now - started_at[i] ))
                    log "  Still building ${path} (pid ${pid}, elapsed ${elapsed}s)"
                    last_heartbeat[i]=$now
                fi
            done
            sleep 0.5
        done
    }

    while IFS=$'\t' read -r pkg_path spec_override; do
        if _package_filtered_out "$pkg_path"; then
            continue
        fi

        # Past the soft deadline: stop DISPATCHING, keep draining. Checked at
        # the package boundary so a package in flight always completes; only
        # work that has not started is deferred.
        if $DEADLINE_HIT || _past_deadline; then
            if ! $DEADLINE_HIT; then
                DEADLINE_HIT=true
                log "  CHAIN_BUDGET_SECONDS=${CHAIN_BUDGET_SECONDS:-} reached; deferring the rest of the chain"
            fi
            DEFERRED_COUNT=$(( DEFERRED_COUNT + 1 ))
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
        local pid=$!
        local now
        now=$(date +%s)
        pids+=("$pid")
        pkg_paths+=("$pkg_path")
        started_at+=("$now")
        last_heartbeat+=("$now")
        active=$(( active + 1 ))
        log "  Queued ${pkg_path} (pid ${pid})"

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
    if ((${#_tier_failed[@]})) && ! $DEADLINE_HIT && [[ "$(_repo_rpm_count)" -gt "$_tier_start_rpms" ]]; then
        local retry=("${_tier_failed[@]}")
        _tier_failed=()
        log "  ${#retry[@]} package(s) failed but the repo grew during this tier;"
        log "  retrying them once in case they lost an intra-tier race"
        local path _retry_start
        for path in "${retry[@]}"; do
            _retry_start=$(date +%s)
            if build_package "$path" ""; then
                log "  [retry] ${path} built on the second pass"
                _record_result "${path}" "${tier_name}" "success" "$(( $(date +%s) - _retry_start ))" "true"
            else
                err "Failed: ${path}"
                _record_result "${path}" "${tier_name}" "failure" "$(( $(date +%s) - _retry_start ))" "true"
                _tier_failed+=("${path}")
            fi
        done
    fi
}

# --- Stream (wavefront) scheduler ---
#
# Builds every package from every tier as one continuous wavefront.
# Packages are dispatched in manifest order, which is a topological order
# over BuildRequires, so most dependencies are already in the local repo
# by the time a consumer starts.  The retry path below catches the few
# that race — the same logic that already handles intra-tier edges.
build_stream() {
    local -n _pkg_total="$1"
    local -n _failed="$2"

    local logdir
    logdir="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap "rm -rf '${logdir}'" RETURN

    local pids=()
    local pkg_paths=()
    local started_at=()
    local last_heartbeat=()
    local heartbeat_interval=60
    local active=0
    local _stream_start_rpms
    _stream_start_rpms="$(_repo_rpm_count)"

    wait_one() {
        while true; do
            local now
            now=$(date +%s)
            for i in "${!pids[@]}"; do
                local pid="${pids[$i]}"
                local path="${pkg_paths[$i]}"
                if ! kill -0 "$pid" 2>/dev/null; then
                    local logfile
                    logfile="${logdir}/$(basename "$path").log"
                    cat "$logfile"
                    local _dur=$(( now - started_at[i] ))
                    if wait "$pid"; then
                        _record_result "${path}" "stream" "success" "${_dur}" "false"
                    else
                        err "Failed: ${path}"
                        _record_result "${path}" "stream" "failure" "${_dur}" "false"
                        _failed+=("${path}")
                    fi
                    unset 'pids[$i]' 'pkg_paths[$i]' 'started_at[$i]' 'last_heartbeat[$i]'
                    active=$(( active - 1 ))
                    # The repo grew; anything still waiting may now have its
                    # dependencies satisfied.  Updating metadata here lets the
                    # next dispatch see freshly built RPMs immediately.
                    if ! $DRY_RUN; then
                        update_local_repo
                    fi
                    return
                fi

                if (( now - last_heartbeat[i] >= heartbeat_interval )); then
                    local elapsed=$(( now - started_at[i] ))
                    log "  Still building ${path} (pid ${pid}, elapsed ${elapsed}s)"
                    last_heartbeat[i]=$now
                fi
            done
            sleep 0.5
        done
    }

    while IFS=$'\t' read -r pkg_path spec_override; do
        if _package_filtered_out "$pkg_path"; then
            continue
        fi

        # Same soft deadline as build_tier: stop dispatching, keep draining.
        if $DEADLINE_HIT || _past_deadline; then
            if ! $DEADLINE_HIT; then
                DEADLINE_HIT=true
                log "  CHAIN_BUDGET_SECONDS=${CHAIN_BUDGET_SECONDS:-} reached; deferring the rest of the stream"
            fi
            DEFERRED_COUNT=$(( DEFERRED_COUNT + 1 ))
            continue
        fi

        _pkg_total=$(( _pkg_total + 1 ))

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
        local pid=$!
        local now
        now=$(date +%s)
        pids+=("$pid")
        pkg_paths+=("$pkg_path")
        started_at+=("$now")
        last_heartbeat+=("$now")
        active=$(( active + 1 ))
        log "  Queued ${pkg_path} (pid ${pid})"

    done < <(python3 "${SCRIPT_DIR}/parse-build-order.py" "$MANIFEST" --all ${FILTER_TIERS:+--tiers-filter "${FILTER_TIERS}"})

    while [[ $active -gt 0 ]]; do
        wait_one
    done

    # Retry failures once, if anything was built during this stream.
    # Same gate as build_tier: if the repo grew, a failed package may
    # have been missing a dependency that has since landed.
    if ((${#_failed[@]})) && ! $DEADLINE_HIT && [[ "$(_repo_rpm_count)" -gt "$_stream_start_rpms" ]]; then
        local retry=("${_failed[@]}")
        _failed=()
        log "  ${#retry[@]} package(s) failed but the repo grew during the stream;"
        log "  retrying them once in case they lost a dependency race"
        local path _retry_start
        for path in "${retry[@]}"; do
            _retry_start=$(date +%s)
            if build_package "$path" ""; then
                log "  [retry] ${path} built on the second pass"
                _record_result "${path}" "stream" "success" "$(( $(date +%s) - _retry_start ))" "true"
            else
                err "Failed: ${path}"
                _record_result "${path}" "stream" "failure" "$(( $(date +%s) - _retry_start ))" "true"
                _failed+=("${path}")
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
    [[ -n "$FILTER_TIERS" ]]   && log "  Tiers:       ${FILTER_TIERS}"
    [[ -n "$FILTER_PACKAGE" ]] && log "  Pkg filter:  ${FILTER_PACKAGE}"
    $WITH_CHECKS && log "  RPM %check: enabled"
    $STREAM && log "  Stream mode: no tier barriers"

    if ! $DRY_RUN; then
        case "$BACKEND" in
            podman) command -v podman   &>/dev/null || { err "podman not found";   exit 1; } ;;
            mock)   command -v mock     &>/dev/null || { err "mock not found";     exit 1; } ;;
            native) command -v rpmbuild &>/dev/null || { err "rpmbuild not found"; exit 1; } ;;
        esac
    fi

    ensure_local_repo

    local tier_count=0
    local pkg_total=0
    local failed=()

    if $STREAM && [[ -z "$FILTER_TIER" ]]; then
        # Stream mode: dispatch all packages across all tiers as one wavefront.
        # The manifest is topologically ordered, so most dependencies are
        # satisfied by the time a package starts.  The retry logic below
        # catches the few that race (same intra-tier retry that already
        # handles BuildRequires edges that fall inside a single tier).
        log ""
        log "===== Stream (all tiers, backend=${BACKEND}, jobs=${JOBS}) ====="
        build_stream pkg_total failed
        tier_count=1
    else
        local tiers
        tiers="$(python3 "${SCRIPT_DIR}/parse-build-order.py" "$MANIFEST" --tiers)"

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
    fi

    log ""
    log "===== Summary ====="
    log "Tiers processed: ${tier_count}"
    log "Packages built:  ${pkg_total}"
    if $DEADLINE_HIT; then
        log "Deferred (deadline): ${DEFERRED_COUNT}"
        # The marker is how the CALLER learns this run is partial. It must be
        # written before the failure exit below: a deferred run with failures
        # is still partial, and a consumer that only checks the exit code
        # would otherwise record it as a complete red rather than a truncated
        # one.
        if [[ -n "${CHAIN_DEFERRED_MARKER:-}" ]]; then
            printf 'deferred=%s\nbudget_seconds=%s\n' \
                "${DEFERRED_COUNT}" "${CHAIN_BUDGET_SECONDS:-}" \
                > "${CHAIN_DEFERRED_MARKER}"
        fi
    fi

    if [[ ${#failed[@]} -gt 0 ]]; then
        err "Failed packages (${#failed[@]}):"
        for f in "${failed[@]}"; do
            err "  - ${f}"
        done
        exit 1
    fi

    if $DEADLINE_HIT; then
        # NOT "all packages built". Exit 0 on purpose: the point of stopping
        # cleanly is that validation, checksums, SBOM, attestation and the
        # publish artifact all run on what DID build. The marker above is what
        # keeps this partial out of the action cache.
        log "Chain stopped at the soft deadline with ${DEFERRED_COUNT} package(s) deferred; partial output is complete and valid as far as it goes."
        return 0
    fi

    log "All packages built successfully!"
}

main
