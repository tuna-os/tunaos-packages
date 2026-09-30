# Security Policy

## Supported Versions

Only the `main` branch has active support. COPR build specs in this
repository target the following RPM delivery channels:

| Project | Base OS | Branch | Status |
|---|---|---|---|
| `c10s-gnome-50` | CentOS Stream 10 | `main` | ✅ Supported |
| `c10s-gnome-49` | CentOS Stream 10 | `main` | ✅ Supported |

## Reporting a Vulnerability

**Do not report security vulnerabilities through public GitHub issues.**

Report them privately through GitHub Security Advisories:

1. Go to the [Security tab](https://github.com/tuna-os/tunaos-packages/security).
2. Click **Report a vulnerability**.
3. Describe the issue in detail, including steps to reproduce.

You can expect:
- **Acknowledgment** within 48 hours
- **Status update** within 5 business days
- **Resolution timeline** based on severity

## RPM Supply Chain Security

This repository provides RPM spec files and patches to build GNOME desktop
packages on Enterprise Linux 10 with COPR. Security practices include:

- **Spec provenance**: The specs track dist-git for Fedora Rawhide and F43. Maintainers keep modifications as local patches and document them in `SRPM-CHANGES.md`.
- **Source integrity**: The build tool downloads the source files with `spectool` from upstream URLs (`Source:` tags in specs). Maintainers record the SHA512 checksums in `sources` files for verification.
- **Build isolation**: COPR build chroots are ephemeral. For self-hosted GHA builds, mock runs inside a container without network access during `%build` and `%install`.
- **No secrets in repo**: The team stores build secrets in encrypted GitHub secrets and never commits them.
- **Pinned actions**: Workflows pin all third-party actions to commit SHAs.
- **GPG signatures**: The pipeline signs RPMs with GPG before upload.

## Disclosure Policy

We follow coordinated disclosure:
1. The reporter submits the vulnerability privately.
2. Maintainers investigate and develop a fix.
3. Maintainers deploy the fix to new COPR builds.
4. Maintainers publish the advisory after deployment.

See [AGENTS.md](AGENTS.md) and [COPR-REPORT.md](COPR-REPORT.md) for build architecture details.
