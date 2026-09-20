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

# Backend implementations are sourced as modules so they can be reviewed,
# tested, and shellchecked independently from orchestration.
#
# ("shellchecked" must not start this comment line: shellcheck parses any
# comment beginning with "shellcheck" as a directive and fails with SC1073.)
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/build-chain/native.sh
source "${SCRIPT_DIR}/lib/build-chain/native.sh"
# shellcheck source=lib/build-chain/mock.sh
source "${SCRIPT_DIR}/lib/build-chain/mock.sh"

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
        --force)       FORCE=true;       shift ;;
        --stream)      STREAM=true;      shift ;;
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

if [[ -n "$SERVED_NVRS_FILE" ]]; then
    if [[ ! -f "$SERVED_NVRS_FILE" ]]; then
        echo "ERROR: --served-nvrs '$SERVED_NVRS_FILE' does not exist" >&2
        exit 1
    fi
    while IFS= read -r _line; do
        _line="${_line%%#*}"
        _line="${_line//[$'\t\r ']/}"
        [[ -n "$_line" ]] && SERVED_NVRS_SET["$_line"]=1
    done < "$SERVED_NVRS_FILE"
    # An empty list is legal: a first publish has nothing served yet.
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

# Opt-in: record what the buildroot resolved for each package, so a red run
# can be diffed against a green one (scripts/diff-buildroots.py) instead of
# reconstructed from issue comments — #480's libnotify hunt took a day of
# archaeology for an answer mock had already written into root.log.
# Enabled by exporting BUILDROOT_MANIFESTS=<dir>; never fatal: a manifest
# that cannot be written must not fail a build that succeeded.
record_buildroot_manifest() {
    local resultdir="$1" pkg_name="$2"
    [[ -n "${BUILDROOT_MANIFESTS:-}" ]] || return 0
    python3 "${REPO_ROOT}/scripts/extract-buildroot-manifest.py" "$resultdir" \
        --output "${BUILDROOT_MANIFESTS}/${pkg_name}.buildroot.txt" \
        || echo "==> [${pkg_name}] buildroot manifest not recorded (non-fatal)"
}

update_local_repo() {
    log "Updating local repo metadata"
    # createrepo_c stages into `.repodata/` and renames it to `repodata/` when
    # it finishes, and it REFUSES to start if that temp directory is already
    # there:
    #
    #     Temporary repodata directory .../.repodata/ already exists!
    #     (Another createrepo process is running?)
    #
    # Nothing else is running -- update_local_repo is called only from
    # wait_one(), a nested function the dispatch loop calls synchronously, so
    # there is never a second createrepo_c in this process. The directory is
    # the debris of an INTERRUPTED one: a cell that hit its deadline mid-index,
    # a cancelled shard, an OOM. It then poisons the workspace permanently,
    # because the retry below used to `rm -rf repodata` -- the FINISHED
    # directory -- and leave the temp one that is actually doing the blocking,
    # so the fallback re-ran into the identical error and the chain died at
    # `createrepo_c "${LOCAL_REPO}"`.
    #
    # Fanout run 33134251127 lost 14 of 30 shards to this, 7 of 8 in band1-x86.
    # The served-NVR skip is what exposed it: skips return in milliseconds, so
    # a shard now reaches its first metadata update almost immediately, and
    # every one of those shards died having built nothing (`new RPMs: 0`).
    rm -rf "${LOCAL_REPO}/.repodata"
    # Attempt to update existing metadata first (faster)
    if ! createrepo_c --update "${LOCAL_REPO}"; then
        warn "createrepo_c --update failed, attempting full re-index"
        rm -rf "${LOCAL_REPO}/.repodata" "${LOCAL_REPO}/repodata"
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

# Move a %find_lang out of %check and onto the end of %install.
#
# rpmbuild --nocheck does not skip the %check *command*, it skips the whole
# %check *section* -- and a spec is free to put anything after %check that is
# not a section header of its own. Fedora's iso-codes does exactly that:
#
#   %check
#   %meson_test
#
#   %find_lang %{name} --all-name
#
#   %files -f %{name}.lang
#
# so with --nocheck the .lang file is never generated and the build dies in
# %files, on a spec that is perfectly correct in Koji:
#
#   error: Could not open %files file .../iso-codes.lang: No such file or directory
#
# %find_lang only reads $RPM_BUILD_ROOT, so the end of %install is both where
# it belongs and where it behaves identically. Rewrite the staged copy, never
# the tree: an imported spec is a verbatim record of dist-git.
#
# Confined to specs that actually have the shape (checks off, a %find_lang
# inside %check, and an %install to move it to); every other spec is passed
# through byte for byte.
hoist_find_lang_out_of_check() {
    local staged_spec="$1"
    local pkg_name="$2"

    $WITH_CHECKS && return 0
    grep -q '^[[:space:]]*%find_lang' "$staged_spec" || return 0

    local rewritten="${staged_spec}.hoisted"
    awk '
        function is_section_header(l) {
            return (l ~ /^%(package|description|prep|generate_buildrequires|conf|build|install|check|files|changelog|pre|post|preun|postun|pretrans|posttrans|verifyscript|trigger|filetrigger|transfiletrigger|sepolicy|patchlist|sourcelist)([[:space:]]|$)/)
        }
        {
            line[NR] = $0
            if (is_section_header($0)) {
                if ($0 ~ /^%install([[:space:]]|$)/)     section = "install"
                else if ($0 ~ /^%check([[:space:]]|$)/)  section = "check"
                else                                     section = "other"
            }
            if (section == "install") install_end = NR
            if (section == "check" && $0 ~ /^[[:space:]]*%find_lang/) hoist[NR] = 1
        }
        END {
            hoisted = 0
            for (i = 1; i <= NR; i++) if (i in hoist) hoisted++
            if (hoisted == 0 || install_end == 0) { for (i = 1; i <= NR; i++) print line[i]; exit 0 }
            for (i = 1; i <= NR; i++) {
                if (i in hoist) continue
                print line[i]
                if (i == install_end)
                    for (j = 1; j <= NR; j++) if (j in hoist) print line[j]
            }
        }
    ' "$staged_spec" > "$rewritten" || { err "spec rewrite failed for ${pkg_name}"; return 1; }

    if ! cmp -s "$staged_spec" "$rewritten"; then
        echo "==> [${pkg_name}] %find_lang sits in %check, which --nocheck skips; hoisting it to %install"
        mv "$rewritten" "$staged_spec"
    else
        rm -f "$rewritten"
    fi
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
    hoist_find_lang_out_of_check "${builddir}/SPECS/$(basename "$spec")" "$pkg_name" || return 1

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
    #
    # $RPM_SOURCES_CACHE, when set, is a host directory that outlives the run.
    # The mock backend has honoured it for a long time; this path did not, so
    # every dispatch re-downloaded every upstream tarball -- and this is the
    # path the Hummingbird desktop builds take, where a tier is a hundred-odd
    # packages and a full desktop is over a thousand.
    #
    # Downloads land in the cache and are hard-linked into the builddir, so a
    # second package needing the same archive costs an inode rather than a
    # transfer. Falls back to copying across filesystems.
    echo "==> [${pkg_name}] Downloading sources via spectool..."
    local sources_cache="${RPM_SOURCES_CACHE:-}"
    local spectool_dest_host="${builddir}/SOURCES"
    local spectool_dest_container="/builddir/SOURCES"
    if [[ -n "$sources_cache" ]]; then
        mkdir -p "$sources_cache"
        spectool_dest_host="$sources_cache"
        spectool_dest_container="/sources-cache"
    fi
    if command -v spectool &>/dev/null; then
        spectool -g -C "${spectool_dest_host}" "$spec"
    else
        podman run --rm \
            --pull=always \
            -v "${builddir}:/builddir:Z" \
            ${sources_cache:+-v "${sources_cache}:/sources-cache:Z"} \
            "${BUILD_IMAGE}" \
            spectool -g -C "${spectool_dest_container}" "/builddir/SPECS/$(basename "$spec")"
    fi || {
        echo "ERROR: spectool failed for ${pkg_name}" >&2
        return 1
    }
    if [[ -n "$sources_cache" ]]; then
        # Never let a cached download land on top of a file that came out of
        # the package directory. Those files are what Fedora committed to
        # dist-git; the cache holds whatever a URL served at download time,
        # and for a spec that carries a patch by URL those are not the same
        # thing.
        #
        # luajit is the worked example. Its Patch2 is
        # https://github.com/luajit/luajit/pull/631.patch, a live pull request
        # whose diff is regenerated against a moving base, and dist-git also
        # ships the pinned copy as luajit-2.1-s390x-support.patch. `ln -f`
        # clobbered the pinned copy with today's regenerated one, which no
        # longer applies: "2 out of 4 hunks FAILED -- saving rejects to file
        # src/lj_ccall.c.rej", %prep dead, run 31294475023 layer-00.
        local cached
        while IFS= read -r -d '' cached; do
            [[ -e "${builddir}/SOURCES/$(basename "$cached")" ]] && continue
            ln -f "$cached" "${builddir}/SOURCES/" 2>/dev/null \
                || cp "$cached" "${builddir}/SOURCES/" 2>/dev/null || true
        done < <(find "$sources_cache" -maxdepth 1 -type f -print0)
    fi

    # Re-assert the dist-git files over anything spectool fetched. When
    # $RPM_SOURCES_CACHE is unset spectool downloads straight into SOURCES,
    # so the guard above never sees those writes; this makes "dist-git wins"
    # true on both paths rather than only on the cached one.
    find "$abs_pkg_dir" -maxdepth 1 -type f \
        ! -name "*.spec" \
        ! -name "sources" \
        ! -name "changelog" \
        ! -name "rpminspect.yaml" \
        ! -name "*.md" \
        -exec cp -f {} "${builddir}/SOURCES/" \;

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
        local entry_algo
        while IFS= read -r line; do
            # Two formats, both current in Rawhide today.
            #
            #   SHA512 (foo-1.0.tar.gz) = <128 hex>     the modern one
            #   <32 hex>  foo-1.0.tar.gz                the legacy md5 one
            #
            # Matching only the first silently skips the second -- `continue`
            # on a line that is not an error -- and the package then dies in
            # rpmbuild with "Bad file: /builddir/SOURCES/...: No such file or
            # directory", which names the tarball but not the reason.
            #
            # That is what happened to lockdev and redhat-menus in gnome-00 of
            # run 31272392927. Both still carry md5 sources files; both are
            # old packages nobody has re-uploaded, and there are more like them
            # across a 1248-package manifest.
            if [[ "$line" =~ ^SHA512\ \((.+)\)\ =\ ([0-9a-f]{128})$ ]]; then
                entry_name="${BASH_REMATCH[1]}"
                entry_hash="${BASH_REMATCH[2]}"
                entry_algo="sha512"
            elif [[ "$line" =~ ^([0-9a-f]{32})\ \ (.+)$ ]]; then
                entry_hash="${BASH_REMATCH[1]}"
                entry_name="${BASH_REMATCH[2]}"
                entry_algo="md5"
            else
                continue
            fi
            # Present is not the same as correct. A `sources` entry pins an
            # exact artifact, but plenty of Rawhide specs point Source0 at a
            # moving ref -- luajit's is
            # https://github.com/LuaJIT/LuaJIT/archive/refs/heads/v2.1/...,
            # the branch head. Fedora builds from the lookaside copy the
            # checksum names; spectool downloads whatever that branch is
            # today. The two drifted, so the patches (cut against the pinned
            # snapshot) stopped applying and luajit failed %prep in
            # run 31294475023 while Fedora's own build of the same commit is
            # fine.
            #
            # So verify what is staged against the recorded hash, and treat a
            # mismatch exactly like a missing file: discard it and take the
            # lookaside copy, which is by definition the artifact the rest of
            # the packaging was written against.
            if [[ -f "${builddir}/SOURCES/${entry_name}" ]]; then
                if echo "${entry_hash}  ${builddir}/SOURCES/${entry_name}" \
                    | "${entry_algo}sum" --check --status -; then
                    continue
                fi
                echo "==> [${pkg_name}] ${entry_name} does not match the ${entry_algo} in dist-git sources; refetching"
                # rm rather than overwrite: with $RPM_SOURCES_CACHE this file
                # is a hard link to the cached copy, and writing through it
                # would edit the cache behind its own checksum check.
                rm -f "${builddir}/SOURCES/${entry_name}"
            fi
            echo "==> [${pkg_name}] Fetching ${entry_name} from the Fedora lookaside cache (${entry_algo})..."
            # The lookaside path carries the algorithm, so the legacy entries
            # live under .../md5/<hash>/... and not under sha512.
            curl -fsSL --retry 3 \
                -o "${builddir}/SOURCES/${entry_name}" \
                "https://src.fedoraproject.org/lookaside/pkgs/rpms/${lookaside_name}/${entry_name}/${entry_algo}/${entry_hash}/${entry_name}" || {
                err "lookaside fetch failed for ${entry_name} (${pkg_name})"
                return 1
            }
            echo "${entry_hash}  ${builddir}/SOURCES/${entry_name}" \
                | "${entry_algo}sum" --check --quiet - || {
                err "lookaside checksum mismatch for ${entry_name} (${pkg_name})"
                return 1
            }
            # Persist it, so the next run and the next package needing the same
            # archive get it for free. Only after the checksum has passed --
            # a cache is a much worse place to put a corrupt file than a
            # builddir that is about to be deleted.
            if [[ -n "${RPM_SOURCES_CACHE:-}" ]]; then
                ln -f "${builddir}/SOURCES/${entry_name}" \
                    "${RPM_SOURCES_CACHE}/${entry_name}" 2>/dev/null \
                    || cp "${builddir}/SOURCES/${entry_name}" \
                        "${RPM_SOURCES_CACHE}/${entry_name}" 2>/dev/null || true
            fi
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
    #
    # --pull=missing, not --pull=always. BUILD_IMAGE is a fixed tag, and the
    # workflow already pulls it once in its own "Pull mock runner" step, so
    # --pull=always bought nothing but a GHCR round-trip -- and this function
    # runs for EVERY package the tier considers, before the skip decision, so
    # it was paid for packages that were about to be skipped too. That cost
    # grows as the R2 seed grows, which is backwards: the further hummingbird
    # converges, the more of each 6-hour run goes on confirming there is
    # nothing to do (tunaos-packages#410, #401).
    #
    # It is also more correct. The NVR this computes is compared against RPMs
    # an earlier run built; re-resolving the tag mid-run could answer from a
    # different image than the one that produced them.
    local nvr
    nvr=$(podman run --rm \
        --pull=missing \
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

    # The published index is the durable record: a served NVR was built,
    # signed and synced by an earlier leg, and consumers resolve it from the
    # repo at priority 11 -- rebuilding it buys nothing. This is what makes
    # legs incremental ACROSS action-key moves, which partials cannot be.
    if [[ -n "${SERVED_NVRS_SET[$nvr]:-}" ]]; then
        echo "==> [${pkg_name}] Skipping: ${nvr} already served by the published index"
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

    # Persistent mock ROOT CACHE, shared by every package in the run.
    #
    # Without it each package pays "installing minimal buildroot with dnf5" in
    # full and then tars a root cache that is thrown away with its container.
    # docs/hummingbird-throughput.md Finding 2 counted it across five real
    # runs: `Start: creating root cache` once per package, `unpacking root
    # cache` ZERO times, and 43 s -- the floor observed anywhere in the corpus
    # -- paid 194 times, 2.32 h of 6.80 h, 34.1%.
    #
    # Sharing one cache between concurrent builds is what mock is built for.
    # buildroot.py keys cachedir on shared_root_name, the config's root from
    # BEFORE --uniqueext is appended, and plugins/root_cache.py guards it with
    # an fcntl lock (shared to unpack, exclusive to rebuild). Correctness
    # against the local repo growing mid-run is equally by design: the tarball
    # holds only the MINIMAL buildroot, BuildRequires resolve after the unpack
    # against the live repos, and the Rawhide template these configs include
    # sets metadata_expire=0.
    #
    # Only <config>/root_cache, not all of /var/cache/mock. The sibling
    # yum_cache accumulates every BuildRequires RPM the chain downloads, which
    # for a desktop closure is unbounded in a way this directory is not (one
    # tarball, rewritten rather than appended). Filling the runner's disk now
    # costs more than it used to: the chain runs 4.5 h and its partial output
    # is what the continuation shards resume from, so an ENOSPC in hour three
    # poisons the whole night rather than one package. Widen it if a measured
    # run shows the headroom.
    #
    # <config> is MOCK_CONFIG because every profile in mock/ sets
    # config_opts['root'] to its own filename stem; a mismatch would mount a
    # path mock never looks at and be silent about it, so
    # tests/test_the_mock_root_cache_is_actually_shared.py pins it.
    MOCK_CACHE_ARGS=()
    if [[ -n "${MOCK_CACHE_DIR:-}" ]]; then
        mkdir -p "${MOCK_CACHE_DIR}/${MOCK_CONFIG}/root_cache"
        # Mode, because the leaf is now created on the HOST rather than by
        # mock inside the container. Run 31268488082 mounted the parent and
        # let mock create this directory itself, so its owner was whatever
        # the container decided; mounting the leaf hands it a directory owned
        # by the runner user, which rootless podman maps to container root
        # while mock drops to builder:mock and has to write the tarball. Same
        # reason `chmod -R a+rX /tmp/mock-configdir` is a few lines below --
        # this is an ephemeral single-tenant runner directory, not a shared
        # host path.
        chmod 0777 "${MOCK_CACHE_DIR}/${MOCK_CONFIG}/root_cache"
        # A TRUNCATED tarball is worse than no tarball: every later package in
        # the job fails to unpack it, so one bad write would cost the whole
        # chain rather than one package. It can happen -- the timeout(1) around
        # this container SIGKILLs a wedged build, and a kill landing in the
        # seconds mock spends tarring the buildroot leaves a partial file
        # behind, with the fcntl lock released by the dead process.
        #
        # Age-gated, and that gate is the load-bearing part. Testing a file
        # another worker is writing RIGHT NOW would read it as corrupt and
        # delete it, and with several workers that ping-pongs forever: the
        # cache would never survive long enough to be used and the whole
        # optimisation would silently do nothing. Tarring a minimal buildroot
        # takes under a minute; ten minutes cannot be an in-flight write.
        _root_cache_tarball="${MOCK_CACHE_DIR}/${MOCK_CONFIG}/root_cache/cache.tar.gz"
        if [[ -f "$_root_cache_tarball" ]] \
           && [[ -z "$(find "$_root_cache_tarball" -mmin -10 2>/dev/null)" ]] \
           && ! gzip -t "$_root_cache_tarball" 2>/dev/null; then
            log "  discarding a corrupt mock root cache: ${_root_cache_tarball}"
            rm -f "$_root_cache_tarball"
        fi
        MOCK_CACHE_ARGS=(-v "${MOCK_CACHE_DIR}/${MOCK_CONFIG}/root_cache:/var/cache/mock/${MOCK_CONFIG}/root_cache:Z")
    fi

    local mock_check_flag="--nocheck"
    $WITH_CHECKS && mock_check_flag=""

    # Wrapped in a function so the retry below re-runs the IDENTICAL
    # invocation with one extra mock argument, rather than a second copy
    # of it drifting out of sync with this one.
    #
    # timeout(1) around the whole container, because a wedged mock hangs
    # the tier SILENTLY: runs 31732589290 and 31757583258 each sat 2+
    # hours with zero further log output after netcdf's builddep retry
    # entered chroot init, held the workflow's concurrency lock the whole
    # time, and only died when a human cancelled them. GitHub's own
    # step timeout cannot help here -- the runner agent stays healthy, it
    # is the container that never returns. An external bound converts the
    # hang into a visible per-package failure (exit 124) that the tier
    # retry logic treats like any other failed package and moves past.
    #
    # 180 minutes default: generous enough for the largest single package
    # a desktop tier carries on a 4-core runner, an order of magnitude
    # above the tier median, and still a third of the 6-hour job budget a
    # single hung package used to consume. MOCK_TIMEOUT_MINUTES overrides
    # it for a known-slow rebuild. --kill-after covers a container that
    # ignores SIGTERM.
    _run_mock_container() {
        local mock_extra_args="${1:-}"
        # A failed dynamic-BuildRequires pass can leave the reused uniqueext
        # chroot wedged (mock's --no-clean is otherwise intentional so the
        # package's buildroot survives for diagnostics). The retry caller can
        # request a clean chroot without duplicating this invocation.
        # "clean" means: no flag at all. mock cleans the chroot before a
        # build BY DEFAULT; --no-clean is what suppresses it. Passing --clean
        # instead makes mock run its `clean` COMMAND and nothing else (one
        # command per invocation, the last one wins), which is what the retry
        # did on the gnome50 gate of 2026-09-03 (run 33753245495):
        # "Start: clean chroot / Finish: clean chroot / Finish: run", no
        # rebuild, "No RPMs produced".
        local mock_clean_flag="--no-clean"
        [ "${2:-}" = "clean" ] && mock_clean_flag=""
        timeout --kill-after=60s "${MOCK_TIMEOUT_MINUTES:-180}m" \
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
                #   * the one thing concurrent builds now DO share is the
                #     root cache under /var/cache/mock/<config>/root_cache,
                #     and mock guards that itself with an fcntl lock (shared
                #     to unpack, exclusive to rebuild) -- repo.lock never
                #     protected it and could not have.
                # So the exclusive lock protected nothing, while serialising
                # the single most expensive step in the run: --jobs N started N
                # workers that then took turns compiling one at a time.
                # Both directories, and both matter. Only the LEAF is
                # bind-mounted, so podman creates the <config> parent itself,
                # root-owned and 0755 -- and mock runs as builder:mock two
                # lines below, so without this it cannot create its siblings
                # (yum_cache) under a directory it does not own. In the image
                # /var/cache/mock is root:mock 2775 and mock makes the whole
                # subtree itself; introducing a mount point is what changes
                # that. || true because a missing cache must never be worse
                # than a slow build, which is the whole point of the mount.
                chmod 0777 /var/cache/mock/${MOCK_CONFIG} \
                           /var/cache/mock/${MOCK_CONFIG}/root_cache 2>/dev/null || true
                flock -s /local-repo/repo.lock -c \"
                    setpriv --reuid=builder --regid=mock --init-groups \\
                    mock --configdir /tmp/mock-configdir -r '${MOCK_CONFIG}' \\
                        --uniqueext='${pkg_name}' \\
                        --rebuild /builddir/SRPMS/*.src.rpm \\
                        --resultdir=/builddir/results \\
                        --define 'dist ${DIST}' \\
                        ${mock_check_flag} ${MOCK_BCOND_ARGS} \\
                        ${mock_clean_flag} \\
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
    #
    # The first attempt uses --no-clean so failed build logs and the chroot
    # remain available for diagnosis. If this exact dnf5 failure occurred,
    # however, the retry must discard that chroot: the failed dynamic-
    # BuildRequires transaction can leave its rpm/dnf state wedged, and the
    # second mock invocation otherwise hangs during create skeleton dirs.
    if ! _run_mock_container ""; then
        # The exact signature, not a bare "is already installed": dnf5 also
        # prints "Package <nevra> is already installed." as INFORMATION for
        # every already-satisfied BuildRequires (root.log is full of them on
        # a healthy build), and matching those turned a genuine %files
        # failure into a mislabelled "dnf5 bug" retry (input-remapper, run
        # 33753245495). Only the transaction failure is the bug, and dnf5
        # prints it across two lines.
        if tr '\n' ' ' < "${builddir}/results/root.log" 2>/dev/null | grep -q "Failed to resolve the transaction:.*is already installed"; then
            echo "==> [${pkg_name}] mock hit the dnf5 already-installed dynamic-BuildRequires bug; cleaning chroot and retrying with dynamic_buildrequires=False"
            _run_mock_container "--config-opts=dynamic_buildrequires=False" "clean"
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

    record_buildroot_manifest "$resultdir" "$pkg_name"

    if [[ $rpm_count -eq 0 ]]; then
        echo "ERROR: No RPMs produced for ${pkg_name}" >&2
        return 1
    fi

    echo "==> [${pkg_name}] Built ${rpm_count} RPM(s)"
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
                    if wait "$pid"; then
                        : # success
                    else
                        err "Failed: ${path}"
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
                    if wait "$pid"; then
                        : # success
                    else
                        err "Failed: ${path}"
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
        local path
        for path in "${retry[@]}"; do
            if build_package "$path" ""; then
                log "  [retry] ${path} built on the second pass"
            else
                err "Failed: ${path}"
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
