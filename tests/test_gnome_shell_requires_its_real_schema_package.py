"""A virtual common provider left the installed GNOME desktop without schemas.

The actual Yellowfin VM installed gnome-shell-50.0-3.el10, but not its common
RPM. DNF accepted the main RPM's self-provider as satisfying Requires. The
common RPM owns org.gnome.shell.gschema.xml; GDM then repeatedly crashed.
GNOME 51 carries the same split and must retain the same dependency contract.
"""
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("version", ["50", "51"])
def test_main_requires_common_instead_of_providing_it(version):
    text = (ROOT / f"src/gnome-{version}/gnome-shell/gnome-shell.spec").read_text()
    header = text.split("%description", 1)[0]
    assert re.search(r"^Requires:\s+%\{name\}-common\s*=\s*%\{version\}-%\{release\}\s*$", header, re.M)
    assert not re.search(r"^(?:Provides|Obsoletes):\s+(?:gnome-shell|%\{name\})-common(?:\s|$)", header, re.M), (
        "The main RPM must not satisfy its own dependency or obsolete the schema RPM"
    )
    assert re.search(r"^%package\s+common\s*$", text, re.M)
    common_files = text.split("%files common\n", 1)[1].split("\n%", 1)[0]
    assert "%{_datadir}/glib-2.0/schemas/*.xml" in common_files
