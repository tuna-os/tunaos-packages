# RFC 011 Phase 3 — runtime gate ledger

Phase 3 provides the 12 gate types that queue manifests declare (counted in [TIDEFORGE-READINESS.md](TIDEFORGE-READINESS.md)).
It provides them for every family.
The RFC needs the uncovered set to shrink.
Each removal cites the run that covered it.
This ledger is that record.

Two different claims live near each other.
Do not confuse them:

- a recipe **declaration** of a gate (`gates:` in `packages/*/package.yaml`) states the contract;
- an **implementation** in CI means a workflow evaluates the gate.

An unimplemented gate is a contract nothing can check.
The readiness document flagged this condition: "no Tideforge recipe is legal for promotion regardless of how green the build matrix is."

| gate type | declared (×) | implemented in CI | evidence / next step |
|---|---:|---|---|
| container-build | 12 | ✅ | factory build matrix |
| mock-build | 5 | ✅ | factory build matrix (mock buildroots) |
| rpm-md-stage-install | 7 | partial | `Clean-install` jobs; per-family coverage not yet universal |
| apt-stage-install | 7 | partial | same |
| pacman-stage-install | 3 | partial | same |
| greetd-login | 7 | ❌ | **first to implement** — most-declared runtime gate, gates COSMIC and niri both; model on `build-gnome50-verify.yml`'s Lima+VNC approach (the worked example the readiness doc names). No skeleton is committed: an unimplemented workflow in `.github/workflows/` reads as a gate that exists, and this ledger's whole point is the difference between declared and implemented. The workflow lands with its Lima boot-and-judge steps, in the same change that flips this row. |
| cosmic-session-smoke | 3 | ❌ | after greetd-login (shares the boot harness) |
| niri-session-smoke | 5 | ❌ | after greetd-login (same) |
| xfce-wayland-session-smoke | 4 | ❌ | same harness, xfce session target |
| gnome-session-smoke | 3 | ❌ | `build-gnome50-verify.yml` already does this for native GNOME; port, don't reinvent |
| plasma-session-smoke | 2 | ❌ | same harness, plasma session target |
| selinux-enforcing | 1 | ❌ | boots the stage-install VM with enforcing=1 and asserts no denials for the payload's scriptlets |

Counts come from the measurement in [TIDEFORGE-READINESS.md](TIDEFORGE-READINESS.md).
Re-count when queue manifests change, not from memory.

## Implementation order and the shared harness

Every ❌ row above is a session gate.
They share one need: boot a VM, reach a login manager or session, and judge it from journal and screen.
The workflow `build-gnome50-verify.yml` is the model (Lima VM, wait for GDM, check for crashes).
The plan provides a reusable workflow with the session assertion as parameter.
We build `greetd-login` first because it is the most declared gate.

## Rules

- A row flips to ✅ only with a linked green run on a real payload.
- Declared counts may grow as new recipes arrive.
- The implemented column must never regress silently.
- To remove a gate, edit this ledger in the same PR.
- Automated promotion stays out of scope for Phase 3. It needs its own RFC with safeguards (`INCIDENT-repo-wipe-gnome.md`).
