"""Independent original GitHub custody, never report-supplied trust.

The CLI must receive a reviewed exact producer head and run ID. There is no
latest-run fallback and no report-provided artifact/key authorization. Fetch
transport is injectable to exercise API rejection controls without network.
"""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
import zipfile
from types import MappingProxyType

REPOSITORY = 'tuna-os/tunaos-packages'
WORKFLOW = '.github/workflows/lint.yml'
CELLS = {'skipjack-el10-x86_64', 'skipjack-el10-aarch64'}
JOB_PREFIX = 'Native EL10 closure / '
SOURCE_FILES = {'scripts/el10-rpm-closure.py','scripts/el10-measure-closure.py',
    'scripts/el10-evidence.py','scripts/el10-producer-custody.py',
    'scripts/el10-producer-inputs.py','scripts/el10-native-producer.py','scripts/el10-native-base.py',
    '.github/workflows/el10-closure-producer.yml',
    'scripts/stage-tuna-desktop-el10.py','scripts/repo_index.py','scripts/rpm_vercmp.py',
    WORKFLOW,'manifests/package-factory.yaml','packages/tuna-desktop/package.yaml','public.gpg',
    'scripts/el10-keys/policy.json','scripts/el10-keys/centosofficial.asc',
    'scripts/el10-keys/centos-extras.asc','scripts/el10-keys/epel10.asc'}
_TOKEN = object()


class VerifiedCustody:
    """Read-only original API admission result, including exact report bytes."""
    __slots__ = ('_head','_run','_attempt','_archive_hashes','_sources','_report_hashes','_deadline')

    def __init__(self, token, head, run, attempt, archive_hashes, sources, report_hashes, deadline):
        if token is not _TOKEN:
            raise ValueError('custody requires original API/artifact/source verification')
        self._head, self._run, self._attempt = head, run, attempt
        self._archive_hashes = MappingProxyType(dict(archive_hashes))
        self._sources = MappingProxyType(dict(sources))
        self._report_hashes = MappingProxyType(dict(report_hashes))
        self._deadline = deadline

    head = property(lambda self: self._head)
    run = property(lambda self: self._run)
    attempt = property(lambda self: self._attempt)
    archive_hashes = property(lambda self: self._archive_hashes)
    sources = property(lambda self: self._sources)
    report_hashes = property(lambda self: self._report_hashes)
    deadline = property(lambda self: self._deadline)


def valid_run(run, head, expected_run):
    if (type(expected_run) is not int or expected_run <= 0 or
        not re.fullmatch('[0-9a-f]{40}',head) or run.get('id') != expected_run or
        run.get('head_sha') != head or run.get('event') != 'workflow_dispatch' or
        run.get('path') != WORKFLOW or run.get('status') != 'completed' or
        run.get('conclusion') != 'success' or type(run.get('run_attempt')) is not int or
        run['run_attempt'] <= 0 or run.get('repository',{}).get('full_name') != REPOSITORY or
        run.get('head_repository',{}).get('full_name') != REPOSITORY):
        raise ValueError('original successful producer run does not match reviewed identity')


def valid_jobs(jobs, attempt):
    if jobs.get('total_count') != len(jobs.get('jobs',[])):
        raise ValueError('incomplete producer job API page')
    names = {JOB_PREFIX+'el10-closure-'+cell for cell in CELLS}
    measured = [j for j in jobs['jobs'] if j.get('name') in names]
    if len(measured) != 2 or {j['name'] for j in measured} != names:
        raise ValueError('missing or duplicated exact native cell jobs')
    for job in measured:
        if job.get('run_attempt') != attempt or job.get('status') != 'completed' or job.get('conclusion') != 'success':
            raise ValueError('actual native producer job is not successful for this attempt')
        steps = job.get('steps',[])
        for required in ('Measure natively inside the pinned container','Verify original local artifact byte bindings','Validate local native semantics without original-run admission'):
            matches = [s for s in steps if s.get('name') == required]
            if len(matches) != 1 or matches[0].get('conclusion') != 'success':
                raise ValueError('actual native producer measurement/admission step missing')


def artifact_contract(item, run, cell):
    if (item.get('name') != 'el10-closure-'+cell+'-attempt-'+str(run['run_attempt']) or item.get('expired') is not False or
        item.get('workflow_run',{}).get('id') != run['id'] or
        item.get('workflow_run',{}).get('head_sha') != run['head_sha'] or
        type(item.get('id')) is not int or item['id'] <= 0 or
        type(item.get('size_in_bytes')) is not int or not 0 < item['size_in_bytes'] <= 2*1024**3 or
        not re.fullmatch('sha256:[0-9a-f]{64}',item.get('digest',''))):
        raise ValueError('original artifact identity/digest/capacity invalid')


def validate_native_pair(reports):
    """Two exact native legs, same source and exhaustive requirement intent."""
    if set(reports)!=CELLS:raise ValueError('exact two native cell reports required')
    common=None; bases=set()
    for cell,report in reports.items():
        arch=cell.rsplit('-',1)[1]
        if report.get('cell')!=cell or report.get('arch')!=arch:
            raise ValueError('native pair cell/architecture mismatch')
        source=report.get('source_sha')
        covers=report.get('covers_requires',{})
        if (not isinstance(source,str) or not re.fullmatch('[0-9a-f]{40}',source) or
            set(covers)!={'build','runtime'} or type(covers['build']) is not list or
            type(covers['runtime']) is not list or len(covers['build'])!=24 or len(covers['runtime'])!=9 or
            any(not isinstance(v,str) or not v for rows in covers.values() for v in rows) or
            any(len(set(rows))!=len(rows) for rows in covers.values())):
            raise ValueError('native pair lacks exact 33-require source intent')
        identity=(source,tuple(sorted(covers['build'])),tuple(sorted(covers['runtime'])))
        if common is not None and common!=identity:
            raise ValueError('native legs measured different source or requirement intent')
        common=identity
        base=report.get('base_image','')
        if not re.fullmatch(r'quay.io/centos/centos@sha256:[0-9a-f]{64}',base):
            raise ValueError('native pair requires exact admitted CentOS image digests')
        bases.add(base)
    if len(bases)!=2:raise ValueError('native architecture legs cannot share one image manifest digest')
    return True


def admit_original(fetch, checkout, head, run_id, destination, *, deadline=None):
    """Fetch original API objects and ZIP bytes, then validate both exact legs.

    fetch takes an API relative path and output cap; caller owns deadline and
    authenticated transport. No GitHub credential is sent into the container.
    """
    import time
    deadline=time.monotonic()+600 if deadline is None else deadline
    def check_deadline():
        if time.monotonic()>=deadline:raise TimeoutError('absolute original custody deadline')
    def api(path):
        check_deadline()
        value=json.loads(fetch(path, 4*1024*1024))
        check_deadline()
        return value
    run = api('actions/runs/'+str(run_id)); valid_run(run,head,run_id)
    jobs = api(f'actions/runs/{run_id}/attempts/{run["run_attempt"]}/jobs?per_page=100')
    valid_jobs(jobs,run['run_attempt'])
    source_hashes = {}
    for name in sorted(SOURCE_FILES):
        data = api('contents/'+name+'?ref='+head)
        if data.get('encoding') != 'base64' or data.get('type') != 'file':
            raise ValueError('original source blob missing')
        raw = base64.b64decode(data['content'],validate=False)
        blob = hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
        if blob != data.get('sha') or raw != (Path(checkout)/name).read_bytes():
            raise ValueError('executing observer differs from reviewed producer source')
        source_hashes[name] = hashlib.sha256(raw).hexdigest()
    listing = api(f'actions/runs/{run_id}/artifacts?per_page=100')
    if listing.get('total_count') != len(listing.get('artifacts',[])):
        raise ValueError('incomplete artifact API page')
    hashes = {}; reports = {}; report_hashes = {}
    evidence_path = Path(__file__).with_name('el10-evidence.py')
    spec=importlib.util.spec_from_file_location('el10_evidence_custody',evidence_path)
    io=importlib.util.module_from_spec(spec);spec.loader.exec_module(io)
    for cell in sorted(CELLS):
        selected = [a for a in listing['artifacts'] if a.get('name') == 'el10-closure-'+cell+'-attempt-'+str(run['run_attempt'])]
        if len(selected) != 1:raise ValueError('missing or ambiguous original cell artifact')
        item=selected[0];artifact_contract(item,run,cell)
        io.capacity(destination,item['size_in_bytes']+4*1024**3)
        transferred=fetch(f'actions/artifacts/{item["id"]}/zip',item['size_in_bytes'])
        check_deadline()
        archive=Path(destination)/(cell+'.zip')
        if isinstance(transferred,Path):
            if (transferred.stat().st_size!=item['size_in_bytes'] or
                io.digest_file(transferred,2*1024**3)!=item['digest'][7:]):
                raise ValueError('original API archive size/hash differs')
            transferred.rename(archive)
        else:
            # Small injectable test transport; real CLI returns a bounded file.
            if (not isinstance(transferred,bytes) or len(transferred)!=item['size_in_bytes'] or
                hashlib.sha256(transferred).hexdigest()!=item['digest'][7:]):
                raise ValueError('original API archive size/hash differs')
            archive.write_bytes(transferred)
        hashes[cell]=item['digest'][7:]
        expanded=Path(destination)/cell;expanded.mkdir(mode=0o700)
        with zipfile.ZipFile(archive) as z:
            members=z.infolist()
            if len(members)>512 or sum(i.file_size for i in members)>4*1024**3:
                raise ValueError('original archive expansion exceeds admission')
            seen=set()
            for member in members:
                check_deadline()
                if member.is_dir():continue
                name=member.filename;io.safe_member(name)
                if name in seen or member.file_size>256*1024**2 or (member.external_attr>>16)&0o170000==0o120000:
                    raise ValueError('ambiguous/oversized/symlink archive evidence')
                seen.add(name)
                path=expanded/name;path.parent.mkdir(parents=True,exist_ok=True)
                with z.open(member) as src,path.open('xb') as out:
                    remaining=member.file_size
                    while block:=src.read(min(128*1024,remaining+1)):
                        check_deadline()
                        remaining-=len(block)
                        if remaining<0:raise ValueError('archive member overflow')
                        out.write(block)
                    check_deadline()
                    if remaining:raise ValueError('archive member truncated')
        # Workflow uploads the measured bundle at root, never a guessed path.
        report=io.verify_bundle(expanded)
        if (report.get('cell')!=cell or report.get('producer',{}).get('head_sha')!=head or
            report['producer'].get('run_id')!=run_id or report['producer'].get('run_attempt')!=run['run_attempt']):
            raise ValueError('actual report producer differs from original custody')
        check_deadline()
        reports[cell]=report
        report_hashes[cell]=hashlib.sha256(json.dumps(report,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    check_deadline()
    validate_native_pair(reports)
    check_deadline()
    return VerifiedCustody(_TOKEN,head,run_id,run['run_attempt'],hashes,source_hashes,report_hashes,deadline),reports
