#!/usr/bin/env python3
"""CI-only native EL10 measurement, not publication or completed-run admission.

A source-admitted job supplies the exact base image digest and CI identity.
This process performs a NEW offline consumer measurement and native local
checks. A separate observer must later acquire original successful run/job/
source/ZIP evidence; this in-progress job cannot authenticate itself.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
from types import SimpleNamespace

HERE=Path(__file__).resolve().parent


def load(name):
    path=HERE/(name+'.py')
    key='el10_'+name+'_'+hashlib.sha256(str(path).encode()).hexdigest()[:12]
    if key not in sys.modules:
        spec=importlib.util.spec_from_file_location(key,path)
        module=importlib.util.module_from_spec(spec);sys.modules[key]=module
        spec.loader.exec_module(module)
    return sys.modules[key]


def native(args):
    io=load('el10-evidence');inputs=load('el10-producer-inputs')
    producer=load('el10-measure-closure')
    closure=load('el10-rpm-closure');out=Path(args.output)
    if out.exists():raise ValueError('native measurement requires fresh owned output')
    if out.resolve()==HERE.parent or HERE.parent in out.resolve().parents:
        raise ValueError('native output must remain outside checked-out source')
    out.mkdir(mode=0o700,parents=True)
    execution=io.Execution(out/'commands',seconds=2400)
    context=dict(head_sha=args.head_sha,run_id=args.run_id,run_attempt=args.run_attempt)
    if (args.arch not in ('x86_64','aarch64') or args.runner_machine!=args.arch or
        args.image_arch not in {'x86_64':('amd64','x86_64'),'aarch64':('arm64','aarch64')}[args.arch] or
        not re.fullmatch('[0-9a-f]{40}',args.head_sha) or
        type(args.run_id) is not int or args.run_id<=0 or type(args.run_attempt) is not int or args.run_attempt<=0):
        raise ValueError('exact native runner/image/current CI identity required')
    producer.EXECUTION=execution
    # The source-hash-pinned certificate policy is applied before package
    # installation. Existing image Python is supplied by its native DNF
    # runtime; subsequent mandatory crypto measurement records actual keys.
    producer.configure_system_repos()
    execution.command(['dnf','-y','install','python3-pyyaml','gnupg2','rust','cargo','gcc',
                       'binutils','pkgconf-pkg-config','dnf-plugins-core'],timeout=900)
    import yaml
    stager=load('stage-tuna-desktop-el10')
    recipe=yaml.safe_load(io.read_regular(args.recipe,1024*1024))
    factory=yaml.safe_load(io.read_regular(args.factory,4*1024*1024))
    cfg=factory['targets']['el10']
    cells=[]
    for arch in ('x86_64','aarch64'):
        base=getattr(args,'base_'+arch)
        if not re.fullmatch(r'quay.io/centos/centos@sha256:[0-9a-f]{64}',base):
            raise ValueError('actual resolver must supply exact two-architecture image digests')
        cells.append(dict(consumer='skipjack',target='el10',arch=arch,base_image=base,
                          base_reference=cfg['probe_image'],platform='linux/'+('amd64' if arch=='x86_64' else 'arm64')))
    archives=out/'archives';archives.mkdir()
    for entry,name in [(recipe['source'],'source.tar.gz'),(recipe['sources'][0],'vendor.tar.gz')]:
        data=execution.fetch(entry['url'],limit=256*1024**2)
        if hashlib.sha256(data).hexdigest()!=entry['sha256']:
            raise ValueError('download differs from original frozen recipe archive hash')
        (archives/name).write_bytes(data)
    consumer=inputs.offline_metadata(recipe,archives/'source.tar.gz',archives/'vendor.tar.gz',
                                     out/'checked-consumer',execution,context)
    staged_inputs=dict(source_sha=consumer['source_sha'],lock_sha256=consumer['lock_sha256'],
        source_archive_sha256=consumer['source_archive_sha256'],vendor_archive_sha256=consumer['archive_sha256'],
        vendor_receipt=consumer,vendor_url=recipe['sources'][0]['url'],cells=cells)
    (out/'inputs.json').write_text(json.dumps(staged_inputs,indent=2)+'\n')
    receipt=stager.stage_el10_only(args.recipe,args.factory,out/'inputs.json',archives/'source.tar.gz',
                                  archives/'vendor.tar.gz',out/'staged',checked_consumer_context=context)
    cell=next(c for c in receipt['cells'] if c['arch']==args.arch)
    mapped=[]
    for capability in recipe['dependencies']['build']['capabilities']:
        mapped.extend(factory['dependency_catalog'][capability]['el10'])
    plan=closure.plan_base_closure(cell,cfg,mapped+stager.BUILD,stager.RUNTIME)
    (out/'plan.json').write_text(json.dumps(plan,indent=2,sort_keys=True)+'\n')
    producer.cmd_policy(SimpleNamespace(factory=args.factory,keyring=args.factory_key,
        head_sha=args.head_sha,workflow='.github/workflows/lint.yml',out=out/'policy.json'))
    action=closure.compute_action_key(plan['cell'],args.arch,cell['base_image'],args.head_sha,
        '.github/workflows/lint.yml',closure.canonical_sha256(plan),closure.canonical_sha256(receipt))
    measured=out/'measured';measured.mkdir()
    measurement=SimpleNamespace(out=str(measured),arch=args.arch,factory_key=args.factory_key,
        receipt=str(out/'staged/candidate-inputs.json'),image_arch=args.image_arch,
        runner_machine=args.runner_machine,base_digest=cell['base_image'].partition('@sha256:')[2],
        run_id=args.run_id,run_attempt=args.run_attempt,
        run_url='https://github.com/tuna-os/tunaos-packages/actions/runs/'+str(args.run_id),
        head_sha=args.head_sha,workflow='.github/workflows/lint.yml',action_key=action,
        key_name='source-pinned factory certificate',key_id='F7A5375898C78B31',execution=execution)
    report,gaps=producer.measure_cell(plan,cell,measurement,closure)
    execution.budget.remaining()
    if gaps:raise ValueError('actual native measurement retained gaps: '+str(len(gaps)))
    io.verify_bundle(measured)
    policy=io.load_json(out/'policy.json')
    closure.validate_producer_evidence(report,plan,receipt,policy)
    io.validate_provider_artifacts(report,measured,policy,deadline=execution.budget.deadline)
    execution.budget.remaining()
    # Pack original bytes required by later completed-run admission, excluding
    # bulky archives, tool scratch, command logs and extracted source/vendor.
    bundle=out/'upload';bundle.mkdir()
    for name in io.load_json(measured/'bundle.json')['members']:
        target=bundle/io.safe_member(name);target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(measured/name,target)
    shutil.copyfile(measured/'bundle.json',bundle/'bundle.json')
    for source,name in [(out/'plan.json','plan.json'),(out/'policy.json','policy.json'),
                        (out/'staged/candidate-inputs.json','candidate-inputs.json'),
                        (out/'checked-consumer/consumer-receipt.json','checked-consumer.json'),
                        (out/'checked-consumer/cargo-metadata.json','cargo-metadata.json')]:
        shutil.copyfile(source,bundle/name)
    execution.budget.remaining()
    return {'scope':'native local checks only; original completed-run admission pending','cell':plan['cell']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('recipe','factory','factory-key','output','arch','image-arch','runner-machine',
                 'head-sha','base-x86_64','base-aarch64'):
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--run-id',required=True,type=int)
    parser.add_argument('--run-attempt',required=True,type=int)
    args=parser.parse_args()
    print(json.dumps(native(args),indent=2))


if __name__=='__main__':main()
