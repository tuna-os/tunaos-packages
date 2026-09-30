# RFC 012: From the ask to a working stack

- **Status:** Proposed
- **Owner:** unassigned
- **Interacts with:**
  - RFC 011 (`docs/rfc/rfc011-unified-gap-driven-factory.md`)
  - `docs/PACKAGE_FACTORY.md`
  - `manifests/package-factory.yaml`
  - `.github/workflows/build-chain-fanout.yml`
  - tunaOS `.github/green-criteria.yml`
  - tunaOS `build_scripts/checks/verify-desktop-experience.sh`
  - tunaOS `.github/workflows/reusable-build-image.yml`

## The deliverable is a stack, not a repository

Users want a bootable image from "gnome 51 on hummingbird".
tunaOS defines this goal in `.github/green-criteria.yml` for each variant and flavor:

| criterion | proves | enforcement |
| --- | --- | --- |
| `builds` | the image builds and reaches its published tag | blocking |
| `desktop` | the declared desktop is present and startable | blocking |
| `boots` | a QEMU boot emits `TUNAOS_DESKTOP_CONTRACT_OK` on ttyS0 | blocking |
| `no_silent_omissions` | the build reports every dropped package | blocking |

Every stage of the chain exists today.
The packages repo generates the build tree, builds the packages, and publishes them to `repo.tunaos.org`.
tunaOS points the hummingbird image at that prefix, and `build-hummingbird.yml` builds the image.
Finally, the contract sweep and the Gate judge the result.

Nothing joins these stages.
The packages repo does not know what the image needs.

## Problem

Today, a person runs the desktop bringup loop by hand.
The GNOME 51 on hummingbird run on 2026-08-28 shows the problem.
Most steps were mechanical:

| What happened | How long | Was a decision needed? |
| --- | --- | --- |
| Dispatch a fan-out wave | seconds | no |
| Wait for it | 3–4 h | no |
| Read the served count from the live index by hand | minutes | no |
| Read eight failed packages, find that seven were one cascade | ~20 min | no |
| Decide that the pango wall in gtk4 needed a spec bump | minutes | **yes** |
| Cancel a superseded wave, dispatch the next | minutes | no |
| Repeat, four times | all night | — |

Two of the waves stopped early and published no artifacts.
The publish job needs `band4-x86` and `band4-arm`.
A wave that fails at band 1 discards three hours of build work.
The third wave rebuilt from the same 580 served packages as the first wave.

Three problems exist:

0. **Repository goal instead of stack goal.**

   On 2026-08-28, the repository had 580 of 673 packages in build order.
   But it had only 3 of 10 packages needed for a GNOME session.
   A loop that optimizes the first number spends waves on `vdirsyncer` and `hplip`.
   At the same time, `gdm`, `gnome-shell`, `gnome-session`, and `nautilus` are missing.
   These waves increase the package count, but do not help the stack.

   The tunaOS contract check shows the result:

   > tunaOS run 32813037866 (2026-08-25): the image carried 410 packages.
   > It had `gnome-backgrounds` and `gnome-user-docs`.
   > It had no `gnome-shell`, no `gdm`, no `mutter`, and no `gtk4`.
   > The check passed.
   > The boot gate failed 15 minutes later on a missing marker.
   > Upstream dropped the packages (tunaos-packages#519).

   `IS_HUMMINGBIRD` waives requirements so hummingbird can bootstrap.
   Because of this waiver, the image build cannot detect missing packages early.
   It builds an empty desktop and fails in the Gate 15 minutes later.
   This repository can detect missing packages in seconds before dispatch.

1. **There is no way to state the request.**

   The gap engine creates the build tree from measurement.
   It reads the live index of the target.
   It computes the transitive closure of desktop roots against a reference.
   It subtracts packages that the target ships.
   Then it tiers the residue by real BuildRequires.

   The engine generates this build tree automatically.
   A user cannot request "gnome 51 on hummingbird" directly.
   Six files must match before the engine produces the tree.
   A missing entry fails silently, and the cache serves an old build.
   Also, `el10` has no `gap_measurement`, so its build orders remain manual.

2. **Nothing continues automatically.**

   A wave is only one wave, not a full bringup.
   All parts to converge exist (skip served NVR, partial resume, budget deferral).
   However, no controller measures, dispatches, and detects progress.

3. **Every run starts cold.**

   `MOCK_CACHE_DIR` uses `runner.temp`.
   Each job rebuilds the local repository.
   Only the published index survives across waves.
   Fixing one spec costs a full wave.
   Rebuild of the minimal buildroot takes 34.1% of mock time (`docs/hummingbird-throughput.md`).

## Design

Three pieces work together. Each piece is also usable alone.

### 1. The request is the front door

`scripts/build_request.py` resolves `"gnome 51 on hummingbird"`.
It reads the target contract, roots manifest, and `package-builds.yaml`.
All returned data comes from these files.
Adding a target needs only a contract block, not code changes.

`scripts/request.py` provides the CLI (`just want "gnome 51 on hummingbird"`).
The `--measure` flag reads the live index.
It reports the status: **580 of 673 served, 93 to go.**

A release that is missing from the roots manifest is a move.
The request reports it as a move.
Six files name `src/gnome-51` across three categories:

| Category | Files | What `--adopt` does |
| --- | --- | --- |
| Mechanical | roots manifest, `package-builds.yaml`, `catalog.yaml`, fan-out epoch | moves all of them, or none |
| Decided | `manifests/dependency-trees/gnome.yaml` (`stable:` and `next:`) | reports it, never rewrites it |
| Historical | comments for issue #542 | never touches them |

`tests/test_a_release_move_touches_every_declaration.py` verifies this table.
It ensures that release moves do not stay partial.

### 2. The objective is the stack, in ordered stages

`scripts/stack_readiness.py` divides wanted packages into three ordered stages.
The first open stage decides the status:

| stage | what it is | why it comes first |
| --- | --- | --- |
| `contract` | desktop `required_packages` | tunaOS `desktop` criterion fails without these |
| `session` | `install_packages` minus contract | portals, keyring, and session units needed for a session |
| `order` | remaining packages in build order | the tail |

Live state on 2026-08-28:

```
contract     3/10    remaining 7   gdm, gnome-control-center, gnome-initial-setup,
                                   gnome-session, gnome-shell, nautilus,
                                   xdg-desktop-portal-gnome
session     30/48    remaining 18
order      562/638   remaining 76
```

Evaluation of stages in order prevents false progress.
The system will not report success while `gdm` is absent.

**Limits of name checks.**
Readiness tools check the names of packages, not the versions of packages.
For example, the index serves `gtk4` 4.22.1, but `gnome-shell` needs `>= 4.23`.
`gtk4` looks satisfied, but dependent packages fail to link.
`scripts/simulate-buildroot-resolution.py` evaluates versions and dependencies.
Adding it as a fourth stage is the next step.
Until then, a closed contract stage does not guarantee a buildable stack.

### 3. The loop stops itself

`.github/workflows/converge.yml` runs a loop:
measure -> wave -> measure -> wave -> measure -> wave -> report.
Each wave uses the standard fan-out workflow (`build-chain-fanout.yml`).
Between waves, `scripts/plan-converge.py` checks the published index:

```
every stage closed         packages-ready
the open stage advanced    continue   the wave moved the STACK
the open stage stood still blocked    rebuilding cannot fix this
waves spent                budget     report the residue
```

The tool checks the published index, not shard results.
A green shard only shows that packages built, not that users can install them.

The terminal state is `packages-ready`, not `done`.
This repository cannot verify that a desktop boots.
Only the tunaOS Gate can verify boot.
This design stops builds when the contract stage has missing packages.

`blocked` shows that rebuilds cannot make further progress.
The loop stops and hands work to developers.

### 4. The handoff gives one blocker per root cause

`scripts/classify-chain-failures.py` converts failures into blockers.
It separates root causes from cascade failures.
On 2026-08-28, eight packages failed: one root blocker and seven dependents.
`gtk4` failed to build on pango 1.57.
Seven packages failed because they depended on `gtk4` or `mutter-devel`.
The script identifies the root cause of failures from build logs.

The tool classifies roots into known categories:
`chain-infra`, `spec-changelog`, `unconditional-test-buildrequires`,
`version-blocked`, `unsatisfied-buildrequires`, `patch-rejected`,
`compile-error`, and `no-output`.
It also supports `unclassified` and `not-reached`.

The tool takes the residue list from measurement, not from log text.
It includes packages from truncated logs and unreached packages.

Unreached packages are not bugs.
On hummingbird, shards never reached 100 of 101 residue packages.
Reports of unreached packages as blockers hide the real issue.
The next wave will build unreached packages.

### 5. The warm host

`scripts/warm-builder.sh` runs builds on a stateful host.
`build-chain.sh` skips packages that exist in the local repository.
It also reuses the mock cache when `MOCK_CACHE_DIR` is set.
Persistent storage enables the reuse of caches across runs:

```
first run     builds the chain, banks every RPM
spec fix      --forget gtk4  drops what that source package produced
next run      rebuilds gtk4 and its dependents; skips the other 580
```

`--forget` removes only matching packages.
`%{SOURCERPM}` identifies matching artifacts accurately.
A fallback covers hosts without `rpm`.

The warm host never publishes packages.
Its local repository is a scratch workspace.
CI publishes packages from clean runners.

## Where KubeStellar Hive fits

Hive runs agents on backlogs.
Triage labels an issue.
A fix agent makes changes in a worktree and opens a pull request.
A deterministic gate runs tests and linters.
Reviewer agents check for regressions.
Admins raise the level of autonomy based on test coverage.

This repository fits Hive well.
Blockers have clear types, and types show whether fixes are mechanical.

| Class | Fix | Suits an agent? |
| --- | --- | --- |
| `spec-changelog` | remove hand-written entries under `%autochangelog` | yes — mechanical |
| `chain-infra` | fix the builder script | yes |
| `patch-rejected` | refresh the patch against source | usually |
| `unsatisfied-buildrequires` | add provider to order or pin repo | sometimes |
| `unconditional-test-buildrequires` | patch `%bcond` or add dependency | **no** — needs a human |
| `version-blocked` | bump provider and broken packages | **no** |
| `compile-error` | fix the source code | **no** |
| `unclassified` / `not-reached` | inspect log without guessing | **no** |

The integration follows these steps:

1. `converge.yml` creates one issue per request and refreshes it.
   It adds labels `package-factory` and `hive`.
2. The issue body contains the classified blocker list.
3. Autonomy increases per class. It begins with `spec-changelog` and `chain-infra`.
4. Hive uses `required-checks` (2000+ tests and linters in ~2 minutes).
   Merge gates do not run full factory builds.

`docs/FACTORY-STATUS.md` tracks progress with days-without-movement metrics.
The convergence issue records the count of served packages.

## What this does not do

- **It does not make `el10` answerable to a request.**
  `el10` has no `gap_measurement`.
  Its build orders remain manual.
  Adding `gap_measurement` needs a contract block and roots manifest.
  It should start in `mode: exhibit` to compare generated orders.

- **It does not generalize the fan-out.**
  `build-chain-fanout.yml` targets hummingbird cells only.
  Workflow converge inherits this scope.

- **It does not publish per band.**
  A wave publishes once after band 4 on both architectures.
  A failure at band 1 discards earlier builds.
  Per-band publish needs careful concurrency controls in GitHub Actions.
  A separate change will address this feature.

## Plan of attack

| # | Step | Status |
| --- | --- | --- |
| 1 | Request front door and release-move safety | this RFC change |
| 2 | Stack readiness stages (contract first) | this RFC change |
| 3 | Convergence loop with `packages-ready` verdict | this RFC change |
| 4 | Blockers classification and single issue per request | this RFC change |
| 5 | Warm host for bringup | this RFC change |
| 6 | Cross-repo handoff to tunaOS image build | not started |
| 7 | Version-aware readiness checks | not started |
| 8 | Build contract closure first | not started |
| 9 | Publish per band | not started |
| 10 | `gap_measurement` for `el10` | not started |
| 11 | Raise Hive autonomy per class | not started |

Steps 6 and 8 remain open.
Step 6 connects `packages-ready` to the tunaOS image build workflow.
Step 8 updates `build-chain.sh` to build contract packages before other stages.

## Risks

- **A closed contract stage does not guarantee build success.**
  Name checks do not verify package versions (`gtk4` 4.22.1 vs `>= 4.23`).
  `stack_readiness.py` documents this limit.
- **Residue reported as bugs.**
  The build never reached most unserved packages.
  When unreached packages count as blockers, they hide true failures.
- **Convergence dispatches into a wall.**
  The `blocked` state prevents runaway waves.
- **Premature done state from unreadable index.**
  When the tool cannot read the index, it yields `blocked` instead of `packages-ready`.
- **Cascade misattributed to a prefix.**
  Name matching uses strict boundaries to separate packages like `gtk4` and `gtk4-layer-shell`.
- **Partial release move.**
  The classification table and tests prevent partial moves.
- **Warm host drift.**
  The warm builder takes configurations from `package-builds.yaml` and never publishes directly.
