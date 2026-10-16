"""Safe mocked ownership controls; no child processes/kernel qualification."""
import importlib.util
from pathlib import Path
import types
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('native_daemon_owner', Path(__file__).with_name('native_daemon_owner.py'))
owner_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(owner_module)


class NativeOwnerControls(unittest.TestCase):
    def owner(self):
        owner = owner_module.NativeDaemonOwner.__new__(owner_module.NativeDaemonOwner)
        owner.libc = types.SimpleNamespace(prctl=Mock(return_value=0))
        owner.previous = types.SimpleNamespace(value=0)
        owner.process = types.SimpleNamespace(pid=100, returncode=None)
        owner.pidfd = 101
        owner.log = Mock()
        owner.cleaned = False
        owner.exit_status = None
        return owner

    def test_observation_keeps_leader_unreaped(self):
        owner = self.owner()
        with patch.object(owner_module.os, 'waitid', return_value=None) as waitid:
            self.assertTrue(owner.alive())
        waitid.assert_called_once_with(owner_module.os.P_PID, 100,
                                      owner_module.os.WEXITED | owner_module.os.WNOHANG | owner_module.os.WNOWAIT)
        self.assertIsNone(owner.process.returncode)

    def test_cleanup_kills_reserved_group_before_any_reap(self):
        owner = self.owner()
        events = []
        with patch.object(owner_module, 'direct_children', side_effect=[[100], []]), \
                patch.object(owner_module.time, 'monotonic', return_value=1), \
                patch.object(owner_module.time, 'sleep'), \
                patch.object(owner_module.os, 'killpg', side_effect=lambda *args: events.append(('group', args))), \
                patch.object(owner_module.os, 'pidfd_open', return_value=102), \
                patch.object(owner_module.signal, 'pidfd_send_signal', side_effect=lambda *args: events.append(('pidfd', args))), \
                patch.object(owner_module.os, 'close') as close, \
                patch.object(owner_module.os, 'waitpid', side_effect=[(100, 0), (0, 0)]) as reap:
            owner.cleanup()
        self.assertEqual(events[0][0], 'group')
        self.assertEqual(events[0][1][0], 100)
        self.assertTrue(owner.cleaned)
        self.assertEqual(owner.process.returncode, 0)
        self.assertEqual(reap.call_count, 2)
        self.assertEqual(close.call_count, 2)
        owner.log.close.assert_called_once()

    def test_adopted_descendant_uses_pidfd_not_environment_or_numeric_kill(self):
        owner = self.owner()
        with patch.object(owner_module, 'direct_children', side_effect=[[100, 200], []]), \
                patch.object(owner_module.time, 'monotonic', return_value=1), \
                patch.object(owner_module.time, 'sleep'), \
                patch.object(owner_module.os, 'killpg'), \
                patch.object(owner_module.os, 'pidfd_open', side_effect=[102, 202]) as opened, \
                patch.object(owner_module.signal, 'pidfd_send_signal') as signaled, \
                patch.object(owner_module.os, 'close'), \
                patch.object(owner_module.os, 'waitpid', side_effect=[(100, 0), (200, 0), (0, 0)]):
            owner.cleanup()
        self.assertEqual([call.args[0] for call in opened.call_args_list], [100, 200])
        self.assertEqual([call.args[0] for call in signaled.call_args_list], [102, 202])
        self.assertTrue(owner.cleaned)

    def test_unconfirmed_retirement_keeps_subreaper_and_observers(self):
        owner = self.owner()
        with patch.object(owner_module, 'direct_children', return_value=[100]), \
                patch.object(owner_module.time, 'monotonic', side_effect=[0, 6]), \
                patch.object(owner_module.os, 'killpg'), \
                patch.object(owner_module.os, 'close') as close:
            with self.assertRaisesRegex(RuntimeError, 'unconfirmed'):
                owner.cleanup()
        self.assertFalse(owner.cleaned)
        self.assertEqual(owner.pidfd, 101)
        owner.libc.prctl.assert_not_called()
        close.assert_not_called()
        owner.log.close.assert_not_called()

    def test_group_signal_permission_error_never_reports_cleaned(self):
        owner = self.owner()
        with patch.object(owner_module.os, 'killpg', side_effect=PermissionError), \
                patch.object(owner_module.os, 'close') as close:
            with self.assertRaises(PermissionError): owner.cleanup()
        self.assertFalse(owner.cleaned)
        close.assert_not_called()
        owner.libc.prctl.assert_not_called()

    def test_cleanup_is_idempotent_after_actual_observation_scope(self):
        owner = self.owner(); owner.cleaned = True
        with patch.object(owner_module.os, 'killpg') as group, patch.object(owner_module, 'direct_children') as children:
            owner.cleanup()
        group.assert_not_called(); children.assert_not_called()


if __name__ == '__main__': unittest.main()
