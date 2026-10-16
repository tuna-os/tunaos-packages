#!/usr/bin/env python3
"""CI-only actual patched daemon/PAM retirement controls. Never run at install.

Requires root on a native Linux host. Owns unique PAM files and a single
subreaper process tree; no production services, account passwords, user PAM
files, environment or kernel policy are changed. Installed admission and the
Desktop common-Slot consumer are NOT qualified by this controlled observer.
"""
import argparse
import json
import os
from pathlib import Path
import resource
import select
import signal
import socket
import stat
import time
import uuid

from native_daemon_owner import NativeDaemonOwner
from retirement_peer import Peer, digest, receive, require_budget, send


def terminal(descriptor):
    watcher = select.poll()
    watcher.register(descriptor, select.POLLIN | select.POLLHUP | select.POLLERR)
    return bool(watcher.poll(0))


def owned_phase(path, daemon, peer, deadline, expected):
    while True:
        require_budget(deadline)
        if not daemon.alive():
            raise ValueError('owned daemon exited before required PAM phase')
        if path.exists():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or not 0 < info.st_size <= 256:
                raise ValueError('bounded root-owned PAM phase file required')
            data = json.loads(path.read_bytes())
            require_budget(deadline)
            if (data.get('schema') != 1 or type(data.get('schema')) is not int
                    or data.get('phase') != expected or type(data.get('pid')) is not int
                    or data['pid'] <= 0 or type(data.get('parent')) is not int
                    or data['parent'] != peer.pid):
                raise ValueError('actual PAM phase daemon ownership differs')
            descriptor = os.pidfd_open(data['pid'])
            try:
                fields = Path('/proc/' + str(data['pid']) + '/stat').read_text().rsplit(')', 1)[1].split()
                if int(fields[1]) != peer.pid or digest('/proc/' + str(data['pid']) + '/exe') != peer.binary_sha256:
                    raise ValueError('actual worker parent/binary differs')
                require_budget(deadline)
                if terminal(descriptor):
                    raise ValueError('worker is already terminal before phase admission')
                return descriptor, {'pid': data['pid'], 'parent': peer.pid,
                                    'start_time': int(fields[19]), 'phase': expected}
            except BaseException:
                os.close(descriptor)
                raise
        time.sleep(.01)


def original_eof(stream, deadline):
    require_budget(deadline)
    stream.settimeout(deadline - time.monotonic())
    data = stream.recv(1)
    require_budget(deadline)
    if data:
        raise ValueError('buffered original response cannot be a retirement receipt')


def expected_question(stream, deadline, style, text):
    response = receive(stream, deadline)
    if response != {'type': 'auth_message', 'auth_message_type': style, 'auth_message': text}:
        raise ValueError('actual PAM style/literal message differs')


def connect(path, daemon, peer, deadline):
    while not path.exists():
        require_budget(deadline)
        if not daemon.alive():
            raise ValueError('owned daemon failed before listener readiness')
        time.sleep(.01)
    stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        require_budget(deadline)
        stream.settimeout(deadline - time.monotonic())
        stream.connect(str(path))
        peer.validate(stream, deadline)
        return stream
    except BaseException:
        stream.close()
        raise


def run_case(binary, module, output, mode, death=False, malformed=False):
    token = uuid.uuid4().hex
    service = 'tuna-retirement-' + token
    greeter_service = service + '-greeter'
    created = []
    daemon = None
    spawn_attempted = False
    worker_fd = None
    original = None
    path = None
    result = {'case': 'malformed' if malformed else 'death' if death else mode, 'completed': False,
              'installed_policy_qualified': False, 'desktop_unlock_qualified': False}
    try:
        for name, content in [
            (service, f'auth required {module} owned-native-proof {mode} {output / "hung.json"}\n'
                      'account required pam_permit.so\nsession required pam_permit.so\n'),
            (greeter_service, 'auth required pam_permit.so\naccount required pam_permit.so\nsession required pam_permit.so\n')]:
            pam = Path('/etc/pam.d') / name
            with pam.open('x') as target:
                created.append(pam)
                target.write(content)
            pam.chmod(0o600)
        config = output / 'config.toml'
        config.write_text(f'[terminal]\nvt="none"\nswitch=false\n[general]\nsource_profile=false\nservice="{service}"\nrunfile="{output / "runfile"}"\n'
                          f'[default_session]\ncommand="/bin/sleep 300"\nuser="root"\nservice="{greeter_service}"\n')
        config.chmod(0o600)
        spawn_attempted = True
        daemon = NativeDaemonOwner([str(binary), '-c', str(config)], output / 'daemon.log')
        peer = Peer(daemon.process.pid, daemon.pidfd, digest(binary))
        path = Path('/run/greetd-' + str(peer.pid) + '.sock')
        deadline = time.monotonic() + 20
        original = connect(path, daemon, peer, deadline)
        send(original, {'type': 'create_session', 'username': 'root'}, deadline)
        expected_question(original, deadline, 'secret', 'Password')
        worker_fd, worker = owned_phase(output / 'hung.json.admitted', daemon, peer, deadline, 'admitted')
        result['worker'] = worker
        # Actual foreign same-daemon connection must refuse without canceling
        # this active owner. The original password conversation then continues.
        with connect(path, daemon, peer, deadline) as foreign:
            send(foreign, {'type': 'cancel_session'}, deadline)
            refusal = receive(foreign, deadline)
            if refusal.get('type') != 'error' or terminal(worker_fd):
                raise ValueError('foreign barrier adopted/canceled active authentication')
        result['foreign_refused_without_cancel'] = True
        if malformed:
            # Actual codec failure on the admitted original stream. No product
            # fault flag, fake daemon or deliberately weakened normal predicate.
            import struct
            original.sendall(struct.pack('=I', 1) + b'{')
            original_eof(original, deadline)
            if not terminal(worker_fd):
                raise ValueError('codec-error path closed before physical worker retirement')
            result['actual_codec_error_retired_worker'] = True
            result['fresh_barrier'] = peer.retirement_barrier(path, deadline, original_canceled_eof=True)
            require_budget(deadline)
            result['completed'] = True
            return result
        send(original, {'type': 'post_auth_message_response', 'response': 'fixture-password'}, deadline)
        expected_question(original, deadline, 'info', 'Literal <fixture> information')
        send(original, {'type': 'post_auth_message_response', 'response': None}, deadline)
        expected_question(original, deadline, 'error', 'Literal & fixture warning')
        send(original, {'type': 'post_auth_message_response', 'response': None}, deadline)
        if mode == 'multi':
            expected_question(original, deadline, 'secret', 'One-time code')
            send(original, {'type': 'post_auth_message_response', 'response': 'fixture-otp'}, deadline)
            expected_question(original, deadline, 'visible', 'Visible confirmation')
            send(original, {'type': 'post_auth_message_response', 'response': 'fixture-visible'}, deadline)
            if receive(original, deadline) != {'type': 'success'}:
                raise ValueError('real multi-round PAM did not authenticate')
            result['actual_second_secret_visible_info_error'] = True
            send(original, {'type': 'cancel_session'}, deadline)
            if receive(original, deadline) != {'type': 'success'} or not terminal(worker_fd):
                raise ValueError('same-owner cancellation ACK preceded physical worker retirement')
            # Idempotent second cancel cannot manufacture another worker.
            send(original, {'type': 'cancel_session'}, deadline)
            if receive(original, deadline) != {'type': 'success'}:
                raise ValueError('repeated cancellation refused after retirement')
            original.shutdown(socket.SHUT_WR)
            original_eof(original, deadline)
            result['fresh_barrier'] = peer.retirement_barrier(path, deadline, original_canceled_eof=True)
        else:
            hung_fd, hung = owned_phase(output / 'hung.json', daemon, peer, deadline, 'hung')
            try:
                if hung['pid'] != worker['pid'] or hung['start_time'] != worker['start_time']:
                    raise ValueError('hung phase replaced original worker')
            finally:
                os.close(hung_fd)
            if death:
                daemon.signal(signal.SIGKILL)
                original_eof(original, deadline)
                if terminal(worker_fd):
                    raise ValueError('required still-hung worker death negative not observed')
                try:
                    peer.retirement_barrier(path, deadline, original_canceled_eof=True)
                except (OSError, ValueError, TimeoutError, EOFError):
                    result['death_eof_denied'] = True
                else:
                    raise ValueError('daemon death EOF qualified physical retirement')
            else:
                original.shutdown(socket.SHUT_WR)
                original_eof(original, deadline)
                if not terminal(worker_fd):
                    raise ValueError('original connection closed before actual hung worker exit')
                result['actual_hung_worker_terminal_before_eof_acceptance'] = True
                result['fresh_barrier'] = peer.retirement_barrier(path, deadline, original_canceled_eof=True)
        require_budget(deadline)
    finally:
        if original is not None:
            original.close()
        if worker_fd is not None:
            os.close(worker_fd)
        if daemon is not None:
            daemon.cleanup()  # positive result requires actual owned-tree retirement
            result['owned_tree_retired'] = daemon.cleaned
        if (daemon is None and not spawn_attempted) or (daemon is not None and daemon.cleaned):
            for pam in created:
                pam.unlink()
            if path is not None:
                path.unlink(missing_ok=True)
    result['completed'] = True
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True, type=Path)
    parser.add_argument('--module', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('actual native root PAM fixture required')
    if any(char.isspace() for path in (args.binary, args.module, args.out) for char in str(path)):
        parser.error('fixture paths must be unambiguous PAM/config tokens')
    binary, module = args.binary.resolve(strict=True), args.module.resolve(strict=True)
    binary_sha, module_sha = digest(binary), digest(module)
    args.out.mkdir(mode=0o700, parents=False, exist_ok=False)
    resource.setrlimit(resource.RLIMIT_MEMLOCK, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
    results = []
    for name, mode, death, malformed in [('multi', 'multi', False, False), ('hung', 'hung', False, False), ('death', 'hung', True, False), ('malformed', 'multi', False, True)]:
        directory = args.out / name
        directory.mkdir(mode=0o700)
        results.append(run_case(binary, module, directory, mode, death, malformed))
    if digest(binary) != binary_sha or digest(module) != module_sha:
        raise ValueError('actually executed native binary/module bytes changed')
    receipt = {'schema': 1, 'completed': True, 'scope': 'controlled native Linux daemon/PAM',
               'binary_sha256': binary_sha, 'module_sha256': module_sha, 'cases': results,
               'installed_policy_qualified': False, 'desktop_unlock_qualified': False,
               'arbitrary_pam_descendants_qualified': False}
    (args.out / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    # Only after all owned trees are physically retired: make these controlled,
    # credential-free fixture receipts/logs readable by the CI artifact actor.
    # No installed files or live user credentials are part of this output.
    for path in args.out.rglob('*'):
        if path.is_symlink():
            raise ValueError('unexpected fixture output symlink')
        path.chmod(0o755 if path.is_dir() else 0o644)
    args.out.chmod(0o755)
    print('GREETD_NATIVE_CONTROLLED_PROOF_PASS')


if __name__ == '__main__':
    main()
