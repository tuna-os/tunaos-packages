"""Candidate source/ownership and actual test admission fixtures; no native builds."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import pytest
import yaml

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import gtk4_candidate as provider

def module(name, filename):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/filename)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value)
    return value

chain=module('gtk4_chain','alma-recipe-chain.py')
adapter=module('gtk4_adapter','run-alma-recipe-chain.py')
SHA='a'*40


def recipe():
    return yaml.safe_load((ROOT/'packages/gtk4-layer-shell/package.yaml').read_text())


@pytest.mark.parametrize('target', ['alma10','alma10-kitten'])
@pytest.mark.parametrize('arch', ['x86_64','aarch64'])
def test_actual_fourcell_candidate_plan_is_narrow_bound_and_default_still_refuses(target,arch):
    before=(ROOT/'packages/gtk4-layer-shell/package.yaml').read_bytes()
    roots=chain.select_roots(ROOT,target,provider.SCOPE)
    action=chain.selection_action_key(target,arch,provider.SCOPE,SHA,roots)
    plan=chain.plan(ROOT,target,arch,SHA,action,roots,candidate_scope=provider.SCOPE)
    assert plan['candidateScope']==provider.SCOPE and plan['readiness'] is False
    assert plan['requestedRoots']==['gtk4-layer-shell']
    record=next(record for record in plan['recipes'] if record['name']=='gtk4-layer-shell')
    assert record['recipeDigest']=='sha256:'+hashlib.sha256(before).hexdigest()
    assert record['candidateRecipeDigest']==chain.digest(provider.project(recipe(),target))
    assert plan['platform']==('linux/amd64/v2' if arch=='x86_64' else 'linux/arm64')
    assert plan['image'].endswith(plan['baseDigest'])
    with pytest.raises(ValueError):
        chain.plan(ROOT,target,arch,SHA,action,roots)
    assert (ROOT/'packages/gtk4-layer-shell/package.yaml').read_bytes()==before


@pytest.mark.parametrize('change', ['name','version','source','targets','dependencies'])
def test_projection_refuses_wrong_or_activated_source(change):
    value=recipe()
    if change=='name': value['name']='another-provider'
    elif change=='version':value['version']='1.4.0'
    elif change=='source':value['source']['sha256']='0'*64
    elif change=='targets':value['targets'].append('alma10')
    else:value['dependencies']['build']['targets']['alma10']=['other-devel']
    with pytest.raises(ValueError):provider.project(value,'alma10')


@pytest.mark.parametrize('bad', ['scope','roots','action'])
def test_plan_rejects_projection_selection_identity(bad):
    roots=['gtk4-layer-shell'];scope=provider.SCOPE
    action=chain.selection_action_key('alma10','x86_64',scope,SHA,roots)
    if bad=='scope':scope='full'
    elif bad=='roots':roots=['cosmic-icon-theme']
    else:action='sha256:'+'0'*64
    with pytest.raises(ValueError):chain.plan(ROOT,'alma10','x86_64',SHA,action,roots,candidate_scope=scope)


@pytest.fixture
def actual_tests(tmp_path):
    names=['layer-probe','lock-probe','unit-test-get-version']
    admitted=json.dumps([{'name':name} for name in names]).encode()
    rows=[{'name':name,'result':'OK','returncode':0,'command':['/native/test','--auto'],
           'starttime':1234.0,'duration':0.1} for name in names]
    log=('\n'.join(json.dumps(row) for row in rows)+'\n').encode()
    receipt={'schema':1,'scope':'upstream-mock-wayland-provider-tests','source_sha256':provider.SOURCE_SHA,
             'admitted_sha256':hashlib.sha256(admitted).hexdigest(),'testlog_sha256':hashlib.sha256(log).hexdigest(),
             'passed':sorted(names),'hardware_qualified':False}
    for name,data in [('provider-admitted-tests.json',admitted),('testlog.json',log),('testlog.txt',b'actual native test log'),
                      ('provider-test-receipt.json',json.dumps(receipt).encode())]:
        (tmp_path/name).write_bytes(data)
    return tmp_path


def test_actual_results_admit_four_exact_files_without_readiness(actual_tests):
    result=provider.admit_tests(actual_tests,provider.SOURCE_SHA)
    assert set(result)==provider.FILES and all(value.startswith('sha256:') for value in result.values())


@pytest.mark.parametrize('bad',['missing','extra','symlink','parent-symlink','skip','duplicate','wrong-source',
                               'wrong-hash','wrong-passed','qualified','missing-command','missing-duration'])
def test_provider_admission_refuses_incomplete_or_wrong_owned_evidence(actual_tests,tmp_path,bad):
    directory=actual_tests;source=provider.SOURCE_SHA
    if bad=='missing':(directory/'testlog.txt').unlink()
    elif bad=='extra':(directory/'extra').write_text('foreign')
    elif bad=='symlink':
        (directory/'testlog.txt').unlink();(directory/'testlog.txt').symlink_to('/etc/hosts')
    elif bad=='parent-symlink':
        link=tmp_path.parent/(tmp_path.name+'-link');link.symlink_to(tmp_path,target_is_directory=True);directory=link
    elif bad=='wrong-source':source='0'*64
    else:
        receipt=json.loads((directory/'provider-test-receipt.json').read_text())
        rows=[json.loads(line) for line in (directory/'testlog.json').read_text().splitlines()]
        if bad=='skip':rows[0]['result']='SKIP'
        elif bad=='duplicate':rows[-1]=rows[0]
        elif bad=='missing-command':rows[0]['command']=[]
        elif bad=='missing-duration':rows[0]['duration']=None
        elif bad=='wrong-hash':receipt['testlog_sha256']='0'*64
        elif bad=='wrong-passed':receipt['passed']=[]
        elif bad=='qualified':receipt['hardware_qualified']=True
        if bad in {'skip','duplicate','missing-command','missing-duration'}:
            log=('\n'.join(json.dumps(row) for row in rows)+'\n').encode()
            (directory/'testlog.json').write_bytes(log);receipt['testlog_sha256']=hashlib.sha256(log).hexdigest()
        (directory/'provider-test-receipt.json').write_text(json.dumps(receipt))
    with pytest.raises(ValueError):provider.admit_tests(directory,source)


def record_and_document():
    roots=['gtk4-layer-shell']
    action=chain.selection_action_key('alma10','x86_64',provider.SCOPE,SHA,roots)
    document=chain.plan(ROOT,'alma10','x86_64',SHA,action,roots,candidate_scope=provider.SCOPE)
    record=next(record for record in document['recipes'] if record['name']=='gtk4-layer-shell')
    return record,document


def test_actual_adapter_prepares_projected_recipe_before_renderer_and_fetch(tmp_path,monkeypatch):
    record,document=record_and_document();calls=[]
    monkeypatch.setattr(adapter,'run',lambda command,**kwargs:calls.append(command))
    item=adapter.Adapter(document,ROOT,tmp_path)
    item.prepare(record,tmp_path/'provider',tmp_path/'repo')
    path=tmp_path/'provider/candidate-recipe.yaml'
    assert yaml.safe_load(path.read_text())==provider.project(recipe(),'alma10')
    assert str(path) in calls[0] and str(path) in calls[1]
    assert recipe()['targets']==['el10']


@pytest.mark.parametrize('bad',['source','projection','owner','scope'])
def test_adapter_projection_cannot_trust_record_mutation(tmp_path,bad):
    record,document=record_and_document()
    if bad=='source':record['recipeDigest']='sha256:'+'0'*64
    elif bad=='projection':record['candidateRecipeDigest']='sha256:'+'0'*64
    elif bad=='owner':record['recipe']='packages/gtk-layer-shell/package.yaml'
    else:document['candidateScope']='full'
    with pytest.raises(ValueError):adapter.Adapter(document,ROOT,tmp_path).projected_recipe(record)


def test_admission_binds_native_cell_source_and_unqualified_scope(actual_tests):
    record,document=record_and_document()
    result=adapter.Adapter(document,ROOT,actual_tests).provider_admission(record,actual_tests)
    assert result['recipeDigest']==record['recipeDigest'] and result['candidateRecipeDigest']==record['candidateRecipeDigest']
    assert result['platform']=='linux/amd64/v2' and result['sourceRevision']==SHA
    assert result['published'] is False and result['readiness'] is False and result['hardwareQualified'] is False


@pytest.mark.parametrize('change',['projection','scope','canonical','published'])
def test_execute_rederives_candidate_plan_before_callbacks(tmp_path,change):
    record,document=record_and_document()
    if change=='projection':document['recipes'][0]['candidateRecipeDigest']='sha256:'+'0'*64
    elif change=='scope':document['candidateScope']='full'
    elif change=='canonical':document['recipes'][0]['recipeDigest']='sha256:'+'0'*64
    else:document['published']=True
    for name in ('meta','state'):
        (tmp_path/name).mkdir();(tmp_path/name/'identity.json').write_text('{}')
    calls=[]
    with pytest.raises(ValueError):
        chain.execute(document,root=ROOT,repo=tmp_path/'repo',state=tmp_path/'state',meta=tmp_path/'meta',work=tmp_path/'work',
                      builder=lambda *args:calls.append('build'),verifier=lambda *args:calls.append('verify'))
    assert calls==[]


def test_actual_adapter_retains_bound_tests_before_candidate_signing_and_verifies_bank(actual_tests,tmp_path,monkeypatch):
    import shutil
    record,document=record_and_document()
    work=tmp_path/'work';repo=tmp_path/'repo';repo.mkdir()
    for directory in ('SPECS','SOURCES','RPMS'):(work/'rpmbuild'/directory).mkdir(parents=True)
    (work/'rpmbuild/RPMS/gtk4-layer-shell.rpm').write_bytes(b'unsigned native artifact fixture')
    (work/'evidence').mkdir();(work/'evidence/build.log').write_text('actual builder fixture')
    shutil.copytree(actual_tests,work/'rpmbuild/provider-test-evidence/gtk4-layer-shell',ignore=shutil.ignore_patterns('work','repo'))
    calls=[]
    def run(command,**kwargs):
        calls.append(command)
        if '/proof/verify.sh' in command:
            proof=tmp_path/'verification/gtk4-layer-shell'
            for name in ('installed.tsv','transaction.txt','repositories.txt','smoke.log'):(proof/name).write_text('actual install fixture')
    monkeypatch.setattr(adapter,'run',run)
    item=adapter.Adapter(document,ROOT,tmp_path)
    build,outputs=item.build(record,work,repo)
    bank=repo/'buildroots/gtk4-layer-shell'
    admission=json.loads((bank/'gtk4-provider-admission.json').read_text())
    assert admission==item.provider_admission(record,bank/'gtk4-provider')
    assert set(provider.admit_tests(bank/'gtk4-provider',provider.SOURCE_SHA))==provider.FILES
    # Signed output fixture is supplied explicitly; this test never signs/builds.
    for output in outputs:shutil.copy2(output,repo/output.name)
    assert item.verify(record,repo)
    observation=json.loads((tmp_path/'verification/gtk4-layer-shell/observation.json').read_text())
    assert observation['candidateProviderTests']==admission and observation['readiness'] is False


@pytest.mark.parametrize('bad',['missing','cell','projection','receipt'])
def test_native_verifier_rejects_bad_banked_provider_before_container(actual_tests,tmp_path,monkeypatch,bad):
    import shutil
    record,document=record_and_document()
    repo=tmp_path/'repo';repo.mkdir();(repo/'gtk4.rpm').write_bytes(b'signed fixture')
    bank=repo/'buildroots/gtk4-layer-shell';bank.mkdir(parents=True)
    shutil.copytree(actual_tests,bank/'gtk4-provider',ignore=shutil.ignore_patterns('repo'))
    item=adapter.Adapter(document,ROOT,tmp_path/'verify-work');item.outputs[record['name']]=['gtk4.rpm']
    admitted=item.provider_admission(record,bank/'gtk4-provider')
    if bad=='cell':admitted['platform']='linux/arm64'
    elif bad=='projection':admitted['candidateRecipeDigest']='sha256:'+'0'*64
    elif bad=='receipt':(bank/'gtk4-provider/testlog.json').write_text('{}')
    if bad!='missing':(bank/'gtk4-provider-admission.json').write_text(json.dumps(admitted))
    calls=[];monkeypatch.setattr(adapter,'run',lambda *args,**kwargs:calls.append(args))
    with pytest.raises(ValueError):item.verify(record,repo)
    assert calls==[]
