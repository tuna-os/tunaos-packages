#!/usr/bin/env python3
"""Resolve one shared native image pair; no image execution or publication."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys


def resolve(execution, reference):
    if reference!='quay.io/centos/centos:stream10':
        raise ValueError('only the frozen factory CentOS native probe is admitted')
    raw=execution.command(['skopeo','inspect','--raw','docker://'+reference],timeout=60)
    index=json.loads(raw)
    descriptors=index.get('manifests')
    if not isinstance(descriptors,list) or not 2<=len(descriptors)<=32:
        raise ValueError('actual native probe image index missing or oversized')
    result={'schema':1,'reference':reference,'manifest_index_sha256':hashlib.sha256(raw).hexdigest(),
            'scope':'observed registry image metadata; not native execution','images':{}}
    for arch,goarch in [('x86_64','amd64'),('aarch64','arm64')]:
        matches=[d for d in descriptors if d.get('platform',{}).get('architecture')==goarch and
                 d.get('platform',{}).get('os')=='linux' and
                 d.get('platform',{}).get('variant','') in ('','v8')]
        if len(matches)!=1 or not re.fullmatch('sha256:[0-9a-f]{64}',matches[0].get('digest','')):
            raise ValueError('native image descriptor missing/ambiguous')
        digest=matches[0]['digest'];image='quay.io/centos/centos@'+digest
        observed=json.loads(execution.command(['skopeo','inspect','docker://'+image],timeout=60))
        if observed.get('Digest')!=digest or observed.get('Architecture')!=goarch or observed.get('Os')!='linux':
            raise ValueError('actual immutable image architecture/digest differs from descriptor')
        result['images'][arch]={'image':image,'architecture':observed['Architecture'],'digest':digest,
                               'configuration_observation':observed}
    if result['images']['x86_64']['digest']==result['images']['aarch64']['digest']:
        raise ValueError('native pair cannot reuse one platform image digest')
    execution.budget.remaining()
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--factory',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--github-output',required=True,type=Path)
    args=parser.parse_args()
    import yaml
    path=Path(__file__).with_name('el10-evidence.py')
    spec=importlib.util.spec_from_file_location('el10_base_io',path)
    io=importlib.util.module_from_spec(spec);spec.loader.exec_module(io)
    if args.output.exists():raise ValueError('fresh base observation output required')
    args.output.mkdir(mode=0o700,parents=True)
    execution=io.Execution(args.output/'commands',seconds=240)
    factory=yaml.safe_load(io.read_regular(args.factory,4*1024*1024))
    report=resolve(execution,factory['targets']['el10']['probe_image'])
    (args.output/'base-pair.json').write_text(json.dumps(report,indent=2)+'\n')
    with args.github_output.open('a') as stream:
        for arch,entry in report['images'].items():
            stream.write(arch+'_image='+entry['image']+'\n')
            stream.write(arch+'_image_arch='+entry['architecture']+'\n')
    execution.budget.remaining()


if __name__=='__main__':main()
