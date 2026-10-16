"""CI-only exclusive Linux daemon ownership, derived from reviewed publisher custody.

No token scan, numeric rediscovery kill, pre-reaped leader or cleanup-on-exit
claim. Kernel/spawn stalls are outside the bounded observation guarantee.
"""
import ctypes
import os
from pathlib import Path
import signal
import subprocess
import time


def direct_children():
    return [int(value) for value in Path('/proc/self/task/' + str(os.getpid()) + '/children').read_text().split()]


class NativeDaemonOwner:
    def __init__(self, argv, log_path):
        if len(list(Path('/proc/self/task').iterdir())) != 1 or direct_children():
            raise ValueError('exclusive single-thread child ownership required')
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.previous = ctypes.c_int()
        if self.libc.prctl(37, ctypes.byref(self.previous), 0, 0, 0) != 0:
            raise OSError('subreaper observation failed')
        if self.libc.prctl(36, 1, 0, 0, 0) != 0:
            raise OSError('subreaper activation failed')
        self.process = None
        self.pidfd = None
        self.log = None
        self.cleaned = False
        self.exit_status = None
        try:
            self.log = Path(log_path).open('xb')
            self.process = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                            stdout=self.log, stderr=subprocess.STDOUT,
                                            start_new_session=True, close_fds=True)
            # Our unreaped direct child cannot be reused before this pidfd open.
            self.pidfd = os.pidfd_open(self.process.pid)
        except BaseException:
            self.cleanup()
            raise

    def observe(self):
        if self.process is None:
            raise ValueError('daemon not owned')
        # Never call Popen.poll/wait: preserve leader/PGID until cleanup.
        return os.waitid(os.P_PID, self.process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)

    def alive(self):
        return self.observe() is None

    def signal(self, value):
        if self.pidfd is None:
            raise ValueError('daemon kernel identity unavailable')
        signal.pidfd_send_signal(self.pidfd, value)

    def cleanup(self):
        if self.cleaned:
            return
        retired = False
        try:
            if self.process is not None:
                # The still-unreaped leader reserves this exact session PGID.
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                children = direct_children()
                if not children:
                    retired = True
                    break
                for pid in children:
                    try:
                        descriptor = os.pidfd_open(pid)
                    except ProcessLookupError:
                        continue
                    try:
                        signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    finally:
                        os.close(descriptor)
                while True:
                    try:
                        pid, status = os.waitpid(-1, os.WNOHANG)
                    except ChildProcessError:
                        break
                    if pid == 0:
                        break
                    if self.process is not None and pid == self.process.pid:
                        self.exit_status = status
                        self.process.returncode = os.waitstatus_to_exitcode(status)
                time.sleep(.01)
            if not retired and not direct_children():
                retired = True
            if not retired:
                raise RuntimeError('owned native daemon retirement unconfirmed')
            if self.libc.prctl(36, self.previous.value, 0, 0, 0) != 0:
                raise OSError('subreaper restoration failed')
            self.cleaned = True
        finally:
            # Closing observers/files never substitutes for child retirement.
            if retired:
                if self.pidfd is not None:
                    os.close(self.pidfd)
                    self.pidfd = None
                if self.log is not None:
                    self.log.close()
