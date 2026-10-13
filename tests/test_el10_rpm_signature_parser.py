"""Actual classifier controls with simulated rpmkeys output; no native crypto proof."""
import importlib.util
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('el10_signature_measure', ROOT/'scripts/el10-measure-closure.py')
measure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(measure)
FP = '99DB70FAE1D7CE227FB6488205B555B38483C65D'
OTHER = '7D8D15CBFC4E62688591FB2633D98517E37ED158'


def signature(header=True, key=FP[-8:], version=4):
    return f'    {"Header " if header else ""}V{version} RSA/SHA256 Signature, key ID {key}: OK\n'


class SignatureParser(unittest.TestCase):
    def parse(self, lines, rc=0, digests=True, fingerprints=None):
        entry = {'rpmdb': '/private/db', 'primary_fingerprint': FP,
                 'signing_fingerprints': fingerprints or [FP]}
        output = '/private/provider.rpm:\n' + lines
        if digests:
            output += '    Header SHA256 digest: OK\n    Payload SHA256 digest: OK\n'
        with mock.patch.dict(measure.KEY_POLICIES, {'baseos': entry}, clear=True), \
                mock.patch.object(measure, 'run', return_value=(rc, output)) as run:
            result = measure.rpm_signature(Path('/private/provider.rpm'), 'baseos', 'provider')
        run.assert_called_once_with(['rpmkeys', '--dbpath', '/private/db', '--checksig',
                                     '--verbose', '/private/provider.rpm'])
        self.assertEqual(result['raw'], output)
        self.assertEqual(result['fingerprint'], FP)
        self.assertTrue(result['digests_verified'])
        self.assertTrue(result['cryptographic_signature_verified'])
        return result

    def test_single_header_or_payload_supported(self):
        for header in (True, False):
            for version in (3, 4):
                with self.subTest(header=header, version=version):
                    self.parse(signature(header, version=version))

    def test_header_payload_same_full_signer_supported(self):
        for version in (3, 4):
            for ids in ((FP[-8:].lower(), FP[-16:]), (FP, FP[-8:])):
                with self.subTest(version=version, ids=ids):
                    self.parse(signature(True, ids[0], version) + signature(False, ids[1], version))

    def test_mixed_untrusted_or_ambiguous_signers_refused(self):
        cases = [(signature()+signature(False, OTHER[-8:]), [FP, OTHER]),
                 (signature()+signature(False, OTHER[-8:]), [FP]),
                 (signature(key=OTHER[-8:]), [FP]),
                 (signature(), [FP, 'A'*32+FP[-8:]])]
        for lines, fingerprints in cases:
            with self.subTest(lines=lines, fingerprints=fingerprints), self.assertRaises(ValueError):
                self.parse(lines, fingerprints=fingerprints)

    def test_duplicate_excessive_or_malformed_signatures_refused(self):
        cases = [signature()*2, signature(False)*2, signature()*2+signature(False),
                 signature()+signature(False).replace(': OK', ': OK extra'),
                 signature()+signature(False).replace('key ID', 'key'),
                 signature()+signature(False).replace('V4', 'V9'),
                 signature()+signature(False).replace(FP[-8:], FP[-4:]),
                 signature()+'    Header RSA signature: malformed\n']
        for lines in cases:
            with self.subTest(lines=lines), self.assertRaises(ValueError):
                self.parse(lines)

    def test_bad_nokey_not_ok_and_nonzero_exit_refused(self):
        for bad in ('BAD', 'NOKEY', 'NOT OK'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.parse(signature()+signature(False).replace(': OK', ': '+bad))
        with self.assertRaises(ValueError):
            self.parse(signature()+signature(False), rc=1)
        with self.assertRaises(ValueError):
            self.parse(signature()+'    Payload SHA256 digest: BAD\n')

    def test_missing_signature_or_digest_refused(self):
        with self.assertRaises(ValueError):
            self.parse('')
        with self.assertRaises(ValueError):
            self.parse(signature()+signature(False), digests=False)


if __name__ == '__main__':
    unittest.main()
