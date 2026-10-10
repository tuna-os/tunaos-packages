# Native Alma Rust bootstrap candidate

`tunaos-rust193` builds the official checksum-pinned Rust 1.93 source release
with the signed native Alma Rust/Cargo 1.92 compiler. Upstream's release
`src/stage0` identifies 1.92 as its bootstrap predecessor:
https://raw.githubusercontent.com/rust-lang/rust/1.93.0/src/stage0

The package installs rustc, Cargo, and native standard libraries together at
`/opt/tunaos/rust-1.93`. It does not provide or replace the system `rust` or
`cargo` packages. COSMIC background explicitly requires the isolated package
on Alma and selects its fixed binary path. Other target paths stay unchanged.

Bootstrap uses local `/usr/bin/rustc` and `/usr/bin/cargo`, offline vendored
dependencies, locked dependency resolution, source LLVM, and disabled CI
compiler/LLVM/GCC downloads. Existing Alma compiler policy supplies C/C++ and
Rust flags; bootstrap copies those measured flags into its configuration.
The native host triple must match the actual execution architecture.

This is candidate source preparation. Actual CI must prove native bootstrap,
signed installation, compiler execution and ELF/runtime/CPU closure on all
four Alma/Kitten architecture combinations. No build budget is increased.
Building source LLVM and the compiler may exceed an existing job budget;
that outcome blocks supply and requires authenticated resumable work rather
than a foreign binary or an ignored compiler floor.
