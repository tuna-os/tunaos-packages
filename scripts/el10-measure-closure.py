#!/usr/bin/env python3
"""Native EL10 dependency/provider/ABI producer (CI-executed, per arch).

Runs INSIDE the pinned arch-native container from the production probe
image; never on the host, never emulated. Measures one staged subset
cell: resolves every plan require with dnf, installs the solver's
choice in the throwaway container, and records per-require provider,
hash, signature, index-provenance, and ABI observations plus native
platform proof. Emits the closure report admission judges.

Signature verification is never disabled anywhere in this producer:
published-index repos are configured with signature and metadata
checks enforced against the imported factory key, and distro repos
keep their secure defaults. An unverifiable provider is recorded as
such and fails admission; it is never silently accepted.

Subcommands (network/native work only in explicitly named operations):
  plan              derive a measurement plan from factory + recipe + cell
  policy            derive an admission policy from factory + keyring
  measure           run the native measurement inside the container
  admit             rejects without independent original custody (legacy shape)
  validate-local    local consistency only, never original-run authentication
  admit-run         acquire original successful API/jobs/source/ZIP and admit
  verify-artifacts  compare bound artifact hashes against bytes on disk

PyYAML is required by planning/policy; native operations run only in CI.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

HERE = pathlib.Path(__file__).resolve().parent

# Gap-filler repo shape, mirrored from run-package-factory-cell.sh: the
# published index may resolve only names the system repos lack, and the
# stale self-obsoleting glib2 pair is excluded so a buildroot never
# takes glib2 from the published repo (publish run 32405815822).
PUBLISHED_REPO_FILE = "tunaos-published.repo"
PUBLISHED_REPO_PRIORITY = "999"
PUBLISHED_REPO_EXCLUDE = "glib2 glib2-devel glib2-static"
PUBLISHED_REPO_PREFIX = "tunaos-published-"

# Factory key location inside the measuring container.
CONTAINER_KEYRING_PATH = "/etc/pki/rpm-gpg/TUNAOS-EL10"

# Named User-Agent, mirrored from gap_engine.fetch: the served indexes
# sit behind Cloudflare and some egress paths answer the default
# Python-urllib agent with 403 while curl gets 200.
USER_AGENT = "tunaos-package-factory"

REPO_NS = "{http://linux.duke.edu/metadata/repo}"
RPM_NS = "{http://linux.duke.edu/metadata/rpm}"

# dnf/rpm field layout for provider observations.
QUERYFORMAT = (
    "%{NAME}|%{EPOCH}|%{VERSION}|%{RELEASE}|%{ARCH}|"
    "%{REPOID}|%{SOURCERPM}|%{SHA256HEADER}\n"
)
INSTALLED_QF = (
    "%{NAME}|%{EPOCH}|%{VERSION}|%{RELEASE}|%{ARCH}|"
    "%{SOURCERPM}|%{SHA256HEADER}\n"
)

# Grounded ABI oracles, transcribed from recipe `verify` blocks (never
# invented): gtk4-layer-shell verify (pkg-config floor, headers,
# typelibs, ctypes named symbols) and libseat verify
# (`pkg-config --modversion libseat && seatd -h`).
GTK4_LAYER_SHELL_PC = "gtk4-layer-shell-0"
GTK4_LAYER_SHELL_HEADERS = [
    "/usr/include/gtk4-layer-shell/gtk4-layer-shell.h",
    "/usr/include/gtk4-layer-shell/gtk4-session-lock.h",
]
GTK4_LAYER_SHELL_TYPELIBS = [
    "/usr/lib64/girepository-1.0/Gtk4LayerShell-1.0.typelib",
    "/usr/lib64/girepository-1.0/Gtk4SessionLock-1.0.typelib",
]
GTK4_LAYER_SHELL_SONAME = "libgtk4-layer-shell.so.0"
GTK4_LAYER_SHELL_SYMBOLS = (
    "gtk_layer_init_for_window",
    "gtk_layer_set_keyboard_mode",
    "gtk_layer_set_namespace",
    "gtk_session_lock_instance_new",
    "gtk_session_lock_instance_lock",
    "gtk_session_lock_instance_unlock",
    "gtk_session_lock_instance_assign_window_to_monitor",
    "gtk_session_lock_instance_is_locked",
)
LIBSEAT_PC = "libseat"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load_sibling(name):
    """Load scripts/<name>.py by path (scripts/ is not a package)."""
    path = HERE / f"{name}.py"
    module_name = "el10_" + name + "_" + hashlib.sha256(str(path).encode()).hexdigest()[:12]
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def dnf_preamble(build_repositories):
    """Target-native EL10 setup, mirroring probe-target-dependencies.

    repository_setup("el10", ...) installs epel-release for epel and
    dnf-plugins-core plus `config-manager --set-enabled crb` for crb;
    the producer then refreshes metadata. Secure repo defaults stay on.
    """
    commands = []
    if "epel" in build_repositories:
        commands.append(["dnf", "-qy", "install", "epel-release"])
    if "crb" in build_repositories:
        commands.append(["dnf", "-qy", "install", "dnf-plugins-core"])
        commands.append(["dnf", "config-manager", "--set-enabled", "crb"])
    commands.append(["dnf", "-qy", "makecache"])
    return commands


def build_repo_section(index, baseurl, key_path):
    """One published-index repo section with verification enforced."""
    return (
        f"[{PUBLISHED_REPO_PREFIX}{index}]\n"
        f"name=TunaOS published index {index} (EL10 closure producer)\n"
        f"baseurl={baseurl}\n"
        "enabled=1\n"
        "gpgcheck=1\n"
        "repo_gpgcheck=1\n"
        f"gpgkey=file://{key_path}\n"
        f"priority={PUBLISHED_REPO_PRIORITY}\n"
        f"excludepkgs={PUBLISHED_REPO_EXCLUDE}\n"
    )


def repoquery_commands(requirement):
    """Query argv for one require: candidates, then pinned header fields."""
    return [
        ["dnf", "-q", "repoquery", "--available", "--whatprovides", requirement],
        [
            "dnf",
            "-q",
            "repoquery",
            "--available",
            "--queryformat",
            QUERYFORMAT,
            "--whatprovides",
            requirement,
        ],
    ]


def parse_pipe_row(line, with_repoid):
    """Parse one QUERYFORMAT/INSTALLED_QF row into header fields."""
    parts = line.rstrip("\n").split("|")
    want = 8 if with_repoid else 7
    require(len(parts) == want, f"rpm query row has {len(parts)} fields, want {want}")
    if with_repoid:
        name, epoch, version, release, arch, repoid, sourcerpm, sha = parts
    else:
        name, epoch, version, release, arch, sourcerpm, sha = parts
        repoid = ""
    require(name and version and release and arch, f"rpm query row lacks NEVRA: {line!r}")
    epoch = "0" if epoch in ("", "(none)") else epoch
    require(epoch.isdecimal(), "invalid RPM epoch")
    epoch_prefix = epoch + ":" if epoch != "0" else ""
    require(
        re.fullmatch(r"[0-9a-f]{64}", sha or "") is not None,
        f"rpm query row header hash malformed: {line!r}",
    )
    return {
        "name": name,
        "epoch": epoch,
        "version": version,
        "release": release,
        "arch": arch,
        "repoid": repoid,
        "sourcerpm": sourcerpm,
        "sha256header": sha,
        "evr": f"{epoch or '0'}:{version}-{release}",
        "nevra": f"{name}-{epoch_prefix}{version}-{release}.{arch}",
    }


def parse_sigpgp_keyid(sigpgp):
    """Extract the signing key ID from `%{SIGPGP:pgpsig}` text."""
    match = re.search(r"Key ID ([0-9a-fA-F]+)", sigpgp or "")
    require(match is not None, f"unparseable SIGPGP text: {sigpgp!r}")
    return match.group(1).upper()


def rpm_k_state(returncode, output):
    """Classify `rpm -K` output: only an explicit OK accepts."""
    text = output or ""
    if returncode == 0 and "OK" in text and "NOT OK" not in text:
        return "gpg-ok"
    return "unverified"


def parse_openpgp_keyids(data):
    """Extract (keyid, fingerprint) pairs from an OpenPGP public key.

    Minimal packet walk over v4 public-key (tag 6) and public-subkey
    (tag 14) packets: keyid is the low 64 bits of the RSA modulus,
    fingerprint is SHA-1 over the 0x99-framed public-key body. Lets the
    `policy` subcommand decode public.gpg with stdlib only. v4 key IDs
    are the low 64 bits of the fingerprint.
    """
    require(isinstance(data, bytes) and data, "empty keyring bytes")
    if data.lstrip().startswith(b"-----BEGIN"):
        import base64

        lines = []
        for line in data.decode("ascii", "strict").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("-----") or ":" in stripped:
                continue
            if stripped.startswith("="):
                continue
            lines.append(stripped)
        require(lines, "armored keyring carries no body")
        data = base64.b64decode("".join(lines))
    keys = []
    pos = 0
    while pos < len(data):
        head = data[pos]
        require(head & 0x80, "not an OpenPGP packet stream")
        pos += 1
        if head & 0x40:
            tag = head & 0x3F
            first = data[pos]
            pos += 1
            if first < 192:
                size = first
            elif first < 224:
                size = ((first - 192) << 8) + data[pos] + 192
                pos += 1
            elif first == 255:
                size = int.from_bytes(data[pos : pos + 4], "big")
                pos += 4
            else:
                raise ValueError("partial OpenPGP lengths unsupported")
        else:
            tag = (head >> 2) & 0x0F
            lentype = head & 0x03
            if lentype == 3:
                size = len(data) - pos
            else:
                nbytes = (1, 2, 4)[lentype]
                size = int.from_bytes(data[pos : pos + nbytes], "big")
                pos += nbytes
        body = data[pos : pos + size]
        require(len(body) == size, "truncated OpenPGP packet")
        pos += size
        if tag in (6, 14) and len(body) > 10 and body[0] == 4:
            algo = body[5]
            if algo in (1, 2, 3):
                bits = int.from_bytes(body[6:8], "big")
                modulus = body[8 : 8 + (bits + 7) // 8]
                require(len(modulus) == (bits + 7) // 8, "truncated RSA modulus")
                framed = b"\x99" + len(body).to_bytes(2, "big") + body
                fingerprint = hashlib.sha1(framed).hexdigest().upper()
                keys.append(
                    {"tag": tag, "keyid": fingerprint[-16:], "fingerprint": fingerprint}
                )
    require(keys, "no public-key packets decoded")
    return keys


# --- Measurement (container-only; never runs on the host) ---


EXECUTION = None


def run(argv, timeout=600, env=None):
    """Shared absolute phase budget and verified owned descendant cleanup."""
    if EXECUTION is None:
        raise ValueError("measurement execution budget not initialized")
    try:
        data = EXECUTION.command(argv, timeout=timeout, env=dict(os.environ, LC_ALL="C", LANG="C") if env is None else env)
        return 0, data.decode("utf-8", "strict")
    except OSError as error:
        return 127, str(error)
    except RuntimeError as error:
        # Retired command failure may be a measured negative. Unknown child
        # retirement, escaped/adopted children and deadlines abort the phase.
        if not isinstance(error, load_sibling("el10-evidence").CommandFailed):
            raise
        return 127, str(error)


def fetch_url(url, timeout=120):
    if EXECUTION is None:
        raise ValueError("measurement execution budget not initialized")
    return EXECUTION.fetch(url)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def split_pkgconfig(requirement):
    """Split `pkgconfig(name) [op version]` or return (None, ...)."""
    match = re.match(r"\Apkgconfig\(([^()\s]+)\)\s*(.*)\Z", requirement.strip())
    if match is None:
        return None, None, None
    rest = match.group(2).strip()
    if not rest:
        return match.group(1), None, None
    opver = re.match(r"\A(>=|<=|=|>|<)\s*(\S+)\Z", rest)
    require(opver is not None, f"require has an unusable shape: {requirement!r}")
    return match.group(1), opver.group(1), opver.group(2)



def apply_repository_key_scope(section, key):
    """Actual DNF configuration, including the Extras bootstrap package filter."""
    section["gpgcheck"]="1"
    section["repo_gpgcheck"]="1"
    section["gpgkey"]="file:///etc/pki/rpm-gpg/EL10-"+key["path"]
    if key.get("scope")=="bootstrap-only":
        require(key.get("packages")==["epel-release"], "Extras trust must remain epel-release only")
        section["includepkgs"]="epel-release"

def configure_system_repos():
    """Persist explicit source-key scope and actual per-repo crypto policy."""
    import configparser
    policy=json.loads((HERE/"el10-keys/policy.json").read_text())
    by_repo={repo:key for key in policy["keys"] for repo in key["repositories"]}
    for key in policy["keys"]:
        source=HERE/"el10-keys"/key["path"]
        require(load_sibling("el10-evidence").digest_file(source,65536)==key["sha256"],"bootstrap public-key bytes differ")
        destination=pathlib.Path("/etc/pki/rpm-gpg")/("EL10-"+key["path"])
        destination.write_bytes(source.read_bytes())
        rc,out=run(["rpmkeys","--import",str(destination)],timeout=30)
        require(rc==0,"pinned bootstrap key import failed")
    for path in pathlib.Path("/etc/yum.repos.d").glob("*.repo"):
        parser=configparser.ConfigParser(interpolation=None)
        parser.read(path)
        for section in parser.sections():
            if section not in by_repo:
                require(parser[section].get("enabled","1").strip()=="0", "unknown enabled native repository")
                continue
            apply_repository_key_scope(parser[section],by_repo[section])
        with path.open("w") as stream:parser.write(stream)


def observed_repo_configuration():
    program=r'''import dnf,json
base=dnf.Base();base.conf.read();base.read_all_repos()
print(json.dumps([dict(name=r.id,baseurl=list(r.baseurl),metalink=r.metalink,
 gpgcheck=bool(r.gpgcheck),repo_gpgcheck=bool(r.repo_gpgcheck),gpgkey=list(r.gpgkey),
 includepkgs=list(r.includepkgs))
 for r in base.repos.iter_enabled()]))
base.close()
'''
    rc,out=run(["python3","-c",program],timeout=30)
    require(rc==0,"actual enabled repo policy unavailable")
    observed=json.loads(out)
    require(type(observed) is list and observed,"no actual enabled repositories observed")
    require(all(r.get("gpgcheck") is True and r.get("repo_gpgcheck") is True and r.get("gpgkey") for r in observed),
            "actual enabled repository lacks mandatory package/metadata checks")
    require(all(r.get("name")!="extras-common" or r.get("includepkgs")==["epel-release"] for r in observed),
            "actual Extras bootstrap package filter missing or broadened")
    return observed


def index_provenance(baseurl, evidence_dir, slot, gnupg_home, signing_fingerprints=None):
    """Fetch repomd/primary/signature evidence for one served index.

    Mirrors gap_engine.primary_of: revision plus the primary location
    (never *.zck), hashing the COMPRESSED bytes as fetched, then
    verifies repodata/repomd.xml.asc against the factory keyring.
    """
    base = baseurl.rstrip("/") + "/"
    repomd = fetch_url(base + "repodata/repomd.xml")
    require(len(repomd) <= 4*1024**2 and b"<!DOCTYPE" not in repomd and
            b"<!ENTITY" not in repomd, "unsafe or oversized repomd")
    root = ET.fromstring(repomd)
    revision = root.findtext(f"{REPO_NS}revision")
    require(revision, f"{base}: repomd.xml carries no revision")
    primaries = [data for data in root.findall(f"{REPO_NS}data")
                 if data.get("type") == "primary"]
    require(len(primaries) == 1, f"{base}: primary index must be unambiguous")
    location = primaries[0].find(f"{REPO_NS}location")
    checksum = primaries[0].find(f"{REPO_NS}checksum")
    require(location is not None and checksum is not None, "primary descriptor missing")
    href = location.get("href", "")
    load_sibling("el10-evidence").safe_member(href)
    require(not href.endswith(".zck") and ":" not in href and "?" not in href and "#" not in href,
            "primary location must remain a relative served-index member")
    algorithm = checksum.get("type")
    declared = checksum.text
    require(algorithm in ("sha256", "sha512"), "weak primary index checksum")
    raw = fetch_url(base + href)
    require(hashlib.new(algorithm, raw).hexdigest() == declared,
            "downloaded primary differs from signed repomd checksum")
    observed = sha256_bytes(raw)
    repomd_path = evidence_dir / f"repomd-{slot}.xml"
    primary_path = evidence_dir / f"primary-{slot}.bin"
    repomd_path.write_bytes(repomd)
    primary_path.write_bytes(raw)
    asc_path = evidence_dir / f"repomd-{slot}.xml.asc"
    try:
        asc_path.write_bytes(fetch_url(base + "repodata/repomd.xml.asc"))
        present = True
    except load_sibling("el10-evidence").CommandFailed:
        present = False
    verified = False
    signer = ""
    if present:
        rc, status = run(["gpg", "--batch", "--no-autostart", "--homedir", str(gnupg_home),
                          "--status-fd=1", "--verify", str(asc_path), str(repomd_path)], timeout=120)
        if rc == 0:
            signer = load_sibling("el10-evidence").valid_signature_status(status,
                signing_fingerprints or KEY_POLICIES["factory"]["signing_fingerprints"])
            verified = True
        (evidence_dir / f"repomd-{slot}.gpg-status.txt").write_text(status)
    return (
        {
            "baseurl": base,
            "revision": revision,
            "primary_href": href,
            "primary_sha256": observed,
            "primary_sha256_declared": declared if algorithm == "sha256" else None,
            "primary_checksum_algorithm": algorithm, "primary_checksum_declared": declared,
            "primary_bytes": len(raw),
        },
        {"present": present, "verified": verified, "signer_fingerprint": signer},
        {"repomd": repomd_path, "asc": asc_path if present else None, "primary": primary_path,
         "status": evidence_dir / f"repomd-{slot}.gpg-status.txt" if present else None},
    )


def dnf_provider_snapshot(requirement):
    """Actual installed solver supplier and available candidates, including origin.

    DNF Package.from_repo reads the transaction history; no first-candidate
    inference. Query runs under the same native DNF cache/configuration.
    """
    program = r'''import dnf,json,sys
base=dnf.Base();base.conf.read();base.read_all_repos();base.fill_sack(load_system_repo=True)
def row(package):
 checksum=package.returnIdSum() if not package._from_system else (None,None)
 return dict(name=package.name,epoch=str(package.epoch or 0),version=package.version,
  release=package.release,arch=package.arch,repoid=package.from_repo if package._from_system else package.reponame,
  nevra=str(package),sourcerpm=package.sourcerpm,checksum_algorithm=checksum[0],
  checksum=checksum[1],location=package.location if not package._from_system else None)
query=base.sack.query().filter(provides=sys.argv[1])
print(json.dumps(dict(installed=[row(p) for p in query.installed()],
 available=[row(p) for p in query.available()]),sort_keys=True))
base.close()
'''
    rc, out = run(["python3", "-c", program, requirement], timeout=120)
    require(rc == 0, "actual DNF supplier/origin observation failed")
    data = json.loads(out)
    require(isinstance(data, dict) and type(data.get("installed")) is list and
            type(data.get("available")) is list, "malformed native DNF observation")
    return data


def select_actual_supplier(snapshot, arch):
    """No arbitrary candidate, cross-arch, epoch or repo-origin substitution."""
    installed = [p for p in snapshot["installed"] if p.get("arch") in (arch,"noarch")]
    require(len(installed) == 1, "missing or ambiguous installed native supplier")
    supplier = installed[0]
    require(supplier.get("repoid") not in (None,"","@System","@commandline"),
            "installed supplier has no observed transaction origin")
    fields = ("name","epoch","version","release","arch","repoid")
    matches = [p for p in snapshot["available"] if all(p.get(k)==supplier.get(k) for k in fields)]
    require(len(matches)==1, "actual installed provider not uniquely bound to candidate origin")
    row = dict(matches[0]);row["evr"] = f"{row['epoch']}:{row['version']}-{row['release']}"
    return row


def download_rpm(row, destdir):
    """One repo-pinned exact NEVRA and independently checked RPM header."""
    destdir.mkdir(mode=0o700,exist_ok=False)
    rc, out = run(["dnf", "-q", "download", "--disablerepo=*",
                   "--enablerepo="+row["repoid"], "--downloaddir", str(destdir), row["nevra"]])
    require(rc == 0, "repo-pinned provider download failed")
    paths = list(destdir.glob("*.rpm"))
    require(len(paths)==1, "provider download produced ambiguous RPM bytes")
    path=paths[0]
    io=load_sibling("el10-evidence")
    digest=io.digest_file(path)
    rc, header = run(["rpm", "-qp", "--qf", INSTALLED_QF, str(path)])
    require(rc == 0, "downloaded provider RPM header unavailable")
    measured=parse_pipe_row(header.strip(),with_repoid=False)
    require(all(measured.get(k)==row.get(k) for k in ("name","epoch","version","release","arch","sourcerpm")),
            "downloaded RPM header differs from installed supplier")
    row["sha256header"]=measured["sha256header"]
    require(row.get("checksum_algorithm") in ("sha256","sha512"), "DNF package checksum is not strong")
    h=hashlib.new(row["checksum_algorithm"])
    with path.open("rb") as stream:
        while chunk:=stream.read(128*1024):h.update(chunk)
    require(h.hexdigest()==row.get("checksum"), "RPM bytes differ from DNF signed-metadata checksum")
    return path,digest


def installed_observation(row):
    rc, head=run(["rpm","-q","--qf",INSTALLED_QF,row["nevra"]])
    require(rc==0, "actual installed RPM header unavailable")
    header=parse_pipe_row(head.strip(),with_repoid=False)
    require(all(header[k]==row[k] for k in ("name","epoch","version","release","arch","sha256header","sourcerpm")),
            "installed and downloaded provider headers differ")
    result={"header":header}
    for field,option in (("requires","-qR"),("provides","-qP"),("files","-ql")):
        rc,text=run(["rpm",option,row["nevra"]])
        require(rc==0, "actual installed RPM ownership observation failed")
        result[field]=sorted(set(text.splitlines()))
    return result


KEY_POLICIES = {}


def initialize_crypto(factory_key, output):
    """Source-pinned certificates, valid bindings and isolated per-key RPMDBs."""
    policy = json.loads((HERE / "el10-keys/policy.json").read_text())
    items = [("factory", policy["factory"], pathlib.Path(factory_key))] + [
        (entry["path"],entry,HERE / "el10-keys" / entry["path"]) for entry in policy["keys"]]
    for name, entry, path in items:
        io = load_sibling("el10-evidence")
        require(io.digest_file(path,65536) == entry["sha256"], "source-pinned key bytes differ")
        home = pathlib.Path(output) / ("key-" + name.replace(".asc", ""))
        home.mkdir(mode=0o700)
        env = dict(os.environ, GNUPGHOME=str(home), LC_ALL="C")
        rc, detail = run(["gpg", "--batch", "--no-autostart", "--homedir", str(home), "--import", str(path)])
        require(rc == 0, "pinned certificate import failed")
        rc, detail = run(["gpg", "--batch", "--no-autostart", "--homedir", str(home), "--with-colons", "--check-sigs"])
        require(rc == 0, "public-key cryptographic binding verification failed")
        io.check_gpg_colons(detail, entry["primary_fingerprint"], entry["signing_fingerprints"])
        db = home / "rpmdb"; db.mkdir()
        rc, detail = run(["rpm", "--dbpath", str(db), "--initdb"])
        require(rc == 0, "private RPMDB init failed")
        rc, detail = run(["rpmkeys", "--dbpath", str(db), "--import", str(path)])
        require(rc == 0, "private RPMDB pinned key import failed")
        entry = dict(entry, homedir=str(home), rpmdb=str(db))
        if name == "factory":
            KEY_POLICIES["factory"] = entry
        else:
            for repo in entry["repositories"]:
                KEY_POLICIES[repo] = entry
    return policy


def rpm_signature(path, repoid, package):
    """Verify actual signature+digests with ONLY this source-pinned key."""
    group = "factory" if repoid.startswith(PUBLISHED_REPO_PREFIX) else repoid
    entry = KEY_POLICIES.get(group)
    require(entry is not None, "provider has no authoritative signing policy")
    if entry.get("scope") == "bootstrap-only":
        require(package in entry.get("packages", []), "Extras key is bootstrap-only")
    rc, out = run(["rpmkeys", "--dbpath", entry["rpmdb"], "--checksig", "--verbose", str(path)])
    # RPM4.19 may verify both a header and a header+payload signature.
    # Inspect every signature line so malformed/additional entries cannot hide
    # behind one successful substring match.
    lines = [line.strip() for line in out.splitlines()
             if re.search(r"\bsignature\b", line, re.IGNORECASE)]
    matches = [re.fullmatch(
        r"(Header )?V[34] [A-Za-z0-9]+/SHA[0-9]+ Signature, key ID "
        r"([0-9A-Fa-f]{8}|[0-9A-Fa-f]{16}|[0-9A-Fa-f]{40}): OK", line)
        for line in lines]
    require(rc == 0 and 1 <= len(matches) <= 2 and all(matches) and
            "NOT OK" not in out and "NOKEY" not in out and "BAD" not in out,
            "actual RPM digital signature failed")
    if len(matches) == 2:
        require({match.group(1) for match in matches} == {None, "Header "},
                "RPM signatures are not one header and one payload signature")
    fingerprints = []
    for match in matches:
        candidates = [v for v in entry["signing_fingerprints"]
                      if v.endswith(match.group(2).upper())]
        require(len(candidates) == 1,
                "RPM signer does not identify one pinned full fingerprint")
        fingerprints.append(candidates[0])
    require(len(set(fingerprints)) == 1, "RPM signatures have different pinned signers")
    require("digest: OK" in out or "digests signatures OK" in out,
            "actual RPM digest verification missing")
    return {"state":"gpg-ok", "fingerprint":fingerprints[0],
            "primary_fingerprint":entry["primary_fingerprint"],
            "keyid":fingerprints[0][-16:], "key":entry["primary_fingerprint"],
            "cryptographic_signature_verified":True, "digests_verified":True,
            "raw":out}


def container_keyring():
    """Keys the measuring container trusts (rpmdb gpg-pubkey)."""
    rc, out = run(["rpm", "-q", "gpg-pubkey", "--qf", "%{VERSION}|%{SUMMARY}\n"])
    entries = []
    if rc == 0:
        for line in out.splitlines():
            parts = line.split("|", 1)
            keyid = parts[0].strip().upper()
            if re.fullmatch(r"[0-9A-F]{8,40}", keyid or ""):
                entries.append(
                    {"keyid": keyid, "summary": parts[1].strip() if len(parts) > 1 else ""}
                )
    return entries


def repoinfo_map():
    """Observed per-repo metadata (revision, mirror, timestamp)."""
    rc, out = run(["dnf", "repoinfo"], timeout=300)
    repos = {}
    current = None
    for line in out.splitlines():
        head, _, value = line.partition(":")
        field = head.strip().lower()
        if field == "repo-id":
            current = value.strip()
            repos[current] = {"repoid": current}
        elif current is not None and field in ("repo-revision", "repo-updated", "repo-baseurl"):
            repos[current][field[5:]] = value.strip()
    return repos


def pkg_config_version(module):
    rc, out = run(["pkg-config", "--modversion", module])
    if rc != 0:
        return ""
    return out.strip().splitlines()[0].strip() if out.strip() else ""


def dlopen_check(soname):
    """Try loading one SONAME with ctypes; record the outcome."""
    rc, _ = run(
        [
            "python3",
            "-c",
            f"import ctypes; ctypes.CDLL({soname!r})",
        ],
        timeout=60,
    )
    return rc == 0


def ctypes_symbols(soname, symbols):
    """Assert named symbols resolve (grounded oracle shape)."""
    wanted = list(symbols)
    rc, out = run(
        [
            "python3",
            "-c",
            "import ctypes, sys; lib = ctypes.CDLL(sys.argv[1]); "
            "[getattr(lib, s) for s in sys.argv[2:]]",
            soname,
            *wanted,
        ],
        timeout=120,
    )
    return rc == 0, out[:2000]


PC_HEADERS = {
    "dbus-1":"dbus/dbus.h", "glib-2.0":"glib.h", "gtk4":"gtk/gtk.h",
    "gtk4-layer-shell-0":"gtk4-layer-shell.h", "libadwaita-1":"adwaita.h",
    "libdrm":"xf86drm.h", "libinput":"libinput.h", "libsecret-1":"libsecret/secret.h",
    "xkbcommon":"xkbcommon/xkbcommon.h", "xkbcommon-x11":"xkbcommon/xkbcommon-x11.h",
    "gbm":"gbm.h", "libpipewire-0.3":"pipewire/pipewire.h", "libseat":"libseat.h",
    "libsystemd":"systemd/sd-bus.h", "libudev":"libudev.h", "egl":"EGL/egl.h",
    "wayland-client":"wayland-client.h", "wayland-server":"wayland-server.h"}
NAMED_SMOKES = {"rust":["rustc","--version"],"cargo":["cargo","--version"],
    "gcc":["gcc","--version"],"pkgconf-pkg-config":["pkg-config","--version"],
    "clang-devel":["clang","--version"]}


def file_evidence(path):
    """Actual readable bytes and native installed RPM owner, not rpm -ql alone."""
    io = load_sibling("el10-evidence")
    EXECUTION.budget.remaining()
    actual = pathlib.Path(path).resolve(strict=True)
    digest = io.digest_file(actual)
    rc, owner = run(["rpm","-qf","--qf","%{NAME}-%{EPOCHNUM}:%{VERSION}-%{RELEASE}.%{ARCH}\n",str(actual)])
    require(rc == 0 and len(owner.strip().splitlines()) == 1, "actual ABI file native owner missing")
    EXECUTION.budget.remaining()
    return {"path":str(actual),"readable":True,"sha256":digest,"owner_nevra":owner.strip()}


def library_evidence(installed):
    """Observe actual ELF SONAMEs from owned library files; no eight-entry cap."""
    sonames = {}; ownership = {}
    paths = [f for f in installed["files"] if re.search(r"\.so(?:\.[0-9]+)*$",f)]
    require(len(paths)<=256,"excessive ABI library enumeration")
    for path in paths:
        if not pathlib.Path(path).is_file():continue
        rc, out = run(["readelf","-d",path],timeout=30)
        require(rc == 0,"actual ELF dynamic section unavailable")
        names = re.findall(r"\(SONAME\).*?\[([^]\n]+)\]",out)
        if not names:continue
        require(len(names)==1,"ambiguous actual ELF SONAME")
        name = names[0]
        sonames[name]=dlopen_check(name)
        ownership[path]=file_evidence(path)
    return sonames,ownership


def abi_for_pkgconfig(module, installed, op=None, ver=None):
    provides = [p for p in installed["provides"] if p.startswith("pkgconfig(")]
    headers = [f for f in installed["files"] if f.startswith("/usr/include/") and pathlib.Path(f).is_file()]
    require(module in PC_HEADERS,"no explicit native header/compile oracle for capability")
    rc, pcdir = run(["pkg-config","--variable=pcfiledir",module])
    require(rc == 0 and pcdir.strip(),"actual pkgconfig file directory missing")
    pcfile = str(pathlib.Path(pcdir.strip())/(module+".pc"))
    ownership = {pcfile:file_evidence(pcfile)}
    for path in headers:
        ownership[path]=file_evidence(path)
    sonames,libs=library_evidence(installed);ownership.update(libs)
    rc, flags = run(["pkg-config","--cflags","--libs",module])
    require(rc == 0,"actual native SDK flags unavailable")
    import shlex
    flags = shlex.split(flags)
    require(len(flags)<=128 and all(len(v)<1024 for v in flags),"excessive native SDK flags")
    binary=EXECUTION.root/("sdk-"+hashlib.sha256(module.encode()).hexdigest()[:16])
    source=("#include <"+PC_HEADERS[module]+">\nint main(void){return 0;}\n").encode()
    io=load_sibling("el10-evidence")
    compiled=True
    try:
        io.bounded_command(["cc","-Wl,--no-as-needed","-x","c","-o",str(binary),"-",*flags],
            EXECUTION.root/(binary.name+".log"),min(EXECUTION.budget.deadline,__import__("time").monotonic()+60),data=source)
        rc,_=run([str(binary)],timeout=30)
        compiled=rc==0
    except io.CommandFailed:
        compiled=False
    abi = {"oracle":"pkgconfig","pkgconfig_version":pkg_config_version(module),
        "provides":provides,"headers":headers,"sonames":sorted(sonames),"dlopen":sonames,
        "file_ownership":ownership,"pc_file":pcfile,"files_count":len(installed["files"]),
        "sdk_compile_and_run":compiled,"header_test":PC_HEADERS[module]}
    return abi


def abi_for_grounded_gtk4(installed):
    abi = abi_for_pkgconfig(GTK4_LAYER_SHELL_PC,installed)
    abi["oracle"]="grounded:gtk4-layer-shell"
    for field, paths in (("headers_present",GTK4_LAYER_SHELL_HEADERS),("typelibs_present",GTK4_LAYER_SHELL_TYPELIBS)):
        abi[field]={}
        for path in paths:
            present=pathlib.Path(path).is_file() and os.access(path,os.R_OK)
            abi[field][path]=present
            if present:abi["file_ownership"][path]=file_evidence(path)
    license_path="/usr/share/licenses/gtk4-layer-shell/LICENSE"
    abi["license_path"]=license_path
    abi["license_present"]=pathlib.Path(license_path).is_file() and os.access(license_path,os.R_OK)
    if abi["license_present"]:abi["file_ownership"][license_path]=file_evidence(license_path)
    ok,detail=ctypes_symbols(GTK4_LAYER_SHELL_SONAME,GTK4_LAYER_SHELL_SYMBOLS)
    abi.update(symbols_sample=list(GTK4_LAYER_SHELL_SYMBOLS),symbols_resolved=ok,symbols_detail=detail)
    rc,version=run(["python3","-c","import ctypes,json; l=ctypes.CDLL('libgtk4-layer-shell.so.0'); print(json.dumps([getattr(l,'gtk_layer_get_'+p+'_version')() for p in ('major','minor','micro')]))"],timeout=30)
    abi["library_version"]=json.loads(version) if rc==0 else None
    return abi


def abi_for_grounded_libseat(installed):
    abi=abi_for_pkgconfig(LIBSEAT_PC,installed)
    abi["oracle"]="grounded:libseat"
    rc,out=run(["seatd","-h"],timeout=30)
    abi.update(seatd_smoke_rc=rc,seatd_smoke_head=out)
    return abi


def abi_for_named(name,installed,evr):
    sonames,ownership=library_evidence(installed)
    files=[f for f in installed["files"] if pathlib.Path(f).is_file()]
    require(files,"actual named provider has no readable payload")
    # A named package without a command (theme/schema/protocol data) carries
    # actual installed content, not a fabricated smoke executable.
    ownership[files[0]]=file_evidence(files[0])
    smoke=[]
    if name in NAMED_SMOKES:
        rc,text=run(NAMED_SMOKES[name],timeout=30)
        smoke=[{"cmd":NAMED_SMOKES[name],"rc":rc,"head":text}]
    if name=="wayland-protocols-devel":
        path="/usr/share/wayland-protocols/staging/ext-session-lock/ext-session-lock-v1.xml"
        ownership[path]=file_evidence(path)
        rc,text=run(["pkg-config","--atleast-version=1.41","wayland-protocols"],timeout=30)
        smoke=[{"cmd":"wayland-protocols-version-and-owned-session-lock-xml","rc":rc,"head":text}]
    return {"oracle":"named-package","evr":evr,"provides_count":len(installed["provides"]),
        "provides_sample":installed["provides"][:30],"files_count":len(installed["files"]),
        "headers":[],"sonames":sorted(sonames),"dlopen":sonames,"file_ownership":ownership,
        "smoke_required":name in NAMED_SMOKES or name=="wayland-protocols-devel","smoke":smoke}


def baseline_probe(arch, baseline):
    """Genuine native compile/ELF/instruction execution, not a copied label."""
    require((arch,baseline) in (("x86_64","x86-64-v3"),("aarch64","armv8-a")), "unknown subset CPU baseline")
    io=load_sibling("el10-evidence")
    output=EXECUTION.root/"cpu-baseline-probe"
    if arch=="x86_64":
        source=b"#include <immintrin.h>\nint main(int n,char **v){volatile int x=n; __m256i a=_mm256_set1_epi32(x); a=_mm256_add_epi32(a,a); __m256 b=_mm256_set1_ps((float)x); b=_mm256_fmadd_ps(b,b,b); volatile unsigned long t=_pext_u64(0x42,0xf0); volatile unsigned c=_lzcnt_u32(1); return _mm256_extract_epi32(a,0)!=2 || _mm256_cvtss_f32(b)!=2.0f || t!=4 || c!=31;}\n"
        flags=["-march=x86-64-v3"]
        expected_machine=62
    else:
        source=b"#include <arm_neon.h>\nint main(int n,char **v){volatile int x=n; int32x4_t a=vdupq_n_s32(x); a=vaddq_s32(a,a); return vgetq_lane_s32(a,0)!=2;}\n"
        flags=["-march=armv8-a"]
        expected_machine=183
    io.bounded_command(["cc","-O0",*flags,"-x","c","-o",str(output),"-"],
        EXECUTION.root/"cpu-baseline-compile.log",min(EXECUTION.budget.deadline,__import__("time").monotonic()+60),data=source)
    with output.open("rb") as stream:header=stream.read(64)
    require(header[:6]==b"\x7fELF\x02\x01" and int.from_bytes(header[18:20],"little")==expected_machine,
            "actual CPU probe ELF architecture differs")
    rc,text=run([str(output)],timeout=30)
    require(rc==0,"actual native baseline instruction execution failed")
    return {"baseline":baseline,"elf_machine":expected_machine,"compile_flags":flags,
            "source_sha256":sha256_bytes(source),"executable_sha256":io.digest_file(output),
            "execution_rc":rc,"scope":"actual sampled baseline instructions, not exhaustive CPU ISA/package-code audit"}


def platform_proof(arch, image_arch, runner_machine, base_digest, cpu_baseline):
    """Native-execution counterproof: container, runner, and image agree."""
    rc, out = run(["uname", "-m"], timeout=30)
    container_machine = out.strip().splitlines()[0].strip() if rc == 0 and out.strip() else ""
    os_id, os_version = "", ""
    try:
        for line in pathlib.Path("/etc/os-release").read_text().splitlines():
            if line.startswith("ID="):
                os_id = line[3:].strip().strip('"')
            elif line.startswith("VERSION_ID="):
                os_version = line[11:].strip().strip('"')
    except OSError:
        pass
    return {
        "arch": arch,
        "container_machine": container_machine,
        "runner_machine": runner_machine,
        "image_arch": image_arch,
        "base_digest": base_digest,
        "cpu_baseline": cpu_baseline,
        "os_id": os_id,
        "os_version": os_version,
        "baseline_probe":baseline_probe(arch,cpu_baseline),
    }


def measure_cell(plan, receipt_cell, args, closure):
    """Run the full native measurement for one cell; return (report, gaps)."""
    arch = plan["arch"]
    out = pathlib.Path(args.out)
    evidence_dir = out / "evidence"
    rpm_dir = out / "rpm-bytes"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    rpm_dir.mkdir(parents=True, exist_ok=True)
    gaps = []
    global EXECUTION
    evidence_io = load_sibling("el10-evidence")
    evidence_io.capacity(out, 12 * 1024**3)
    EXECUTION = getattr(args,"execution",None) or evidence_io.Execution(out / "commands")

    key_policy = initialize_crypto(args.factory_key, out)
    key_src = pathlib.Path(args.factory_key).read_bytes()
    require(key_src, "factory key bytes empty")
    key_path = pathlib.Path(CONTAINER_KEYRING_PATH)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key_src)
    rc, out_text = run(["rpm", "--import", str(key_path)])
    require(rc == 0, f"rpm key import failed: {out_text[:2000]}")

    configure_system_repos()
    for argv in dnf_preamble(plan["repos"]["build"]):
        rc, text = run(argv, timeout=900)
        require(rc == 0, f"repo setup failed ({' '.join(argv)}): {text[:2000]}")
        configure_system_repos()

    repo_path = pathlib.Path("/etc/yum.repos.d") / PUBLISHED_REPO_FILE
    if repo_path.exists():
        repo_path.unlink()
    repo_entries = []
    with repo_path.open("a", encoding="utf-8") as stream:
        for index, baseurl in enumerate(plan["repos"]["published_index"]):
            stream.write(build_repo_section(index, baseurl, key_path))
            repo_entries.append(
                {"name": f"{PUBLISHED_REPO_PREFIX}{index}", "baseurl": baseurl,
                 "gpgcheck": True, "repo_gpgcheck": True}
            )
    rc, text = run(["dnf", "-qy", "makecache"], timeout=900)
    require(rc == 0, f"makecache over the cell repo set failed: {text[:2000]}")

    gnupg_home = pathlib.Path(KEY_POLICIES["factory"]["homedir"])

    index_proof = {}
    asc_states = {}
    index_files = {}
    for slot, baseurl in enumerate(plan["repos"]["published_index"]):
        provenance, asc, files = index_provenance(baseurl, evidence_dir, slot, gnupg_home)
        index_proof[provenance["baseurl"]] = provenance
        asc_states[provenance["baseurl"]] = asc
        index_files[provenance["baseurl"]] = files
        if provenance["primary_checksum_algorithm"] == "sha256" and provenance["primary_sha256"] != provenance["primary_sha256_declared"]:
            gaps.append(f"index {baseurl} primary bytes differ from repomd")
        if not (asc["present"] and asc["verified"]):
            gaps.append(f"index {baseurl} repomd signature unverified")

    observed_repos=observed_repo_configuration()
    system_repos = repoinfo_map()
    keyring = container_keyring()
    key_names = {entry["keyid"]: entry.get("summary") or entry["keyid"] for entry in keyring}

    requires = plan["requires"]
    ordered = [
        (kind, item) for kind in ("build", "runtime") for item in requires[kind]
    ]
    dependencies = {}
    packages = []
    rpm_k = []
    artifacts = []
    missing = []
    conflicts = []
    header_slot = 0

    for kind, requirement in ordered:
        rc, text = run(["dnf", "-y", "install", requirement], timeout=1200)
        if rc != 0:
            missing.append(requirement)
            gaps.append(f"require {requirement} solver walk failed")
            continue
        snapshot = dnf_provider_snapshot(requirement)
        native_installed = [p for p in snapshot["installed"] if p.get("arch") in (arch,"noarch")]
        if len(native_installed) == 1 and native_installed[0].get("repoid") in (None,"","@System","@commandline"):
            existing = native_installed[0]
            fields = ("name","epoch","version","release","arch")
            candidates = [p for p in snapshot["available"] if all(p.get(k)==existing.get(k) for k in fields)]
            require(len(candidates)==1, "inherited supplier origin unknown/ambiguous; cannot bind it")
            chosen=candidates[0]
            rc,text=run(["dnf","-y","--disablerepo=*","--enablerepo="+chosen["repoid"],
                         "reinstall",chosen["nevra"]],timeout=600)
            require(rc==0, "genuine inherited-package reinstall did not establish transaction origin")
            snapshot=dnf_provider_snapshot(requirement)
        row = select_actual_supplier(snapshot, arch)
        nevras = [p["nevra"] for p in snapshot["available"]]
        served_from, provenance = _serving_evidence(
            row["repoid"], plan["repos"]["published_index"], index_proof, system_repos
        )
        if served_from is None:
            missing.append(requirement)
            gaps.append(
                f"require {requirement} provider {row['nevra']} served from "
                f"{row['repoid']}, outside the measured repo set"
            )
            continue
        repourl = served_from.get("repourl") or served_from.get("baseurl")
        repourl=repourl.rstrip("/")+"/"
        if row["repoid"].startswith(PUBLISHED_REPO_PREFIX):
            served_from["repourl"]=repourl
        else:
            served_from["baseurl"]=repourl
        if repourl not in index_proof:
            key=KEY_POLICIES.get(row["repoid"])
            require(key is not None and key.get("scope")!="bootstrap-only", "no provider index signing policy")
            provenance,asc,files=index_provenance(repourl,evidence_dir,len(index_files),
                pathlib.Path(key["homedir"]),key["signing_fingerprints"])
            index_proof[repourl]=provenance;asc_states[repourl]=asc;index_files[repourl]=files
            require(asc["verified"],"actual system index signature unverified")
        provenance=index_proof[repourl]

        rpm_path, rpm_sha = download_rpm(row, rpm_dir / f"provider-{header_slot:03d}")
        installed = installed_observation(row)
        signature = rpm_signature(rpm_path, row["repoid"], row["name"])
        signature["key"] = key_names.get(signature["keyid"], args.key_name)
        origin_path=evidence_dir/f"origin-{header_slot:03d}.json"
        origin_path.write_text(json.dumps({"require":requirement,"arch":arch,"snapshot":snapshot},
                                         indent=2,sort_keys=True)+"\n")
        artifacts.append({"name":f"evidence/{origin_path.name}","kind":"provider-origin",
            "sha256":evidence_io.digest_file(origin_path),"run_id":args.run_id,"require":requirement})
        header_record = {
            "require": requirement,
            "nevra": row["nevra"],
            "header": installed["header"],
            "requires": installed["requires"],
            "provides": installed["provides"],
            "files": installed["files"],
        }
        header_path = evidence_dir / f"headers-{header_slot}.json"
        header_slot += 1
        header_path.write_text(json.dumps(header_record, indent=2, sort_keys=True) + "\n")
        artifacts.append(
            {"name": f"evidence/{header_path.name}", "kind": "provider-headers",
             "sha256": sha256_bytes(header_path.read_bytes()), "run_id": args.run_id,
             "require": requirement}
        )
        # Every selected provider, including distro packages, retains exact bytes.
        kept = evidence_dir / f"provider-{header_slot:03d}.rpm"
        shutil.copyfile(rpm_path, kept)
        artifacts.append(
            {"name": f"evidence/{kept.name}", "kind": "provider-rpm",
             "sha256": rpm_sha, "run_id": args.run_id, "require": requirement,
             "repoid": row["repoid"], "nevra":row["nevra"]}
        )
        signature_path=evidence_dir / f"signature-{header_slot:03d}.json"
        signature_path.write_text(json.dumps({"require":requirement,"nevra":row["nevra"],
            "rpm_sha256":rpm_sha,"signature":signature},indent=2,sort_keys=True)+"\n")
        artifacts.append({"name":f"evidence/{signature_path.name}","kind":"provider-signature",
            "sha256":evidence_io.digest_file(signature_path),"run_id":args.run_id,"require":requirement})
        rpm_path.unlink(missing_ok=True)

        module, op, ver = split_pkgconfig(requirement)
        if module == GTK4_LAYER_SHELL_PC:
            abi = abi_for_grounded_gtk4(installed)
        elif module == LIBSEAT_PC:
            abi = abi_for_grounded_libseat(installed)
        elif module is not None:
            abi = abi_for_pkgconfig(module, installed, op, ver)
        else:
            abi = abi_for_named(requirement, installed, row["evr"])
        if row["arch"]=="noarch":
            regular=[path for path in installed["files"] if pathlib.Path(path).is_file()]
            require(len(regular)<=100000,"excessive noarch payload enumeration")
            elf=False
            for path in regular:
                EXECUTION.budget.remaining()
                with pathlib.Path(path).open("rb") as stream:
                    if stream.read(4)==b"\x7fELF":elf=True
            abi["architecture_neutral_payload"] = not elf
            abi["noarch_regular_files_checked"] = len(regular)
        constraint = None
        if op is not None:
            observed = abi.get("pkgconfig_version") or ""
            satisfied = (
                bool(observed)
                and load_sibling("repo_index").satisfies("rpm", observed, op, ver)
            )
            constraint = {"op": op, "version": ver, "observed": observed,
                          "satisfied": satisfied, "dnf_arbitrated": True}
            if not satisfied:
                conflicts.append(requirement)
                gaps.append(f"require {requirement} observed {observed} fails {op} {ver}")

        if signature["state"] != "gpg-ok":
            missing.append(requirement)
            gaps.append(f"require {requirement} provider {row['nevra']} signature unverified")
            continue
        entry = {
            "require": requirement,
            "kind": kind,
            "status": "resolved",
            "providers": nevras,
            "selected": {
                "nevra": row["nevra"],
                "evr": row["evr"],
                "arch": row["arch"],
                "repoid": row["repoid"],
                "repourl": repourl,
                "sourcerpm": row["sourcerpm"],
                "sha256header": row["sha256header"],
                "rpm_sha256": rpm_sha,
                "signature": signature,
                "dnf_arbitrated": True,
            },
            "served_from": served_from,
            "abi": abi,
            "constraint": constraint,
        }
        entry["index_provenance"] = provenance
        entry["index_signature"] = asc_states[repourl]
        dependencies[requirement] = [entry]
        packages.append(
            {"name": row["name"], "require": requirement, "status": "resolved",
             "evr": row["evr"]}
        )
        rpm_k.append({"nevra": row["nevra"], "state": signature["state"],
                      "keyid": signature["keyid"], "fingerprint":signature["fingerprint"]})

    for baseurl, files in index_files.items():
        for kind, suffix in (("repomd-xml", files["repomd"]), ("primary-xml", files["primary"])):
            artifacts.append(
                {"name": f"evidence/{suffix.name}", "kind": kind,
                 "sha256": sha256_bytes(suffix.read_bytes()), "run_id": args.run_id,
                 "index_url": baseurl}
            )
        if files["status"] is not None:
            artifacts.append({"name": f"evidence/{files['status'].name}",
                "kind":"repomd-gpg-status", "sha256":evidence_io.digest_file(files["status"]),
                "run_id":args.run_id, "index_url":baseurl})
        if files["asc"] is not None:
            artifacts.append(
                {"name": f"evidence/{files['asc'].name}", "kind": "repomd-asc",
                 "sha256": sha256_bytes(files["asc"].read_bytes()),
                 "run_id": args.run_id, "index_url": baseurl}
            )

    plan_bytes = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    receipt = json.loads(pathlib.Path(args.receipt).read_bytes())
    receipt_bytes = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    report = {
        "schema": 1,
        "cell": plan["cell"],
        "arch": arch,
        "plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
        "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        "source_sha": receipt["source_sha"],
        "base_image": receipt_cell["base_image"],
        "base_reference": receipt_cell.get("base_reference", ""),
        "producer": {
            "run_id": args.run_id,
            "run_attempt": args.run_attempt,
            "run_url": args.run_url,
            "head_sha": args.head_sha,
            "workflow": args.workflow,
            "cell_id": plan["cell"],
            "action_key": args.action_key,
        },
        "platform_proof": platform_proof(
            arch, args.image_arch, args.runner_machine, args.base_digest,
            receipt_cell.get("cpu_baseline", ""),
        ),
        "repo_config": {
            "gpgcheck": True,
            "repo_gpgcheck": True,
            "gpgkey": f"file://{CONTAINER_KEYRING_PATH}",
            "repos": observed_repos,
            "published_repos":repo_entries,
        },
        "rpm_keyring": keyring,
        "signatures": {
            "signed_by": "tunaos-factory",
            "scope": "per-RPM/index verifier results; report authentication requires independent original CI custody",
            "key": args.key_name,
            "keyid": args.key_id,
            "fingerprint":KEY_POLICIES["factory"]["signing_fingerprints"][-1],
            "rpm_K": rpm_k,
            "repomd_asc": asc_states,
        },
        "dependencies": dependencies,
        "covers_requires": {
            "build": list(requires["build"]),
            "runtime": list(requires["runtime"]),
        },
        "missing": missing,
        "abi_conflicts": conflicts,
        "packages": packages,
        "indexes": index_proof,
        "artifacts": artifacts,
    }
    EXECUTION.budget.remaining()
    load_sibling("el10-evidence").write_bundle(report, out)
    EXECUTION.budget.remaining()
    return report, gaps


def _serving_evidence(repoid, plan_indexes, index_proof, system_repos):
    """Serving-repo evidence for a selected provider: (served_from, provenance).

    Published-index providers bind to the measured index provenance;
    system-repo providers bind to the observed repoinfo revision, mirror
    baseurl, and timestamp. Anything else is outside the measured set.
    """
    if repoid.startswith(PUBLISHED_REPO_PREFIX):
        try:
            slot = int(repoid[len(PUBLISHED_REPO_PREFIX):])
        except ValueError:
            return None, None
        if not 0 <= slot < len(plan_indexes):
            return None, None
        base = plan_indexes[slot].rstrip("/") + "/"
        provenance = index_proof.get(base)
        if provenance is None:
            return None, None
        return (
            {"kind": "published-index", "repoid": repoid, "repourl": base},
            provenance,
        )
    info = system_repos.get(repoid)
    if not info or not info.get("revision") or not info.get("baseurl"):
        return None, None
    return (
        {
            "kind": "system-repo",
            "repoid": repoid,
            "baseurl": info["baseurl"],
            "revision": info["revision"],
            "updated": info.get("updated", ""),
        },
        None,
    )


# --- Offline subcommands (plan, policy, admit, verify-artifacts) ---


def cmd_plan(args):
    """Derive a measurement plan from factory + recipe + staged cell."""
    import yaml

    closure = load_sibling("el10-rpm-closure")
    stager = load_sibling("stage-tuna-desktop-el10")
    factory = yaml.safe_load(pathlib.Path(args.factory).read_text())
    recipe = yaml.safe_load(pathlib.Path(args.recipe).read_text())
    cell = json.loads(args.cell_json)
    target = cell["target"]
    cfg = (factory.get("targets") or {}).get(target)
    require(isinstance(cfg, dict) and cfg, f"factory has no target {target}")
    caps = ((recipe.get("dependencies") or {}).get("build") or {}).get("capabilities") or []
    mapped = []
    for cap in caps:
        names = ((factory.get("dependency_catalog") or {}).get(cap) or {}).get(target)
        require(isinstance(names, list) and names, f"missing native capability: {target}/{cap}")
        mapped.extend(names)
    cell["base_image"] = args.base_image
    plan = closure.plan_base_closure(cell, cfg, mapped + stager.BUILD, stager.RUNTIME)
    pathlib.Path(args.out).write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"plan": args.out, "plan_sha256": closure.canonical_sha256(plan)}))
    return 0


def cmd_policy(args):
    """Derive an admission policy from factory + keyring + run identity."""
    import yaml

    factory = yaml.safe_load(pathlib.Path(args.factory).read_text())
    cfg = (factory.get("targets") or {}).get("el10")
    require(isinstance(cfg, dict) and cfg, "factory has no el10 target")
    raw_index = cfg.get("published_index") or {}
    require(isinstance(raw_index, dict) and raw_index, "el10 declares no per-arch index")
    expected = {}
    for arch, entry in raw_index.items():
        urls = [entry] if isinstance(entry, str) else list(entry)
        require(urls and all(isinstance(u, str) and u.strip() for u in urls),
                f"el10/{arch} index malformed")
        expected[arch] = urls
    io = load_sibling("el10-evidence")
    pins = io.load_json(HERE / "el10-keys/policy.json", 65536)
    require(io.digest_file(args.keyring, 65536) == pins["factory"]["sha256"],
            "factory public certificate differs from reviewed source pin")
    require(re.fullmatch(r"[0-9a-f]{40}", args.head_sha or "") is not None,
            "policy head must be a full commit SHA")
    trusted = [{"keyid": value, "fingerprint": value}
               for value in pins["factory"]["signing_fingerprints"]]
    system_keys = {}
    bootstrap = {}
    for entry in pins["keys"]:
        require(io.digest_file(HERE / "el10-keys" / entry["path"], 65536) == entry["sha256"],
                "system certificate differs from reviewed source pin")
        for repo in entry["repositories"]:
            if entry.get("scope") == "bootstrap-only":
                require(entry.get("packages") == ["epel-release"],
                        "Extras bootstrap allowlist must remain exactly epel-release")
                bootstrap[repo] = {"packages": entry["packages"],
                                   "fingerprints": entry["signing_fingerprints"]}
            else:
                system_keys[repo] = entry["signing_fingerprints"]
    policy = {
        "trusted_heads": [args.head_sha], "trusted_workflow": args.workflow,
        "trusted_keys": trusted, "system_keys": system_keys,
        "bootstrap_keys": bootstrap, "expected_indexes": expected,
        "key_policy_sha256": io.digest_file(HERE / "el10-keys/policy.json", 65536),
    }
    pathlib.Path(args.out).write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"policy": str(args.out), "keys": len(trusted),
                      "indexes": {a: len(u) for a, u in expected.items()}}))
    return 0


def cmd_measure(args):
    """Run the native measurement (inside the container only)."""
    closure = load_sibling("el10-rpm-closure")
    plan = json.loads(pathlib.Path(args.plan).read_text())
    receipt = json.loads(pathlib.Path(args.receipt).read_text())
    _, cells = closure.validate_candidate_receipt(receipt)
    key = plan["cell"]
    cell = next(
        (c for c in cells if f"{c['consumer']}-{c['target']}-{c['arch']}" == key),
        None,
    )
    require(cell is not None, f"plan cell {key} is not staged")
    require(plan.get("arch") == args.arch == cell["arch"], "arch mismatch across inputs")
    report, gaps = measure_cell(plan, cell, args, closure)
    print(json.dumps({"report": str(pathlib.Path(args.out) / "report.json"),
                      "missing": report["missing"], "abi_conflicts": report["abi_conflicts"],
                      "gaps": gaps}))
    if gaps:
        print(f"measurement completed with {len(gaps)} gap(s); admission will fail closed",
              file=sys.stderr)
        return 3
    return 0


def cmd_admit(args):
    """Judge a report against plan + receipt + policy; fail closed."""
    closure = load_sibling("el10-rpm-closure")
    report = json.loads(pathlib.Path(args.report).read_text())
    plan = json.loads(pathlib.Path(args.plan).read_text())
    receipt = json.loads(pathlib.Path(args.receipt).read_text())
    policy = json.loads(pathlib.Path(args.policy).read_text())
    try:
        summary = closure.admit_producer_closure(report, plan, receipt, policy)
    except ValueError as error:
        print(f"admission rejected: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0



def cmd_validate_local(args):
    """Local native consistency, explicitly not original completed-run admission."""
    closure=load_sibling("el10-rpm-closure");io=load_sibling("el10-evidence")
    report=io.verify_bundle(pathlib.Path(args.report).parent)
    if report!=io.load_json(args.report):raise ValueError("report path differs from local bundle")
    summary=closure.validate_producer_evidence(report,io.load_json(args.plan),
        io.load_json(args.receipt),io.load_json(args.policy))
    print(json.dumps(dict(summary,scope="local native consistency only; original API custody pending")))
    return 0


def cmd_admit_run(args):
    """Independent completed-run admission from original GitHub API and ZIP bytes."""
    io=load_sibling("el10-evidence");authority=load_sibling("el10-producer-custody")
    closure=load_sibling("el10-rpm-closure");inputs=load_sibling("el10-producer-inputs")
    root=pathlib.Path(args.out)
    root.mkdir(mode=0o700,parents=True)
    execution=io.Execution(root/'observer-commands',seconds=600)
    def fetch(path,cap):
        endpoint='/repos/'+authority.REPOSITORY+'/'+path
        if path.endswith('/zip'):
            execution.index+=1;target=execution.root/f'archive-{execution.index:05d}.zip'
            io.bounded_command(['gh','api','--hostname','github.com',endpoint],target,
                min(execution.budget.deadline,time.monotonic()+180),limit=cap,
                stderr_path=execution.root/f'archive-{execution.index:05d}.stderr.log')
            return target
        return execution.command(['gh','api','--hostname','github.com',endpoint],timeout=60,limit=cap)
    custody,reports=authority.admit_original(fetch,HERE.parent,args.head_sha,args.run_id,root,deadline=execution.budget.deadline)
    summaries=[]
    for cell,report in reports.items():
        directory=root/cell
        plan=io.load_json(directory/'plan.json');receipt=io.load_json(directory/'candidate-inputs.json')
        policy=io.load_json(directory/'policy.json');consumer=io.load_json(directory/'checked-consumer.json')
        if (closure.canonical_sha256(plan)!=report.get('plan_sha256') or
            closure.canonical_sha256(receipt)!=report.get('receipt_sha256') or
            receipt.get('vendor_receipt')!=consumer or
            io.digest_file(directory/'cargo-metadata.json',64*1024**2)!=consumer.get('metadata_json_sha256')):
            raise ValueError('actual original plan/receipt/offline metadata bytes differ from report')
        inputs.validate_checked_consumer(consumer,source_sha=receipt['source_sha'],
            lock_sha256=receipt['lock_sha256'],archive_sha256=receipt['vendor_archive_sha256'],
            source_archive_sha256=receipt['source_archive_sha256'],head_sha=custody.head,
            run_id=custody.run,run_attempt=custody.attempt)
        metadata=io.load_json(directory/'cargo-metadata.json',64*1024**2)
        inputs.validate_metadata_payload(metadata)
        execution.budget.remaining()
        summaries.append(closure.admit_producer_closure(report,plan,receipt,policy,
                         custody=custody,artifact_dir=directory))
    execution.budget.remaining()
    result={'schema':1,'head_sha':custody.head,'run_id':custody.run,'run_attempt':custody.attempt,
            'original_archive_sha256':dict(custody.archive_hashes),'cells':summaries,
            'scope':'two-architecture native producer evidence only; not all 14 variants or publication'}
    (root/'admitted.json').write_text(json.dumps(result,indent=2)+'\n')
    execution.budget.remaining()
    print(json.dumps(result,indent=2))
    return 0

def cmd_verify_artifacts(args):
    """Actual finalizer's external manifest, bytes and semantic payload agree."""
    observed = load_sibling("el10-evidence").verify_bundle(args.dir)
    if observed != json.loads(pathlib.Path(args.report).read_text()):
        raise ValueError("report path differs from actual original bundle")
    print(json.dumps({"verified": len(observed["artifacts"])}))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="derive a measurement plan")
    plan.add_argument("--factory", required=True)
    plan.add_argument("--recipe", required=True)
    plan.add_argument("--cell-json", required=True)
    plan.add_argument("--base-image", required=True)
    plan.add_argument("--out", required=True)
    plan.set_defaults(func=cmd_plan)

    policy = sub.add_parser("policy", help="derive an admission policy")
    policy.add_argument("--factory", required=True)
    policy.add_argument("--keyring", required=True)
    policy.add_argument("--head-sha", required=True)
    policy.add_argument("--workflow", required=True)
    policy.add_argument("--out", required=True)
    policy.set_defaults(func=cmd_policy)

    measure = sub.add_parser("measure", help="run the native measurement")
    measure.add_argument("--plan", required=True)
    measure.add_argument("--receipt", required=True)
    measure.add_argument("--arch", required=True)
    measure.add_argument("--out", required=True)
    measure.add_argument("--factory-key", required=True)
    measure.add_argument("--key-name", required=True)
    measure.add_argument("--key-id", required=True)
    measure.add_argument("--image-arch", required=True)
    measure.add_argument("--runner-machine", required=True)
    measure.add_argument("--base-digest", required=True)
    measure.add_argument("--run-id", required=True, type=int)
    measure.add_argument("--run-attempt", required=True, type=int)
    measure.add_argument("--run-url", required=True)
    measure.add_argument("--head-sha", required=True)
    measure.add_argument("--workflow", required=True)
    measure.add_argument("--action-key", required=True)
    measure.set_defaults(func=cmd_measure)

    admit = sub.add_parser("admit", help="judge a measured report")
    admit.add_argument("--report", required=True)
    admit.add_argument("--plan", required=True)
    admit.add_argument("--receipt", required=True)
    admit.add_argument("--policy", required=True)
    admit.set_defaults(func=cmd_admit)

    local = sub.add_parser("validate-local", help="native local consistency; no API admission")
    for name in ("report","plan","receipt","policy"):
        local.add_argument("--"+name,required=True)
    local.set_defaults(func=cmd_validate_local)
    original=sub.add_parser("admit-run",help="original completed-run API/ZIP/source admission")
    original.add_argument("--run-id",required=True,type=int)
    original.add_argument("--head-sha",required=True)
    original.add_argument("--out",required=True)
    original.set_defaults(func=cmd_admit_run)

    verify = sub.add_parser("verify-artifacts", help="compare bound artifact hashes")
    verify.add_argument("--report", required=True)
    verify.add_argument("--dir", required=True)
    verify.set_defaults(func=cmd_verify_artifacts)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.exit(1, f"el10 producer rejected: {error}\n")


if __name__ == "__main__":
    sys.exit(main())
