"""Actual artifact byte joins with synthetic RPM/header/signature controls.

These test consistency, not RPM authenticity. Original API/source custody and
native RPM/GPG execution remain mandatory. Decoder retirement has separate real
subprocess controls; this test isolates byte/identity joins using the real parser.
"""
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('el10_byte_io',ROOT/'scripts/el10-evidence.py')
io=importlib.util.module_from_spec(spec);spec.loader.exec_module(io)


class ProviderByteJoins(unittest.TestCase):
    def fixture(self,root):
        rpm=b'not a native RPM: synthetic byte-join control';rpmsha=hashlib.sha256(rpm).hexdigest()
        fingerprint='A'*40;url='https://controlled.test/repo/';requirement='control-requirement'
        signature={'fingerprint':fingerprint,'raw':'controlled native-verifier output; not real crypto'}
        header={'name':'fixture','epoch':'0','version':'1','release':'1','arch':'noarch',
                'evr':'0:1-1','sha256header':'b'*64}
        selected=dict(nevra='fixture-1-1.noarch',evr='0:1-1',arch='noarch',repoid='baseos',
                      repourl=url,sha256header='b'*64,rpm_sha256=rpmsha,signature=signature)
        primary=gzip.compress(('<metadata xmlns="http://linux.duke.edu/metadata/common"><package>'
          '<name>fixture</name><arch>noarch</arch><version epoch="0" ver="1" rel="1"/>'
          '<checksum type="sha256">'+rpmsha+'</checksum><location href="fixture.rpm"/>'
          '</package></metadata>').encode())
        primarysha=hashlib.sha256(primary).hexdigest()
        repomd=('<repomd xmlns="http://linux.duke.edu/metadata/repo"><data type="primary">'
           '<location href="repodata/primary.gz"/><checksum type="sha256">'+primarysha+
           '</checksum></data></repomd>').encode()
        blobs={'headers.json':json.dumps({'require':requirement,'nevra':selected['nevra'],'header':header}).encode(),
               'origin.json':json.dumps({'require':requirement,'arch':'x86_64','snapshot':{
                    'installed':[dict(header,repoid='baseos',nevra=selected['nevra'])],
                    'available':[dict(header,repoid='baseos',nevra=selected['nevra'],checksum_algorithm='sha256',checksum=rpmsha)]}}).encode(),
               'provider.rpm':rpm,'signature.json':json.dumps({'require':requirement,'nevra':selected['nevra'],
               'rpm_sha256':rpmsha,'signature':signature}).encode(),'primary.gz':primary,'repomd.xml':repomd,
               'status.txt':('[GNUPG:] VALIDSIG '+fingerprint+' 2026-10-13 1791849600 0 4 0 1 8 00\n').encode(),
               'repomd.asc':b'synthetic detached signature, never a native proof'}
        artifacts=[]
        kinds={'origin.json':'provider-origin','headers.json':'provider-headers','provider.rpm':'provider-rpm','signature.json':'provider-signature',
               'primary.gz':'primary-xml','repomd.xml':'repomd-xml','status.txt':'repomd-gpg-status','repomd.asc':'repomd-asc'}
        for name,blob in blobs.items():
            (root/name).write_bytes(blob);artifact=dict(name=name,kind=kinds[name],sha256=hashlib.sha256(blob).hexdigest())
            if name in ('headers.json','provider.rpm','signature.json','origin.json'):artifact['require']=requirement
            else:artifact['index_url']=url
            if name=='provider.rpm':artifact.update(nevra=selected['nevra'],repoid='baseos')
            artifacts.append(artifact)
        report={'arch':'x86_64','dependencies':{requirement:[{'selected':selected,'index_provenance':{'primary_sha256':primarysha,
                   'primary_bytes':len(primary),'primary_href':'repodata/primary.gz',
                   'primary_checksum_algorithm':'sha256','primary_checksum_declared':primarysha}}]},'artifacts':artifacts,
                'signatures':{'repomd_asc':{url:{'signer_fingerprint':fingerprint}}}}
        policy={'trusted_keys':[],'system_keys':{'baseos':[fingerprint]}}
        return report,policy

    def check(self,report,root,policy):
        with mock.patch.object(io,'primary_packages_supervised',side_effect=lambda path,deadline,expected:
                               io.primary_packages(io.read_regular(path,128*1024**2))):
            return io.validate_provider_artifacts(report,root,policy,deadline=time.monotonic()+5)

    def test_actual_provider_header_primary_and_rpm_bytes_join(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);report,policy=self.fixture(root)
            self.assertTrue(self.check(report,root,policy))

    def test_changed_header_signature_primary_rpm_or_index_claim_rejected(self):
        for case in ('header-arch','header-sha','header-nevra','signature','primary-bytes','rpm-bytes',
                     'index-claim','untrusted-key','weak-status','origin-unknown','origin-epoch','origin-ambiguous','origin-checksum','missing-rpm','duplicate-rpm'):
            with self.subTest(case=case),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);report,policy=self.fixture(root)
                if case.startswith('header-'):
                    header=json.loads((root/'headers.json').read_text())
                    if case=='header-arch':header['header']['arch']='foreign'
                    if case=='header-sha':header['header']['sha256header']='0'*64
                    if case=='header-nevra':header['nevra']='other-1-1.noarch'
                    (root/'headers.json').write_text(json.dumps(header))
                elif case.startswith('origin-'):
                    origin=json.loads((root/'origin.json').read_text())
                    if case=='origin-unknown':origin['snapshot']['installed'][0]['repoid']='@System'
                    if case=='origin-epoch':origin['snapshot']['available'][0]['epoch']='1'
                    if case=='origin-ambiguous':origin['snapshot']['available'].append(dict(origin['snapshot']['available'][0]))
                    if case=='origin-checksum':origin['snapshot']['available'][0]['checksum']='0'*64
                    (root/'origin.json').write_text(json.dumps(origin))
                elif case=='signature':(root/'signature.json').write_text('{}')
                elif case=='primary-bytes':(root/'primary.gz').write_bytes(b'changed')
                elif case=='rpm-bytes':(root/'provider.rpm').write_bytes(b'changed')
                elif case=='index-claim':
                    (root/'repomd.xml').write_bytes((root/'repomd.xml').read_bytes().replace(b'repodata/primary.gz',b'repodata/other.gz'))
                elif case=='untrusted-key':policy['system_keys']['baseos']=['B'*40]
                elif case=='weak-status':(root/'status.txt').write_bytes((root/'status.txt').read_bytes().replace(b' 1 8 00',b' 1 2 00'))
                elif case=='missing-rpm':report['artifacts']=[a for a in report['artifacts'] if a['kind']!='provider-rpm']
                elif case=='duplicate-rpm':report['artifacts'].append(next(a for a in report['artifacts'] if a['kind']=='provider-rpm'))
                with self.assertRaises(ValueError):self.check(report,root,policy)


if __name__=='__main__':unittest.main()
