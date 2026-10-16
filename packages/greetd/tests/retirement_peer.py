"""CI controlled-daemon observer, not an installed desktop admission policy.

Existing IPC bytes are used. EOF or an old buffered Success never settles a
canceled attempt; a fresh response from the same admitted daemon is required.
The caller retains the original owned canceled attempt and physical worker
observer separately. This module does not grant authentication or unlock.
"""
import hashlib
import json
import os
import select
import socket
import stat
import struct
import time
from pathlib import Path

MAX_FRAME = 64 * 1024
MAX_BINARY = 128 * 1024 * 1024


def require_budget(deadline):
    if time.monotonic() >= deadline:
        raise TimeoutError('absolute controlled-daemon observation deadline')


def digest(path):
    total = 0
    result = hashlib.sha256()
    with Path(path).open('rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BINARY:
            raise ValueError('bounded regular daemon binary required')
        while data := source.read(1024 * 1024):
            total += len(data)
            if total > MAX_BINARY:
                raise ValueError('daemon binary expansion exceeds bound')
            result.update(data)
    return result.hexdigest()


class Peer:
    def __init__(self, daemon_pid, daemon_pidfd, binary_sha256):
        if type(daemon_pid) is not int or daemon_pid <= 0:
            raise ValueError('positive genuine daemon PID required')
        self.pid = daemon_pid
        self.pidfd = daemon_pidfd  # owned by the caller, retained until teardown
        self.binary_sha256 = binary_sha256

    def validate(self, stream, deadline):
        require_budget(deadline)
        watcher = select.poll()
        watcher.register(self.pidfd, select.POLLIN | select.POLLHUP | select.POLLERR)
        if watcher.poll(0):
            raise ValueError('admitted daemon is terminal or identity unavailable')
        pid, uid, gid = struct.unpack('3i', stream.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if pid != self.pid or uid != 0 or gid != 0:
            raise ValueError('controlled root daemon peer identity changed')
        if digest('/proc/' + str(self.pid) + '/exe') != self.binary_sha256:
            raise ValueError('admitted daemon binary changed')
        require_budget(deadline)
        if watcher.poll(0):
            raise ValueError('admitted daemon terminated during identity observation')

    def retirement_barrier(self, socket_path, deadline, *, original_canceled_eof):
        # A cold empty-server Success cannot substitute for original attempt
        # cancellation. This is a controlled observer seam, not caller-trusted
        # production authentication authority or an installed policy admission.
        if original_canceled_eof is not True:
            raise ValueError('original owned canceled connection EOF required')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
            require_budget(deadline)
            stream.settimeout(deadline - time.monotonic())
            stream.connect(str(socket_path))
            self.validate(stream, deadline)
            send(stream, {'type': 'cancel_session'}, deadline)
            response = receive(stream, deadline)
            self.validate(stream, deadline)
            if response != {'type': 'success'}:
                raise ValueError('fresh global physical-retirement barrier refused')
        return {'fresh_cancel_response': 'success', 'same_daemon_verified': True,
                'authentication_granted': False, 'installed_policy_qualified': False}


def send(stream, value, deadline):
    require_budget(deadline)
    payload = json.dumps(value, separators=(',', ':')).encode()
    if not 0 < len(payload) <= MAX_FRAME:
        raise ValueError('bounded request required')
    stream.settimeout(deadline - time.monotonic())
    stream.sendall(struct.pack('=I', len(payload)) + payload)
    require_budget(deadline)


def read_exact(stream, count, deadline):
    result = bytearray()
    while len(result) < count:
        require_budget(deadline)
        stream.settimeout(deadline - time.monotonic())
        part = stream.recv(count - len(result))
        require_budget(deadline)
        if not part:
            raise EOFError('daemon response truncated or absent')
        result.extend(part)
    return bytes(result)


def receive(stream, deadline):
    size, = struct.unpack('=I', read_exact(stream, 4, deadline))
    if not 0 < size <= MAX_FRAME:
        raise ValueError('daemon response exceeds frame bound')
    raw = read_exact(stream, size, deadline)
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('duplicate daemon response field')
            value[key] = item
        return value
    value = json.loads(raw, object_pairs_hook=unique)
    require_budget(deadline)
    if type(value) is not dict:
        raise ValueError('daemon response object required')
    return value
