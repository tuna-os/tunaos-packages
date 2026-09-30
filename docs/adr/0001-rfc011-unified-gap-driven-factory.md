# ADR 0001: One gap-driven factory (RFC 011)

- Status: accepted
- Date: 2026-08-18
- RFC: [docs/rfc/rfc011-unified-gap-driven-factory.md](../rfc/rfc011-unified-gap-driven-factory.md)
- Issue: [#418](https://github.com/tuna-os/tunaos-packages/issues/418)
- Sign-off: hanthor (maintainer), 2026-08-18
- Policy: tunaOS RFC lifecycle (tunaOS `docs/RFC-PROCESS.md`, ADR 0004) — this ADR satisfies the merge gate.

## Context

The factory consists of five workflow families.
Each family carried its own build order, repo generation, publish gates, and drift detection.
This caused copy-paste drift (#358 fixed one copy but left others, see #421).
Curated build orders also rotted (hummingbird listed 1248 sources, but the real runtime gap was 673).
Most families did not query system repos first.

## Decision

**Adopt RFC 011, option C:** one catalog (`manifests/catalog.yaml`) owns package identity.
A generalized gap engine (`scripts/measure-target-gap.py`) computes build orders against repo indexes with drift PRs.
One unified factory replaces hand-copied families (`package-factory.yml` planner and `package-factory-cell.yml`).
Package payloads stay heterogeneous: Tideforge recipes where proven, and native EL10 specs where needed.

**Considered options:**

1. **Status quo with discipline** — rejected: #358 proves discipline does not hold across copies.
2. **Rewrite everything into Tideforge recipes** — rejected: EL10 GNOME needs scriptlets, file triggers, and SELinux policy.
3. **Catalog + gap engine + orchestrator, heterogeneous payloads** — chosen: we keep the existing specs.

## Consequences

- Phases 0–3 land independently, each as a safe stage. Phase 0 changes no CI behavior.
- Success criteria: bug class #358 cannot recur. A distro update creates a PR that removes work. A new package is a catalog entry. The list of exceptions cannot grow in secret.
- Automated R2 promotion stays out of scope and needs its own RFC with safeguards from `INCIDENT-repo-wipe-gnome.md`.
