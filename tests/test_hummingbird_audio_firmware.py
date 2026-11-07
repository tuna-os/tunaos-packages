"""tuna-os/tunaos#2064: laptop audio needs separate SOF and ALSA firmware.

Both desktop root lists feed the runtime closure and generated build order.
QEMU's virtio audio cannot prove vendor firmware is present on laptops.
"""
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("desktop", ["gnome", "cosmic"])
def test_audio_firmware_is_a_measured_and_validated_root(desktop):
    catalog = yaml.safe_load(
        (ROOT / "manifests/hummingbird-desktops.yaml").read_text()
    )
    expected = {"alsa-sof-firmware", "alsa-firmware"}
    profile = catalog["desktops"][desktop]
    assert expected <= set(profile["required_packages"])
    assert expected <= set(profile["install_packages"])
    assert expected <= set(catalog["components"][desktop])


def test_audio_sources_are_in_the_generated_build_order():
    order = yaml.safe_load(
        (ROOT / "build-order-hummingbird-desktops.yml").read_text()
    )
    sources = {
        package.get("distgit")
        for tier in order["tiers"]
        for package in tier["packages"]
    }
    assert {"alsa-sof-firmware", "alsa-firmware"} <= sources
