# BATS Tests for tuna-os/tunaos-packages

This directory contains BATS (Bash Automated Testing System) tests for
the tunaos-packages build and CI scripts (formerly the `github-copr` repo).

## Running

```bash
# Install BATS
sudo apt-get install bats

# Run all tests
bats tests/bats/*.bats

# Run a single test file
bats tests/bats/test_build_chain.bats
```

## Test files

| File | Script tested |
|------|--------------|
| test_arch_clean_install.bats | scripts/arch-clean-install.sh |
| test_arch_runtime_closure.bats | scripts/assert-arch-runtime-closure.sh |
| test_build_chain.bats | scripts/build-chain.sh |
| test_build_local.bats | scripts/build-local.sh |
| test_gnome50_el10_compat_useradd_wrapper.bats | src/deps/gnome50-el10-compat (useradd wrapper) |
| test_install_gnome49.bats | contrib/install-gnome49.sh |
| test_install_repo.bats | contrib/install.sh |
| test_tuna_os_repo.bats | contrib/tuna-os.repo |
| test_upload_sources.bats | scripts/upload-sources.sh |
| test_watch_pipeline.bats | scripts/watch-pipeline.sh |
