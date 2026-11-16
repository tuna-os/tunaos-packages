#!/usr/bin/env bash

# Podman-backed mock executor for build-chain.sh.
#
# Contract supplied by the orchestrator:
#   globals: ALMA_CANDIDATE, ALMA_CANDIDATE_META, ALMA_CANDIDATE_STATE,
#            ALMA_KEY_MOUNT_ARGS, BUILD_IMAGE, DIST, FORCE, LOCAL_REPO,
#            MOCK_CACHE_DIR, MOCK_CONFIG, REPO_ROOT, SCRIPT_DIR, SRPM_BCOND_ARGS,
#            UTAH_MOUNT_ARGS, WITH_CHECKS
#   functions: check_package_exists, err, find_spec, log, prepare_sources,
#              record_buildroot_manifest, update_local_repo
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
    if $ALMA_CANDIDATE && ! $FORCE && check_package_exists "$pkg_name" "$spec" "$builddir"; then
        return 0
    fi

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
    if $ALMA_CANDIDATE; then
        update_local_repo
    else
        flock "${LOCAL_REPO}/repo.lock" -c "createrepo_c --update \"${LOCAL_REPO}\""
    fi

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
            "${UTAH_MOUNT_ARGS[@]}" \
            "${ALMA_KEY_MOUNT_ARGS[@]}" \
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
                if ${ALMA_CANDIDATE}; then
                    test -s /keys/candidate-public.gpg
                    # Policy binds keys into mock's real chroot and rejects
                    # unsigned metadata; its key is only a run-local candidate.
                    cat /keys/mock-candidate-policy.cfg >> /tmp/mock-configdir/${MOCK_CONFIG}.cfg
                fi
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
                if ${ALMA_CANDIDATE}; then
                    setpriv --reuid=builder --regid=mock --init-groups \\
                    mock --configdir /tmp/mock-configdir -r '${MOCK_CONFIG}' \\
                        --uniqueext='${pkg_name}' --chroot -- \\
                        /bin/sh /keys/measure-buildroot.sh \\
                        > /builddir/results/candidate-buildroot-observed.txt
                fi
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
    local alma_completed_outputs=()
    while IFS= read -r -d '' rpm; do
        if $ALMA_CANDIDATE; then
            python3 "${SCRIPT_DIR}/candidate-rpm-repository.py" install \
                --state "$ALMA_CANDIDATE_STATE" --repo "$LOCAL_REPO" --rpm "$rpm"
        else
            cp "$rpm" "${LOCAL_REPO}/"
        fi
        echo "==> [${pkg_name}] -> $(basename "$rpm")"
        rpm_count=$(( rpm_count + 1 ))
        alma_completed_outputs+=(--output "$(basename "$rpm")")
    done < <(find "$resultdir" -name "*.rpm" ! -name "*.src.rpm" -print0)

    record_buildroot_manifest "$resultdir" "$pkg_name"

    if [[ $rpm_count -eq 0 ]]; then
        echo "ERROR: No RPMs produced for ${pkg_name}" >&2
        return 1
    fi

    if $ALMA_CANDIDATE && [[ -n "${ALMA_CANDIDATE_META:-}" ]]; then
        python3 "${SCRIPT_DIR}/alma-candidate-resume.py" record \
            --meta "$ALMA_CANDIDATE_META" --repo "$LOCAL_REPO" --name "$pkg_name" \
            --builddir "$builddir" "${alma_completed_outputs[@]}"
    fi
    echo "==> [${pkg_name}] Built ${rpm_count} RPM(s)"
}
