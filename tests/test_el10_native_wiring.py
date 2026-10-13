"""Execute bounded resolver/native refusal controls; no container or DNF runs."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import yaml

ROOT=Path(__file__).resolve().parents[1]


def load(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/(name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


base=load('el10-native-base');native=load('el10-native-producer')


class FakeExecution:
    def __init__(self, mismatch=None):
        self.mismatch=mismatch;self.calls=[];self.budget=SimpleNamespace(remaining=lambda:30)
    def command(self,argv,**kwargs):
        self.calls.append((argv,kwargs))
        if '--raw' in argv:
            descriptors=[dict(platform=dict(architecture=arch,os='linux'),digest='sha256:'+str(n)*64)
                         for n,arch in enumerate(('amd64','arm64'),1)]
            if self.mismatch=='duplicate':descriptors.append(dict(descriptors[0]))
            if self.mismatch=='missing':descriptors.pop()
            return json.dumps({'manifests':descriptors}).encode()
        arm=argv[-1].endswith('2'*64)
        data={'Digest':'sha256:'+('2' if arm else '1')*64,'Architecture':'arm64' if arm else 'amd64','Os':'linux'}
        if self.mismatch=='architecture':data['Architecture']='foreign'
        if self.mismatch=='digest':data['Digest']='sha256:'+'f'*64
        if self.mismatch=='os':data['Os']='foreign'
        return json.dumps(data).encode()


class NativeWiring(unittest.TestCase):
    def test_actual_shared_resolver_uses_two_exact_platform_manifests(self):
        execution=FakeExecution();report=base.resolve(execution,'quay.io/centos/centos:stream10')
        self.assertEqual(set(report['images']),{'x86_64','aarch64'})
        self.assertEqual(len(execution.calls),3)
        self.assertTrue(all(call[1]['timeout']==60 for call in execution.calls))
        self.assertTrue(all('@sha256:' in call[0][-1] for call in execution.calls[1:]))

    def test_actual_resolver_rejects_missing_duplicate_mismatched_or_foreign_platform(self):
        for mismatch in ('missing','duplicate','architecture','digest','os'):
            with self.subTest(mismatch=mismatch),self.assertRaises(ValueError):
                base.resolve(FakeExecution(mismatch),'quay.io/centos/centos:stream10')
        with self.assertRaises(ValueError):base.resolve(FakeExecution(),'foreign/image:latest')

    def test_native_identity_refusal_precedes_any_bootstrap_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            args=SimpleNamespace(output=str(Path(tmp)/'output'),arch='aarch64',runner_machine='x86_64',
                                 image_arch='arm64',head_sha='a'*40,run_id=1,run_attempt=1)
            io=native.load('el10-evidence')
            with mock.patch.object(io.Execution,'command',side_effect=AssertionError('native command executed')):
                with self.assertRaisesRegex(ValueError,'native runner'):native.native(args)

    def test_registered_manual_input_default_disabled_and_existing_jobs_retained(self):
        lint=yaml.safe_load((ROOT/'.github/workflows/lint.yml').read_text())
        on=lint.get('on',lint.get(True))
        field=on['workflow_dispatch']['inputs']['el10_native_closure']
        self.assertIs(field['default'],False);self.assertEqual(field['type'],'boolean')
        job=lint['jobs']['el10-native-closure']
        self.assertIn("github.event_name == 'workflow_dispatch'",job['if'])
        self.assertIn('inputs.el10_native_closure',job['if'])
        self.assertEqual(job['uses'],'./.github/workflows/el10-closure-producer.yml')
        self.assertEqual(job['permissions'],{'contents':'read'})
        self.assertIn('required-checks',lint['jobs'])
        self.assertNotIn('el10-native-closure',lint['jobs']['required-checks']['needs'])
        workflow=yaml.safe_load((ROOT/'.github/workflows/el10-closure-producer.yml').read_text())
        self.assertIn('workflow_call',workflow.get('on',workflow.get(True)))
        self.assertNotIn('workflow_dispatch',workflow.get('on',workflow.get(True)))
        self.assertEqual(workflow['permissions'],{'contents':'read'})
        steps=workflow['jobs']['produce']['steps']
        self.assertFalse(any('offline_metadata_success' in str(s) for s in steps))
        self.assertTrue(any('Validate local native semantics without original-run admission'==s.get('name') for s in steps))
        self.assertTrue(any('attempt-${{ github.run_attempt }}' in str(s.get('with',{}).get('name','')) for s in steps))


if __name__=='__main__':unittest.main()
