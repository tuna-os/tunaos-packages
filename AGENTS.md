# AGENTS.md — GNOME 49/50 packaging conventions

This is the canonical agent-instructions doc for the GNOME 49/50 packaging
work in this repo (COPR-based bootstrap for CentOS Stream 10 / EL10).
Previously duplicated across `AGENTS.md` and `GEMINI.md` with no single
source of truth — consolidated here (tunaos-packages#393); `GEMINI.md` is
deleted, this file wins on any future conflict.

Scope note: this file covers the GNOME 49/50 pipeline specifically, not the
whole repo — other package families (xfce, fprintd, hummingbird, gnome51)
have their own `build-order-*.yml` manifests and are out of scope here.

## Package source priority

When adding or updating a package, prefer sources in this order:

1. **Fedora Dist-Git** (`just copr-build <name>`) — unmodified packages.
   Uses Rawhide dist-git commit for GNOME 50, F43 for GNOME 49.
2. **GitHub SCM** (`just copr-scm-build <path>`) — modified specs (patches,
   EL10-specific fixes). Preferred over `copr-srpm-build` because spec
   changes stay versioned in this repo.
3. **Local SRPM** (`just copr-srpm-build <path>`) — last resort / emergency
   overrides only.

Both recipes default to the `jreilly1821/c10s-gnome-50` COPR project
(`jreilly1821/c10s-gnome-49` for the GNOME 49 recipes) — a personal,
unpinned namespace; see tunaos-packages#391 for the supply-chain risk this
carries and the CI freshness check added for the GNOME 49 build. The GNOME
49 mock CI config (`mock/centos-stream-10-ci-gnome49.cfg`) now also
consults the org-owned `[tunaos-gnome49]` R2 mirror ahead of the personal
COPR, but that mirror only covers part of the stack today, so the COPR
stays enabled as a fallback until it's fully migrated.

## ICU 77 isolation ("repo poisoning")

GNOME 50 (`mozjs140`, `tinysparql`) requires ICU 77; EL10 base ships ICU 74.

- **Do not** build ICU 77 in the main COPR repo as a standalone package —
  it would let users accidentally upgrade their system ICU and break base
  packages that expect 74.
- Bundle ICU 77 (static linking or private shared libs) into the packages
  that need it instead.
- Build-time-only tools (e.g. Autoconf 2.72) should build against system
  ICU, or bundle if they can't.

## Key mandatory workarounds

- **Split schema packages**: GNOME 50/51's XML files belong to the real
  `gnome-shell-common` RPM. The main RPM must require it, never provide or
  obsolete its name. The false provider let DNF omit the schemas and GDM
  crashed before login (issue #747, Yellowfin VM proof on 2026-09-26).
- **PAM**: `gnome50-el10-compat` (or `gnome49-el10-compat`) must be present
  — fixes GDM dynamic-user login on EL10.
- **SELinux**: EL10's base `selinux-policy` (42.x) has no policy for GDM
  50's dynamic greeter users — pull the 43.x backport from COPR instead of
  excluding `selinux-policy*`.
- **fontconfig**: COPR pango is built against fontconfig 2.17.0; a bootc
  base image ships 2.15.0, which is missing symbols pango needs
  (`FcConfigSetDefaultSubstitute`) — upgrade fontconfig from COPR before
  installing the GNOME stack, and versionlock it against an inadvertent
  downgrade.
- **dbus-daemon**: it's a `Recommends:` of gdm, not a hard `Requires:`, so
  a bootc image build prunes it — install it explicitly or
  `gdm-wayland-session` fails to open its session message bus.
- **Rust vendoring**: packages lacking EL10 crate dependencies (e.g.
  `gnome-user-share`) need vendored tarballs and offline builds.
- **GDM varlink (GNOME 49)**: EL10 libsystemd 257 rejects
  `sd_varlink_server_listen_address()` calls with mode bits outside 0777;
  GDM built against newer systemd headers passes `0x400001b6`
  (`0666 | SD_VARLINK_SERVER_MODE_MKDIR_0755`), which EINVALs. Patch
  (`src/gnome-49/gdm/0001-el10-force-varlink-mode-0666.patch`) forces that
  bit to 0 regardless of compile-time headers.

## Self-hosted GitHub Actions pipeline (GNOME 49)

Runs alongside COPR, not a replacement for it:

```
src/gnome-49/ specs
   -> GitHub Actions (mock/podman per-package matrix jobs)
   -> GPG sign (secrets.GPG_PRIVATE_KEY)
   -> Cloudflare R2 (r2:bluefin/gnome49/10-stream-x86_64/)
   -> repo.tunaos.org/gnome49/10-stream-x86_64/ (Cloudflare Worker, no transform)
   -> DNF repo usable by end users
```

| File | Purpose |
|------|---------|
| `.copr/build-order-gnome49.yml` | GNOME 49 tier manifest, separate from GNOME 50's root `build-order.yml` |
| `.github/workflows/build-gnome49-distributed.yml` | Full bootstrap: all tiers in sequence, per-package parallel matrix. Generated — regenerate with `scripts/generate-distributed-workflow.py`, don't hand-edit the matrix structure |
| `.github/workflows/build-gnome49-package.yml` | Incremental: triggered by Renovate PRs or manual dispatch for a single package |
| `.github/workflows/build-gnome49-verify.yml` | Post-publish: verifies repo.tunaos.org is actually serving the packages |
| `scripts/watch-pipeline.sh` | Local script to trigger/watch GHA runs via `gh` CLI (`run`, `watch`, `package <path>`, `status`) |
| `contrib/install-gnome49.sh` | User install script — `gpgcheck=1`, hardcoded baseurl (no `$releasever` expansion, GNOME 49 targets one EL10 stream) |
| `renovate.json` | Tracks `src/gnome-49/**/*.spec` `Version:` fields against Fedora F43 dist-git |
| `SRPM-CHANGES.md` | Log of every manual spec/source modification (PAM fixes, Rust vendoring, dependency injections) |
| `COPR-REPORT.md` | Categorizes packages by origin: custom vs. modified vs. unmodified Rawhide |

R2 path layout — GNOME 49 and GNOME 50 are fully separate, do not cross them:

- GNOME 49: `r2:bluefin/gnome49/10-stream-x86_64/` -> `https://repo.tunaos.org/gnome49/10-stream-x86_64/`
- GNOME 50: `r2:bluefin/gnome50/10-stream-x86_64/` -> `https://repo.tunaos.org/gnome50/10-stream-x86_64/`
  (the factory's family prefix since 2026-09-03; `repo/10-x86_64` is the tideforge
  mirror and is never a build-chain destination -- see `docs/GNOME50-REPO-PUBLISH.md`)

The Cloudflare Worker's path transform only touches `/repo/...`; `/gnome49/...`
is served directly from R2 with no transform — don't add one without reason.

## Rules for touching this pipeline

- Don't modify the GNOME 50 manifest (root `build-order.yml`) or its
  workflows (`build-distributed.yml`, `build.yml`) while working on GNOME
  49 — they're independent pipelines with independent tier structures.
- Don't change `workers/repo-proxy.ts` unless the change is explicitly
  about the Worker; the `/gnome49/...` no-transform behavior above depends
  on it staying that way.
- Don't mix GNOME 49 packages into the GNOME 50 manifest/R2 paths or vice
  versa.
- Renovate PRs for major components (`gdm`, `mutter`, `gnome-shell`) must
  not automerge — they need manual verification that the EL10 patches
  still apply.

## Validation

- Test changes against `podman run --rm -it ghcr.io/ublue-os/bluefin:lts`
  or a local CS10 container before trusting a build.
- Record every manual spec/source change in `SRPM-CHANGES.md`.
- Commit local `src/` changes before triggering `copr-scm-build` — it
  builds from the pushed commit, not the working tree.
