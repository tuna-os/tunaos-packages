"""Actual injectable API/ZIP custody control execution, never a real native run."""
import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def load(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/(name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


custody=load('el10-producer-custody'); evidence=load('el10-evidence')
HEAD='a'*40; RUN=123; ATTEMPT=2


class OriginalCustody(unittest.TestCase):
    def fixture(self, root):
        source=b'controlled source\n';(root/'fixture.py').write_bytes(source)
        api={'actions/runs/123':{'id':RUN,'head_sha':HEAD,'event':'workflow_dispatch',
            'path':custody.WORKFLOW,'status':'completed','conclusion':'success','run_attempt':ATTEMPT,
            'repository':{'full_name':custody.REPOSITORY},'head_repository':{'full_name':custody.REPOSITORY}},
            'contents/fixture.py?ref='+HEAD:{'encoding':'base64','type':'file','content':base64.b64encode(source).decode(),
             'sha':hashlib.sha1(b'blob '+str(len(source)).encode()+b'\0'+source).hexdigest()}}
        names=('Measure natively inside the pinned container','Verify original local artifact byte bindings',
               'Validate local native semantics without original-run admission')
        jobs=[{'name':custody.JOB_PREFIX+'el10-closure-'+cell,'run_attempt':ATTEMPT,'status':'completed','conclusion':'success',
               'steps':[{'name':name,'conclusion':'success'} for name in names]} for cell in sorted(custody.CELLS)]
        api['actions/runs/123/attempts/2/jobs?per_page=100']={'total_count':2,'jobs':jobs}
        artifacts=[];zips={}
        for number,cell in enumerate(sorted(custody.CELLS),1):
            bundle=root/('bundle-'+str(number));bundle.mkdir()
            report={'cell':cell,'arch':cell.rsplit('-',1)[1],'source_sha':'b'*40,
                    'covers_requires':{'build':['controlled-build-'+str(n) for n in range(24)],
                                       'runtime':['controlled-runtime-'+str(n) for n in range(9)]},
                    'base_image':'quay.io/centos/centos@sha256:'+str(number)*64,'producer':{'head_sha':HEAD,'run_id':RUN,'run_attempt':ATTEMPT,'action_key':'b'*64},
                    'artifacts':[]}
            evidence.write_bundle(report,bundle)
            blob=io.BytesIO()
            with zipfile.ZipFile(blob,'w',compression=zipfile.ZIP_DEFLATED) as z:
                for file in bundle.iterdir():z.writestr(file.name,file.read_bytes())
            raw=blob.getvalue();zips['actions/artifacts/'+str(number)+'/zip']=raw
            artifacts.append({'id':number,'name':'el10-closure-'+cell+'-attempt-'+str(ATTEMPT),'expired':False,
                'workflow_run':{'id':RUN,'head_sha':HEAD},'size_in_bytes':len(raw),
                'digest':'sha256:'+hashlib.sha256(raw).hexdigest()})
        api['actions/runs/123/artifacts?per_page=100']={'total_count':2,'artifacts':artifacts}
        def fetch(path,cap):
            data=zips[path] if path in zips else json.dumps(api[path]).encode()
            if len(data)>cap:raise ValueError('test transport cap')
            return data
        return api,zips,fetch

    def invoke(self,root,fetch):
        out=root/'admitted';out.mkdir()
        # The production set remains exhaustive. This fixture tests the actual
        # loop on a controlled one-file source closure, not remote authorization.
        with mock.patch.object(custody,'SOURCE_FILES',{'fixture.py'}),mock.patch.object(evidence,'capacity'):
            # admit_original loads its own module; no large capacity reservation
            # is needed for these 2 KiB synthetic archives on tmpfs.
            with mock.patch('os.statvfs',return_value=type('Capacity',(),{'f_bavail':100*1024**3,'f_frsize':1})()):
                return custody.admit_original(fetch,root,HEAD,RUN,out)

    def test_both_original_zip_hashes_and_source_bytes_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);api,zips,fetch=self.fixture(root)
            admitted,reports=self.invoke(root,fetch)
            self.assertEqual(set(reports),custody.CELLS)
            self.assertEqual(set(admitted.archive_hashes),custody.CELLS)
            for cell,report in reports.items():
                self.assertEqual(admitted.report_hashes[cell],hashlib.sha256(json.dumps(report,sort_keys=True,separators=(',',':')).encode()).hexdigest())
            with self.assertRaises(AttributeError):admitted.head='f'*40
            with self.assertRaises(TypeError):admitted.archive_hashes['foreign']='0'*64

    def test_native_pair_rejects_mixed_source_requirements_and_same_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);api,zips,fetch=self.fixture(root)
            admitted,reports=self.invoke(root,fetch)
            for field,value in [('source_sha','c'*40),('arch','foreign'),
                                ('base_image',next(iter(reports.values()))['base_image'])]:
                changed=json.loads(json.dumps(reports));last=sorted(changed)[-1]
                if field=='base_image':
                    value=changed[sorted(changed)[0]]['base_image']
                changed[last][field]=value
                with self.subTest(field=field),self.assertRaises(ValueError):custody.validate_native_pair(changed)
            changed=json.loads(json.dumps(reports));last=sorted(changed)[-1]
            changed[last]['covers_requires']['build'][0]='different-requirement'
            with self.assertRaises(ValueError):custody.validate_native_pair(changed)

    def test_actual_run_source_and_archive_rejections(self):
        for change in ('fork','wrong-event','attempt','source','archive','missing-leg','missing-step'):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);api,zips,fetch=self.fixture(root)
                run=api['actions/runs/123']
                if change=='fork':run['head_repository']['full_name']='foreign/fork'
                elif change=='wrong-event':run['event']='pull_request'
                elif change=='attempt':api['actions/runs/123/attempts/2/jobs?per_page=100']['jobs'][0]['run_attempt']=1
                elif change=='source':(root/'fixture.py').write_bytes(b'other executing source')
                elif change=='archive':zips['actions/artifacts/1/zip']=b'x'*len(zips['actions/artifacts/1/zip'])
                elif change=='missing-leg':api['actions/runs/123/attempts/2/jobs?per_page=100']['jobs'].pop()
                elif change=='missing-step':api['actions/runs/123/attempts/2/jobs?per_page=100']['jobs'][0]['steps'].pop()
                with self.assertRaises(ValueError):self.invoke(root,fetch)


if __name__=='__main__':unittest.main()
