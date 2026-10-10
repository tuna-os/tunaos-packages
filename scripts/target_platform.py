"""Select architecture-specific build inputs from the factory contract."""
from __future__ import annotations

import re
from typing import Any


def build_context(target: dict[str, Any], architecture: str) -> dict[str, str]:
    images = target.get("probe_images")
    if images is not None:
        if not isinstance(images, dict) or architecture not in images:
            raise ValueError(f"missing native probe image for {architecture}")
        image = images[architecture]
        if not isinstance(image, str) or not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image):
            raise ValueError("architecture-specific probe image must be digest-pinned")
        platform = (target.get("platforms") or {}).get(architecture)
        baseline = (target.get("cpu_baselines") or {}).get(architecture)
        expected = {"x86_64": (("linux/amd64", "x86-64") if target.get("format") == "pkg.tar.zst"
                               else ("linux/amd64/v2", "x86-64-v2")),
                    "aarch64": ("linux/arm64", "armv8-a")}
        if (platform, baseline) != expected.get(architecture):
            raise ValueError(f"unsupported native platform/baseline for {architecture}")
        return {"image": image, "platform": platform, "cpu_baseline": baseline}
    image = target.get("probe_image")
    if not isinstance(image, str) or not image:
        raise ValueError("target declares no probe image")
    return {"image": image}
