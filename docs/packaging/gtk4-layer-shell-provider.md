# Native GTK4 layer shell provider

Use `packages/gtk4-layer-shell/package.yaml` for GTK4.
The library has APIs for layer shell and session lock.
Both APIs use one library. The GTK3 recipe stays unchanged.
The two recipes use different libraries, headers, pkg-config names and typelibs.
GTK3 cannot supply Tuna's GTK4 APIs.

## Source and API checks

We used desktop candidate `aeb53bfb8a0449445b2924f193f199b05adf875e` for the Rust API review.
This candidate is not a final source pin for Tuna Desktop.

- The official [release v1.3.0](https://github.com/wmww/gtk4-layer-shell/releases/tag/v1.3.0) dates from 2025-10-29.
  Its tag points to commit `1c963c51514581c41b9bdae08cdf69171265cdda`.
- We downloaded the official [source archive](https://github.com/wmww/gtk4-layer-shell/archive/refs/tags/v1.3.0.tar.gz).
  It has 79,405 bytes. Its SHA256 is `1ebb01ab14e98afd1727f68f64981c37bd23305b1f131f5667c02b94cf593192`.
  Tags can change. The hash identifies the source bytes.
- The upstream [install rules](https://github.com/wmww/gtk4-layer-shell/blob/1c963c51514581c41b9bdae08cdf69171265cdda/src/meson.build) put both APIs in `libgtk4-layer-shell.so.0`.
  They also install two headers, two typelibs, two GIR files and a helper for preload.
- We checked the [crate for layer shell](https://static.crates.io/crates/gtk4-layer-shell-sys/gtk4-layer-shell-sys-0.6.1.crate) version 0.6.1 against Cargo.lock.
  Its SHA256 is `bcf19bb884ef0ef55b9e6b2b369c39b4fcc0c41e3a0c1cbc8c267720338b690b`.
  It uses the API `gtk4-layer-shell-0`.
- We checked the [crate for session lock](https://static.crates.io/crates/gtk4-session-lock-sys/gtk4-session-lock-sys-0.4.1.crate) version 0.4.1 against Cargo.lock.
  Its SHA256 is `51b694670ce8c85cfdafeaec6faf1567bf6abe661be147ba9dab0059564522ba`.
  The feature `v1_1` needs the same API at version 1.1 or higher.
  This recipe uses version 1.3.0. CI must still prove that Rust and the native library work together.

The release fixes a use-after-free error when session lock fails.
This statement comes from upstream. It is not our own security qualification.

The runtime package owns the MIT license, versioned ELF, helper for preload and both typelibs.
The development package owns the headers, link, pkg-config file, GIR and Vala files.
It needs the runtime package at the same version and architecture.
RPM produces dependencies from ELF files and runs ldconfig.

## CI test checks

The build uses native RPM flags from `%{set_build_flags}`.
Meson enables introspection, Vala and tests. The build checks the test list first.
The list must contain layer tests, lock tests and the version test.
Meson then runs the tests against the upstream mock server.
The tests use Wayland and Cairo.

They do not control a real desktop.
The build disables tests of Python examples. It keeps the C tests of both protocols.

An exit status of 0 from Meson is insufficient. The recipe reads each result from `meson-logs/testlog.json`.
Each test from the test list must appear exactly once and report `OK` with return code 0.
The result must contain a command, start time and duration.
The recipe rejects missing tests, extra tests, duplicates, skips, timeouts and errors.

It accepts no expected failures. Upstream defines no such exceptions for these tests.
The receipt records the source hash, test list hash, log hash and names of passed tests.

The build keeps the test list, receipt, JSON log and text log under the RPM topdir.
The factory copies them to `artifacts/provider-tests/` for its existing upload step.
A failed cell uploads its whole directory, which includes available logs from the build.
The smoke test checks the exact versions of the library and pkg-config.
It also checks headers, typelibs, license and API symbols. It does not open a display.

## Target scope and open gates

The recipe declares EL10, Alma10 and Alma10-Kitten on x86_64 and aarch64.
Each Alma cell builds from its own native factory root.
Each cell must keep its consumer, architecture, CPU baseline, image digest and repository inputs.
It must also keep proof from RPM and install checks.
A CentOS build does not qualify AlmaLinux or an installed Tuna session.

We have not run a local build or container test.
CI must prove that GTK4 version 4.12 or higher and the native tools exist on each base.
Those tools include Wayland client/server/scanner, introspection, Vala and a compiler.
The recipe also checks that the XML for ext-session-lock exists.
The minimum version of protocols from upstream does not prove that this XML exists.

Do not add unrelated GNOME repositories without review of their dependencies.
Replacement GLib packages can damage the base repository.
Source tests do not show that repositories have these dependencies.
CI must check the ABI, signatures and installed files.

This recipe does not publish or choose the final release of Tuna Desktop.
Issue #828 stays open. CI must prove native builds, installation, package ownership and signatures before promotion.
Real sessions and hardware need separate tests.
