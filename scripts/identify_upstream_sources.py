import os
import re
import sys


def parse_spec_source(content):
    """
    Simplistic SPEC parser to find Source0 URL and expand basic macros.
    """
    macros = {}
    source0 = ""
    
    # First pass: find defines and standard tags
    for line in content.splitlines():
        # Standard tags: Name, Version, etc.
        tag_match = re.match(r"^(\w+):\s*(.*)", line, re.IGNORECASE)
        if tag_match:
            tag_name = tag_match.group(1).lower()
            tag_value = tag_match.group(2).strip()
            macros[tag_name] = tag_value
            if tag_name.startswith("source0"):
                source0 = tag_value
                
        # Defines: %global or %define
        define_match = re.match(r"^%(?:global|define)\s+(\w+)\s+(.*)", line, re.IGNORECASE)
        if define_match:
            macros[define_match.group(1)] = define_match.group(2).strip()
            
    if not source0:
        return None
        
    # Iterative macro expansion (to handle nested macros)
    for _ in range(3):
        for m_name, m_value in macros.items():
            source0 = source0.replace(f"%{{{m_name}}}", m_value).replace(f"%{m_name}", m_value)
            
    return source0

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 identify_upstream_sources.py <src_dir>")
        sys.exit(1)
        
    src_dir = sys.argv[1]
    manifest = []
    
    for root, dirs, files in os.walk(src_dir):
        for file in files:
            if file.endswith(".spec"):
                pkg_name = os.path.basename(root)
                spec_path = os.path.join(root, file)
                with open(spec_path) as f:
                    content = f.read()
                    source_url = parse_spec_source(content)
                    if source_url:
                        manifest.append(f"{pkg_name} {source_url}")
                    else:
                        print(f"  Warning: No Source0 found for {pkg_name}", file=sys.stderr)
                        
    for item in manifest:
        print(item)

if __name__ == "__main__":
    main()
