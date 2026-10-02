import os
import subprocess
import sys


def download_source(url, dest_dir):
    """
    Downloads a source file using wget.
    """
    if not os.path.exists(dest_dir):
        os.makedirs(dest_dir)
        
    filename = url.split('/')[-1]
    dest_path = os.path.join(dest_dir, filename)
    
    if os.path.exists(dest_path):
        print(f"  File {filename} already exists, skipping download.")
        return True
        
    command = ["wget", "-O", dest_path, url]
    try:
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode == 0:
            return True
        else:
            print(f"  Error downloading {url}: {result.stderr}", file=sys.stderr)
            return False
    except Exception as e:
        print(f"  Exception downloading {url}: {e}", file=sys.stderr)
        return False

def main():
    if len(sys.argv) < 3:
        print("Usage: python3 download_upstream_sources.py <manifest_file> <dest_dir>")
        sys.exit(1)
        
    manifest_file = sys.argv[1]
    dest_dir = sys.argv[2]
    
    if not os.path.exists(manifest_file):
        print(f"Manifest file {manifest_file} not found")
        sys.exit(1)
        
    with open(manifest_file) as f:
        lines = [line.strip() for line in f if line.strip()]
        
    for line in lines:
        parts = line.split()
        if len(parts) >= 2:
            pkg_name, url = parts[0], parts[1]
            # Some URLs might have unexpanded macros or are not valid URLs
            if not url.startswith("http"):
                print(f"Skipping invalid URL for {pkg_name}: {url}")
                continue
                
            print(f"Downloading upstream source for {pkg_name}...")
            # We store sources in src/gnome-49/<pkg_name>/
            pkg_dest_dir = os.path.join(dest_dir, pkg_name)
            download_source(url, pkg_dest_dir)

if __name__ == "__main__":
    main()
