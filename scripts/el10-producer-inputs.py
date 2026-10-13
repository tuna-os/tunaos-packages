"""Genuine new checked-consumer offline metadata; original vendor origin unknown.

Invoked only in the native producer CI container after signed native tools are
installed. No original vendor run or successful metadata flag is invented.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import time


def load(name):
    path=Path(__file__).with_name(name+'.py')
    spec=importlib.util.spec_from_file_location('inputs_'+name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def unpack(archive, destination, execution):
    """Bounded members/expansion; no links/devices/writes outside owned scratch."""
    io=load('el10-evidence');root=Path(destination);root.mkdir(mode=0o700)
    with tarfile.open(archive,'r:*') as tar:
        members=[];seen=set();expanded=0
        for item in tar:
            execution.budget.remaining()
            name=PurePosixPath(item.name)
            if (name.is_absolute() or '..' in name.parts or item.name in seen or
                len(item.name)>512 or not (item.isfile() or item.isdir() or item.issym())):
                raise ValueError('unsafe or duplicate source/vendor archive member')
            seen.add(item.name);members.append(item);expanded+=item.size
            if len(members)>250000 or expanded>4*1024**3 or item.size>256*1024**2:
                raise ValueError('source/vendor archive expansion bound')
        io.capacity(root,expanded+1024**3)
        for item in members:
            execution.budget.remaining();path=root/item.name
            if item.isdir():path.mkdir(parents=True,exist_ok=True);continue
            if item.issym():continue
            path.parent.mkdir(parents=True,exist_ok=True)
            with tar.extractfile(item) as source,path.open('xb') as out:
                left=item.size
                while block:=source.read(min(128*1024,left+1)):
                    execution.budget.remaining();left-=len(block)
                    if left<0:raise ValueError('archive file grew beyond declared size')
                    out.write(block)
                if left:raise ValueError('archive file truncated')
            path.chmod(0o755 if item.mode&0o111 else 0o644)
        # Links are installed last, so no extraction can traverse them.
        for item in members:
            if not item.issym():continue
            path=root/item.name;target=PurePosixPath(item.linkname)
            if target.is_absolute():raise ValueError('absolute archive symlink')
            resolved=(path.parent/item.linkname).resolve()
            if root.resolve() not in resolved.parents or not resolved.is_file():
                raise ValueError('archive symlink target escapes or is missing')
            path.parent.mkdir(parents=True,exist_ok=True);path.symlink_to(item.linkname)
    execution.budget.remaining()
    return root



def validate_metadata_payload(data):
    """Sanity-check the actual Cargo format-1 workspace, never an echoed flag."""
    if (not isinstance(data,dict) or type(data.get('version')) is not int or data['version']!=1 or
        type(data.get('packages')) is not list or not data['packages'] or
        type(data.get('workspace_members')) is not list or not data['workspace_members']):
        raise ValueError('actual Cargo metadata workspace missing or mistyped')
    identifiers=set()
    for package in data['packages']:
        if (not isinstance(package,dict) or any(not isinstance(package.get(k),str) or not package[k]
            for k in ('id','name','version','manifest_path')) or package['id'] in identifiers or
            not Path(package['manifest_path']).is_absolute()):
            raise ValueError('actual Cargo metadata package identity missing or ambiguous')
        identifiers.add(package['id'])
    if any(not isinstance(value,str) or value not in identifiers for value in data['workspace_members']):
        raise ValueError('actual Cargo workspace member absent from package metadata')
    return data

def offline_metadata(recipe, source_archive, vendor_archive, output, execution, context):
    """Actually run the locked/offline consumer operation, retaining its bytes."""
    io=load('el10-evidence');out=Path(output)
    out.mkdir(mode=0o700,parents=True,exist_ok=True)
    for name in ('run_id','run_attempt'):
        if type(context.get(name)) is not int or context[name]<=0:raise ValueError('actual current CI identity missing')
    if not re.fullmatch('[0-9a-f]{40}',context.get('head_sha','')):raise ValueError('actual producer head missing')
    sha=recipe['source']['url'].rsplit('/',1)[-1].removesuffix('.tar.gz')
    if not re.fullmatch('[0-9a-f]{40}',sha):raise ValueError('source recipe is not exact commit')
    if (io.digest_file(source_archive,1024**3)!=recipe['source']['sha256'] or
        io.digest_file(vendor_archive,1024**3)!=recipe['sources'][0]['sha256']):
        raise ValueError('source/vendor bytes differ from frozen recipe')
    source_root=unpack(source_archive,out/'source',execution)/('tuna-desktop-'+sha)
    vendor_root=unpack(vendor_archive,out/'vendor',execution)
    # The reviewed recipe carries vendor/ plus the crates-io replacement.
    if not (vendor_root/'vendor').is_dir():raise ValueError('vendor archive layout differs from recipe')
    if (source_root/'vendor').exists():raise ValueError('source already has conflicting vendor tree')
    (source_root/'vendor').symlink_to(vendor_root/'vendor',target_is_directory=True)
    cargo=source_root/'.cargo';cargo.mkdir(exist_ok=True)
    (cargo/'config.toml').write_text(recipe['build']['cargo_config'])
    lock_before=io.digest_file(source_root/'Cargo.lock',16*1024**2)
    cargo_executable=shutil.which('cargo')
    if not cargo_executable:raise ValueError('native installed Cargo executable missing')
    cargo_executable=str(Path(cargo_executable).resolve(strict=True))
    cargo_hash=io.digest_file(cargo_executable,128*1024**2)
    cargo_home=out/'cargo-home';cargo_home.mkdir(mode=0o700)
    cargo_env=dict(os.environ,CARGO_NET_OFFLINE='true',CARGO_HOME=str(cargo_home))
    version=execution.command([cargo_executable,'--version','--verbose'],timeout=30,env=cargo_env).decode()
    command=['cargo','metadata','--locked','--offline','--format-version','1']
    metadata_path=out/'cargo-metadata.json'
    # Owned child needs a deliberate cwd; use a fixed exec wrapper rather than
    # ambient shell interpolation or changing the observer's global cwd.
    wrapper='import os,sys;os.chdir(sys.argv[1]);os.execvp(sys.argv[2],sys.argv[2:])'
    io.bounded_command([__import__('sys').executable,'-c',wrapper,str(source_root),cargo_executable,*command[1:]],
        metadata_path,min(execution.budget.deadline,time.monotonic()+600),
        limit=64*1024**2,env=cargo_env,stderr_path=out/'cargo-metadata.stderr.log')
    data=io.load_json(metadata_path,64*1024**2)
    validate_metadata_payload(data)
    if io.digest_file(source_root/'Cargo.lock',16*1024**2)!=lock_before:
        raise ValueError('offline metadata incomplete or locked source changed')
    execution.budget.remaining()
    receipt={'schema':1,'kind':'new-consumer-offline-metadata','measurement_run':context['run_id'],
        'measurement_attempt':context['run_attempt'],'head_sha':context['head_sha'],
        'source_sha':sha,'source_archive_sha256':recipe['source']['sha256'],
        'archive_sha256':recipe['sources'][0]['sha256'],'lock_sha256':lock_before,
        'metadata_json_sha256':io.digest_file(metadata_path,64*1024**2),
        'cargo_version':version,'cargo_executable_sha256':cargo_hash,'private_cargo_home':True,'command':command,'exit_code':0,'offline_metadata_success':True,
        'original_vendor_producing_run':None,
        'scope':'new real checked-consumer operation; not original vendor-producing evidence'}
    (out/'consumer-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    return receipt


def validate_checked_consumer(receipt, *, source_sha, lock_sha256, archive_sha256,
                              source_archive_sha256, head_sha, run_id, run_attempt):
    """Validate new-operation binding; original API custody is still required later.

    This is a local consistency check, never authorization of report-supplied CI
    identity and never evidence of the original vendor-producing operation.
    """
    if (not isinstance(receipt, dict) or receipt.get('schema') != 1 or
        receipt.get('kind') != 'new-consumer-offline-metadata' or
        receipt.get('original_vendor_producing_run') is not None or
        'run_id' in receipt or receipt.get('offline_metadata_success') is not True or
        type(receipt.get('exit_code')) is not int or receipt['exit_code'] != 0 or
        receipt.get('command') != ['cargo','metadata','--locked','--offline','--format-version','1']):
        raise ValueError('new checked-consumer operation must remain distinct from vendor origin')
    expected = {'source_sha':source_sha,'lock_sha256':lock_sha256,
                'archive_sha256':archive_sha256,'source_archive_sha256':source_archive_sha256,
                'head_sha':head_sha,'measurement_run':run_id,'measurement_attempt':run_attempt}
    if any(receipt.get(key) != value for key,value in expected.items()):
        raise ValueError('checked-consumer bytes/source/current measurement binding differs')
    if (type(run_id) is not int or run_id <= 0 or type(run_attempt) is not int or run_attempt <= 0 or
        not re.fullmatch('[0-9a-f]{40}',head_sha or '') or
        not re.fullmatch('[0-9a-f]{64}',receipt.get('metadata_json_sha256','')) or
        receipt.get('private_cargo_home') is not True or
        not re.fullmatch('[0-9a-f]{64}',receipt.get('cargo_executable_sha256','')) or
        not isinstance(receipt.get('cargo_version'),str) or not receipt['cargo_version'].strip()):
        raise ValueError('checked-consumer operation lacks actual measurement identity')
    return receipt
