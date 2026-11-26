# Artifact contracts

A recipe can opt in to checks on the archives it built. The block is
`verify.artifact` in `package.yaml`. `scripts/artifact_contract.py` reads each
built `.rpm` and `.deb` into one model and compares it with the block, so one
declaration covers both formats.

The checks run in `scripts/verify-package-factory-cell.sh`, before the native
install. They add to `verify.smoke`, lintian and rpmlint. They do not replace
them. They read archive metadata only, so they cannot show who owns a path
after installation or what an upgrade does. The install and upgrade gates
still decide that.

```yaml
verify:
  smoke: ...
  artifact:
    files:
      - {path: /usr/bin/tool, type: file, mode: "0755"}
      - {path: /etc/tool.conf, type: file, mode: "0644", config: true}
      - {path: /etc/only-rpm.conf, formats: [rpm]}
    absent:
      - /usr/bin/old-tool
    scriptlets:
      - {name: post-install, contains: systemctl}
```

- `files[]`: `path` must be a normalised absolute path. `type` is `file`,
  `dir` or `symlink`. `mode` is a quoted four-digit octal string.
  `config: true` needs an RPM `%config(noreplace)` file or a DEB
  `conffiles` entry. `config: false` needs an ordinary file.
- `absent[]`: paths that must not ship.
- `scriptlets[]`: `name` is `pre-install`, `post-install`, `pre-remove` or
  `post-remove`. They map to `%pre`, `%post`, `%preun` and `%postun` in an RPM
  and to `preinst`, `postinst`, `prerm` and `postrm` in a DEB. `contains` is an
  optional piece of text that must appear in the script body.
- Every entry takes optional `formats: [rpm, deb]` and `package: <name>`, so you
  can limit an entry to a split package or to one format.

Extraction has fixed limits and never writes to disk. RPM metadata comes from
the header sections only. The reader sets a cap on decompressed bytes, an entry
cap and a size cap for each DEB member. The reader rejects each path that
has `..`, `.`, a backslash, NUL or empty segments. An unknown, truncated or
malformed archive is an error and never a pass.
