import argparse
import os
import subprocess
import sys


def clone_package(package_name, dest_dir, branch="rawhide"):
    """
    Clones a package from Fedora Dist-Git via HTTPS with specific branch.
    """
    pkg_dir = os.path.join(dest_dir, package_name)
    if os.path.exists(pkg_dir):
        # If it exists, try to switch branch or just pull
        print(f"  Directory {pkg_dir} already exists. Attempting to switch to {branch}...")
        try:
            subprocess.run(["git", "-C", pkg_dir, "fetch", "origin", branch, "--depth", "1"], capture_output=True)
            result = subprocess.run(["git", "-C", pkg_dir, "checkout", branch], capture_output=True, text=True)
            if result.returncode == 0:
                return True
        except Exception as e:
            print(f"  Error switching branch for {package_name}: {e}", file=sys.stderr)
            return False
        
    url = f"https://src.fedoraproject.org/rpms/{package_name}.git"
    command = ["git", "clone", "-b", branch, url, pkg_dir, "--depth", "1"]
    try:
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode == 0:
            return True
        else:
            # Fallback to rawhide if branch doesn't exist? 
            # No, user asked for f43 specifically.
            print(f"  Error cloning {package_name} branch {branch}: {result.stderr}", file=sys.stderr)
            return False
    except Exception as e:
        print(f"  Exception cloning {package_name}: {e}", file=sys.stderr)
        return False

def main():
    parser = argparse.ArgumentParser(description="Fetch Fedora specs for a list of packages.")
    parser.add_argument("manifest", help="Path to a file containing package names (one per line)")
    parser.add_argument("dest", help="Destination directory for cloned repositories")
    parser.add_argument("-b", "--branch", default="rawhide", help="Branch to fetch (e.g., f43, f42, rawhide)")
    
    args = parser.parse_args()
    
    if not os.path.exists(args.manifest):
        print(f"Manifest file {args.manifest} not found")
        sys.exit(1)
        
    if not os.path.exists(args.dest):
        os.makedirs(args.dest)
        
    with open(args.manifest) as f:
        packages = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        
    print(f"Fetching specifications from branch '{args.branch}'...")
    for pkg in packages:
        print(f"Fetching {pkg}...")
        clone_package(pkg, args.dest, args.branch)

if __name__ == "__main__":
    main()
