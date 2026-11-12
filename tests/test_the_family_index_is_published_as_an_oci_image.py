"""Every published build-chain family index is also an OCI image, by digest.

Maintainer directive, 2026-09-03: no more COPR -- build in GitHub like
projectbluefin/utah-packages (#673). The building half was already true: the
GNOME 50 tier is made by package-factory.yml in a CentOS Stream 10 mock root.
What utah-packages adds is how the result is CONSUMED: an OCI image whose
only content is the createrepo output, pinned by digest, bind-mounted into
the consumer's build. tunaOS already consumes utah-packages that way for
hummingbird:gnome; these tests pin that this publisher gives tunaOS the same
handle for EL10.

The properties, each with the reason it is not optional:

  same bytes      The image is built from `repo/` AFTER the R2 sync-up, so
                  the digest and https://repo.tunaos.org/<r2_path>/ name one
                  index. Publishing before the sync (or from `staged/`)
                  would let the two drift on the first partial failure.
  one tag per     The tag is the cell id (gnome50-el10-x86_64): one image
  cell            per family and architecture, never a shared `latest` that
                  a second family could overwrite.
  no dry-run push A dry run must not publish anything, in either channel.
  signed          cosign, keyless, over the digest -- which is why the job
                  needs id-token: write, and packages: write for the push.
"""

from __future__ import annotations

import pathlib

import pytest

yaml = pytest.importorskip("yaml")

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "publish-build-chain-rpms.yml"
DOC = ROOT / "docs" / "PACKAGE_FACTORY.md"

IMAGE = "ghcr.io/${{ github.repository_owner }}/tunaos-packages"


@pytest.fixture(scope="module")
def publish() -> dict:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return doc["jobs"]["publish"]


@pytest.fixture(scope="module")
def steps(publish) -> list[dict]:
    return publish["steps"]


def _index(steps, name: str) -> int:
    names = [s.get("name") for s in steps]
    assert name in names, f"step {name!r} not found; have {names}"
    return names.index(name)


def _step(steps, name: str) -> dict:
    return steps[_index(steps, name)]


def test_the_publish_job_can_push_and_sign(publish):
    perms = publish["permissions"]
    assert perms.get("packages") == "write", "the OCI push needs packages: write"
    assert perms.get("id-token") == "write", "keyless cosign needs id-token: write"


def test_the_image_is_built_from_the_synced_tree_after_the_sync_up(steps):
    oci = _step(steps, "Publish the family index as an OCI image")
    assert "COPY repo /repository" in oci["run"], (
        "the image must carry repo/, the tree R2 now serves"
    )
    assert "FROM scratch" in oci["run"], "data, not a runnable image"
    assert _index(steps, "Sync up") < _index(steps, "Publish the family index as an OCI image"), (
        "publish the image after the R2 sync-up, so digest and https name one index"
    )


def test_one_tag_per_cell_never_a_shared_latest(steps):
    oci = _step(steps, "Publish the family index as an OCI image")
    assert oci["env"]["IMAGE"] == IMAGE
    assert oci["env"]["TAG"] == "${{ matrix.id }}"
    assert "latest" not in oci["run"]


def test_the_digest_is_what_consumers_are_told_to_pin(steps):
    oci = _step(steps, "Publish the family index as an OCI image")
    assert oci.get("id") == "oci"
    assert 'echo "digest=${digest}" >> "$GITHUB_OUTPUT"' in oci["run"]
    assert "image-versions.yaml" in oci["run"], "the summary must say where tunaOS pins it"
    assert "/repository" in oci["run"]


def test_a_dry_run_publishes_nothing_in_either_channel(steps):
    for name in (
        "Sync up",
        "Publish the family index as an OCI image",
        "Install cosign",
        "Sign the published image",
    ):
        assert _step(steps, name).get("if") == "${{ !inputs.dry_run }}", f"{name} runs on a dry run"


def test_the_image_is_signed_by_digest(steps):
    sign = _step(steps, "Sign the published image")
    assert "cosign sign" in sign["run"]
    assert sign["env"]["STEPS_OCI_OUTPUTS_DIGEST"] == "${{ steps.oci.outputs.digest }}"
    assert '@${STEPS_OCI_OUTPUTS_DIGEST}"' in sign["run"]
    assert (
        _index(steps, "Publish the family index as an OCI image")
        < _index(steps, "Install cosign")
        < _index(steps, "Sign the published image")
    )


def test_the_shared_login_reaches_both_podman_and_cosign(steps):
    """cosign reads the Docker config, podman its own authfile; the login must
    land where both look or the signature step 401s after a successful push."""
    oci = _step(steps, "Publish the family index as an OCI image")
    sign = _step(steps, "Sign the published image")
    assert oci["env"]["DOCKER_CONFIG"] == sign["env"]["DOCKER_CONFIG"]
    assert '--authfile "${DOCKER_CONFIG}/config.json"' in oci["run"]


def test_the_factory_doc_says_what_the_image_is_for():
    text = DOC.read_text(encoding="utf-8")
    assert "ghcr.io/tuna-os/tunaos-packages:gnome50-el10-x86_64" in text
    assert "image-versions.yaml" in text
    # The live endpoint does not move: the doc must keep saying so.
    assert "not as the\nlive DNF/APT/Pacman endpoint" in text
