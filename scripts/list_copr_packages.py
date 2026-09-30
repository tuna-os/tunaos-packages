import json
import sys


def parse_copr_json(json_str):
    """
    Parses the JSON output from `copr-cli list-packages` and returns a list of package names.
    """
    try:
        data = json.loads(json_str)
        if isinstance(data, list):
            return [pkg["name"] for pkg in data if "name" in pkg]
        return []
    except (json.JSONDecodeError, KeyError, TypeError):
        return []

def main():
    if len(sys.argv) > 1:
        # If a file path is provided, read from it
        with open(sys.argv[1]) as f:
            content = f.read()
    else:
        # Otherwise, read from stdin
        content = sys.stdin.read()
    
    packages = parse_copr_json(content)
    for pkg in packages:
        print(pkg)

if __name__ == "__main__":
    main()
