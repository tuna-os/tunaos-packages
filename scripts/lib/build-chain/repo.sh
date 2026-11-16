#!/usr/bin/env bash

# Local RPM repository service for build-chain.sh: create, index and count the
# repository every backend builds into and resolves BuildRequires from.
#
# Contract supplied by the orchestrator:
#   globals: ALMA_CANDIDATE, ALMA_CANDIDATE_META, ALMA_CANDIDATE_STATE,
#            ALMA_RESUME_DIR, BACKEND, BUILDROOT_MANIFESTS, LOCAL_REPO,
#            REPO_ROOT, SCRIPT_DIR
#   functions: log, warn
#   sets: ALMA_CANDIDATE_PARENT, ALMA_CANDIDATE_STATE, ALMA_KEY_MOUNT_ARGS

ensure_local_repo() {
    mkdir -p "${LOCAL_REPO}"
    if $ALMA_CANDIDATE; then
        if [[ -z "${ALMA_CANDIDATE_STATE:-}" ]]; then
            ALMA_CANDIDATE_PARENT=$(mktemp -d "${TMPDIR:-/tmp}/tunaos-candidate.XXXXXXXX")
            ALMA_CANDIDATE_STATE="${ALMA_CANDIDATE_PARENT}/state"
            # Invoked through the EXIT trap below.
            # shellcheck disable=SC2329
            cleanup_alma_candidate() {
                gpgconf --homedir "$ALMA_CANDIDATE_STATE/gnupg" --kill gpg-agent || true
                rm -rf -- "$ALMA_CANDIDATE_PARENT"
            }
            trap cleanup_alma_candidate EXIT
            trap 'exit 143' TERM
            trap 'exit 130' INT
            python3 "${SCRIPT_DIR}/candidate-rpm-repository.py" init \
                --state "$ALMA_CANDIDATE_STATE" --repo "$LOCAL_REPO"
            if [[ -n "${ALMA_RESUME_DIR:-}" && -d "$ALMA_RESUME_DIR" ]]; then
                python3 "${SCRIPT_DIR}/alma-candidate-resume.py" admit \
                    --state "$ALMA_CANDIDATE_STATE" --repo "$LOCAL_REPO" \
                    --meta "${ALMA_CANDIDATE_META:?}" --destination "$ALMA_RESUME_DIR"
            fi
            if [[ -n "${ALMA_CANDIDATE_META:-}" ]]; then
                cp "$ALMA_CANDIDATE_STATE/keys/candidate-public.gpg" "$ALMA_CANDIDATE_META/candidate-public.gpg"
                cp "$ALMA_CANDIDATE_STATE/identity.json" "$ALMA_CANDIDATE_META/candidate-identity.json"
            fi
            # Passed to the sourced podman backend.
            # shellcheck disable=SC2034
            ALMA_KEY_MOUNT_ARGS=(-v "${ALMA_CANDIDATE_STATE}/keys:/keys:ro,Z")
            echo 'Alma bootstrap scope: run-local candidate, unpromoted; not production readiness'
        fi
        python3 "${SCRIPT_DIR}/candidate-rpm-repository.py" index \
            --state "$ALMA_CANDIDATE_STATE" --repo "$LOCAL_REPO"
        return
    fi
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
    if $ALMA_CANDIDATE; then
        # Actual chroot observations are retained separately from declarations
        # and the legacy log-derived inventory. They do not prove CPU readiness.
        test -s "$resultdir/candidate-buildroot-observed.txt"
        mkdir -p "$BUILDROOT_MANIFESTS"
        cp "$resultdir/candidate-buildroot-observed.txt" \
            "$BUILDROOT_MANIFESTS/${pkg_name}.candidate-observed.txt"
        cp "$ALMA_CANDIDATE_STATE/identity.json" \
            "$BUILDROOT_MANIFESTS/${pkg_name}.candidate-scope.json"
        cp "$ALMA_CANDIDATE_STATE/keys/candidate-public.gpg" \
            "$BUILDROOT_MANIFESTS/${pkg_name}.candidate-public.gpg"
    fi
    python3 "${REPO_ROOT}/scripts/extract-buildroot-manifest.py" "$resultdir" \
        --output "${BUILDROOT_MANIFESTS}/${pkg_name}.buildroot.txt" \
        || echo "==> [${pkg_name}] buildroot manifest not recorded (non-fatal)"
}

update_local_repo() {
    log "Updating local repo metadata"
    if $ALMA_CANDIDATE; then
        python3 "${SCRIPT_DIR}/candidate-rpm-repository.py" index \
            --state "$ALMA_CANDIDATE_STATE" --repo "$LOCAL_REPO"
        return
    fi
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

# How many RPMs the local repo holds. Used to decide whether a tier's failures
# are worth retrying: a retry can only help if something new landed.
_repo_rpm_count() {
    find "$LOCAL_REPO" -maxdepth 1 -name '*.rpm' 2>/dev/null | wc -l
}
