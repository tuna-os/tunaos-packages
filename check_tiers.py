import json
import os
import subprocess
from pathlib import Path

import yaml

ARM_CHROOT = "epel-10-aarch64"
V2_CHROOT = "alma-kitten+epel-10-x86_64_v2"
PROJECT = "jreilly1821/c10s-gnome-50"

def get_pkg_name(path):
    try:
        # Find the .spec file in the path
        specs = [f for f in os.listdir(path) if f.endswith('.spec') and 'bootstrap' not in f]
        if not specs:
            specs = [f for f in os.listdir(path) if f.endswith('.spec')]
        if not specs:
            return os.path.basename(path)
        spec_path = os.path.join(path, specs[0])
        name = subprocess.check_output(['rpmspec', '-q', '--qf', '%{name}\n', spec_path], text=True).splitlines()[0]
        return name
    except Exception:
        return os.path.basename(path)

def get_status():
    print("Fetching latest build statuses from COPR...")
    res = subprocess.check_output(['copr-cli', 'monitor', PROJECT, '--fields', 'name,chroot,state', '--output-format', 'json'], text=True)
    data = json.loads(res)
    
    # latest_status[(pkg, chroot)] = state
    # monitor output is ordered from newest to oldest usually, let's verify if that's true or just take the first one we see
    status_map = {}
    for entry in data:
        key = (entry['name'], entry['chroot'])
        if key not in status_map:
            status_map[key] = entry['state']
    return status_map

def main():
    # Path.read_text (not open()) so callers/tests can substitute the
    # build-order content cleanly.
    config = yaml.safe_load(Path('build-order.yml').read_text())
    
    status_map = get_status()
    
    for tier in config['tiers']:
        print(f"Checking tier: {tier['name']}")
        packages_to_build = {ARM_CHROOT: [], V2_CHROOT: []}
        all_succeeded = True
        
        tier_pkgs = []
        for pkg_entry in tier['packages']:
            path = pkg_entry.get('path')
            if path:
                name = get_pkg_name(path)
            else:
                name = pkg_entry.get('copr_name')
                path = ""
            
            if not name:
                continue
                
            tier_pkgs.append(name)
            
            for chroot in [ARM_CHROOT, V2_CHROOT]:
                state = status_map.get((name, chroot))
                if state != "succeeded":
                    print(f"  [MISSING/FAILED] {name} in {chroot} (state: {state})")
                    packages_to_build[chroot].append((name, path))
                    all_succeeded = False
                else:
                    # print(f"  [OK] {name} in {chroot}")
                    pass
        
        if not all_succeeded:
            print(f"\nTier '{tier['name']}' is incomplete. Action needed.")
            
            # Combine by package to avoid duplicate triggers if possible
            pkg_map = {} # name -> {'path': path, 'chroots': []}
            for chroot, pkgs in packages_to_build.items():
                for name, path in pkgs:
                    if name not in pkg_map:
                        pkg_map[name] = {'path': path, 'chroots': []}
                    pkg_map[name]['chroots'].append(chroot)
            
            for name, info in pkg_map.items():
                # Special case: icu needs spec_override sometimes, but for now let's just trigger build-package if it exists in copr
                # If it doesn't exist or needs special setup, we might need justfile commands
                print(f"Triggering build for {name} in {info['chroots']}...")
                cmd = f"copr-cli build-package {PROJECT} --name {name} {' '.join(['--chroot ' + c for c in info['chroots']])} --nowait"
                # Use justfile command if possible for local paths
                # But build-package is safer if it's already set up correctly in COPR
                subprocess.run(cmd, shell=True)
            
            print("\nTriggered all missing builds for this tier. Wait for them to finish.")
            return

    print("All tiers are complete!")

if __name__ == "__main__":
    main()
