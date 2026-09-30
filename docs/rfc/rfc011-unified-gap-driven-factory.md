# RFC 011: One gap-driven factory

**Status:** Accepted by hanthor, 2026-08-18  
**Amended:** 2026-08-19 (#430) -- Design §3 and Phase 2 updated to the unified factory.  
**ADR:** [0001](../adr/0001-rfc011-unified-gap-driven-factory.md)  
**Issue:** [#418](https://github.com/tuna-os/tunaos-packages/issues/418)  
**Owner:** hanthor  
**Interacts with:** `docs/PACKAGE_FACTORY.md`, `docs/TIDEFORGE-READINESS.md`, `manifests/package-factory.yaml`

## Problem

TunaOS delivers a unified desktop experience across multiple distribution bases.
Before this RFC, independent projects organized their own build systems:

| Factory family | Workflows | Origin |
| --- | --- | --- |
| EL10 GNOME backport | `build-gnome49/50/51-{distributed,package,verify}` (9) | EL10 ships GNOME 47-era |
| XFCE / XFWL4 | `build-xfce-{distributed,package,fedora,arch-validation}` (4) | Wayland XFCE exists nowhere upstream |
| Hummingbird | `build-hummingbird-{distributed,desktops}` + `hummingbird-gap-drift` (3) | A distro whose repos are incomplete |
| Tideforge | `build-tideforge-{supported,arch}`, `publish-tideforge-debs`, `seed-tideforge-source-cache` (4) | The intended generalization |
| One-offs | `build-fprintd-aarch64` etc. | Individual gaps |

Each family maintained its own build order files, repository generation, and publish gates.
This structure led to three issues:

1. **Configuration drift across files:**
   Bug fixes applied to one workflow did not propagate to other copies (#358).

2. **Manual maintenance of build orders:**
   Maintainers edited build orders by hand.
   They did not recompute them from target repositories.
   When upstream distributions added new packages, the factory continued to build duplicates.

3. **No enforcement of source policy:**
   The policy gives priority to packages from distributions.
   No tool checked the repositories of targets for build needs.

## Existing foundations

The factory unifies existing components:

- **The gap engine:**
  `scripts/measure-hummingbird-gap.py` computes dependency closures against live repository indexes.
  It generates tiered build orders and detects upstream drift.

- **The catalog structure:**
  `manifests/hummingbird-desktops.yaml` defines packages without execution details.

- **The target contract:**
  `manifests/package-factory.yaml` defines targets, formats, and R2 paths.
  `manifests/package-builds.yaml` lists queues of native specs as data.

- **The recipe layer:**
  Tideforge builds portable packages.
  Specs handle the bootstrap step.

- **Image validation:**
  TunaOS measures parity against reference images daily.

## Options considered

**A. Status quo with manual discipline:**
Rejected because manual synchronization fails across duplicated workflows (#358).

**B. Rewrite all packages into Tideforge recipes:**
Rejected because bootstrap packages need native spec features such as scriptlets and SELinux policies.

**C. Unified catalog and gap measurement with a single orchestrator:**
Selected.
The catalog defines identity.
The gap engine computes build needs per target, and the orchestrator executes builds.
Payload formats remain specialized where needed.

## Design

### 1. The catalog (`manifests/catalog.yaml`)

Each package has a single catalog entry:

```yaml
packages:
  - name: xfconf
    upstream:
      source: https://…/xfconf-4.20.0.tar.bz2
      sha256: "…"
      version: 4.20.0
    patches: [patches/xfconf/*.patch]        # shared across all targets
    packaging:
      rpm: { tideforge: packages/xfconf }    # or: native: src/xfce-wayland/xfconf
      deb: { tideforge: packages/xfconf }
    targets: [el10, fc44, hummingbird, noble]  # where a gap may exist
    membership: runtime                        # runtime | selfhost, as today
```

Rules enforced by tests:

- Every package in a workflow matrix must exist in the catalog.
- `targets` must reference valid entries from `manifests/package-factory.yaml`.
- Target assignments need matching format handlers in the recipe.

### 2. The gap engine (`scripts/measure-target-gap.py`)

The gap engine runs with a target parameter:

```bash
measure-target-gap.py --catalog manifests/catalog.yaml --target el10
```

- Target indexes determine build requirements dynamically.
  When an upstream distribution adds a package, the engine removes it from the build order.
- Workflows check the revision of target repositories.
  They open pull requests when upstream indexes change.
- Automated generation replaces hand-curated build orders.

### 3. The unified orchestrator

PR #430 unified the orchestrator design:

- `package-factory.yml` plans builds and executes required gates.
  It computes matrix coordinates and dispatches required cells.
- `package-factory-cell.yml` acts as the execution boundary.
  It derives cache keys from inputs, restores verified results, and skips redundant compilation.
- `manifests/package-factory.yaml` defines the target contract, while `manifests/package-builds.yaml` configures native spec builds.

This design removes duplicate workflows.

### 4. Preserved boundaries

- **Native EL10 specs remain authoritative** for GNOME bootstrap packages.
- **Publication to R2 remains manual**, following incident safety policies.
- **Source tier policies** remain unchanged.
- **Binary payloads remain distinct per distribution format.**

## Execution phases

**Phase 0 -- Catalog creation:**
Create `manifests/catalog.yaml` to record all packages.
Add CI tests to check the catalog coverage.

**Phase 1 -- Gap engine:**
Generate build orders from queries to target repositories.
Replace static lists with generated orders for each desktop family.

**Phase 2 -- Orchestrator consolidation:**
Completed in PR #430.
Replaced duplicated workflows with `package-factory.yml` and `package-factory-cell.yml`.

**Phase 3 -- Runtime gates and promotion:**
Build runtime gates defined in `docs/TIDEFORGE-READINESS.md`.
Add install verification for COSMIC and DMS desktop closures.

**Cleanup:**
Completed in PR #430.
Reduced workflow count from ~20 to 2 main factory workflows and shared utility workflows.

### Workflow census (2026-08-31)

| Workflow | Verdict | Boundary / follow-up |
|---|---|---|
| `build.yml` | **Break-glass, expiry 2026-12-31** | Retained for emergency RPM builds; standard builds use `package-factory.yml`. |
| `build-distributed.yml` | **Break-glass, expiry 2026-12-31** | Retained for topology tooling audits; not used for publishing. |
| `validate-hummingbird-desktops.yml` | **Retained validation** | Validates hummingbird catalog input data. |
| `seed-tideforge-source-cache.yml` | **Retained auxiliary** | Populates content-addressed source caches for factory cells. |
| `bump-stable-package-sources.yml` | **Retained auxiliary** | Updates upstream source pins. |
| `upstream-drift.yml` | **Parameterized** | Checks upstream repository drift based on manifest declarations. |

This strategy preserves tools until all consumers migrate.

## Risks and mitigations

- **Catalog data drift:**
  Completeness tests in CI prevent divergence between recipes and build configurations.

- **Build order differences:**
  Pull requests for conversion document and justify differences from historical lists.

- **Distribution-specific build quirks:**
  Special configurations remain isolated in mock files and spec headers.

## Success criteria

1. Unified repository generation prevents drift bugs (#358).
2. Updates from upstream open pull requests to delete obsolete packages.
3. Adding a package for a new target needs catalog entries instead of new workflows.
4. Test suites prevent undocumented omissions from build matrices.
