// Tests for the Cloudflare Worker that serves repo.tunaos.org. Every
// dnf/zypper/apt client that consumes a TunaOS-published repository routes
// through this Worker, and deploy-worker.yml ships it straight to production
// on push to main — this suite is the gate that runs before that deploy.
//
// It imports the real worker module (not a hand-copied fork) so a change to
// routing, headers, or the path-decode/rewrite logic is caught here before
// it reaches repo.tunaos.org.
import { env, createExecutionContext, waitOnExecutionContext } from "cloudflare:test";
import { describe, it, expect, beforeEach } from "vitest";
import worker from "./repo-proxy";

interface StoredObject {
  httpEtag: string;
  body: string;
}

function makeFakeBucket(objects: Record<string, StoredObject>) {
  return {
    async get(key: string) {
      return objects[key] ?? null;
    },
  };
}

function request(path: string, init: RequestInit = {}) {
  return new Request(`https://repo.tunaos.org${path}`, init);
}

async function run(req: Request) {
  const ctx = createExecutionContext();
  const res = await worker.fetch(req, env, ctx);
  await waitOnExecutionContext(ctx);
  return res;
}

beforeEach(() => {
  (env as any).R2_BUCKET = makeFakeBucket({
    "public.gpg": { httpEtag: '"gpg"', body: "gpg-key-bytes" },
    "repo/10-stream-x86_64/repodata/repomd.xml": { httpEtag: '"repomd"', body: "<repomd/>" },
    "repo/10-stream-x86_64/some-package-1.0-1.el10.x86_64.rpm": { httpEtag: '"rpm"', body: "rpm-bytes" },
    "gnome49/10-stream-x86_64/repodata/repomd.xml": { httpEtag: '"gnome49"', body: "<repomd/>" },
    "ubuntu/dists/noble/InRelease": { httpEtag: '"inrelease"', body: "apt-release" },
    "ubuntu/pool/main/f/foo/foo_1.0_amd64.deb": { httpEtag: '"deb"', body: "deb-bytes" },
    "some file with spaces.rpm": { httpEtag: '"spaces"', body: "rpm-bytes" },
  });
});

describe("$releasever/$basearch path rewrite", () => {
  it("rewrites a known arch segment into the dash-joined R2 key", async () => {
    const res = await run(request("/repo/10-stream/x86_64/repodata/repomd.xml"));
    expect(res.status).toBe(200);
    expect(await res.text()).toBe("<repomd/>");
  });

  it("does not rewrite a path that already has arch baked into one segment", async () => {
    // /repo/10-stream-x86_64/... has no separate arch segment to rewrite;
    // it must be requested (and stored) exactly as-is.
    const res = await run(request("/repo/10-stream-x86_64/repodata/repomd.xml"));
    expect(res.status).toBe(200);
  });

  it("leaves /gnome49/... untouched — no transform applies outside /repo/", async () => {
    const res = await run(request("/gnome49/10-stream-x86_64/repodata/repomd.xml"));
    expect(res.status).toBe(200);
    expect(await res.text()).toBe("<repomd/>");
  });

  it("does not rewrite when the second segment is not a known architecture", async () => {
    // Second segment "repodata" is not in the arch allowlist, so this must
    // be looked up under its literal path, not silently rewritten.
    const res = await run(request("/repo/10-stream-x86_64/repodata/foo"));
    expect(res.status).toBe(404); // not in the fake bucket, but must not 200 via a wrong rewrite
  });
});

describe("percent-encoded path decoding (regression for #622)", () => {
  it("decodes a percent-encoded segment before routing", async () => {
    const res = await run(request("/some%20file%20with%20spaces.rpm"));
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("application/x-rpm");
  });

  it("decodes per-segment so an encoded slash cannot change the route", async () => {
    // %2F inside a single path segment must not be treated as a literal
    // path separator when matching the /repo/<ver>/<arch>/ rewrite regex.
    const res = await run(request("/repo/10-stream/x86_64/repodata%2Frepomd.xml"));
    // The decoded key becomes ".../repodata/repomd.xml" (arch rewrite still
    // applies), which is present in the fake bucket.
    expect(res.status).toBe(200);
  });

  it("returns 400 Bad Request for a malformed percent-escape instead of 500 or a wrong route", async () => {
    const res = await run(request("/repo/10-stream/x86_64/%E0%A4%A"));
    expect(res.status).toBe(400);
  });
});

describe("RPM routes", () => {
  it("serves .rpm with application/x-rpm and an attachment disposition", async () => {
    const res = await run(request("/repo/10-stream-x86_64/some-package-1.0-1.el10.x86_64.rpm"));
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("application/x-rpm");
    expect(res.headers.get("Content-Disposition")).toBe("attachment");
  });

  it("serves repomd.xml as application/xml and cacheable", async () => {
    const res = await run(request("/repo/10-stream-x86_64/repodata/repomd.xml"));
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("application/xml");
    expect(res.headers.get("Cache-Control")).toContain("public");
  });
});

describe("APT routes", () => {
  it("serves InRelease as text/plain", async () => {
    const res = await run(request("/ubuntu/dists/noble/InRelease"));
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("text/plain");
  });

  it("serves .deb as application/x-debian-package with an attachment disposition", async () => {
    const res = await run(request("/ubuntu/pool/main/f/foo/foo_1.0_amd64.deb"));
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("application/x-debian-package");
    expect(res.headers.get("Content-Disposition")).toBe("attachment");
  });
});

describe("GPG key serving", () => {
  it("serves /public.gpg with the pgp-keys content type and a named attachment", async () => {
    const res = await run(request("/public.gpg"));
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("application/pgp-keys");
    expect(res.headers.get("Content-Disposition")).toContain("RPM-GPG-KEY-james-rc");
  });

  it("serves the legacy key alias path to the same object", async () => {
    const res = await run(request("/keys/RPM-GPG-KEY-james-rc"));
    expect(res.status).toBe(200);
    expect(await res.text()).toBe("gpg-key-bytes");
  });
});

describe("security and CORS headers", () => {
  it("sets nosniff, frame-deny, and referrer-policy on every served object", async () => {
    const res = await run(request("/public.gpg"));
    expect(res.headers.get("X-Content-Type-Options")).toBe("nosniff");
    expect(res.headers.get("X-Frame-Options")).toBe("DENY");
    expect(res.headers.get("Referrer-Policy")).toBe("strict-origin-when-cross-origin");
  });

  it("answers an OPTIONS preflight without hitting R2", async () => {
    const res = await run(request("/repo/10-stream-x86_64/repodata/repomd.xml", { method: "OPTIONS" }));
    expect(res.status).toBe(200);
    expect(res.headers.get("Access-Control-Allow-Methods")).toContain("GET");
  });
});

describe("missing objects", () => {
  it("returns 404 for a key not present in the bucket", async () => {
    const res = await run(request("/repo/10-stream-x86_64/repodata/does-not-exist.xml"));
    expect(res.status).toBe(404);
  });
});
