"""Native candidate source/API/crypto mutation fixtures; no native execution."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import zipfile

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import tuna_native_inputs as native
import tuna_candidate_inputs as members
from test_tuna_candidate_inputs import admitted_fixture


def metadata_zip(fixture, metadata):
    rp, fp, cells, archive, output, receipt = fixture
    with zipfile.ZipFile(archive) as source:
        entries = {name: source.read(name) for name in source.namelist()}
    entries['cargo-metadata.json'] = json.dumps(metadata).encode()
    with zipfile.ZipFile(archive, 'w') as target:
        for name, blob in entries.items(): target.writestr(name, blob)
    receipt['artifacts']['cargo-metadata.json'] = {'sha256': members.digest(entries['cargo-metadata.json']),
                                                 'size': len(entries['cargo-metadata.json'])}
    receipt['admission']['artifact_sha256'] = members.digest(archive.read_bytes())
    receipt['admission']['artifact_size'] = archive.stat().st_size
    return fixture


def test_four_cell_native_projection_actual_msrv_and_selected_sources(admitted_fixture):
    rp, fp, cells, archive, output, receipt = metadata_zip(admitted_fixture, {'packages': [
        {'rust_version': '1.88'}, {'rust_version': None}, {'rust_version': '1.92'}]})
    four = [cell for cell in cells if cell['target'] != 'el10']
    result = members.assemble(rp, fp, four, archive, output, receipt, native=True,
                              selected_cell=('alma10','aarch64'))
    recipe = yaml.safe_load((output/'recipe/package.yaml').read_text())
    assert recipe['targets'] == ['alma10','alma10-kitten']
    assert result['minimum_rust_version'] == '1.92.0'
    assert recipe['dependencies']['build']['common'][-2:] == ['rust >= 1.92.0','cargo >= 1.92.0']
    assert 'rust' not in recipe['dependencies']['build']['capabilities']
    assert len(result['cells']) == 4 and result['candidate_only'] and not result['release_qualified']
    assert all('rust >= 1.92.0' in cell['build_requires'] for cell in result['cells'])
    assert len(list(output.glob('*/rpmbuild'))) == 1
    spec = next(output.glob('*/rpmbuild/SPECS/*.spec')).read_text()
    assert 'rust >= 1.92.0' in spec and 'cargo >= 1.92.0' in spec
    assert '--locked --offline' in spec and '/releases/download/' not in spec


@pytest.mark.parametrize('metadata', [ {}, {'packages': []}, {'packages':[{'rust_version':None}]},
    {'packages':[{'rust_version':'1.92-beta'}]}, {'packages':[{'rust_version':1.92}]} ])
def test_native_msrv_missing_or_nonexact_refused(metadata):
    with pytest.raises((ValueError, KeyError)):
        members.rust_requirement({'cargo-metadata.json':json.dumps(metadata).encode()})


def test_default_staging_still_requires_six_cells(admitted_fixture):
    rp, fp, cells, archive, output, receipt = admitted_fixture
    four = [cell for cell in cells if cell['target'] != 'el10']
    with pytest.raises(ValueError): members.assemble(rp,fp,four,archive,output,receipt)
    assert not output.exists()


@pytest.mark.parametrize('mode', ['native','alma',1,None])
def test_arbitrary_native_mode_refused(admitted_fixture, mode):
    rp,fp,cells,archive,output,receipt = admitted_fixture
    with pytest.raises(ValueError): members.assemble(rp,fp,cells,archive,output,receipt,native=mode)
    assert not output.exists()


@pytest.mark.parametrize('cell', [('el10','aarch64'),('alma10','s390x'),('foreign','x86_64')])
def test_selected_foreign_cell_refused(admitted_fixture, cell):
    rp,fp,cells,archive,output,receipt = admitted_fixture
    four = [value for value in cells if value['target'] != 'el10']
    with pytest.raises(ValueError): members.assemble(rp,fp,four,archive,output,receipt,native=True,selected_cell=cell)
    assert not output.exists()


@pytest.mark.parametrize('text', ['{"source_run":1,"source_run":2}', '{"x":NaN}', '[]'])
def test_input_json_refuses_duplicates_nonfinite_nonobject(tmp_path,monkeypatch,text):
    path=tmp_path/'inputs.json';path.write_text(text)
    monkeypatch.setattr(native,'prepare',lambda *args: pytest.fail('untrusted JSON reached admission'))
    with pytest.raises(ValueError):native.from_file(path,ROOT,'alma10','x86_64')


@pytest.mark.parametrize('name,value', [('producer_sha','a'*40),('source_sha','latest'),
    ('source_run',True),('producer_run',0),('artifact',-1)])
def test_desktop_external_trust_pin_required_before_remote_admission(tmp_path,monkeypatch,name,value):
    options={'source_sha':'a'*40,'source_run':1,'producer_sha':native.PRODUCER_SHA,
             'producer_run':2,'artifact':3,'helper_checkout':'/fixture'}
    options[name]=value
    monkeypatch.setattr(members,'independently_admit',lambda *args:pytest.fail('untrusted identity used'))
    with pytest.raises(ValueError): native.admit_desktop(options,ROOT,'alma10','x86_64',tmp_path/'work')


def provider_api():
    run={'id':native.PROVIDER_RUN,'head_sha':native.PROVIDER_SHA,'run_attempt':1,
         'event':'workflow_dispatch','path':native.WORKFLOW,'head_branch':native.PROVIDER_REF[11:],
         'repository':{'full_name':native.PACKAGE_REPO},'head_repository':{'full_name':native.PACKAGE_REPO},
         'status':'completed','conclusion':'success'}
    job={'name':'native (alma10, x86_64)','status':'completed','conclusion':'success',
         'run_id':native.PROVIDER_RUN,'head_sha':native.PROVIDER_SHA}
    artifact={'id':7,'name':f'tideforge-chain-alma10-x86_64-alma-candidate-{native.PROVIDER_RUN}-1-chain',
              'workflow_run':{'id':native.PROVIDER_RUN,'head_sha':native.PROVIDER_SHA}}
    return run,job,artifact


@pytest.mark.parametrize('location,name,value', [('run','head_sha','0'*40),('run','path','.github/workflows/other.yml'),
    ('run','run_attempt',2),('run','conclusion','failure'),('job','conclusion','failure'),
    ('job','head_sha','0'*40),('artifact','name','foreign-cell'),('artifact','workflow_run',{'id':1,'head_sha':native.PROVIDER_SHA})])
def test_provider_run_job_artifact_replay_refused_before_download(tmp_path,monkeypatch,location,name,value):
    run,job,artifact=provider_api()
    {'run':run,'job':job,'artifact':artifact}[location][name]=value
    def request(path,*args):
        if '/jobs?' in path:return {'jobs':[job]}
        if path.endswith('/artifacts'):return {'artifacts':[artifact]}
        return run
    monkeypatch.setattr(native,'request',request)
    monkeypatch.setattr(native,'download',lambda *args:pytest.fail('invalid provider reached download'))
    with pytest.raises(ValueError):native.admit_provider(ROOT,'alma10','x86_64',tmp_path/'work')


@pytest.mark.parametrize('shape', ['path','symlink','foreign','duplicate'])
def test_provider_zip_unsafe_or_foreign_refused(tmp_path,shape):
    archive=tmp_path/'provider.zip'
    with zipfile.ZipFile(archive,'w') as target:
        if shape=='symlink':
            entry=zipfile.ZipInfo('payload/link');entry.external_attr=0o120777<<16;target.writestr(entry,b'/foreign')
        else:
            name={'path':'../snapshot.json','foreign':'foreign.json','duplicate':'snapshot.json'}[shape]
            target.writestr(name,b'{}')
            if shape=='duplicate':target.writestr(name,b'{}')
    with pytest.raises(ValueError):native.extract_provider(archive,tmp_path/'out')
    assert not (tmp_path/'out').exists()


def test_provider_download_requires_current_api_digest_even_cached(tmp_path):
    archive=tmp_path/'cached.zip';archive.write_bytes(b'wrong')
    artifact={'id':1,'expired':False,'size_in_bytes':5,'digest':'sha256:'+'0'*64}
    with pytest.raises(ValueError):native.download(native.PACKAGE_REPO,artifact,archive,64)


def test_workflow_candidate_inputs_are_explicit_and_no_publication():
    text=(ROOT/'.github/workflows/alma-recipe-chain-ci.yml').read_text()
    assert native.PRODUCER_SHA in text
    for name in ('source_run','source_sha','producer_run','source_artifact'):assert name in text
    assert "inputs.scope == 'tuna'" in text
    assert 'secrets.' not in text
    wrapper=(ROOT/'scripts/verify-alma-recipe-chain-ci.sh').read_text()
    assert wrapper.index('PY_SELECTION')<wrapper.index('candidate-rpm-repository.py init')
    assert 'native.admit_into' in wrapper or '.admit_into' in wrapper


def load_script(name,filename):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/filename)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value)
    return value


@pytest.fixture
def native_fixture(admitted_fixture):
    fixture=metadata_zip(admitted_fixture,{'packages':[{'rust_version':'1.92'}]})
    rp,fp,all_cells,archive,out,receipt=fixture
    four=[cell for cell in all_cells if cell['target']!='el10']
    result=members.assemble(rp,fp,four,archive,out,receipt,native=True,selected_cell=('alma10','x86_64'))
    factory=yaml.safe_load((ROOT/'manifests/package-factory.yaml').read_text())
    recipe=yaml.safe_load((out/'recipe/package.yaml').read_text())
    desktop={'recipe':recipe,'recipe_digest':members.digest((out/'recipe/package.yaml').read_bytes()),
        'assembly':str(out/'albacore-alma10-x86_64/rpmbuild'),'source':receipt,'minimum_rust_version':'1.92.0'}
    provider={'identity':native.provider_identity('alma10','x86_64',factory),
        'snapshot_digest':'sha256:'+'c'*64,'artifact_digest':'sha256:'+'d'*64,'artifact_id':7,'tests':{}}
    return native.Inputs({},desktop,provider)


def test_native_actual_planner_keeps_comparators_and_provider_cell(native_fixture):
    chain=load_script('tuna_planner_test','alma-recipe-chain.py')
    key=chain.native_action_key('alma10','x86_64','a'*40,native_fixture)
    plan=chain.plan(ROOT,'alma10','x86_64','a'*40,key,['tuna-desktop'],candidate_scope='tuna',native_inputs=native_fixture)
    record=next(row for row in plan['recipes'] if row['name']=='tuna-desktop')
    assert 'rust >= 1.92.0' in record['requirements']['build']
    assert 'pkgconfig(gtk4-layer-shell-0) >= 1.1' in record['requirements']['build']
    assert 'gtk4-layer-shell' not in plan['dependencies']['tuna-desktop']
    assert plan['readiness'] is False and plan['published'] is False
    native_fixture.provider['identity']['target']='alma10-kitten'
    key=chain.native_action_key('alma10','x86_64','a'*40,native_fixture)
    with pytest.raises(ValueError,match='pinned cell'):
        chain.plan(ROOT,'alma10','x86_64','a'*40,key,['tuna-desktop'],candidate_scope='tuna',native_inputs=native_fixture)


def test_source_vendor_identity_changes_native_action_key(native_fixture):
    chain=load_script('tuna_action_test','alma-recipe-chain.py')
    before=chain.native_action_key('alma10','x86_64','a'*40,native_fixture)
    native_fixture.desktop['source']['lock_sha256']='f'*64
    assert before!=chain.native_action_key('alma10','x86_64','a'*40,native_fixture)


@pytest.mark.parametrize('scope',[None,'native','alma','full'])
def test_generic_planner_refuses_native_input_scope(native_fixture,scope):
    chain=load_script('tuna_scope_test','alma-recipe-chain.py')
    with pytest.raises(ValueError):
        chain.plan(ROOT,'alma10','x86_64','a'*40,'sha256:'+'b'*64,['tuna-desktop'],candidate_scope=scope,native_inputs=native_fixture)


@pytest.mark.parametrize('change',['source','vendor','spec','binding'])
def test_native_adapter_refuses_mutated_material_before_build(native_fixture,tmp_path,monkeypatch,change):
    chain=load_script('tuna_adapter_plan','alma-recipe-chain.py')
    adapter=load_script('tuna_adapter_test','run-alma-recipe-chain.py')
    key=chain.native_action_key('alma10','x86_64','a'*40,native_fixture)
    plan=chain.plan(ROOT,'alma10','x86_64','a'*40,key,['tuna-desktop'],candidate_scope='tuna',native_inputs=native_fixture)
    record=next(row for row in plan['recipes'] if row['name']=='tuna-desktop')
    item=adapter.Adapter(plan,ROOT,tmp_path,native_inputs=native_fixture)
    prepared=Path(native_fixture.desktop['assembly'])
    if change=='source':next(p for p in (prepared/'SOURCES').iterdir() if 'vendor' not in p.name).write_bytes(b'changed')
    elif change=='vendor':(prepared/'SOURCES'/members.MEMBER).write_bytes(b'changed')
    elif change=='spec':next((prepared/'SPECS').glob('*.spec')).write_text('changed')
    else:native_fixture.desktop['minimum_rust_version']='1.99.0'
    monkeypatch.setattr(adapter,'run',lambda *args,**kwargs:pytest.fail('mutated inputs reached native execution'))
    with pytest.raises(ValueError):item.prepare(record,tmp_path/'build',tmp_path/'repo')


@pytest.mark.parametrize('result,signature',[
    ('pkg: digests OK','RSA/SHA512, Key ID aaaaaaaaaaaaaaaa'),
    ('pkg: signatures NOT OK','RSA/SHA512, Key ID aaaaaaaaaaaaaaaa'),
    ('pkg: signatures OK NOKEY','RSA/SHA512, Key ID aaaaaaaaaaaaaaaa'),
    ('pkg: signatures OK','(none)'),
    ('pkg: signatures OK','RSA/SHA512, Key ID bbbbbbbbbbbbbbbb')])
def test_real_signature_predicate_refuses_unsigned_wrong_key_or_legacy_column(result,signature):
    with pytest.raises(ValueError):native.signed_rpm(result,signature,'a'*40)


def test_real_signature_predicate_accepts_direct_header_and_rpmkeys():
    native.signed_rpm('pkg: digests signatures OK','RSA/SHA512, Key ID aaaaaaaaaaaaaaaa','a'*40)


@pytest.mark.parametrize('field,value',[('baseDigest','sha256:'+'0'*64),('platform',{'os':'linux','architecture':'arm64','variant':None}),
    ('cpuBaseline','x86-64-v3'),('sourceRevision','0'*40),('runId',1)])
def test_native_planner_refuses_changed_provider_base_cpu_source_run(native_fixture,field,value):
    chain=load_script('tuna_cell_mutation','alma-recipe-chain.py')
    native_fixture.provider['identity'][field]=value
    key=chain.native_action_key('alma10','x86_64','a'*40,native_fixture)
    with pytest.raises(ValueError,match='pinned cell'):
        chain.plan(ROOT,'alma10','x86_64','a'*40,key,['tuna-desktop'],candidate_scope='tuna',native_inputs=native_fixture)


@pytest.mark.parametrize('field,value',[('base_image','quay.io/example/new@sha256:'+'0'*64),
    ('base_reference','quay.io/example/new@sha256:'+'0'*64),('platform','linux/amd64/v3'),('arch','s390x')])
def test_native_stage_refuses_altered_image_platform_or_arch(admitted_fixture,field,value):
    rp,fp,cells,archive,output,receipt=metadata_zip(admitted_fixture,{'packages':[{'rust_version':'1.92'}]})
    four=copy.deepcopy([cell for cell in cells if cell['target']!='el10'])
    four[0][field]=value
    with pytest.raises(ValueError):members.assemble(rp,fp,four,archive,output,receipt,native=True)
    assert not output.exists()


def test_native_audio_capability_keeps_player_theme_and_backend_smoke(native_fixture):
    recipe=native_fixture.recipe
    for target in ['alma10','alma10-kitten']:
        runtime=recipe['dependencies']['runtime']['targets'][target]
        assert 'libcanberra-pulse.so()(64bit)' in runtime
        assert 'libcanberra-pulseaudio' not in runtime
        assert 'libcanberra-gtk3' in runtime and 'sound-theme-freedesktop' in runtime
    assert 'canberra-gtk-play' in recipe['verify']['smoke']
    assert 'libcanberra-pulse.so' in recipe['verify']['smoke']
    assert 'audio-volume-change.oga' in recipe['verify']['smoke']
    stage=load_script('native_audio_default_stager','stage-tuna-desktop-el10.py')
    assert 'libcanberra-pulseaudio' in stage.runtime_requirements(False)


@pytest.fixture
def bank_fixture(native_fixture,tmp_path,monkeypatch):
    import shutil
    chain=load_script('native_bank_fixture_chain','alma-recipe-chain.py')
    resume=load_script('native_bank_fixture_resume','alma-candidate-resume.py')
    key=chain.native_action_key('alma10','x86_64','a'*40,native_fixture)
    doc=chain.plan(ROOT,'alma10','x86_64','a'*40,key,['tuna-desktop'],candidate_scope='tuna',native_inputs=native_fixture)
    repo=tmp_path/'execution';repo.mkdir()
    state=tmp_path/'signing';(state/'keys').mkdir(parents=True)
    meta=tmp_path/'meta';meta.mkdir()
    work=tmp_path/'native-work';work.mkdir()
    public=b'public current-run key fixture'
    for path in [repo/'candidate-public.gpg',state/'keys/candidate-public.gpg',meta/'candidate-public.gpg']:path.write_bytes(public)
    identity={field:doc[field] for field in ['cell','sourceRevision','actionKey','target','baseDigest','cpuBaseline']}
    identity.update({'repository':native.PACKAGE_REPO,'workflow':native.WORKFLOW,'signerWorkflow':native.WORKFLOW,
       'sourceRef':'refs/heads/fixture','runId':10,'runAttempt':1,'platform':{'os':'linux','architecture':'amd64','variant':'v2'}})
    (meta/'identity.json').write_text(json.dumps(identity))
    signing={'scope':'run-local-unpromoted-candidate','productionReady':False,'fingerprint':'C'*40}
    for path in [state/'identity.json',meta/'candidate-identity.json']:path.write_text(json.dumps(signing))
    name='libseat';prepared=work/name/'rpmbuild'
    shutil.copytree(Path(native_fixture.desktop['assembly']),prepared)
    (repo/'libseat.rpm').write_bytes(b'current completed output')
    (repo/'tuna-desktop.rpm').write_bytes(b'current FAILED verification output')
    (repo/'gtk4-layer-shell.rpm').write_bytes(b'foreign 338 dependency')
    row={'name':name,'inputDigest':resume.inputs(prepared,identity),
         'outputs':[{'path':'libseat.rpm','digest':native.digest(repo/'libseat.rpm')}]}
    (meta/'completed-packages.json').write_text(json.dumps([row]))
    for package in ['libseat','tuna-desktop','gtk4-layer-shell','tuna-provider-input']:
        bank=repo/'buildroots'/package;bank.mkdir(parents=True)
        (bank/'observation.json').write_text('public fixture')
    (repo/'buildroots/tuna-provider-input/provider-input.json').write_text(json.dumps(native_fixture.binding()['provider']))
    checks=[]
    monkeypatch.setattr(native,'verify_bank_rpm',lambda root,path,state:checks.append(path.name))
    commands=[]
    monkeypatch.setattr(native,'command',lambda args,**kwargs:commands.append(args) or '')
    return native_fixture,doc,repo,state,meta,work,tmp_path/'fresh-bank',checks,commands


def test_bank_current_outputs_only_preserves_execution_and_strict_inventory(bank_fixture):
    inputs,doc,repo,state,meta,work,out,checks,commands=bank_fixture
    before={p.name:p.read_bytes() for p in repo.glob('*.rpm')}
    inputs.bank_native(ROOT,doc,repo,state,meta,work,out)
    assert {p.name for p in out.glob('*.rpm')}=={'libseat.rpm'}
    assert checks==['libseat.rpm'] and commands[0][2]=='index'
    assert before=={p.name:p.read_bytes() for p in repo.glob('*.rpm')}
    assert not (out/'buildroots/tuna-desktop').exists()
    assert not (out/'buildroots/gtk4-layer-shell').exists()
    assert (out/'buildroots/tuna-provider-input/provider-input.json').is_file()
    snapshot=load_script('native_bank_strict_snapshot','alma-candidate-snapshot.py')
    identity=json.loads((meta/'identity.json').read_text())
    completed=json.loads((meta/'completed-packages.json').read_text())
    # Actual unchanged production snapshot inventory validator accepts the
    # selected pool and rejects adding either unverified or foreign RPMs.
    document=snapshot.create(out,identity,'candidate-public.gpg','C'*40,completed,False)
    assert {row['name'] for row in document['completedPackages']}=={'libseat'}
    (out/'tuna-desktop.rpm').write_bytes(before['tuna-desktop.rpm'])
    with pytest.raises(ValueError,match='RPM inventory'):
        snapshot.create(out,identity,'candidate-public.gpg','C'*40,completed,False)


@pytest.mark.parametrize('change',['output','inputs','foreign','duplicate','key','epoch','provider','symlink','signature','index'])
def test_bank_rejects_changed_or_unqualified_ownership(bank_fixture,monkeypatch,change):
    inputs,doc,repo,state,meta,work,out,checks,commands=bank_fixture
    if change=='output':(repo/'libseat.rpm').write_bytes(b'changed')
    elif change=='inputs':next((work/'libseat/rpmbuild/SPECS').glob('*.spec')).write_text('changed')
    elif change in ['foreign','duplicate']:
        rows=json.loads((meta/'completed-packages.json').read_text())
        if change=='foreign':rows[0]['name']='gtk4-layer-shell'
        else:rows.append(rows[0])
        (meta/'completed-packages.json').write_text(json.dumps(rows))
    elif change=='key':(repo/'candidate-public.gpg').write_bytes(b'foreign key')
    elif change=='epoch':
        identity=json.loads((meta/'identity.json').read_text());identity['actionKey']='sha256:'+'0'*64
        (meta/'identity.json').write_text(json.dumps(identity))
    elif change=='provider':(repo/'buildroots/tuna-provider-input/provider-input.json').write_text('{}')
    elif change=='symlink':(repo/'buildroots/libseat/link').symlink_to(repo/'tuna-desktop.rpm')
    elif change=='signature':monkeypatch.setattr(native,'verify_bank_rpm',lambda *args:(_ for _ in ()).throw(ValueError('unsigned')))
    else:monkeypatch.setattr(native,'command',lambda *args,**kwargs:(_ for _ in ()).throw(ValueError('index signing failed')))
    with pytest.raises(ValueError):inputs.bank_native(ROOT,doc,repo,state,meta,work,out)
    assert not out.exists()
    assert (repo/'tuna-desktop.rpm').read_bytes()==b'current FAILED verification output'


def test_banking_wrapper_re_admits_provider_and_uses_strict_create():
    text=(ROOT/'scripts/verify-alma-recipe-chain-ci.sh').read_text()
    block=text.split("<<'PY_TUNA_BANK'",1)[1].split('\nPY_TUNA_BANK',1)[0]
    assert 'from_file(' in block and 'bank_native(' in block
    assert '--repo "$bank_repo"' in text
    assert 'if [[ $SCOPE == tuna ]]' in text


@pytest.mark.parametrize('change',['ancestor','destination','file','total','count'])
def test_bank_observation_preflight_limits_before_copy(bank_fixture,monkeypatch,change):
    import shutil
    inputs,doc,repo,state,meta,work,out,checks,commands=bank_fixture
    before={p.name:p.read_bytes() for p in repo.glob('*.rpm')}
    if change=='ancestor':
        original=repo/'buildroots'; moved=repo.parent/'observations';original.rename(moved);original.symlink_to(moved,target_is_directory=True)
    elif change=='destination':
        moved=out.parent/'external';moved.mkdir();link=out.parent/'linked';link.symlink_to(moved,target_is_directory=True);out=link/'new-bank'
    else:
        resume=load_script('bank_limits_test_resume','alma-candidate-resume.py')
        monkeypatch.setattr(native,'bank_resume',lambda root:resume)
        # Isolate observation-copy budgets from the already validated prepared
        # input collector, whose separate mutation regression remains real.
        prepared_digest=json.loads((meta/'completed-packages.json').read_text())[0]['inputDigest']
        monkeypatch.setattr(resume,'inputs',lambda *args:prepared_digest)
        if change=='file':
            monkeypatch.setattr(resume.snapshot,'MAX_FILE',4096)
            (repo/'buildroots/libseat/oversized').write_bytes(b'x'*4097)
        elif change=='count':monkeypatch.setattr(resume.snapshot,'MAX_FILES',2)
        else:monkeypatch.setattr(resume.snapshot,'MAX_TOTAL',len((state/'keys/candidate-public.gpg').read_bytes())+len((repo/'libseat.rpm').read_bytes()))
    with pytest.raises(ValueError,match='bank'):
        inputs.bank_native(ROOT,doc,repo,state,meta,work,out)
    assert not out.exists() and not commands
    assert before=={p.name:p.read_bytes() for p in repo.glob('*.rpm')}


def test_native_digest_streams_without_whole_file_read(tmp_path,monkeypatch):
    path=tmp_path/'payload';path.write_bytes(b'abc')
    monkeypatch.setattr(Path,'read_bytes',lambda self:pytest.fail('whole-file allocation'))
    assert native.digest(path)=='sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'
