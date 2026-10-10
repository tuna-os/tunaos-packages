#!/usr/bin/env bash
# Verify one unified matrix row. Package-format mechanics live here; session
# requirements remain recipe/queue data and are invoked through their gates.
set -eEuo pipefail

out=${OUT_DIR:-"$PWD/.factory/${CELL_ID:?}"}
artifacts="$out/artifacts"
test -d "$artifacts"

# The target's runtime repo set (#446): the clean-install verify must resolve
# against the same repos a produced image enables (for el10: crb + epel, per
# tunaOS build_scripts/10-base-packages.sh), or real-world-installable
# packages fail the gate on deps the image serves fine — run 32382594650
# failed every cosmic el10 cell on EPEL-served gnome-icon-theme/libdav1d.so.7.
# Empty for targets that declare none; only the dnf paths consume it today.
system_repos="$(python3 - "${TARGET:-}" <<'PY'
import pathlib, sys, yaml
d = yaml.safe_load(pathlib.Path("manifests/package-factory.yaml").read_text()) or {}
t = d.get("targets", {}).get(sys.argv[1]) or {}
print(" ".join(t.get("system_repositories") or []))
PY
)"

# The target contract's `published_index` (served read URL, distinct from
# `r2_path`) is added as a lower-priority repo so the local cell artifacts win
# and the published repo only fills in deps. Space-separated: an arch may
# declare several indexes (#467). URLs never contain whitespace, so
# word-splitting in the container is safe.
#
# Resolved ONCE above BOTH the engine branch and the format case. It was
# assigned below the build-chain branch, so build-chain cells got no published
# repo at all -- which is why xfce-el10 could not resolve the greetd that this
# factory builds and serves at rpm/el10/{arch} (#482). It had already been
# hoisted once, past the rpm branch, for the same reason on deb.
published_index="$(python3 scripts/published_index.py "$TARGET" "${ARCHITECTURE:-}" --join)"

if [[ ${ENGINE:?} == build-chain ]]; then
  mapfile -d '' rpms < <(find "$artifacts" -type f -name '*.rpm' ! -name '*.src.rpm' -print0)
  ((${#rpms[@]} > 0)) || { echo "native queue produced no RPMs" >&2; exit 1; }
  docker run --rm --entrypoint /bin/bash --volume "$artifacts:/artifacts:ro" \
    --volume "$PWD/scripts:/scripts:ro" "${IMAGE:?}" \
    /scripts/lint-generated-rpm.sh /artifacts
  docker run --rm --entrypoint /bin/bash \
    --env TARGET="${TARGET:?}" --env SYSTEM_REPOS="$system_repos" \
    --env PUBLISHED_INDEX="$published_index" \
    --volume "$artifacts:/artifacts:ro" "${IMAGE:?}" -lc '
    set -euo pipefail
    for sysrepo in ${SYSTEM_REPOS:-}; do
      case $sysrepo in
        epel) dnf -y install epel-release ;;
        *) dnf -y install "dnf-command(config-manager)"
           dnf config-manager --set-enabled "$sysrepo" ;;
      esac
    done
    dnf -y install createrepo_c
    mkdir /factory-repo
    find /artifacts -maxdepth 1 -type f -name "*.rpm" ! -name "*.src.rpm" -exec cp -t /factory-repo {} +
    createrepo_c /factory-repo
    # Same shape as the tideforge branch: priority 50 so the local cell
    # artifacts (priority 1) always win and the published repo only fills in
    # dependencies this chain does not build itself.
    published_args=()
    published_n=0
    for published_url in ${PUBLISHED_INDEX:-}; do
      published_args+=(--repofrompath "tunaos${published_n},${published_url}"
                       "--setopt=tunaos${published_n}.priority=50"
                       --enablerepo="tunaos${published_n}")
      published_n=$((published_n + 1))
    done
    # Only the NEWEST BUILD OF EACH SOURCE. Installing by name alone was not
    # enough, and the residue is instructive: a subpackage that exists ONLY in
    # the superseded bootstrap build has its own newest version in that older
    # build, and drags in an exact `= EVR` dependency on a sibling whose
    # newest is the newer one:
    #
    #   cannot install both gobject-introspection-debuginfo-1.86.0-2 and
    #   -1 from factory
    #     - gobject-introspection-devel-debuginfo-1.86.0-1 requires
    #       gobject-introspection-debuginfo(aarch-64) = 1.86.0-1.el10
    #
    # gobject-introspection-devel-debuginfo is produced by the bootstrap pass
    # and not by the full one, so "newest by name" picks a package the chain
    # never meant to ship. "Newest by SOURCE" is the right unit: it is exactly
    # what the chain intends to ship, and the bootstrap pass drops out whole
    # rather than leaving orphan subpackages behind.
    #
    # sort -V rather than the rpm library comparison, because the container is
    # the target image and may carry dnf5 without python3-rpm. Adequate HERE
    # and not in general: the two builds come from one chain run of one recipe
    # and differ only in RELEASE (2.88.0-1 vs 2.88.0-4, 1.86.0-1 vs -2), which
    # sort -V orders correctly. It implements neither epochs nor the rpm tilde
    # rule, so this must not be lifted somewhere those can occur.
    #
    # No apostrophes anywhere in this block: it lives inside a single-quoted
    # bash -lc string, and one would end the string mid-script.
    declare -A newest_srpm
    while IFS="|" read -r srpm pkgname; do
      [ -n "$srpm" ] || continue
      base=${srpm%-*-*.src.rpm}
      cur=${newest_srpm[$base]:-}
      if [ -z "$cur" ] || [ "$(printf "%s\n%s\n" "$cur" "$srpm" | sort -V | tail -1)" = "$srpm" ]; then
        newest_srpm[$base]=$srpm
      fi
    done < <(rpm -qp --qf "%{SOURCERPM}|%{NAME}\n" /factory-repo/*.rpm 2>/dev/null)
    mapfile -t names < <(
      rpm -qp --qf "%{SOURCERPM}|%{NAME}\n" /factory-repo/*.rpm 2>/dev/null |
        while IFS="|" read -r srpm pkgname; do
          [ -n "$srpm" ] || continue
          base=${srpm%-*-*.src.rpm}
          [ "${newest_srpm[$base]:-}" = "$srpm" ] && printf "%s\n" "$pkgname"
        done | sort -u
    )
    echo "==> verifying ${#names[@]} package name(s) from the newest build of each source"
    # Install by NAME, not by file path. A build-chain tier list may build the
    # same recipe twice on purpose -- gnome51 has glib2-bootstrap at tier 2 and
    # glib2-full at tier 6, both src/gnome-51/glib2, and the same for
    # gobject-introspection at tiers 5 and 7 -- so /artifacts holds two EVRs of
    # those names. Passing every FILE asked dnf to install both at once, which
    # is unsatisfiable by construction:
    #
    #   cannot install both glib2-2.88.0-4.el10.aarch64 from @commandline
    #   and glib2-2.88.0-1.el10.aarch64 from @commandline
    #
    # By name, dnf resolves each to the newest EVR in the factory repo, which
    # is the build the chain means to ship; the superseded bootstrap pass is
    # scaffolding. rpm -q and rpm -V below still cover every name, so nothing
    # goes unchecked. factory.priority=1 keeps these ahead of the published
    # repo at priority 50.
    # ${a[@]+"${a[@]}"} and not "${a[@]}": under `set -u` an empty array
    # expansion is an unbound-variable error on bash < 4.4.
    dnf -y install --nogpgcheck --repofrompath factory,file:///factory-repo \
      --setopt=factory.priority=1 --enablerepo=factory \
      ${published_args[@]+"${published_args[@]}"} \
      "${names[@]}"
    rpm -q "${names[@]}"
    # rpm -V, minus two classes of difference a THROWAWAY CONTAINER reports by
    # construction rather than because a package is wrong. Both took all four
    # gnome cells red in run 32646102181 AFTER a clean build, a clean lint and
    # a clean install:
    #
    #   .....UG..    /var/lib/avahi-autoipd
    #   .M.......  g /var/lib/selinux/minimum/active/policy.linked
    #
    # GHOST FILES (the `g` in the second column). A %%ghost path is one rpm
    # knows the name of but ships no content for -- it is created at runtime
    # by a scriptlet, and the attributes in the header are placeholders.
    # selinux-policy declares its .linked files that way and semodule builds
    # them. Comparing a placeholder against what a scriptlet produced is not
    # a measurement of anything.
    #
    # OWNERSHIP. A service account like avahi-autoipd is created by sysusers
    # at install time; a container without systemd running does not get one,
    # so rpm falls back to root and then reports U and G as differing. That
    # is a fact about the verification environment.
    #
    # What is deliberately still checked: digest, size, mode, link target,
    # capabilities. Those catch a corrupt or mis-packaged payload, which is
    # what this step is for. Note also what rpm -V CANNOT tell us here -- it
    # compares the installed tree against the header rpm itself just wrote,
    # so a wrong owner or mode in the spec would agree with itself and pass.
    # Ownership policy is verified where it takes effect, in the image build,
    # not in a container that discards itself a second later.
    rpm -V --nouser --nogroup "${names[@]}" > /tmp/rpm-verify.out 2>&1 || true
    grep -vE "^[.?SM5DLUGTPa]{8,9} +g " /tmp/rpm-verify.out > /tmp/rpm-verify.real || true
    if [ -s /tmp/rpm-verify.real ]; then
      echo "==> rpm -V reported differences outside the excluded classes:" >&2
      cat /tmp/rpm-verify.real >&2
      exit 1
    fi
    echo "==> rpm -V clean across ${#names[@]} package name(s)"
  '
  exit 0
fi

recipe=${RECIPE:?}
if python3 - "$recipe" <<'PY'
import pathlib, sys, yaml
recipe = yaml.safe_load(pathlib.Path(sys.argv[1]).read_text()) or {}
raise SystemExit(0 if recipe.get("verify") else 1)
PY
then
  python3 scripts/tideforge.py verify "$recipe" --target "${TARGET:?}" --field smoke > "$out/smoke.sh"
  install_name=$(python3 scripts/tideforge.py verify "$recipe" --target "$TARGET" --field install_name)
else
  printf 'true\n' > "$out/smoke.sh"
  install_name=$(python3 - "$recipe" <<'PY'
import pathlib, sys, yaml
recipe = yaml.safe_load(pathlib.Path(sys.argv[1]).read_text()) or {}
print(recipe.get("name") or pathlib.Path(sys.argv[1]).parent.name)
PY
)
fi

# Cross-cell dependency resolution (#440): a clean-install verify of a package
# whose runtime deps are themselves in the gap (e.g. niri needs libseat) can
# only resolve them from the PUBLISHED factory repo, built by earlier waves.
# The target contract's `published_index` (served read URL, distinct from
# `r2_path`) is added as a lower-priority repo so the local cell artifacts win
# and the published repo only fills in deps. Space-separated: an arch may
# declare several indexes (#467) — el10 x86_64 has the tideforge mirror and the
# xfce build-chain prefix. URLs never contain whitespace, so word-splitting in
# the container is safe.
#
case ${FORMAT:?} in
  rpm)
    docker run --rm --entrypoint /bin/bash --volume "$artifacts:/artifacts:ro" \
      --volume "$PWD/scripts:/scripts:ro" "${IMAGE:?}" \
      /scripts/lint-generated-rpm.sh /artifacts
    docker run --rm --entrypoint /bin/bash --env INSTALL_NAME="$install_name" \
      --env PUBLISHED_INDEX="$published_index" --env SYSTEM_REPOS="$system_repos" \
      --volume "$artifacts:/artifacts:ro" --volume "$out/smoke.sh:/smoke.sh:ro" \
      --volume "$PWD/scripts:/scripts:ro" "${IMAGE:?}" -lc '
        set -euo pipefail
        mkdir /factory-repo
        cp /artifacts/*.rpm /factory-repo/
        if command -v zypper >/dev/null; then
          zypper --non-interactive install createrepo_c
          createrepo_c /factory-repo
          zypper --non-interactive addrepo --no-gpgcheck --priority 1 file:///factory-repo tideforge
          published_n=0
          for published_url in ${PUBLISHED_INDEX:-}; do
            zypper --non-interactive addrepo --no-gpgcheck --priority 50 \
              "$published_url" "tunaos-published-${published_n}"
            published_n=$((published_n + 1))
          done
          bash /scripts/zypper-refresh-with-retry.sh
          zypper --non-interactive --no-gpg-checks install "$INSTALL_NAME"
        else
          for sysrepo in ${SYSTEM_REPOS:-}; do
            case $sysrepo in
              epel) dnf -y install epel-release ;;
              *) dnf -y install "dnf-command(config-manager)"
                 dnf config-manager --set-enabled "$sysrepo" ;;
            esac
          done
          dnf -y install createrepo_c
          createrepo_c /factory-repo
          published_args=()
          published_n=0
          for published_url in ${PUBLISHED_INDEX:-}; do
            published_args+=(--repofrompath "tunaos${published_n},${published_url}"
                             "--setopt=tunaos${published_n}.priority=50"
                             --enablerepo="tunaos${published_n}")
            published_n=$((published_n + 1))
          done
          # ${a[@]+"${a[@]}"} and not "${a[@]}": under `set -u` an empty
          # array expansion is an unbound-variable error on bash < 4.4, and
          # the probe images are not all bash 5.
          dnf -y install --nogpgcheck --repofrompath tideforge,file:///factory-repo \
            --setopt=tideforge.priority=1 --enablerepo=tideforge \
            ${published_args[@]+"${published_args[@]}"} \
            "$INSTALL_NAME"
        fi
        rpm -q "$INSTALL_NAME"
        bash /smoke.sh
      '
    ;;
  deb)
    docker run --rm --volume "$artifacts:/artifacts:ro" \
      --volume "$PWD/scripts:/scripts:ro" "${IMAGE:?}" \
      bash /scripts/lint-generated-deb.sh /artifacts
    docker run --rm --env INSTALL_NAME="$install_name" \
      --env PUBLISHED_INDEX="$published_index" \
      --volume "$artifacts:/artifacts:ro" --volume "$out/smoke.sh:/smoke.sh:ro" \
      "${IMAGE:?}" bash -lc '
        set -euo pipefail
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq
        # ca-certificates BEFORE the HTTPS indexes are added, not after. apt
        # skips an unreachable source with a WARNING and exits 0, so adding
        # them first turns a missing CA bundle into an unexplained missing
        # package a hundred lines later. Same ordering as the build container.
        apt-get install -y --no-install-recommends dpkg-dev pkg-config ca-certificates
        mkdir /repo && cp /artifacts/*.deb /repo/ && cd /repo
        dpkg-scanpackages . /dev/null | gzip -9c > Packages.gz
        printf "deb [trusted=yes] file:/repo ./\n" > /etc/apt/sources.list.d/tideforge.list
        printf "Package: *\nPin: origin \"\"\nPin-Priority: 1001\n" > /etc/apt/preferences.d/tideforge.pref
        # Both RPM branches above already declare the published indexes here;
        # the deb branch did not, so a package whose runtime closure includes a
        # factory-PUBLISHED deb could never be verified. quickshell links
        # libcpptrace.so.1, dpkg-shlibdeps resolved that to libcpptrace-dev,
        # and the install died with "Depends libcpptrace-dev (>= 1.0.4) but
        # none of the choices are installable: [no choices]" -- against a
        # package that is built, published and served.
        #
        # Priority 100 is below apt default 500 and far below the local
        # repo pin of 1001: the published index may only ever FILL A GAP, never
        # outrank the artifact under test or the distro archive.
        published_n=0
        for published_url in ${PUBLISHED_INDEX:-}; do
          printf "deb [trusted=yes] %s ./\n" "$published_url" \
            > "/etc/apt/sources.list.d/tunaos-published-${published_n}.list"
          published_host=$(printf "%s" "$published_url" | sed -E "s#^[a-z]+://([^/]+).*#\\1#")
          printf "Package: *\nPin: origin \"%s\"\nPin-Priority: 100\n" "$published_host" \
            > "/etc/apt/preferences.d/tunaos-published-${published_n}.pref"
          published_n=$((published_n + 1))
        done
        apt-get update > /tmp/apt-update.log 2>&1 || { cat /tmp/apt-update.log >&2; exit 1; }
        cat /tmp/apt-update.log
        if grep -q "Failed to fetch" /tmp/apt-update.log; then
          echo "ERROR: a declared published index could not be fetched (see above)." >&2
          exit 1
        fi
        apt-get install -y "$INSTALL_NAME"
        dpkg-query -W "$INSTALL_NAME"
        bash /smoke.sh
      '
    ;;
  pkg.tar.zst)
    python3 scripts/validate-built-arch-package.py "$recipe" "$out/package-info.txt"
    docker run --rm --user root --env TUNAOS_ARCHITECTURE="${ARCHITECTURE:?}" \
      --volume "$artifacts:/artifacts:ro" \
      --volume "$out/smoke.sh:/smoke.sh:ro" --volume "$PWD/scripts:/scripts:ro" \
      "${IMAGE:?}" bash /scripts/arch-clean-install.sh "$install_name" /artifacts bash /smoke.sh
    ;;
  *) echo "unsupported package format: $FORMAT" >&2; exit 2 ;;
esac
