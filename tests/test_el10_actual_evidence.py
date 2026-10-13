"""Executable actual artifact finalizer/bound checks; no RPM/container claims."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('el10_evidence_test',ROOT/'scripts/el10-evidence.py')
io = importlib.util.module_from_spec(spec);spec.loader.exec_module(io)


class BundleChecks(unittest.TestCase):
    def bundle(self, root):
        (root/'evidence').mkdir()
        (root/'evidence/provider.json').write_text('{"actual":true}\n')
        report = {'producer':{'run_id':123,'action_key':'a'*64},'cell':'cell',
                  'artifacts':[dict(name='evidence/provider.json',kind='provider-headers',
                                    sha256=io.digest_file(root/'evidence/provider.json'),run_id=123)]}
        return io.write_bundle(report,root)

    def test_actual_finalizer_and_verifier_no_self_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);report=self.bundle(root)
            self.assertEqual(io.verify_bundle(root),report)
            self.assertNotIn('report.json',[a['name'] for a in report['artifacts']])
            self.assertEqual(json.loads((root/'bundle.json').read_text())['members']['report.json'],io.digest_file(root/'report.json'))

    def test_changed_bytes_payload_or_key_rejected(self):
        for name in ('report.json','observations.json','action-key.txt','evidence/provider.json'):
            with self.subTest(name=name),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);self.bundle(root)
                with (root/name).open('ab') as stream:stream.write(b'altered')
                with self.assertRaises(ValueError):io.verify_bundle(root)

    def test_matching_hashes_do_not_hide_semantic_payload_or_action_key_drift(self):
        for name in ('observations.json','action-key.txt'):
            with self.subTest(name=name),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);self.bundle(root)
                (root/name).write_text('{}' if name.endswith('.json') else 'different\n')
                report=json.loads((root/'report.json').read_text())
                for entry in report['artifacts']:
                    if entry['name']==name:entry['sha256']=io.digest_file(root/name)
                (root/'report.json').write_text(json.dumps(report))
                manifest=json.loads((root/'bundle.json').read_text())
                for key in manifest['members']:manifest['members'][key]=io.digest_file(root/key)
                (root/'bundle.json').write_text(json.dumps(manifest))
                with self.assertRaises(ValueError):io.verify_bundle(root)

    def test_duplicate_and_traversal_refused(self):
        for name in ('../outside','/absolute','a/../../outside','a\\b'):
            with self.subTest(name=name),self.assertRaises(ValueError):io.safe_member(name)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'x').write_text('x')
            entry=dict(name='x',kind='provider-headers',sha256=io.digest_file(root/'x'),run_id=1)
            report={'producer':{'run_id':1,'action_key':'x'},'artifacts':[entry,entry]}
            with self.assertRaises(ValueError):io.write_bundle(report,root)

    def test_symlink_nonregular_oversized_inputs_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'real').write_text('abcdef');(root/'link').symlink_to(root/'real')
            for path,limit in [(root/'link',100),(root,100),(root/'real',3)]:
                with self.subTest(path=path),self.assertRaises(ValueError):io.digest_file(path,limit)


if __name__=='__main__':unittest.main()
