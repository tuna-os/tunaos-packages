"""Execute source-policy and archive controls; no native producer qualification."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    path = ROOT / 'scripts' / (name + '.py')
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


measure = module('el10-measure-closure')
inputs = module('el10-producer-inputs')
evidence = module('el10-evidence')


class InputControls(unittest.TestCase):
    def archive(self, path, members):
        with tarfile.open(path, 'w:gz') as archive:
            for name, content, link in members:
                item = tarfile.TarInfo(name)
                if link is not None:
                    item.type = tarfile.SYMTYPE; item.linkname = link
                    archive.addfile(item)
                else:
                    item.size = len(content); archive.addfile(item, io.BytesIO(content))

    def test_actual_unpack_retains_regular_and_safe_relative_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); archive = root / 'source.tgz'
            self.archive(archive, [('source/Cargo.lock', b'locked', None),
                                   ('source/link', b'', 'Cargo.lock')])
            execution = SimpleNamespace(budget=evidence.Budget(10))
            with mock.patch.object(inputs, 'load', return_value=evidence), mock.patch.object(evidence, 'capacity'):
                inputs.unpack(archive, root / 'output', execution)
            self.assertEqual((root/'output/source/link').read_bytes(), b'locked')

    def test_traversal_duplicate_and_escaping_link_actual_unpack_refused(self):
        cases = [[('../escape', b'x', None)], [('x', b'x', None), ('x', b'y', None)],
                 [('source/x', b'x', None), ('source/link', b'', '../../outside')],
                 [('/absolute', b'x', None)]]
        for members in cases:
            with self.subTest(members=members), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); archive = root/'source.tgz'; self.archive(archive, members)
                execution = SimpleNamespace(budget=evidence.Budget(10))
                with mock.patch.object(inputs, 'load', return_value=evidence), mock.patch.object(evidence, 'capacity'):
                    with self.assertRaises(ValueError): inputs.unpack(archive, root/'output', execution)

    def test_hash_mismatch_cannot_invent_offline_metadata_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'source').write_bytes(b'wrong'); (root/'vendor').write_bytes(b'wrong')
            recipe={'source':{'url':'https://example.test/'+('a'*40)+'.tar.gz','sha256':'0'*64},
                    'sources':[{'sha256':'0'*64}]}
            execution=SimpleNamespace(budget=evidence.Budget(10))
            with self.assertRaises(ValueError):
                inputs.offline_metadata(recipe,root/'source',root/'vendor',root/'output',execution,
                                        {'run_id':1,'run_attempt':1,'head_sha':'b'*40})
            self.assertFalse((root/'output/consumer-receipt.json').exists())

    def test_real_offline_receipt_writer_requires_operation_success_and_unchanged_lock(self):
        # Only the Cargo command is simulated. Archive extraction, config,
        # byte hashes, private home, lock validation and receipt writing execute.
        for result in ('success','command-failure','changed-lock','missing-packages'):
            with self.subTest(result=result),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);sha='a'*40
                source=root/'source.tgz';vendor=root/'vendor.tgz';cargo=root/'cargo-control'
                cargo.write_bytes(b'not a real Cargo executable: control only')
                self.archive(source,[('tuna-desktop-'+sha+'/Cargo.lock',b'locked',None)])
                self.archive(vendor,[('vendor/control',b'control',None)])
                recipe={'source':{'url':'https://source.test/'+sha+'.tar.gz',
                                 'sha256':evidence.digest_file(source)},
                        'sources':[{'sha256':evidence.digest_file(vendor)}],
                        'build':{'cargo_config':'[source.vendored-sources]\ndirectory = "vendor"\n'}}
                execution=SimpleNamespace(budget=evidence.Budget(10),
                                          command=mock.Mock(return_value=b'cargo simulated control'))
                def command(argv,path,deadline,**kwargs):
                    self.assertEqual(argv[-5:],['metadata','--locked','--offline','--format-version','1'])
                    self.assertEqual(kwargs['env']['CARGO_NET_OFFLINE'],'true')
                    home=Path(kwargs['env']['CARGO_HOME'])
                    self.assertEqual(home.stat().st_mode&0o777,0o700)
                    self.assertEqual(home,root/'output/cargo-home')
                    if result=='command-failure':raise evidence.CommandFailed('simulated Cargo failure')
                    path.write_text(json.dumps({'version':1,'workspace_members':['control'],
                        'packages':[] if result=='missing-packages' else [{'name':'control','version':'0.0.0','id':'control',
                        'manifest_path':str(root/'output/source'/('tuna-desktop-'+sha)/'Cargo.toml')}]}))
                    if result=='changed-lock':
                        (root/'output/source'/('tuna-desktop-'+sha)/'Cargo.lock').write_bytes(b'changed')
                with mock.patch.object(inputs,'load',return_value=evidence),mock.patch.object(evidence,'capacity'),\
                     mock.patch.object(inputs.shutil,'which',return_value=str(cargo)),\
                     mock.patch.object(evidence,'bounded_command',side_effect=command):
                    call=lambda:inputs.offline_metadata(recipe,source,vendor,root/'output',execution,
                         {'run_id':123,'run_attempt':2,'head_sha':'b'*40})
                    if result=='success':
                        receipt=call()
                        self.assertTrue(receipt['offline_metadata_success'])
                        self.assertIsNone(receipt['original_vendor_producing_run'])
                        self.assertNotIn('run_id',receipt)
                        self.assertTrue((root/'output/consumer-receipt.json').is_file())
                    else:
                        with self.assertRaises((ValueError,evidence.CommandFailed)):call()
                        self.assertFalse((root/'output/consumer-receipt.json').exists())

    def test_checked_consumer_is_not_original_vendor_and_exact_identity_required(self):
        args=dict(source_sha='a'*40,lock_sha256='b'*64,archive_sha256='c'*64,
                  source_archive_sha256='d'*64,head_sha='e'*40,run_id=123,run_attempt=2)
        receipt=dict(schema=1,kind='new-consumer-offline-metadata',
                     original_vendor_producing_run=None,offline_metadata_success=True,
                     exit_code=0,command=['cargo','metadata','--locked','--offline','--format-version','1'],
                     source_sha=args['source_sha'],lock_sha256=args['lock_sha256'],
                     archive_sha256=args['archive_sha256'],source_archive_sha256=args['source_archive_sha256'],
                     head_sha=args['head_sha'],measurement_run=123,measurement_attempt=2,
                     metadata_json_sha256='f'*64,cargo_executable_sha256='1'*64,private_cargo_home=True,
                     cargo_version='cargo control fixture')
        self.assertEqual(inputs.validate_checked_consumer(receipt,**args),receipt)
        for key,value in [('measurement_run',124),('measurement_attempt',3),
                          ('head_sha','0'*40),('exit_code',1),('exit_code',False),
                          ('original_vendor_producing_run',123),('run_id',123),
                          ('offline_metadata_success',False),('metadata_json_sha256','missing')]:
            changed=dict(receipt);changed[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):
                inputs.validate_checked_consumer(changed,**args)

    def test_json_read_bound_and_absolute_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'json'; path.write_text('{"value":true}')
            with self.assertRaises(ValueError): evidence.load_json(path, 3)
            with self.assertRaises(TimeoutError): evidence.read_regular(path, deadline=0)
            self.assertEqual(evidence.load_json(path), {'value':True})


class SourceKeyPolicyControls(unittest.TestCase):
    def test_actual_policy_full_pins_and_extras_bootstrap_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'policy.json'
            args=SimpleNamespace(factory=ROOT/'manifests/package-factory.yaml',keyring=ROOT/'public.gpg',
                                 head_sha='a'*40,workflow='.github/workflows/el10-closure-producer.yml',out=out)
            measure.cmd_policy(args)
            policy=json.loads(out.read_text())
            self.assertEqual(set(policy['system_keys']), {'baseos','appstream','crb','epel'})
            self.assertNotIn('extras-common',policy['system_keys'])
            self.assertEqual(policy['bootstrap_keys']['extras-common']['packages'], ['epel-release'])
            self.assertTrue(all(k['keyid']==k['fingerprint'] and len(k['keyid'])==40
                                for k in policy['trusted_keys']))
            self.assertEqual(policy['key_policy_sha256'],hashlib.sha256((ROOT/'scripts/el10-keys/policy.json').read_bytes()).hexdigest())
            (Path(tmp)/'foreign.asc').write_text('foreign')
            args.keyring=Path(tmp)/'foreign.asc'
            with self.assertRaises(ValueError): measure.cmd_policy(args)

    def test_actual_extra_repository_configuration_is_package_scoped(self):
        pins=json.loads((ROOT/'scripts/el10-keys/policy.json').read_text())
        key=next(k for k in pins['keys'] if k.get('scope')=='bootstrap-only')
        section={'includepkgs':'*','gpgcheck':'0','repo_gpgcheck':'0'}
        measure.apply_repository_key_scope(section,key)
        self.assertEqual(section['includepkgs'],'epel-release')
        self.assertEqual(section['gpgcheck'],'1')
        self.assertEqual(section['repo_gpgcheck'],'1')
        broadened=dict(key,packages=['epel-release','anything'])
        with self.assertRaises(ValueError):measure.apply_repository_key_scope({},broadened)

    def test_signature_status_rejects_expiry_weak_hash_or_arbitrary_fingerprint(self):
        fingerprint='A'*40
        valid='[GNUPG:] VALIDSIG '+fingerprint+' 2026-10-13 1791849600 0 4 0 1 8 00\n'
        self.assertEqual(evidence.valid_signature_status(valid,[fingerprint]),fingerprint)
        for value in [valid+'[GNUPG:] KEYEXPIRED 1\n',valid.replace(' 1 8 00',' 1 2 00'),
                      valid.replace(fingerprint,'B'*40),'[GNUPG:] \n',valid+valid]:
            with self.subTest(value=value),self.assertRaises(ValueError):
                evidence.valid_signature_status(value,[fingerprint])

    def test_primary_download_mismatch_rejected_before_signature_claim(self):
        ns='http://linux.duke.edu/metadata/repo'
        repomd=(f'<repomd xmlns="{ns}"><revision>1</revision><data type="primary">'
                '<location href="repodata/primary.gz"/><checksum type="sha256">'+('0'*64)+
                '</checksum></data></repomd>').encode()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(measure,'fetch_url',side_effect=[repomd,b'wrong']):
            with self.assertRaisesRegex(ValueError,'differs from signed repomd'):
                measure.index_provenance('https://repo.test/',Path(tmp),0,Path(tmp))

    def test_primary_escape_and_multiple_descriptors_rejected(self):
        ns='http://linux.duke.edu/metadata/repo'
        for href, count in [('../escape',1),('https://foreign.test/file',1),('repodata/p',2)]:
            data=f'<data type="primary"><location href="{href}"/><checksum type="sha256">'+('0'*64)+'</checksum></data>'
            repomd=(f'<repomd xmlns="{ns}"><revision>1</revision>'+data*count+'</repomd>').encode()
            with self.subTest(href=href,count=count),tempfile.TemporaryDirectory() as tmp, mock.patch.object(measure,'fetch_url',return_value=repomd):
                with self.assertRaises(ValueError): measure.index_provenance('https://repo.test/',Path(tmp),0,Path(tmp))


if __name__ == '__main__': unittest.main()
