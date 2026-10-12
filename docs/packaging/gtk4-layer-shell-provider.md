# Native GTK4 layer shell provider

The separate `packages/gtk4-layer-shell/package.yaml` recipe supplies GTK4
layer shell **and** GTK4 session lock from one native library. It leaves the
existing GTK3 `gtk-layer-shell` recipe unchanged. They have distinct library,
header, pkg-config and introspection names; GTK3 cannot satisfy Tuna's GTK4
bindings.

## Source and API admission

The Rust lockfile review used desktop candidate
`aeb53bfb8a0449445b2924f193f199b05adf875e`; this is review provenance,
not a final Tuna Desktop source pin.

- Official [release v1.3.0](https://github.com/wmww/gtk4-layer-shell/releases/tag/v1.3.0),
  published 2025-10-29, resolves to upstream commit
  `1c963c51514581c41b9bdae08cdf69171265cdda`.
- Official [tag source archive](https://github.com/wmww/gtk4-layer-shell/archive/refs/tags/v1.3.0.tar.gz)
  was downloaded and hashed: 79,405 bytes, SHA256
  `1ebb01ab14e98afd1727f68f64981c37bd23305b1f131f5667c02b94cf593192`.
  The hash binds the archive; tags and release URLs are mutable.
- Upstream's [source installation rules](https://github.com/wmww/gtk4-layer-shell/blob/1c963c51514581c41b9bdae08cdf69171265cdda/src/meson.build)
  provide both APIs in `libgtk4-layer-shell.so.0`, the two typelibs/GIR files,
  and the optional runtime preload helper. Both public headers are packaged.
- Tuna's locked `gtk4-layer-shell-sys` 0.6.1
  [crate source](https://static.crates.io/crates/gtk4-layer-shell-sys/gtk4-layer-shell-sys-0.6.1.crate)
  was verified against Cargo.lock SHA256
  `bcf19bb884ef0ef55b9e6b2b369c39b4fcc0c41e3a0c1cbc8c267720338b690b`.
  Its baseline uses `gtk4-layer-shell-0`; the separate session lock binding
  imposes the stronger API requirement below.
- Tuna's locked `gtk4-session-lock-sys` 0.4.1
  [crate source](https://static.crates.io/crates/gtk4-session-lock-sys/gtk4-session-lock-sys-0.4.1.crate)
  was checked against Cargo.lock SHA256
  `51b694670ce8c85cfdafeaec6faf1567bf6abe661be147ba9dab0059564522ba`.
  Its enabled `v1_1` feature requires the same `gtk4-layer-shell-0` pkg-config
  API >=1.1. This recipe's 1.3.0 release meets that declared version requirement.
  Successful Rust/native linking still needs CI evidence.
- The release includes upstream's session-lock failure use-after-free fix.
  This is upstream release evidence, not independent security qualification.

The library's MIT license is installed in the runtime package. Runtime
versioned ELF, preload helper and both typelibs remain separate from the
headers/unversioned link/pkg-config/GIR/Vala development package. The latter
requires the exact matching runtime EVR and architecture. RPM retains
native ELF dependency generation and the standard ldconfig scriptlets.

## CI build and test contract

The custom build applies native RPM `%{set_build_flags}`, configures Meson
with introspection and Vala enabled, then builds and executes upstream's
mock Wayland layer/session-lock tests and version unit test. Test-list
admission rejects zero tests or missing layer/lock/version coverage. Python
language examples and display smoke tests are disabled, since they are not
required by the C/Rust provider contract. The upstream C protocol tests
remain mandatory and run against upstream's isolated mock server, with
Wayland backend and Cairo rendering. They do not control a real desktop.
The installed smoke checks exact library and pkg-config versions, both
headers/typelibs, license and exported API symbols without opening a display.

EL10 x86_64/aarch64 cells are declared using current main's native factory.
Alma10 and Alma10-Kitten dependency map entries are explicit but not activated:
their factory targets/adapters/child images require the separate PR819
review and qualification. Once those targets exist, activating each native
recipe cell must preserve the consumer, architecture, CPU, immutable image,
repository closure and RPM/install receipts. A CentOS build is not an Alma
build or an installed Tuna Desktop acceptance result.

No local build or container test has run. GTK4 >=4.12, native Wayland client/
server/scanner, the actual ext-session-lock XML, introspection/Vala and
compiler inputs remain dependency/provider availability gates in CI. The
XML is explicitly required because upstream's older generic protocol
version minimum alone does not prove that the staging protocol is present.
Do not enable unrelated GNOME bootstrap repositories to satisfy these inputs
without a reviewed target closure: replacement GLib packages can poison the
base repository. Neither a rendered spec nor passing source contract tests
proves those dependencies are served, ABI compatible, signed or installed.

This recipe does not publish, pin Tuna Desktop's final release, complete
issue #828 or qualify DRM/greeter/lock hardware behavior. Native build,
pristine installation, package ownership/signature and real-session evidence
remain required before promotion.
