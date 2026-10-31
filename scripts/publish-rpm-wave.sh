#!/usr/bin/env bash
# Sign, place and index one wave of RPMs into a local repo tree.
#
# This is the half of publishing that is pure file manipulation: no network,
# no secrets, no rclone. The caller syncs the destination down first, calls
# this, then syncs up. Splitting it out is not tidiness -- publish-tideforge
# -rpms.yml and publish-build-chain-rpms.yml both need it, and every safety
# rule below was learned once and must not have to be learned again in the
# second copy. The repo has already paid for that kind of drift twice (the
# nightly cron stagger documented but never applied; the readiness stamp read
# from two paths flatpak had stopped using).
#
# The rules, each anchored to the incident that produced it:
#
#   EMPTY WAVE      A wave with no RPMs means the build produced nothing and
#                   the publish is a no-op that would still rewrite repodata.
#                   Refuse, so a silently-empty build cannot look published.
#
#   NEVER SHRINK    #124 / INCIDENT-repo-wipe-gnome: `rclone sync` makes the
#                   destination match the source, so a locally-incomplete
#                   tree DELETES the served repo. The caller guards the
#                   sync-down; this guards the processing. Publishing adds
#                   packages -- if the tree came out smaller than it came in,
#                   something dropped files and syncing up would erase them
#                   from the bucket.
#
#   '+' IN NAMES    librepo percent-encodes '+' when building download URLs,
#                   but the repo.tunaos.org worker looks up R2 keys with the
#                   raw request path, so any filename containing '+' 404s at
#                   install time (run 32411090239: oversteer-udev-0.8.3+git…
#                   serves 200 at the literal-'+' URL and 404 at the %2b
#                   one). Renamed across the WHOLE tree, not just the staged
#                   files, so the sync also replaces any '+'-named object
#                   already in the bucket. Only the file name changes; the
#                   version inside the rpm metadata is untouched.
#
#   SRPMS EXCLUDED  Source RPMs are not installable content and bloat the
#                   index; the tideforge publisher has always excluded them.
#
#   SUPERSEDE       A publisher writes into REPO/SUBDIR, but the prefixes it
#                   writes to were served for months by earlier publishers
#                   that wrote flat at the root. Copying in leaves BOTH, and
#                   an rpm-md index with two entries for one NEVRA is
#                   ambiguous: dnf picks one, and the two are not the same
#                   bytes -- measured on xfce/10-stream-x86_64, which held
#                   107 same-NEVRA pairs with 107 differing checksums after
#                   the first build-chain wave. The staged copy is the
#                   freshly signed one, so it wins and the older file with
#                   the same name is removed from anywhere else in the tree.
#                   Subtracted from the NEVER SHRINK baseline, since this is
#                   the one shrink that is intended.
#
#   FOREIGN CONTENT A family prefix carries what this factory built and
#   (--evict-foreign) signed, and nothing else. gnome50/10-stream-x86_64 was
#                   filled on 2026-08-04 by the since-deleted
#                   refresh-gnome50-r2.yml, which downloaded the
#                   jreilly1821/c10s-gnome-50 COPR and synced it in: 466
#                   package names signed by the COPR project key
#                   (99b9f29ec528e021), not the publisher's. A consumer
#                   with gpgcheck=1 and the publisher's public.gpg fails on
#                   every one of them -- tunaOS run 33750514082: 'GPG check
#                   FAILED, Public key for glib2-2.88.0-4.el10.x86_64.rpm
#                   is not installed'. And the maintainer's directive
#                   (2026-09-03) is no more COPR: build in GitHub. So an
#                   RPM in the synced-down tree whose header signature is
#                   not by the publisher's key (any subkey of %_gpg_name)
#                   is evicted before indexing, and the sync-up deletes it
#                   from the bucket. Opt-in per publisher: the build-chain
#                   publisher owns its family prefixes outright; the
#                   tideforge publisher writes into repo/10-stream-x86_64,
#                   which has more history than one key, and does not ask.
#                   Guarded: the freshly signed staged RPMs must be
#                   recognised as the publisher's own before anything is
#                   evicted -- a key-id derivation that fails that check
#                   would otherwise evict the whole tree, which is #124 by
#                   another road.
set -euo pipefail

STAGED="" REPO="" SUBDIR="" EVICT_FOREIGN=0 EXCLUDE_FILE=""
while [ $# -gt 0 ]; do
	case "$1" in
	--staged) STAGED="$2"; shift 2 ;;
	--repo) REPO="$2"; shift 2 ;;
	--subdir) SUBDIR="$2"; shift 2 ;;
	--evict-foreign) EVICT_FOREIGN=1; shift ;;
	--exclude-file) EXCLUDE_FILE="$2"; shift 2 ;;
	*) echo "unknown argument: $1" >&2; exit 2 ;;
	esac
done
[ -n "$STAGED" ] && [ -n "$REPO" ] && [ -n "$SUBDIR" ] || {
	echo "usage: $0 --staged DIR --repo DIR --subdir NAME [--evict-foreign] [--exclude-file PATH]" >&2
	exit 2
}

count_rpms() { find "$1" -name '*.rpm' ! -name '*.src.rpm' 2>/dev/null | wc -l; }

staged_count=$(count_rpms "$STAGED")
if [ "$staged_count" -eq 0 ]; then
	echo "ERROR: no RPMs staged in ${STAGED}; refusing to publish an empty wave" >&2
	exit 1
fi

# NEVER BREAK RDEPS: before anything is signed or moved, simulate the
# publish against the synced-down tree's own repodata — the served state —
# and refuse a wave that leaves a surviving package unresolvable (the
# glib2-Obsoletes hijack and the libnotify version trap are the incidents;
# scripts/check-reverse-deps.py carries the receipts). Entirely local:
# the staged dir gets a throwaway index, the served state is already on
# disk. A first-ever publish has no served repodata and nothing to break.
if [ -f "${REPO}/repodata/repomd.xml" ]; then
	createrepo_c --quiet "$STAGED" || createrepo_c "$STAGED"
	python3 "$(dirname "$0")/check-reverse-deps.py" \
		--wave-repo "$STAGED" --served-repo "$REPO"
	rm -rf "${STAGED}/repodata"
else
	echo "==> no served repodata in ${REPO}; first publish, reverse-dep gate skipped"
fi

mkdir -p "$REPO"

find "$STAGED" -name '*.rpm' ! -name '*.src.rpm' -exec rpmsign --addsign {} \;

# Which key IDs count as "ours": every subkey of the key rpmsign just used,
# because gpg signs with a signing SUBKEY and rpm reports that subkey's ID,
# not the primary's. Lower-cased to match rpm's pgpsig rendering.
signature_key_ids() {
	rpm -qp --nosignature --qf '%{RSAHEADER:pgpsig}|%{SIGPGP:pgpsig}|%{DSAHEADER:pgpsig}|%{SIGGPG:pgpsig}' "$1" 2>/dev/null \
		| tr '[:upper:]' '[:lower:]' | grep -o 'key id [0-9a-f]*' | sed 's/^key id //' | sort -u
}
evicted=0
if [ "$EVICT_FOREIGN" -eq 1 ]; then
	gpg_name=$(rpm --eval '%{?_gpg_name}')
	if [ -z "$gpg_name" ]; then
		echo "ERROR: --evict-foreign needs %_gpg_name (~/.rpmmacros) to know whose signature counts as ours" >&2
		exit 1
	fi
	mapfile -t publisher_keys < <(gpg --batch --with-colons --list-keys "$gpg_name" 2>/dev/null \
		| awk -F: '($1 == "pub" || $1 == "sub") && $5 != "" { print tolower($5) }' | sort -u)
	if [ "${#publisher_keys[@]}" -eq 0 ]; then
		echo "ERROR: gpg lists no key for %_gpg_name=${gpg_name}; refusing to decide what is foreign" >&2
		exit 1
	fi
	is_ours() {
		local id
		while IFS= read -r id; do
			local k
			for k in "${publisher_keys[@]}"; do
				[ "$id" = "$k" ] && return 0
			done
		done < <(signature_key_ids "$1")
		return 1
	}
	# The guard: rpmsign just signed the staged wave with this very key. If
	# that signature does not read as ours, the key-id derivation is wrong
	# and eviction would empty the tree. Stop here, touch nothing.
	# `-print -quit`, never `| head -n 1`: under `set -o pipefail` head closes
	# the pipe after the first line, find dies of EPIPE, and the pipeline's
	# non-zero status kills the script. That is not hypothetical -- it is how
	# the first real publish of this family died (run 33819328227:
	# "find: 'standard output': Broken pipe / find: write error"), after the
	# wave was already signed. It hid in testing because the race needs enough
	# files for find to still be writing when head exits; one staged RPM never
	# reproduces it, 345 always do.
	probe=$(find "$STAGED" -name '*.rpm' ! -name '*.src.rpm' -print -quit)
	if ! is_ours "$probe"; then
		echo "ERROR: freshly signed ${probe} is not recognised as signed by ${gpg_name}" \
		     "(publisher keys: ${publisher_keys[*]}; found: $(signature_key_ids "$probe" | tr '\n' ' '))" >&2
		echo "       refusing to evict anything: with that mismatch every package would look foreign" >&2
		exit 1
	fi
	while IFS= read -r f; do
		if ! is_ours "$f"; then
			echo "==> evicting foreign package: ${f} (signed by: $(signature_key_ids "$f" | tr '\n' ' ')" \
			     "-- not the publisher's key)"
			rm -f "$f"
			evicted=$((evicted + 1))
		fi
	done < <(find "$REPO" -name '*.rpm')
	[ "$evicted" -eq 0 ] || echo "==> evicted ${evicted} foreign RPM(s); the sync-up deletes them from the bucket"
fi

baseline=$(count_rpms "$REPO")
echo "==> staged ${staged_count} RPM(s); repo already holds ${baseline}"

mkdir -p "${REPO}/${SUBDIR}"

# ONE COPY PER FILENAME. The same wave reaches this job twice: once as the
# run's own upload-artifact (flat in staged/) and once from
# `oras-stage-artifacts.py pull-all`, which re-pulls what this very run
# staged (staged/oras/.factory/<cell>/artifacts/). Handing `cp -t` two
# sources with the same basename in one invocation makes it refuse the
# second -- "cp: will not overwrite just-created ..." -- and exit 1, which
# is how run 33823845082 died after evicting 358 foreign RPMs and signing
# 192 good ones. Whether it fires at all depends on whether the duplicates
# land in the same `-exec ... {} +` batch, so it is a latent coin flip for
# every publisher, not a property of this family.
#
# The two copies are the same bytes from the same build, and both were
# signed by the loop above, so either is correct; take the first and skip
# the rest by name.
declare -A _wave_seen=()
while IFS= read -r -d '' _staged_rpm; do
	_rpm_name=$(basename "$_staged_rpm")
	if [ -n "${_wave_seen[$_rpm_name]:-}" ]; then
		continue
	fi
	_wave_seen[$_rpm_name]=1
	cp "$_staged_rpm" "${REPO}/${SUBDIR}/${_rpm_name}"
done < <(find "$STAGED" -name '*.rpm' ! -name '*.src.rpm' -print0)
echo "==> placed ${#_wave_seen[@]} distinct RPM(s) from ${staged_count} staged file(s)"

# '^' joins '+': same worker, same failure. Fedora's snapshot-version
# convention puts '^' in filenames (quickshell-0.2.1^git…, signon-8.60^…);
# librepo and dnf percent-encode it to %5E, the worker looks up raw R2
# keys, and the file 404s. Found by simulate-buildroot-resolution.py's
# fetchability pass (~30 served files, every one HEAD-verified 404 at the
# encoded URL) -- and unlike '+', these are runtime packages the desktop
# lanes install with --skip-unavailable, so the failure is a silently
# thinner image rather than a red build.
find "$REPO" \( -name '*+*.rpm' -o -name '*^*.rpm' \) -print0 | while IFS= read -r -d '' f; do
	mv "$f" "$(dirname "$f")/$(basename "$f" | tr '+^' '..')"
done

# Runs AFTER the rename so a legacy 'foo+1.rpm' and a staged 'foo+1.rpm'
# compare as the same name rather than surviving as a stale duplicate.
superseded=0
while IFS= read -r staged_file; do
	name=$(basename "$staged_file")
	while IFS= read -r older; do
		echo "==> superseding older copy: $older"
		rm -f "$older"
		superseded=$((superseded + 1))
		# Excluded by PATH, not by comparing against the staged file's own
		# path: `find repo/ ...` and `find repo//build-chain ...` print the
		# same file under different strings, so a string compare would let a
		# trailing slash in --repo make the wave delete what it just staged.
	done < <(find "$REPO" -name "$name" ! -name '*.src.rpm' ! -path "*/${SUBDIR}/*")
done < <(find "${REPO}/${SUBDIR}" -name '*.rpm' ! -name '*.src.rpm')
[ "$superseded" -eq 0 ] || echo "==> superseded ${superseded} older copy/copies"

final=$(count_rpms "$REPO")
if [ "$final" -lt $((baseline - superseded)) ]; then
	echo "ERROR: repo shrank from ${baseline} to ${final} RPMs" \
	     "(${superseded} superseded); refusing to sync" >&2
	echo "       a sync from a smaller tree DELETES the difference from the bucket" >&2
	exit 1
fi
echo "==> repo now holds ${final} RPM(s)"

# WITHDRAWAL (tunaos-packages#525): one glob per line in EXCLUDE_FILE,
# produced by scripts/plan-withdrawn-excludes.py from
# manifests/withdrawn-packages.yaml. Passed straight to createrepo_c -x,
# which filters its directory walk BEFORE comparing against the old
# repodata it is updating -- a name-evr this run excludes is therefore
# dropped from the regenerated index exactly like a file that went missing,
# with the file itself untouched on disk. That keeps withdrawal invisible
# to the NEVER SHRINK guard above (it counts files, and none were removed)
# and reversible (delete the manifest entry, the next publish indexes the
# file back in).
excludes=()
if [ -n "$EXCLUDE_FILE" ] && [ -f "$EXCLUDE_FILE" ]; then
	while IFS= read -r pattern; do
		[ -z "$pattern" ] && continue
		case "$pattern" in '#'*) continue ;; esac
		excludes+=(-x "$pattern")
	done < "$EXCLUDE_FILE"
	if [ "${#excludes[@]}" -gt 0 ]; then
		echo "==> excluding ${#excludes[@]} withdrawn pattern(s) from repodata (files stay in the tree)"
	fi
fi

createrepo_c --update "${excludes[@]}" "$REPO" 2>/dev/null || createrepo_c "${excludes[@]}" "$REPO"

# Detached signature over repomd.xml, via the shared signer.
#
# scripts/sign-repomd.sh carries the reasoning and is called by every publisher
# that re-indexes a tree before syncing it up -- including the ones that do not
# go through this script. `rclone sync` makes the destination match the source,
# so a publisher that indexes without signing DELETES the signature a previous
# publisher wrote. One signer, called from every such path, is what keeps
# repo_gpgcheck=1 reachable. See #509.
bash "$(dirname "${BASH_SOURCE[0]}")/sign-repomd.sh" "$REPO"
