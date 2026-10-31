# Package build failures

Candidates are not published supply. Keep failed attempts and check each
architecture against its native base before publication.

## Pinned factory runner exits 126

**Symptom:** A consumer checks out an immutable commit, then reports
`env: 'scripts/run-package-factory-cell.sh': Permission denied`.

**Cause:** Git tracks the runner as mode `100644` in commit
`c0998b94bd7b4fbc67360c239e439de6debf539b` and its parent.
Factory workflows use `bash` and do not need an executable file.
Consumers that execute the script through its shebang need that mode.

**Fix:** Track the runner as executable (`100755`).
Consumers can execute it directly or use Bash.
Pin a commit with this fix. Do not change source hashes or chmod downloaded sources.
The Bats test executes the runner through `env`.

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

## Mock bootstrap cannot read its candidate key

**Symptom:** Native Alma canaries report Curl37 for `/keys/candidate-public.gpg`
([run 38080413978](https://github.com/tuna-os/tunaos-packages/actions/runs/38080413978)).

**Cause:** Mock excludes the bind-mount plugin from bootstrap. A copy of that configuration
does not expose the key there. Mock mounts the roots of local repositories before DNF starts.

**Fix:** Stage only the public key in the signed local repository and reference
that mounted path. Keep the checks for package and metadata signatures enabled. Native CI
canaries must prove this repair.

## COSMIC Debian session installs without its desktop

**Symptom:** The session package installs, but its required siblings are absent.

**Cause:** `cosmic-session` declared empty Debian and Ubuntu runtime lists.
The single-package gate could not resolve siblings, so the declaration omitted
them. That did not prove a usable desktop.

**Fix:** Declare the versioned session siblings and launcher dependencies in
control files for Debian. Both queues include all providers. The complete candidate
installation must pass with signatures before this supply becomes ready.

## Snapshot artifact lookup fails

**Symptom:** [Resume run 38081590705](https://github.com/tuna-os/tunaos-packages/actions/runs/38081590705) fails before verification.

**Cause:** Repository-wide artifact metadata returns invalid JSON.

**Fix:** Query exact workflow and source runs, then their artifacts. Retain authentication and the shared request limit.

## Portable payload digest changes after extraction

**Symptom:** `just check` fails
`test_tideforge_intermediate.py::test_one_payload_digest_is_reused_by_two_target_plans`
with expected tree digest
`898945fe81d0a9aa5cd778e6cffb801cdca4b2a4a5fcffde88cdfbbc25836959`
but actual digest
`b71256f1f4aaf8dc42af9e95b3843f3b70e3869366167c0a8ae90cf31e6e3107`.

**Cause:** The retained failing archive inventories three directories as `0775`
and its `built once\n` message as `0664`. Python's `tarfile` data filter clears
group-write from the message, extracting it as `0644`; it also ignores archived
directory permissions and instead uses the extraction process's umask. The
inventory includes permissions, so a byte-identical payload can fail verification.

**Fix:** Verification and carrier extraction share `extract_payload`, which
retains ordinary `0777` permission bits after the data filter's path, link, file
type and ownership checks. Creation and extraction reject setuid, setgid and
sticky modes instead of silently normalizing them. The consumer regression
explicitly stages `0775` directories and a `0664` file, tests a different verifier
umask and read-only file permissions, and retains rejection coverage for unsafe
archive members. Do not replace the expected digest or drop modes from the
inventory.
