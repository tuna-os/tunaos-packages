from scripts.identify_upstream_sources import parse_spec_source


def test_parse_spec_source():
    spec_content = """
Name:           xdg-desktop-portal
Version:        1.18.4
Source0:        https://github.com/flatpak/xdg-desktop-portal/releases/download/%{version}/%{name}-%{version}.tar.xz
"""
    expected = "https://github.com/flatpak/xdg-desktop-portal/releases/download/1.18.4/xdg-desktop-portal-1.18.4.tar.xz"
    assert parse_spec_source(spec_content) == expected

def test_parse_spec_source_simple():
    spec_content = "Source0: http://example.com/source.tar.gz"
    assert parse_spec_source(spec_content) == "http://example.com/source.tar.gz"
