# Alma candidate builds

Candidates are not published supply. Keep failed attempts and check each
architecture against its native base before publication.

## Old partial RPMs in a new candidate

**SYMPTOM:** The candidate initializer reports that its repository is not empty.
[CI run 38073315753](https://github.com/tuna-os/tunaos-packages/actions/runs/38073315753)
failed at this check.

**CAUSE:** The workflow restored RPMs from an earlier attempt. A new attempt
uses a new signing key; those RPMs are not authenticated inputs.

**FIX:** Initial Alma builds start with an empty repository. Resuming requires
an authenticated immutable dependency snapshot. Keep the earlier artifacts.

## Kitten release package

**SYMPTOM:** Mock cannot find `almalinux-release` in Kitten BaseOS.
[CI run 38074178800](https://github.com/tuna-os/tunaos-packages/actions/runs/38074178800)
reached the native repository and failed during bootstrap.

**CAUSE:** Kitten supplies `almalinux-kitten-release`.

**FIX:** Use that package in both Kitten architecture configurations.

## Missing CPU proof

**SYMPTOM:** A signed installation succeeds, but CPU collection fails.

**CAUSE:** Installation does not prove that every binary and linked dependency
satisfies the requested CPU baseline.

**FIX:** Read `collection.json` and `verifier-result.json` in the CI evidence
artifact. Repair the reported input or dependency; retain the failing gate.
The public key, signed RPM bytes and compiler observation must survive failure.
# COSMIC Debian session installs without its desktop

**Symptom:** The session package installs, but its required siblings are absent.

**Cause:** `cosmic-session` declared empty Debian and Ubuntu runtime lists.
The single-package gate could not resolve siblings, so the declaration omitted
them. That did not prove a usable desktop.

**Fix:** Require the versioned session siblings and launcher dependencies in
native Debian control files. Both queues include all providers. Signed complete
candidate installation must pass before this supply becomes ready.

