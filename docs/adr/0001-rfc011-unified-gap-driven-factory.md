# ADR 0001: One gap-driven factory (RFC 011)

- Status: accepted
- Date: 2026-08-18
- RFC: [docs/rfc/rfc011-unified-gap-driven-factory.md](../rfc/rfc011-unified-gap-driven-factory.md)
- Issue: [#418](https://github.com/tuna-os/tunaos-packages/issues/418)
- Sign-off: hanthor (maintainer), 2026-08-18
- Policy: tunaOS RFC lifecycle (tunaOS `docs/RFC-PROCESS.md`, ADR 0004).
  This ADR satisfies the merge gate. It is also the first ADR in this
  repository.

## Context

The factory has five workflow families. Each family arose from a separate
crisis. Each family manages its own build order, repository creation,
publish gates, and drift. The cost is copy-paste drift: contributors fixed issue #358
in one copy while it remained latent in others. In addition, hand-curated build orders
rot. Hummingbird declared 1248 sources, but the measured runtime gap was
673. Finally, only the hummingbird family queries the policy for system
repositories.

## Decision

**Adopt RFC 011, option C:** one catalog (`manifests/catalog.yaml`) owns
package identity. A generalized gap engine (`scripts/measure-target-gap.py`)
computes build orders against live repository indexes. One unified factory
(`package-factory.yml` and `package-factory-cell.yml`, landed in #430)
replaces the hand-copied families. Package payloads stay heterogeneous:
Tideforge recipes where proven, and native EL10 specs where the verdict in
[TIDEFORGE-READINESS.md](../TIDEFORGE-READINESS.md) shows no need to switch.

**Considered options:**

1. **Status quo with discipline** — rejected: #358 is the measured proof
   that discipline does not hold across copies.
2. **Rewrite everything into Tideforge recipes** — rejected by evidence in
   [TIDEFORGE-READINESS.md](../TIDEFORGE-READINESS.md). The EL10 GNOME bootstrap
   needs spec control (such as scriptlets and SELinux policy) that simple
   recipes exclude.
3. **Catalog, gap engine, orchestrator, and heterogeneous payloads** — chosen;
   we do not rewrite proven parts.

## Consequences

- Phases 0–3 land independently, each as a safe milestone. Phase 0
  (catalog and completeness tests) changes no CI behavior.
- Success criteria: issue #358 cannot recur. When an upstream distribution
  catches up, a PR removes work automatically. A new package-target pair needs
  only a catalog entry. The list of exceptions must not grow silently.
- Automated R2 promotion stays out of scope and needs its own RFC with
  the `INCIDENT-repo-wipe-gnome.md` safeguards.
