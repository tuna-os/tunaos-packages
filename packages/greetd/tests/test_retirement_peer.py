"""Safe parser/absolute-budget controls; no daemon/kernel/PAM qualification."""
import importlib.util
from pathlib import Path
import struct
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('retirement_peer', Path(__file__).with_name('retirement_peer.py'))
peer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(peer)


class Stream:
    def __init__(self, payload):
        self.payload = payload
        self.sent = []

    def settimeout(self, timeout):
        if timeout <= 0:
            raise AssertionError('nonpositive timeout')

    def recv(self, count):
        result, self.payload = self.payload[:count], self.payload[count:]
        return result

    def sendall(self, data):
        self.sent.append(data)


def frame(value):
    return struct.pack('=I', len(value)) + value


class ObserverControls(unittest.TestCase):
    def test_existing_native_endian_success_bytes(self):
        with patch.object(peer.time, 'monotonic', return_value=1):
            self.assertEqual(peer.receive(Stream(frame(b'{"type":"success"}')), 2), {'type': 'success'})

    def test_truncated_response_never_success(self):
        with patch.object(peer.time, 'monotonic', return_value=1):
            for payload in [b'', b'\x01', struct.pack('=I', 20) + b'{}']:
                with self.subTest(payload=payload), self.assertRaises(EOFError):
                    peer.receive(Stream(payload), 2)

    def test_duplicate_success_is_invalid(self):
        with patch.object(peer.time, 'monotonic', return_value=1):
            with self.assertRaises(ValueError):
                peer.receive(Stream(frame(b'{"type":"error","type":"success"}')), 2)

    def test_array_or_null_is_invalid(self):
        with patch.object(peer.time, 'monotonic', return_value=1):
            for value in [b'[]', b'null', b'true']:
                with self.subTest(value=value), self.assertRaises(ValueError):
                    peer.receive(Stream(frame(value)), 2)

    def test_zero_and_oversized_frame_refused_before_payload(self):
        with patch.object(peer.time, 'monotonic', return_value=1):
            for size in [0, peer.MAX_FRAME + 1]:
                stream = Stream(struct.pack('=I', size) + b'untouched')
                with self.subTest(size=size), self.assertRaises(ValueError):
                    peer.receive(stream, 2)
                self.assertEqual(stream.payload, b'untouched')

    def test_positive_read_finishing_after_deadline_refused(self):
        with patch.object(peer.time, 'monotonic', side_effect=[1, 1, 2]):
            with self.assertRaises(TimeoutError):
                peer.read_exact(Stream(b'positive'), 8, 2)

    def test_positive_send_finishing_after_deadline_refused(self):
        stream = Stream(b'')
        with patch.object(peer.time, 'monotonic', side_effect=[1, 1, 2]):
            with self.assertRaises(TimeoutError):
                peer.send(stream, {'type': 'cancel_session'}, 2)
        self.assertEqual(len(stream.sent), 1)  # no retry/replay

    def test_expired_send_never_writes(self):
        stream = Stream(b'')
        with patch.object(peer.time, 'monotonic', return_value=2):
            with self.assertRaises(TimeoutError):
                peer.send(stream, {'type': 'cancel_session'}, 2)
        self.assertEqual(stream.sent, [])

    def test_cold_barrier_never_substitutes_for_original(self):
        controlled = peer.Peer(123, 0, 'a' * 64)
        for value in [False, None, 1, 'true']:
            with self.subTest(value=value), patch.object(peer.socket, 'socket') as socket_factory:
                with self.assertRaises(ValueError):
                    controlled.retirement_barrier('/unused', 2, original_canceled_eof=value)
                socket_factory.assert_not_called()

    def test_invalid_pid_types_refused(self):
        for value in [True, 0, -1, '123']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                peer.Peer(value, 0, 'a' * 64)


if __name__ == '__main__':
    unittest.main()
