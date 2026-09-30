#!/usr/bin/env python3
"""Download a Tideforge source archive and verify its pinned SHA-256."""
from __future__ import annotations

import argparse
import hashlib
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import tideforge_cache
import yaml

RETRYABLE_HTTP = {408, 429, 500, 502, 503, 504}
DOWNLOAD_ATTEMPTS = 4
DOWNLOAD_TIMEOUT_SECONDS = 60


def download(request: Request, *, keep_payload: bool) -> tuple[str, bytes | None]:
    """Stream and hash a source, retrying only transient transport failures."""
    for attempt in range(DOWNLOAD_ATTEMPTS):
        try:
            digest = hashlib.sha256()
            chunks = [] if keep_payload else None
            with urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:  # nosec B310
                while chunk := response.read(1024 * 1024):
                    digest.update(chunk)
                    if chunks is not None:
                        chunks.append(chunk)
            return digest.hexdigest(), b"".join(chunks) if chunks is not None else None
        except HTTPError as exc:
            if exc.code not in RETRYABLE_HTTP or attempt == DOWNLOAD_ATTEMPTS - 1:
                raise
        except URLError:
            if attempt == DOWNLOAD_ATTEMPTS - 1:
                raise
        time.sleep(2**attempt)
    raise AssertionError("unreachable")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("recipe", type=Path)
    parser.add_argument("--cache-dir", type=Path, default=None,
                        help="content-addressed store consulted before the network; "
                             "verified downloads are stored for later fetches")
    args = parser.parse_args()
    recipe = yaml.safe_load(args.recipe.read_text())
    sources = tideforge_cache.recipe_sources(recipe)
    for index, source in enumerate(sources):
        if args.cache_dir is not None:
            payload = tideforge_cache.oras_lookup(args.cache_dir, source["sha256"])
            if payload is not None:
                print(f"source {index} verified from cache ({source['sha256'][:12]})")
                continue
            print(f"source {index} cache miss, downloading ({source['sha256'][:12]})")
        request = Request(source["url"], headers={"User-Agent": "tunaos-package-factory"})
        digest, payload = download(request, keep_payload=args.cache_dir is not None)
        if digest != source["sha256"]:
            raise SystemExit(f"source {index} checksum mismatch: expected {source['sha256']}, got {digest}")
        if args.cache_dir is not None:
            assert payload is not None
            tideforge_cache.store(args.cache_dir, payload)
            tideforge_cache.oras_push(args.cache_dir, source["sha256"])
    print(f"{recipe['name']}: {len(sources)} source checksum(s) verified")


if __name__ == "__main__":
    main()
