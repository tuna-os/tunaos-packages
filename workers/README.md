# repo-proxy Cloudflare Worker

`repo-proxy.ts` serves `repo.tunaos.org` — every `dnf`/`zypper`/`apt` client
that consumes a TunaOS-published repository routes through it. It handles
`$releasever/$basearch` path rewriting, RPM/DEB/GPG-key content routing, and
security/CORS headers in front of the `bluefin` R2 bucket.

`.github/workflows/deploy-worker.yml` deploys this Worker straight to
production on every push to `main` that touches `workers/**`. Run the tests
before pushing a change here.

## Running tests

```bash
cd workers
npm install
npm test
```

`repo-proxy.test.ts` imports the real Worker module and runs it inside an
actual Workers runtime (via `@cloudflare/vitest-pool-workers` + Miniflare),
with a fake R2 binding — not a hand-copied fork of the routing logic. Add a
case here for any new route, header, or path-decoding rule.
