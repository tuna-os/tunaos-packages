#!/usr/bin/env bash
# Refresh zypper metadata, tolerating a mirror caught mid-rotation.
#
# openSUSE Tumbleweed publishes a new snapshot frequently, and
# download.opensuse.org is a REDIRECTOR to a fleet of mirrors that sync at
# different speeds. During a rotation a mirror can serve a repomd.xml that
# names files it has not received yet, and zypper refuses the whole
# repository:
#
#   Repository 'openSUSE-Tumbleweed-Oss' is invalid.
#   [repo-oss|http://download.opensuse.org/tumbleweed/repo/oss/] Failed to
#   retrieve new repository metadata.
#   History:
#    - File './repodata/dbe06c23…-appdata-icons.tar.gz' not found on medium
#   Some of the repositories have not been refreshed because of an error.
#   No provider of 'rpmlint' found.
#
# (tideforge-wayland-protocols-opensuse-tumbleweed-x86_64, run 32586260792,
# 16:57:59Z.) Measured afterwards: every one of the 20 files repomd.xml lists
# resolves 200, and so does that exact appdata-icons.tar.gz. Nothing was
# wrong with the repository or with us — the mirror simply did not have the
# file yet at that second.
#
# What a retry does and does not buy — corrected after watching it fail:
#
#   clean --metadata zypper caches the repomd it already fetched. Retrying
#                    without clearing it re-reads the same index naming the
#                    same absent file, forever. The clean is what makes the
#                    next attempt genuinely new, and it is why a short retry
#                    recovers a BRIEF skew.
#   the redirector   a second attempt CAN land on a different mirror, but the
#                    routing is sticky enough that it usually does not. All
#                    four attempts failed identically in run 32593296994.
#
# So this handles the brief case and honestly fails the persistent one, which
# is the correct behaviour: a repository whose index names files no reachable
# mirror serves is broken, and pretending otherwise would publish against a
# package universe we could not actually read.
#
# The same posture scripts/pull-container-image.sh takes for transient
# registry failures. A real breakage still fails: the attempts are bounded
# and the last failure's own output is what the log ends on.
set -euo pipefail

attempts=${ZYPPER_REFRESH_ATTEMPTS:-4}
delay=${ZYPPER_REFRESH_DELAY:-5}

for attempt in $(seq 1 "$attempts"); do
  if zypper --non-interactive --gpg-auto-import-keys refresh; then
    [ "$attempt" -eq 1 ] || echo "zypper refresh succeeded on attempt ${attempt}" >&2
    exit 0
  fi
  if [ "$attempt" -eq "$attempts" ]; then
    echo "ERROR: zypper refresh failed ${attempts} times." >&2
    echo "       If the log says a repodata file was 'not found on medium'," >&2
    echo "       this is upstream MIRROR SKEW, not a fault in this build:" >&2
    echo "       download.opensuse.org serves repomd.xml from one source and" >&2
    echo "       redirects the DATA FILE request to a mirror that may be on a" >&2
    echo "       different snapshot and has deleted the file repomd names." >&2
    echo "       Retrying cannot fix that — it re-lands on the same pair." >&2
    echo "       Measured on 2026-08-22 for one appdata-icons.tar.gz:" >&2
    echo "         download.opensuse.org  rev 1787317599  names it  404" >&2
    echo "         cdn.opensuse.org       rev 1787317599  names it  404" >&2
    echo "         mirror.umd.edu         rev 1787377763  moved on  404" >&2
    echo "         ftp.halifax.rwth-...   rev 1787317599  names it  200" >&2
    echo "       It resolves when the mirrors converge. Re-run then." >&2
    exit 1
  fi
  echo "zypper refresh failed (attempt ${attempt}/${attempts});" \
       "clearing cached metadata, retrying in ${delay}s" >&2
  zypper --non-interactive clean --metadata || true
  sleep "$delay"
  delay=$((delay * 2))
done
