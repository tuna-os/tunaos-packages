# RFC 012: From the ask to a working stack

**Status:** Proposed
**Owner:** unassigned
**Interacts with:** RFC 011 (`docs/rfc/rfc011-unified-gap-driven-factory.md`,
the gap engine this rides), `docs/PACKAGE_FACTORY.md` (promotion contract),
`manifests/package-factory.yaml` (target contract),
`.github/workflows/build-chain-fanout.yml` (the wave),
tunaOS `.github/green-criteria.yml` (**what "working" means**),
tunaOS `build_scripts/checks/verify-desktop-experience.sh`,
tunaOS `.github/workflows/reusable-build-image.yml` (the boot Gate)

## The deliverable is a stack, not a repository

The thing anyone wants from "gnome 51 on hummingbird" is an image that boots
into a usable session. tunaOS already defines exactly that, per (variant,
flavor) cell, in `.github/green-criteria.yml`:

| criterion | proves | enforcement |
| --- | --- | --- |
| `builds` | the image builds and reaches its published tag | blocking |
| `desktop` | the declared desktop is present and startable | blocking |
| `boots` | a QEMU boot emits `TUNAOS_DESKTOP_CONTRACT_OK` on ttyS0 | blocking |
| `no_silent_omissions` | nothing was dropped without being surfaced | blocking |

**Every stage of the chain already exists.** The packages repo generates the
build tree, builds it, and publishes to `repo.tunaos.org`;
tunaOS's `build_scripts/10-base-packages.sh` points the hummingbird image at
that exact prefix; `build-hummingbird.yml -f flavor=gnome` builds the image;
the contract sweep and the Gate judge it. Nothing joins them, and — the part
that costs whole runs — **the packages side has no idea what the image side
needs.**

## Problem

Bringing a desktop up on a target is, today, a human driving a loop by hand.
The GNOME 51 / hummingbird night of 2026-08-28 is the record, and every step
of it was mechanical except three:

| What happened | How long | Was a decision needed? |
| --- | --- | --- |
| Dispatch a fan-out wave | seconds | no |
| Wait for it | 3–4 h | no |
| Read the served count off the live index by hand | minutes | no |
| Read eight failed packages, establish that seven were one cascade | ~20 min | no |
| Decide gtk4's pango wall needed a spec bump | minutes | **yes** |
| Cancel a superseded wave, dispatch the next | minutes | no |
| Repeat, four times | all night | — |

Two waves were cancelled mid-flight and published *nothing* — the publish job
needs `band4-x86` and `band4-arm`, so a wave that dies at band 1 discards three
hours of build. The third wave then rebuilt from the same 580 served packages
the first had started from.

Three separable problems, and only one of them is about building:

0. **The objective was the repository, not the stack.** Measured against the
   live hummingbird index on 2026-08-28, the same repo on the same day reads
   580/673 (86%) over the build order and **3/10** over the packages a GNOME
   session requires. A loop optimising the first number spends waves on
   `vdirsyncer` and `hplip` while `gdm`, `gnome-shell`, `gnome-session` and
   `nautilus` are absent, and every one of those waves moves 580/673 upward
   and moves the stack not at all.

   This is not hypothetical. tunaOS's own contract check records the outcome:

   > Measured on tunaOS run 32813037866 (2026-08-25): the image carried 410
   > packages — `gnome-backgrounds` and `gnome-user-docs`, no `gnome-shell`,
   > no `gdm`, no `mutter`, no `gtk4` — and this check called it passed. The
   > boot gate then failed 15 minutes later on a marker that could never be
   > emitted, because the packages were dropped upstream
   > (tunaos-packages#519).

   `IS_HUMMINGBIRD` waives every requirement there so hummingbird can
   bootstrap against incomplete repos — a deliberate policy, and a human's to
   change. Its consequence is that the image build *cannot* tell you the repo
   is not ready. It builds an empty desktop and the Gate discovers it a
   quarter of an hour later. **This side can answer it in seconds, before
   anything is dispatched.**

1. **There is no way to say the ask.** The gap engine already generates the
   build tree from measurement — it reads the target's live index, takes the
   transitive closure of the desktop's roots against a reference, subtracts
   what the target ships, and tiers the residue by real BuildRequires. That is
   the buildtree, and it is generated, not curated. But "gnome 51 on
   hummingbird" is not something you can *say*: it is a property that must
   already be true across six files before the engine will produce it, and
   missing one of the six is silent (an un-listed `source_paths` entry does not
   re-key its cell, so the cache serves output built from the old spec).
   Meanwhile `el10` declares no `gap_measurement` at all, which is exactly why
   `build-order-gnome51.yml` and `build-order.yml` are hand-curated.

2. **Nothing keeps going.** A wave is not a bringup; it is one wave. Every
   piece needed to converge exists — the served-NVR skip, partial resume,
   budget deferral, `!cancelled()` collection — and there is no controller that
   measures, dispatches, re-measures, and knows the difference between "still
   working" and "stuck".

3. **Every attempt starts cold.** `MOCK_CACHE_DIR` is `runner.temp`, the local
   repo is rebuilt per job, and the only thing that survives a wave is the
   published index — which a fan-out updates once, at the end. So the
   fix-one-spec-and-retry loop costs a wave, and rebuilding the same minimal
   buildroot is 34.1% of all mock time (`docs/hummingbird-throughput.md`
   Finding 2) on top.

## Design

Three pieces, each usable without the others.

### 1. The request is the front door

`scripts/build_request.py` resolves `"gnome 51 on hummingbird"` against the
target contract, the roots manifest it names, and `package-builds.yaml`.
Everything it returns comes out of those files, so adding a target stays a
contract block rather than a code change — the rule
`scripts/factory/factory_contract.py` already enforces on the other side of the same
manifests. `scripts/request.py` is the CLI (`just want "gnome 51 on
hummingbird"`), and `--measure` reads the live index and answers the only
number that matters: **580 of 673 served, 93 to go.**

A release the roots manifest does not declare is not a typo, it is a **move**,
and the request reports it as one. The six files that name `src/gnome-51` fall
into three categories, and the difference is load-bearing:

| Category | Files | What `--adopt` does |
| --- | --- | --- |
| Mechanical | roots manifest, `package-builds.yaml`, `catalog.yaml`, the fan-out's epoch derivation | moves all of them, or none |
| Decided | `manifests/dependency-trees/gnome.yaml` — a `stable:`/`next:` table where a real move shifts both rows | reports it, never rewrites it |
| Historical | comments recording #542's downgrade and the guards against it | never touched |

`tests/test_a_release_move_touches_every_declaration.py` asserts that no
tracked file naming a live track escapes all three categories, so the table
cannot silently go stale — which is the only way a move goes partial.

### 2. The objective is the stack, in ordered stages

`scripts/stack_readiness.py` partitions every wanted name into three ordered
stages, and **only the first open one decides anything**:

| stage | what it is | why it comes first |
| --- | --- | --- |
| `contract` | the desktop's `required_packages` | tunaOS's `desktop` criterion hard-fails without these, and the Gate cannot emit its marker |
| `session` | `install_packages` minus the contract | the portals, keyring, gvfs backends and session units that make a session *usable* rather than merely startable |
| `order` | everything else the build order carries | the tail |

Live, 2026-08-28:

```
contract     3/10    remaining 7   gdm, gnome-control-center, gnome-initial-setup,
                                   gnome-session, gnome-shell, nautilus,
                                   xdg-desktop-portal-gnome
session     30/48    remaining 18
order      562/638   remaining 76
```

A stage that is scored but not *ordered* would still let the tail read as
progress. Judging on the open stage is what makes "580/673 and climbing" stop
reading as success while gdm is absent.

**The limit of a name.** Readiness is measured by name, and a name is
necessary but not sufficient: the index serves `gtk4` at 4.22.1 against a
gnome-shell needing ≥ 4.23, so `gtk4` reads as satisfied while nothing built
against it can link. That is the pango 1.57/1.58 wall, and it is not peculiar
to this module — tunaOS's contract check "deliberately validates names", and
the gap engine resolves by name too. `scripts/simulate-buildroot-resolution.py`
already evaluates versioned and boolean dependencies against the real
buildroot repo set; wiring its verdict in as a fourth stage is the natural
next increment, and until then a closed contract stage must not be read as a
buildable one.

### 3. The loop stops itself

`.github/workflows/converge.yml` is measure → wave → measure → wave →
measure → wave → report. Each wave is the *same* fan-out a hand dispatch
runs (`build-chain-fanout.yml` gained `workflow_call`; its inputs are
identical, asserted by test). Between waves, `scripts/plan-converge.py`
measures against the **published index** and decides:

```
every stage closed         packages-ready
the open stage advanced    continue   the wave moved the STACK
the open stage stood still blocked    rebuilding cannot fix this
waves spent                budget     report the residue
```

The index, not the wave's own result, because a green shard says its packages
built — which says nothing about whether anyone can install them, and is
exactly the direction #519 got wrong.

The terminal verdict is deliberately **not** called `done`. Nothing on this
side proves a desktop boots; only tunaOS's Gate can. What this side asserts is
the negative that was costing whole runs: while the contract stage is open, an
image build *cannot* boot into a session.

`blocked` is the point of the whole design. It is the loop reaching the edge
of what rebuilding can fix, and it hands over rather than burning runners on a
wall.

### 4. The handoff is one blocker per root cause

`scripts/classify-chain-failures.py` turns the residue into blockers. Its one
non-obvious job is the cascade split, and the 2026-08-28 wave is the
motivating case: eight red packages, **one blocker and seven dependents**.
gtk4 could not build against pango 1.57; libadwaita, mutter, gnome-shell,
nautilus, xdg-desktop-portal-gnome, gnome-control-center and
gnome-initial-setup build against gtk4 or mutter-devel and never had a chance.
Establishing that took a person twenty minutes; it is now a function of the
logs.

Roots are then classified into the classes this factory has actually paid for
— `chain-infra`, `spec-changelog`, `unconditional-test-buildrequires`,
`version-blocked`, `unsatisfied-buildrequires`, `patch-rejected`,
`compile-error`, `no-output` — plus `unclassified` and `not-reached`, which
are first-class outcomes. A classifier that always answers would assert a
cause from log-line proximity at scale, and this repo has already made that
exact mistake once in writing.

The residue list comes from the measurement rather than from scraping
`Failed packages` out of a job log: a failure whose line scrolled past a
truncated tail is still in the residue, and so is a package no shard ever
reached.

That last case is most of it, and it must not be reported as a bug. The live
hummingbird residue is 101 names of which one had a failure log; counting
`not-reached` as a blocker reports "101 blockers" and buries the one that is
real. Nothing is wrong with an unreached package — the next wave builds it.

### 5. The warm host

`scripts/warm-builder.sh` runs a cell on a host that remembers. Nothing new is
needed in the builder — `build-chain.sh` already skips a package whose exact
NVR sits in the local repo, and already shares one mock root cache when
`MOCK_CACHE_DIR` is set. Both are per-run only because the *directories* are
per-run. Point them at a persistent volume:

```
first run     builds the chain, banks every RPM
spec fix      --forget gtk4  drops what that source package produced
next run      rebuilds gtk4 and its dependents; skips the other 580
```

`--forget` has to be exact in both directions, and both errors are silent:
dropping only the main binary leaves `gtk4-devel` from the failed build for
every dependent to compile against, and dropping by bare prefix takes
`gtk4-layer-shell` with it. `%{SOURCERPM}` answers exactly; the
version-release fallback covers a host without `rpm(1)`.

The warm host **never publishes**. Its local repo is a bringup workspace, not
a repository anyone consumes — promotion stays with the gated publishers, for
the reasons `INCIDENT-repo-wipe-gnome.md` records. Get the chain green warm,
then let CI build and publish it from a clean runner, where the result is
reproducible rather than merely present.

## Where KubeStellar Hive fits

Hive runs a fleet of agents over a backlog: triage labels an issue, a fix
agent writes the change in an isolated worktree and opens a PR, a deterministic
coverage gate runs build/lint/test, reviewer agents check for regressions, and
merge happens according to an autonomy level an admin raises by hand. Its own
framing is that *coverage*, not model choice, earns the next level.

That is a good fit here for one specific reason: **this repo's blockers are
already typed, and the type says whether the fix is mechanical.**

| Class | Fix | Suits an agent? |
| --- | --- | --- |
| `spec-changelog` | delete hand-written entries under `%autochangelog` | yes — mechanical, and #572 did exactly this across 25 specs |
| `chain-infra` | fix the builder | yes — #576 was two `rm -rf` lines |
| `patch-rejected` | refresh the patch against the imported source | usually |
| `unsatisfied-buildrequires` | add the provider to the order, or pin the repo | sometimes |
| `unconditional-test-buildrequires` | a packaging decision: patch in a `%bcond`, or carry the test dependency | **no** — needs a human |
| `version-blocked` | bump the provider, and everything it breaks | **no** — this is the pango wall |
| `compile-error` | read the code | **no** |
| `unclassified` / `not-reached` | read the log; do not guess | **no** |

So the integration is not "point Hive at the repo". It is:

1. `converge.yml` files **one issue per request**, refreshed rather than
   duplicated, labelled `package-factory` and `hive`. A backlog full of
   near-identical build reports is worse than none — nobody, human or agent,
   can tell which is current.
2. The issue body carries the classified blocker list, so triage is reading a
   field rather than a log.
3. Hive's autonomy is raised **per class**, not per repo: start with
   `spec-changelog` and `chain-infra` at the level where an agent may open a
   hold-gated PR, and leave `version-blocked` and `compile-error` advisory
   until the evidence says otherwise.
4. The coverage gate Hive wants already exists and is already the right one:
   `required-checks` — 2000+ tests and five linters, ~2 minutes. The Package
   Factory cells are deliberately *not* in it (see
   `tests/test_the_factory_is_not_a_merge_queue_gate.py`), which is what makes
   an agent-driven PR loop viable at all: a 2.5-hour gate behind a 1-hour
   queue timeout cannot pass, and #567 proved it three times in one night.

The honest caveat: nothing here has been run against Hive. The claim is that
the *interface* — a typed, deduplicated, per-class blocker feed with a fast
deterministic gate behind it — is what an agent fleet needs, and that this repo
now has it. Whether the fleet closes the mechanical classes is measurable, and
the measurement is already published: `docs/FACTORY-STATUS.md` reports
built-vs-needed per target with days-without-movement, and the convergence
issue carries the served count each run.

## What this does not do

- **It does not make `el10` answerable to a request.** el10 declares no
  `gap_measurement`, so its build orders stay hand-curated and `request.py`
  says so rather than pretending. Adding one is a contract block plus a roots
  manifest — configuration, not a fork of the engine — and it should land as
  `mode: exhibit` first, so the generated order can be diffed against the
  curated one before anything trusts it. That is the same discipline the Fedora
  XFCE conversion row is following.

- **It does not generalise the fan-out.** `build-chain-fanout.yml` is scoped to
  the hummingbird cells and says so; converge inherits that scope. Generalising
  is that file's band blocks (GitHub Actions YAML has no anchors), not this
  design.

- **It does not publish per band.** This is the biggest remaining structural
  win and it is deliberately out of scope here, because the hazard is real. A
  wave publishes once, after band 4 on both arches, so a cancel at band 1
  discards everything. Publishing each band's *new* RPMs (the
  `fanout-new-<cell>-b<N>-s*` artifacts are already exactly that set) would
  make every band permanent. The hazard is concurrency: GitHub keeps at most
  one running plus one pending job per concurrency group and **cancels a
  second pending one**, so ten band-publishes contending for `publish-rpms`
  would cancel each other. The shape that avoids it is one publish job per
  band covering both arches, with band N+1 gated on band N's publish — which
  also improves cross-shard dependency resolution, at the cost of a sync per
  band on the critical path. It needs its own change, with its own guards.

## Plan of attack

| # | Step | Status |
| --- | --- | --- |
| 1 | Request front door + release-move safety | this RFC's change |
| 2 | Stack readiness as the objective — ordered stages, contract first | this RFC's change |
| 3 | Convergence loop stopping on the open stage, terminal `packages-ready` | this RFC's change |
| 4 | Typed blocker classification and one refreshed issue per request | this RFC's change |
| 5 | Warm host for the bringup loop | this RFC's change |
| 6 | **Cross-repo handoff**: `packages-ready` dispatches tunaOS's image build and reads the Gate verdict back | **not started — needs write access to `tuna-os/tunaos`** |
| 7 | Version-aware readiness: `simulate-buildroot-resolution.py` as a fourth stage, so `gtk4-4.22.1` stops satisfying `>= 4.23` | not started |
| 8 | Build the contract closure FIRST — the wave currently builds tier order, not stage order | not started |
| 9 | Per-band publish, with the concurrency hazard addressed | not started |
| 10 | `gap_measurement` for el10 in `mode: exhibit` | not started |
| 11 | Hive autonomy raised per blocker class, starting with `spec-changelog` | not started |

Steps 6 and 8 are what remain between this and the literal ask. **6** closes
the loop across the two repositories: today `packages-ready` prints the
`gh workflow run "Build Hummingbird" -R tuna-os/tunaos -f flavor=gnome` a human
or an agent runs, because this session has read-only access to that repo.
**8** is the ordering consequence of the staged objective — the measurement now
knows the contract comes first, but a wave still builds in tier order, so the
loop closes the contract stage eventually rather than deliberately. Pointing
`build-chain.sh --packages-file` at the open stage's closure would make each
wave spend its runners on the packages that decide.

## Risks

- **A closed contract stage read as a buildable one.** Names are not
  versions; `gtk4-4.22.1` satisfies the name and not `>= 4.23`. Stated in
  `stack_readiness.py` where it is relied on, and pinned by a test that fails
  if the limit stops being documented — the alternative is the next reader
  taking `packages-ready` for "it will boot".
- **A residue reported as bugs.** Most unserved names are packages the wave
  never reached. Counting them as blockers buries the one that is real (101
  vs 1 on the live residue).
- **A convergence that dispatches into a wall.** Mitigated by the `blocked`
  rule and pinned by `tests/test_convergence_stops_when_it_stops_making_progress.py`:
  a wave whose `if:` does not require the preceding measurement's `continue` is
  a runaway, and that is asserted per wave.
- **A `done` from an index that could not be read.** `done` ends the loop, so
  it is the one verdict that must never be reached on partial evidence; an
  unreachable index downgrades it to `blocked`.
- **A cascade misattributed to a prefix.** `gtk4-layer-shell` is not `gtk4`,
  and `-` is a regex word boundary. Caught by mutation-testing the guard, not
  by reading it.
- **A partial release move.** The three-category table plus the
  no-unclassified-file test; `adopt()` raises rather than rewriting a
  declaration whose count moved under it.
- **A warm host's local repo drifting from what CI can reproduce.** The warm
  builder takes its manifest, mock config and image from
  `package-builds.yaml`, never from flags, and never publishes.
