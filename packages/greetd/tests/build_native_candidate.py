#!/usr/bin/env python3
"""CI-only native greetd build/admission; no package or image publication."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import tarfile
import time
import urllib.request

import ci_admission
from retirement_peer import digest

ARCHIVE_URL = 'https://git.sr.ht/~kennylevinsen/greetd/archive/0.10.3.tar.gz'
ARCHIVE_SHA = 'ee5cb70e0add4ca9c9fe57e47581ab0002d44c07743fb5492469f3b570db640b'


def admitted_child_environment(root, architecture):
    """Admit this run with the API token, then keep it out of child processes."""
    admission = ci_admission.current(root, architecture)
    child_env = os.environ.copy()
    child_env.pop('GH_TOKEN', None)
    return admission, child_env


def bounded_command(argv, path, deadline, *, data=None, limit=4*1024*1024, env=None):
    """Linux single-owner command retirement, including orphaned descendants.

    Require one thread and no preexisting children. A temporary subreaper scope
    owns adopted descendants. Keep the session leader unreaped until group kill
    so its PID cannot be reused. Adopted children are killed via pidfds and reaped.
    Cleanup has a separate five-second budget; unconfirmed retirement fails closed.
    Subreaper ownership lasts only while this process lives, not after CLI exit.
    Spawn/kernel I/O stalls are not hard real-time guarantees.
    """
    def children():
        return [int(value) for value in Path('/proc/self/task/'+str(os.getpid())+'/children').read_text().split()]
    if len(list(Path('/proc/self/task').iterdir()))!=1 or children():
        raise ValueError('command supervisor requires exclusive single-thread child ownership')
    if deadline-time.monotonic()<=0:raise TimeoutError('absolute publisher deadline')
    if data is not None and len(data)>4096:raise ValueError('credential stdin exceeds bound')
    libc=ctypes.CDLL(None,use_errno=True)
    prior=ctypes.c_int()
    if libc.prctl(37,ctypes.byref(prior),0,0,0)!=0 or libc.prctl(36,1,0,0,0)!=0:
        raise OSError('command subreaper setup failed')
    proc=None;lingered=False;retired=False;primary=None
    try:
        with path.open('wb') as f:
            proc=subprocess.Popen(argv,stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                                  stdout=f,stderr=subprocess.DEVNULL,start_new_session=True,env=env)
            if data is not None:
                proc.stdin.write(data);proc.stdin.close()
            while True:
                # WNOWAIT preserves the owned leader PID until group retirement.
                status=os.waitid(os.P_PID,proc.pid,os.WEXITED|os.WNOHANG|os.WNOWAIT)
                if status is not None:break
                if time.monotonic()>=deadline or f.tell()>limit:raise TimeoutError('publisher command bound')
                time.sleep(.02)
            if status.si_code!=os.CLD_EXITED or status.si_status!=0 or f.tell()>limit:
                raise RuntimeError('publisher command failed: '+Path(argv[0]).name)
    except BaseException as error:
        primary=error
    finally:
        if proc is not None:
            if proc.stdin is not None and not proc.stdin.closed:proc.stdin.close()
            # The unreaped owned leader reserves the PGID even after exit.
            # Always kill the group: a successful leader can leave writers alive.
            try:
                os.killpg(proc.pid,signal.SIGKILL)
            except ProcessLookupError:
                pass
            cleanup_deadline=time.monotonic()+5
            while time.monotonic()<cleanup_deadline:
                direct=children()
                if not direct:
                    retired=True;break
                for pid in direct:
                    if pid!=proc.pid:lingered=True
                    try:
                        fd=os.pidfd_open(pid)
                    except ProcessLookupError:
                        continue
                    try:
                        # Direct children cannot be stolen/reused without our
                        # reaping; pidfd also binds the actual kernel process.
                        signal.pidfd_send_signal(fd,signal.SIGKILL)
                    finally:
                        os.close(fd)
                while True:
                    try:pid,status=os.waitpid(-1,os.WNOHANG)
                    except ChildProcessError:break
                    if pid==0:break
                    if pid==proc.pid:proc.returncode=os.waitstatus_to_exitcode(status)
                time.sleep(.01)
            if not retired and not children():retired=True
        else:
            retired=not children()
        if retired:
            if libc.prctl(36,prior.value,0,0,0)!=0:
                raise OSError('command subreaper restore failed')
        else:
            # Keep ownership while this process lives; an ensuing CLI exit is not
            # physical retirement or persistent quarantine proof.
            raise RuntimeError('command retirement unconfirmed; publisher must abort') from primary
    if primary is not None:raise primary
    if lingered:raise RuntimeError('command left adopted descendants; retired and refused')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--architecture', choices=['x86_64', 'aarch64'], required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--cargo', type=Path, required=True)
    parser.add_argument('--rustc', type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError('native command owner requires root to retire its actual PAM descendants')
    root = Path(__file__).resolve().parents[3]
    cargo, rustc = args.cargo.resolve(strict=True), args.rustc.resolve(strict=True)
    output = args.out.absolute()
    if root == output or root in output.parents:
        raise ValueError('native candidate evidence must remain outside source checkout')
    before, env = admitted_child_environment(root, args.architecture)
    output.mkdir(mode=0o755, parents=False, exist_ok=False)
    deadline = time.monotonic() + 1800
    archive = output / 'greetd-0.10.3.tar.gz'
    with urllib.request.urlopen(ARCHIVE_URL, timeout=30) as response:
        raw = response.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024 or hashlib.sha256(raw).hexdigest() != ARCHIVE_SHA:
        raise ValueError('actual pinned upstream source archive bytes differ')
    archive.write_bytes(raw)
    with tarfile.open(archive) as source:
        entries = source.getmembers()
        if len(entries) > 128 or len({entry.name for entry in entries}) != len(entries):
            raise ValueError('bounded unique source members required')
        for entry in entries:
            parts = Path(entry.name).parts
            if (not parts or parts[0] != 'greetd-0.10.3' or entry.name.startswith('/')
                    or '..' in parts or entry.size > 256 * 1024):
                raise ValueError('protected source member differs')
            license_link = (entry.name == 'greetd-0.10.3/greetd_ipc/LICENSE'
                            and entry.issym() and entry.linkname == '../LICENSE')
            if not (entry.isfile() or entry.isdir() or license_link):
                raise ValueError('unexpected source link/device')
        if sum(entry.size for entry in entries) > 1024 * 1024:
            raise ValueError('source expanded bytes exceed bound')
        source.extractall(output, filter='data')
    workspace = output / 'greetd-0.10.3'
    lock_sha = hashlib.sha256((workspace / 'Cargo.lock').read_bytes()).hexdigest()
    patch = root / 'packages/greetd/0001-Owned-authentication-worker-retirement.patch'
    env['CARGO_TARGET_DIR'] = str(output / 'target')
    env['RUSTC'] = str(rustc)
    def command(argv, name, *, bound=4*1024*1024):
        bounded_command(argv, output / name, deadline, env=env, limit=bound)
    command(['patch', '--batch', '--fuzz=0', '-p1', '-d', str(workspace), '-i', str(patch)], 'apply.log')
    command([str(rustc), '-Vv'], 'rustc.txt')
    command(['cc', '--version'], 'cc.txt')
    manifest = workspace / 'Cargo.toml'
    command([str(cargo), 'build', '--locked', '--release', '-p', 'greetd',
             '--manifest-path', str(manifest), '--message-format=json'], 'cargo-build.jsonl')
    artifacts = []
    for line in (output / 'cargo-build.jsonl').read_text().splitlines():
        data = json.loads(line)
        if data.get('reason') == 'compiler-artifact' and data.get('executable'):
            artifacts.append(data)
    candidates = [data for data in artifacts if data.get('target', {}).get('name') == 'greetd'
                  and data.get('target', {}).get('kind') == ['bin']
                  and data.get('profile', {}).get('test') is False]
    if len(candidates) != 1:
        raise ValueError('one actual non-test greetd Cargo executable role required')
    binary = Path(candidates[0]['executable']).resolve(strict=True)
    if (output / 'target').resolve() not in binary.parents:
        raise ValueError('actual executable escaped owned target directory')
    # Fixture workers are clone3 children without CLONE_FILES or exec, so each
    # inherits every descriptor open in the test binary. Parallel tests would
    # let one fixture's child hold another fixture's release pipe and deadlock
    # both waits (run 38468594829 aarch64); run each filter on one thread.
    command([str(cargo), 'test', '--locked', '-p', 'greetd', '--manifest-path', str(manifest),
             'retirement::tests::', '--', '--nocapture', '--test-threads=1'], 'native-worker-tests.log')
    command([str(cargo), 'test', '--locked', '-p', 'greetd', '--manifest-path', str(manifest),
             'context::retirement_tests::', '--', '--nocapture', '--test-threads=1'], 'native-context-tests.log')
    for filename in ['native-worker-tests.log', 'native-context-tests.log']:
        text = (output / filename).read_text()
        if text.count('test result: ok. 3 passed; 0 failed; 0 ignored;') != 1:
            raise ValueError('actual required native filter did not execute three controls')
    command([str(cargo), 'test', '--locked', '-p', 'greetd', '--manifest-path', str(manifest),
             'server::retirement_tests::', '--', '--nocapture', '--test-threads=1'], 'native-handler-tests.log')
    if (output / 'native-handler-tests.log').read_text().count('test result: ok. 2 passed; 0 failed; 0 ignored;') != 1:
        raise ValueError('actual required handler abort/unwind controls did not execute')
    module = output / 'pam_retirement_fixture.so'
    command(['cc', '-shared', '-fPIC', '-Wall', '-Wextra', '-Werror',
             '-o', str(module), str(root / 'packages/greetd/tests/pam_retirement_fixture.c'), '-lpam'], 'module-build.log')
    binary_sha, module_sha = digest(binary), digest(module)
    command(['sudo', 'env', 'PYTHONDONTWRITEBYTECODE=1', 'python3',
             str(root / 'packages/greetd/tests/greetd_native_proof.py'),
             '--binary', str(binary), '--module', str(module), '--out', str(output / 'native')],
            'actual-daemon-proof.log')
    if (output / 'actual-daemon-proof.log').read_text().count('GREETD_NATIVE_CONTROLLED_PROOF_PASS') != 1:
        raise ValueError('actual controlled daemon proof marker missing or duplicated')
    native = json.loads((output / 'native/receipt.json').read_bytes())
    if (native.get('completed') is not True or native.get('binary_sha256') != binary_sha
            or native.get('module_sha256') != module_sha
            or native.get('installed_policy_qualified') is not False
            or native.get('desktop_unlock_qualified') is not False
            or {case.get('case') for case in native.get('cases', [])} != {'multi', 'hung', 'death', 'malformed'}
            or len(native.get('cases', [])) != 4
            or any(case.get('completed') is not True or case.get('owned_tree_retired') is not True
                   for case in native['cases'])):
        raise ValueError('actual native receipt/binary roles differ')
    if digest(binary) != binary_sha or digest(module) != module_sha:
        raise ValueError('executed native bytes changed')
    if hashlib.sha256((workspace / 'Cargo.lock').read_bytes()).hexdigest() != lock_sha:
        raise ValueError('locked upstream dependencies changed')
    if time.monotonic() >= deadline:
        raise TimeoutError('absolute native candidate deadline before final admission')
    after = ci_admission.current(root, args.architecture)
    if time.monotonic() >= deadline:
        raise TimeoutError('absolute native candidate deadline after final admission')
    if before != after:
        raise ValueError('executing source/run admission changed')
    receipt = {'schema': 1, 'completed': True, 'source_admission_before': before,
               'source_admission_after': after, 'archive_sha256': ARCHIVE_SHA,
               'cargo_lock_sha256': lock_sha, 'actual_cargo_binary_role': candidates[0],
               'binary_sha256': binary_sha, 'module_sha256': module_sha,
               'host_kernel': platform.release(), 'host_architecture': platform.machine(),
               'native_receipt': native, 'candidate_only': True, 'release_qualified': False,
               'publication': False, 'installed_policy_qualified': False}
    (output / 'candidate-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print('GREETD_CANDIDATE_SOURCE_NATIVE_CONTROLS_PASS')


if __name__ == '__main__':
    main()
