"""Tests for scripts/measure-gnome-parity-gap.py.

The parity audit resolves the GNOME desktop contract against the per-base
requested package lists of the tunaOS desktop manifests.  These tests pin the
resolution semantics offline — most importantly the #132 failure shape: an
openSUSE list that asks only for the gnome pattern is a session skeleton and
must come out with core components missing.
"""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "measure_gnome_parity_gap",
    ROOT / "scripts" / "measure-gnome-parity-gap.py",
)
assert SPEC and SPEC.loader
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)

ZYPPER_PM = "zypper"
APT_PM = "apt"
DNF_PM = "dnf"


def _resolve(requested, pm, groups=False):
    section = {"groups": ["Whatever"]} if groups else None
    return audit.resolve_components(set(requested), pm, section)


def test_dnf_list_names_apps_and_leaves_transitive_deps_to_the_resolver():
    # The fedora reference list names the apps explicitly and lets dnf resolve
    # hard Requires (mutter, keyring, tracker) transitively.
    requested = {
        "gdm", "gnome-shell", "gnome-settings-daemon",
        "gnome-control-center", "nautilus", "gvfs-fuse", "gvfs-smb",
        "xdg-desktop-portal-gnome", "xdg-desktop-portal-gtk",
        "gnome-bluetooth", "gnome-initial-setup", "gnome-disk-utility",
        "yelp", "gnome-color-manager", "gnome-remote-desktop",
        "gnome-user-docs", "gnome-system-monitor",
    }
    components, metapackages = _resolve(requested, DNF_PM)
    assert metapackages == []
    assert components["nautilus"]["status"] == "explicit"
    assert components["mutter"]["status"] == "indirect"
    assert components["gnome-keyring"]["status"] == "indirect"
    assert all(
        components[name]["status"] != "missing" for name in audit.COMPONENTS
    )


def test_full_opensuse_list_covers_the_contract():
    # The expanded sailfin zypper list: every app component is explicit and the
    # session skeleton comes from the patterns' measured fact.
    requested = {
        "patterns-gnome-gnome", "patterns-gnome-gnome_basis",
        "gdm", "gnome-session", "gnome-settings-daemon",
        "gnome-control-center", "gnome-shell-extensions-common",
        "nautilus", "gvfs", "gvfs-backends", "gvfs-fuse",
        "gnome-keyring", "gnome-keyring-pam",
        "xdg-desktop-portal-gnome", "xdg-desktop-portal-gtk",
        "tinysparql", "localsearch", "orca", "yelp", "fwupd",
        "gnome-bluetooth", "gnome-online-accounts", "gnome-initial-setup",
        "gnome-disk-utility", "gnome-color-manager",
        "gnome-remote-desktop", "gnome-user-docs",
    }
    components, metapackages = _resolve(requested, ZYPPER_PM)
    assert metapackages == ["patterns-gnome-gnome", "patterns-gnome-gnome_basis"]
    assert components["nautilus"]["status"] == "explicit"
    assert components["gvfs"]["status"] == "explicit"
    assert components["gnome-keyring"]["status"] == "explicit"
    # The pattern supplies the session skeleton (measured fact), not the apps.
    assert components["gnome-shell"]["status"] == "metapackage"
    assert components["mutter"]["status"] == "metapackage"
    assert all(
        components[name]["status"] != "missing" for name in audit.COMPONENTS
    )


def test_pattern_only_opensuse_list_is_a_session_skeleton():
    # The exact #132 failure: patterns-gnome-gnome plus gdm only.  The pattern
    # resolves to 475 packages but contains none of the app components, so the
    # list must name them — a list that does not is a hard-fail shape.
    requested = {"patterns-gnome-gnome", "patterns-gnome-gnome_basis", "gdm"}
    components, _ = _resolve(requested, ZYPPER_PM)
    assert components["gdm"]["status"] == "explicit"
    for component in ("gnome-shell", "mutter"):
        assert components[component]["status"] == "metapackage"
    for component in ("nautilus", "gvfs", "gnome-keyring",
                      "xdg-desktop-portal-gnome", "orca", "search-index"):
        assert components[component]["status"] == "missing", component


def test_gnome_core_covers_the_whole_desktop_for_debian():
    # Debian's gnome-core pulls 903 packages under --no-install-recommends
    # (measured in the #132 thread); flounder's list also names nautilus and
    # the portals explicitly.
    requested = {"gnome-core", "nautilus", "xdg-desktop-portal-gnome"}
    components, metapackages = _resolve(requested, APT_PM)
    assert metapackages == ["gnome-core"]
    assert components["nautilus"]["status"] == "explicit"
    for name in ("mutter", "gvfs", "gnome-keyring", "orca", "search-index"):
        assert components[name]["status"] == "metapackage", name


def test_ubuntu_minimal_covers_everything_except_the_keyring():
    # Measured on published grouper:gnome: ubuntu-desktop-minimal ships
    # everything except gnome-keyring, which the manifest now adds explicitly.
    requested = {"ubuntu-desktop-minimal"}
    components, _ = _resolve(requested, APT_PM)
    assert components["gnome-keyring"]["status"] == "missing"
    assert components["nautilus"]["status"] == "metapackage"
    requested_with_keyring = {"ubuntu-desktop-minimal", "gnome-keyring",
                              "libpam-gnome-keyring"}
    components, _ = _resolve(requested_with_keyring, APT_PM)
    assert components["gnome-keyring"]["status"] == "explicit"
    assert components["mutter"]["status"] == "metapackage"


def test_dnf_variants_are_indirect_not_missing_for_transitive_deps():
    # dnf installs hard Requires transitively and the reference editions are
    # runtime-verified healthy; a component the list does not name is expected
    # to arrive via the dependency resolver, never flagged missing.
    requested = {"gnome-shell", "nautilus", "gdm"}
    components, _ = _resolve(requested, DNF_PM)
    assert components["mutter"]["status"] == "indirect"
    assert components["nautilus"]["status"] == "explicit"


def test_parse_section_handles_list_and_dict_shapes():
    assert audit.parse_section(["gdm", "gnome-shell"]) == ["gdm", "gnome-shell"]
    assert audit.parse_section(
        {"packages": ["gdm"], "optional": ["gvfs-afp"], "exclude": ["PackageKit"]}
    ) == ["gdm", "gvfs-afp"]
    assert audit.parse_section({"groups": ["Core"], "packages": ["nautilus"]}) == [
        "nautilus"
    ]


def test_audit_hard_fails_on_a_thin_opensuse_list(tmp_path):
    manifests = tmp_path / "manifests" / "desktops"
    manifests.mkdir(parents=True)
    (manifests / "gnome.yaml").write_text(
        "display_manager: gdm\npackages:\n"
        "  zypper:\n    - patterns-gnome-gnome\n    - gdm\n"
    )
    (manifests / "gnome-debian.yaml").write_text(
        "display_manager: gdm3\npackages:\n  apt:\n    - gnome-core\n"
    )
    section, hard_failed = audit.audit(tmp_path, None)
    assert hard_failed
    sailfin = section["variants"]["sailfin"]
    assert sailfin["core_components_missing"] == [
        "gnome-keyring", "gvfs", "nautilus", "xdg-desktop-portal-gnome",
    ]
    # flounder via gnome-core is complete and does not fail.
    assert section["variants"]["flounder"]["core_components_missing"] == []


def test_audit_passes_on_the_current_tunaos_manifests(tmp_path):
    manifests = tmp_path / "manifests" / "desktops"
    manifests.mkdir(parents=True)
    # Minimal stand-ins: the real manifests are fetched live in production;
    # here we only exercise the resolver's contract via the variant table.
    (manifests / "gnome.yaml").write_text(
        "display_manager: gdm\npackages:\n"
        "  zypper:\n"
        "    - patterns-gnome-gnome\n    - patterns-gnome-gnome_basis\n"
        "    - gdm\n    - gnome-session\n    - gnome-settings-daemon\n"
        "    - gnome-control-center\n    - nautilus\n    - gvfs\n"
        "    - gnome-keyring\n    - xdg-desktop-portal-gnome\n"
        "    - xdg-desktop-portal-gtk\n    - tinysparql\n    - localsearch\n"
        "    - orca\n    - yelp\n    - fwupd\n    - gnome-bluetooth\n"
        "    - gnome-online-accounts\n    - gnome-initial-setup\n"
        "    - gnome-disk-utility\n    - gnome-color-manager\n"
        "    - gnome-remote-desktop\n    - gnome-user-docs\n"
        "  fedora:\n    - gnome-shell\n    - nautilus\n"
        "  el10:\n    - gdm\n    - gnome-shell\n    - nautilus\n"
        "  apt:\n    - ubuntu-desktop-minimal\n    - gnome-keyring\n"
    )
    (manifests / "gnome-debian.yaml").write_text(
        "display_manager: gdm3\npackages:\n  apt:\n    - gnome-core\n"
    )
    section, hard_failed = audit.audit(tmp_path, None)
    assert not hard_failed
    assert section["variants"]["sailfin"]["missing_components"] == []
    assert section["variants"]["flounder"]["missing_components"] == []
    assert section["variants"]["grouper"]["missing_components"] == []


def test_tracked_report_json_has_the_shape_maintenance_needs():
    report = json.loads((ROOT / "docs" / "gnome-parity-gap.json").read_text())
    assert report["issue"].endswith("/issues/132")
    assert report["registry_ok"] is True
    assert set(report["audit"]["variants"]) == set(audit.VARIANTS)
    for variant in ("sailfin", "flounder", "grouper"):
        assert report["audit"]["variants"][variant]["core_components_missing"] == []
    for variant in ("sailfin", "flounder", "grouper"):
        assert "delta_gb" in report["sizes"][variant]
