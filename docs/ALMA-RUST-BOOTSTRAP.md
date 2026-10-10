# Native Alma Rust bootstrap candidate

`tunaos-rust193` builds the official checksum-pinned Rust 1.93 source release
with the signed native Alma Rust/Cargo 1.92 compiler. Upstream's release
`src/stage0` identifies 1.92 as its bootstrap predecessor:
https://raw.githubusercontent.com/rust-lang/rust/1.93.0/src/stage0

The package installs rustc, Cargo, and the standard libraries at
`/opt/tunaos/rust-1.93`. It does not provide or replace the system `rust` or
`cargo` packages. COSMIC background needs the isolated package
on Alma and selects its fixed binary path. Other target paths stay unchanged.

Bootstrap uses local `/usr/bin/rustc` and `/usr/bin/cargo`, offline vendored
dependencies, locked dependency resolution, source LLVM, and disabled CI
compiler/LLVM/GCC downloads. The policy for Alma supplies C/C++ and
Rust flags; bootstrap copies those measured flags into its configuration.
The native host triple must match the actual execution architecture.

This is candidate source preparation. Actual CI must prove native bootstrap,
signed installation, compiler execution and ELF/runtime/CPU closure on all
four Alma/Kitten architecture combinations. All build budgets stay fixed.

The LLVM and Rust builds may exceed a job budget.
That outcome blocks supply. A later job must resume authenticated work.
Do not use binaries from another distribution to meet this requirement. The compiler must provide Rust 1.93 or newer.
