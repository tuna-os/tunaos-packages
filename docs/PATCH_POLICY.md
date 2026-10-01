# Patch policy

TunaOS does not copy downstream patches to match another distribution package.
The default Tideforge recipe is an unpatched upstream release.

Carry a downstream patch only when all of these are true:

1. A reproducible build, install, boot, or desktop-session failure affects a supported TunaOS target.
2. You cannot solve the issue with an upstream release, dependency choice, build option, or configuration.
3. The patch has a short target-specific rationale and an upstream issue or submission reference.
4. A regression test or runtime gate proves both the failure and the fix.

Differences in a distribution release number, build dependencies, optional features, compiler flags, or patch stack are advisory comparison data only.
The tooling never imports them automatically.

The existing GNOME backport specs for EL10 are an exception only where tests validate their documented workarounds for SELinux, PAM, bootstrap, or ABI compatibility.
They stay native until Tideforge can demonstrate the same behavior.
