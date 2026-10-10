import json

from scripts.list_copr_packages import parse_copr_json


def test_parse_copr_json():
    mock_json = [
        {"name": "pkg1", "ownername": "user"},
        {"name": "pkg2", "ownername": "user"}
    ]
    expected = ["pkg1", "pkg2"]
    assert parse_copr_json(json.dumps(mock_json)) == expected

def test_parse_copr_json_empty():
    assert parse_copr_json("[]") == []
