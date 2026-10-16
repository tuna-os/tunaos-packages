# repo-proxy Cloudflare Worker

`repo-proxy.ts` serves `repo.tunaos.org`. Every `dnf`, `zypper`, and `apt`
client that uses a TunaOS repository routes through this Worker. It does
path rewriting for `$releasever/$basearch`, routes RPM/DEB/GPG-key content,
and sets security and CORS headers for the `bluefin` R2 bucket.

`.github/workflows/deploy-worker.yml` deploys this Worker to production
when you push to `main` if you change `workers/**` files. Always run the
tests before you push a change here.

## Run tests

```bash
cd workers
npm install
npm test
```

`repo-proxy.test.ts` loads the actual Worker module and runs it in a real
Workers runtime. It uses `@cloudflare/vitest-plugin` and Miniflare
with a fake R2 binding. This is not a copy of the routing logic. Add a test
case for each new route, header, or path-decoding change.
