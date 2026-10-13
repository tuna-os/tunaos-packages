"""Bounded original EL10 evidence IO; no publication or package build.

Command ownership adapted from reviewed Tuna Desktop node publisher 66c766d3.
The single-thread Linux subreaper must remain alive through retirement; failure
never proves persistent quarantine or a hard kernel IO deadline.
"""
import contextlib
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time

class CommandFailed(RuntimeError):
    pass


class RetirementUnconfirmed(RuntimeError):
    pass


class DescendantsRefused(RuntimeError):
    pass


def bounded_command(argv, path, deadline, *, data=None, limit=4*1024*1024, env=None, stderr_path=None):
    """Linux single-owner command retirement, including orphaned descendants.

    Require one thread and no preexisting children. A temporary subreaper scope
    owns adopted descendants. Keep the session leader unreaped until group kill
    so its PID cannot be reused. Adopted children are killed via pidfds and reaped.
    Cleanup has a separate five-second budget; unconfirmed retirement fails closed.
    Subreaper ownership lasts only while this process lives, not after CLI exit.
    Spawn/kernel I/O stalls are not hard real-time guarantees.
    """
    def children():
        return [int(value) for value in Path('/proc/self/task/'+str(os.getpid())+'/children').read_text().split()]
    if len(list(Path('/proc/self/task').iterdir()))!=1 or children():
        raise ValueError('command supervisor requires exclusive single-thread child ownership')
    if deadline-time.monotonic()<=0:raise TimeoutError('absolute EL10 producer deadline')
    if data is not None and len(data)>4096:raise ValueError('credential stdin exceeds bound')
    libc=ctypes.CDLL(None,use_errno=True)
    prior=ctypes.c_int()
    if libc.prctl(37,ctypes.byref(prior),0,0,0)!=0 or libc.prctl(36,1,0,0,0)!=0:
        raise OSError('command subreaper setup failed')
    proc=None;lingered=False;retired=False;primary=None
    try:
        with contextlib.ExitStack() as files:
            f=files.enter_context(path.open('wb'))
            errors=f if stderr_path is None else files.enter_context(Path(stderr_path).open('wb'))
            def oversized():return f.tell()>limit or errors.tell()>4*1024*1024
            proc=subprocess.Popen(argv,stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                                  stdout=f,stderr=errors,start_new_session=True,env=env)
            if data is not None:
                proc.stdin.write(data);proc.stdin.close()
            while True:
                # WNOWAIT preserves the owned leader PID until group retirement.
                status=os.waitid(os.P_PID,proc.pid,os.WEXITED|os.WNOHANG|os.WNOWAIT)
                if status is not None:break
                if time.monotonic()>=deadline or oversized():raise TimeoutError('EL10 producer command bound')
                time.sleep(.02)
            if time.monotonic()>=deadline or status.si_code!=os.CLD_EXITED or status.si_status!=0 or oversized():
                raise CommandFailed('EL10 producer command failed: '+Path(argv[0]).name)
    except BaseException as error:
        primary=error
    finally:
        if proc is not None:
            if proc.stdin is not None and not proc.stdin.closed:proc.stdin.close()
            # The unreaped owned leader reserves the PGID even after exit.
            # Always kill the group: a successful leader can leave writers alive.
            try:
                os.killpg(proc.pid,signal.SIGKILL)
            except ProcessLookupError:
                pass
            cleanup_deadline=time.monotonic()+5
            while time.monotonic()<cleanup_deadline:
                direct=children()
                if not direct:
                    retired=True;break
                for pid in direct:
                    if pid!=proc.pid:lingered=True
                    try:
                        fd=os.pidfd_open(pid)
                    except ProcessLookupError:
                        continue
                    try:
                        # Direct children cannot be stolen/reused without our
                        # reaping; pidfd also binds the actual kernel process.
                        signal.pidfd_send_signal(fd,signal.SIGKILL)
                    finally:
                        os.close(fd)
                while True:
                    try:pid,status=os.waitpid(-1,os.WNOHANG)
                    except ChildProcessError:break
                    if pid==0:break
                    if pid==proc.pid:proc.returncode=os.waitstatus_to_exitcode(status)
                time.sleep(.01)
            if not retired and not children():retired=True
        else:
            retired=not children()
        if retired:
            if libc.prctl(36,prior.value,0,0,0)!=0:
                raise OSError('command subreaper restore failed')
        else:
            # Keep ownership while this process lives; an ensuing CLI exit is not
            # physical retirement or persistent quarantine proof.
            raise RetirementUnconfirmed('command retirement unconfirmed; EL10 producer must abort') from primary
    if primary is not None:raise primary
    if lingered:raise DescendantsRefused('command left adopted descendants; retired and refused')


class Budget:
    def __init__(self, seconds=2400):
        if not 0 < seconds <= 3000:
            raise ValueError('invalid producer phase budget')
        self.deadline = time.monotonic() + seconds

    def remaining(self, cap=None):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError('EL10 producer absolute deadline')
        return min(left, cap) if cap is not None else left


def safe_member(name):
    if not isinstance(name, str):
        raise ValueError('unsafe evidence member')
    p = Path(name)
    if (not name or len(name) > 240 or
            p.is_absolute() or '..' in p.parts or '\\' in name or
            any(ord(c) < 32 for c in name)):
        raise ValueError('unsafe evidence member')
    return p


def read_regular(path, limit=8*1024*1024, *, deadline=None, sink=None):
    """Read bounded bytes from one stable regular inode, never reopen by name."""
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ValueError('nonregular or oversized evidence')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size,
                                  value.st_mtime_ns, value.st_ctime_ns)
        if identity(before) != identity(info):
            raise ValueError('evidence replaced before read')
        chunks = []; total = 0
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('evidence read exceeded absolute deadline')
            chunk = os.read(fd, 128*1024)
            if not chunk: break
            total += len(chunk)
            if total > limit:
                raise ValueError('evidence grew beyond bound')
            if sink is None: chunks.append(chunk)
            else: sink(chunk)
        if identity(os.fstat(fd)) != identity(before) or total != before.st_size:
            raise ValueError('evidence changed during read')
        return b''.join(chunks)
    finally:
        os.close(fd)


def digest_file(path, limit=256*1024*1024):
    digest = hashlib.sha256()
    read_regular(path, limit, sink=digest.update)
    return digest.hexdigest()


def write_bundle(report, directory):
    """No self-hash: immutable observations + external final file manifest."""
    root = Path(directory)
    observations = {k: v for k, v in report.items() if k != 'artifacts'}
    observed_path = root / 'observations.json'
    observed_path.write_text(json.dumps(observations, indent=2, sort_keys=True) + '\n')
    report['artifacts'].append(dict(name='observations.json', kind='closure-report',
        sha256=digest_file(observed_path), run_id=report['producer']['run_id']))
    (root/'action-key.txt').write_text(report['producer']['action_key']+'\n')
    report['artifacts'].append(dict(name='action-key.txt', kind='action-key',
        sha256=digest_file(root/'action-key.txt'), run_id=report['producer']['run_id']))
    report_path = root/'report.json'
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True)+'\n')
    members = {'report.json': digest_file(report_path)}
    for entry in report['artifacts']:
        name = entry['name']; safe_member(name)
        if name in members:
            raise ValueError('duplicate evidence member')
        measured = digest_file(root/name)
        if measured != entry['sha256']:
            raise ValueError('artifact changed before finalization')
        members[name] = measured
    (root/'bundle.json').write_text(json.dumps({'schema':1,'members':members},indent=2,sort_keys=True)+'\n')
    return report


def verify_bundle(directory):
    root = Path(directory)
    manifest = load_json(root/'bundle.json')
    if manifest.get('schema') != 1 or not 2 <= len(manifest.get('members',{})) <= 512:
        raise ValueError('invalid evidence bundle manifest')
    for name, digest in manifest['members'].items():
        safe_member(name)
        if digest_file(root/name) != digest:
            raise ValueError('original evidence byte hash mismatch')
    report = load_json(root/'report.json')
    expected = {'report.json'} | {e['name'] for e in report['artifacts']}
    if set(manifest['members']) != expected:
        raise ValueError('artifact manifest coverage mismatch')
    for entry in report['artifacts']:
        if manifest['members'][entry['name']] != entry['sha256']:
            raise ValueError('artifact claim differs from original bytes')
    observed = load_json(root/'observations.json')
    if observed != {k:v for k,v in report.items() if k!='artifacts'}:
        raise ValueError('immutable observation payload differs')
    if read_regular(root/'action-key.txt',4096).decode('utf-8') != report['producer']['action_key']+'\n':
        raise ValueError('actual action key bytes differ')
    return report


def capacity(directory, reserve):
    if type(reserve) is not int or reserve <= 0:
        raise ValueError('explicit storage reserve required')
    fs = os.statvfs(directory)
    free = fs.f_bavail * fs.f_frsize
    if free < reserve:
        raise ValueError('insufficient actual producer filesystem capacity')
    return free


def load_json(path, limit=8*1024*1024):
    return json.loads(read_regular(path, limit))


class Execution:
    """One private single-owner native measurement command/network budget."""
    def __init__(self, directory, seconds=2400):
        self.root=Path(directory)
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.budget=Budget(seconds)
        self.index=0

    def command(self, argv, timeout=600, limit=4*1024*1024, env=None):
        deadline=min(self.budget.deadline,time.monotonic()+timeout)
        self.index+=1
        path=self.root/f'command-{self.index:05d}.log'
        bounded_command(argv,path,deadline,limit=limit,env=env,
                        stderr_path=self.root/f'command-{self.index:05d}.stderr.log')
        self.budget.remaining()
        return path.read_bytes()

    def fetch(self, url, limit=256*1024*1024):
        # The network operation lives in an owned process. Socket idle timeout
        # cannot substitute for the parent's absolute deadline/retirement.
        if not isinstance(url,str) or not url.startswith('https://'):
            raise ValueError('only explicit HTTPS evidence sources admitted')
        program=r'''import sys,urllib.request
class SecureRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,req,fp,code,msg,headers,newurl):
  if not newurl.startswith('https://'):raise ValueError('HTTPS downgrade refused')
  return super().redirect_request(req,fp,code,msg,headers,newurl)
request=urllib.request.Request(sys.argv[1],headers={'User-Agent':'tunaos-package-factory'})
with urllib.request.build_opener(SecureRedirect).open(request,timeout=30) as response:
 total=0
 while True:
  block=response.read(65536)
  if not block:break
  total+=len(block)
  if total>int(sys.argv[2]):raise ValueError('network evidence size bound')
  sys.stdout.buffer.write(block)
'''
        return self.command([sys.executable,'-c',program,url,str(limit)],timeout=120,limit=limit)


def check_gpg_colons(text, primary, allowed):
    """Check exact fingerprints and actual valid self/subkey binding records."""
    keys=[];current=None
    for line in text.splitlines():
        parts=line.split(':')
        if parts[0] in ('pub','sub'):
            if len(parts)<12 or parts[1] in ('r','e','d','i'):
                raise ValueError('revoked/expired/invalid pinned public certificate')
            if parts[6] not in ('','0') and (not parts[6].isdigit() or int(parts[6])<=time.time()):
                raise ValueError('pinned public certificate has expired')
            current={'kind':parts[0],'keyid':parts[4],'caps':parts[11],
                     'fingerprint':None,'bound':False,'expired':parts[6]}
            keys.append(current)
        elif parts[0]=='fpr' and current and current['fingerprint'] is None:
            current['fingerprint']=parts[9]
        elif parts[0]=='sig' and current and len(parts)>10:
            if (parts[1]=='!' and parts[4]==primary[-16:] and
                ((current['kind']=='pub' and parts[10].startswith('13')) or
                 (current['kind']=='sub' and parts[10].startswith('18')))):
                current['bound']=True
    if (len([k for k in keys if k['kind']=='pub'])!=1 or not keys or
        keys[0]['fingerprint']!=primary or any(k['fingerprint'] is None for k in keys)):
        raise ValueError('public key primary/fingerprint mismatch')
    admitted={k['fingerprint'] for k in keys if 's' in k['caps'].lower() and k['bound']}
    if set(allowed)!=admitted:
        raise ValueError('pinned signing subkeys differ from actual valid bindings')
    return admitted


def valid_signature_status(text, allowed):
    valid=[]
    for line in text.splitlines():
        if not line.startswith('[GNUPG:] '):continue
        fields=line[9:].split()
        if not fields:raise ValueError('malformed cryptographic signature status')
        if fields[0] in ('BADSIG','ERRSIG','EXPSIG','EXPKEYSIG','REVKEYSIG','NO_PUBKEY','FAILURE','KEYEXPIRED','SIGEXPIRED'):
            raise ValueError('cryptographic signature failure')
        if fields[0]=='VALIDSIG':
            if len(fields)<10 or fields[1] not in allowed or fields[8] not in ('8','9','10'):
                raise ValueError('signature from untrusted full fingerprint')
            valid.append(fields[1])
    if len(valid)!=1:
        raise ValueError('exactly one actual valid signature required')
    return valid[0]


def primary_packages(raw):
    """Parse bounded actual primary XML; no external entities or checksum echo."""
    import bz2
    import gzip
    import io
    import lzma
    import xml.etree.ElementTree as ET
    if len(raw)>128*1024**2:raise ValueError('compressed primary index bound')
    if raw.startswith(b'\x1f\x8b'): reader=gzip.GzipFile(fileobj=io.BytesIO(raw))
    elif raw.startswith(b'BZh'): reader=bz2.BZ2File(io.BytesIO(raw))
    elif raw.startswith(b'\xfd7zXZ'):reader=lzma.LZMAFile(io.BytesIO(raw))
    else:reader=io.BytesIO(raw)
    with reader:
        decoded=reader.read(256*1024**2+1)
    if len(decoded)>256*1024**2 or b'<!DOCTYPE' in decoded or b'<!ENTITY' in decoded:
        raise ValueError('unsafe or oversized primary XML')
    ns='{http://linux.duke.edu/metadata/common}'
    root=ET.fromstring(decoded)
    packages={}
    for node in root.findall(ns+'package'):
        version=node.find(ns+'version');checksum=node.find(ns+'checksum');location=node.find(ns+'location')
        if version is None or checksum is None or location is None:
            raise ValueError('incomplete signed primary package')
        identity=(node.findtext(ns+'name'),version.get('epoch','0'),version.get('ver'),version.get('rel'),node.findtext(ns+'arch'))
        if any(not isinstance(v,str) or not v for v in identity) or identity in packages:
            raise ValueError('ambiguous signed primary identity')
        algorithm=checksum.get('type')
        if algorithm not in ('sha256','sha512') or not re.fullmatch('[0-9a-f]{'+('64' if algorithm=='sha256' else '128')+'}',checksum.text or ''):
            raise ValueError('signed RPM package checksum missing/weak')
        packages[identity]={'algorithm':algorithm,'checksum':checksum.text,'location':location.get('href')}
        if len(packages)>200000:raise ValueError('excessive primary package count')
    return packages


def primary_packages_supervised(path, deadline, expected_sha256):
    """Decode/parse untrusted compressed XML in an owned bounded child."""
    import tempfile
    if deadline is None or time.monotonic() >= deadline:
        raise TimeoutError('absolute primary decoding deadline required')
    with tempfile.TemporaryDirectory(prefix='el10-primary-') as tmp:
        output=Path(tmp)/'decoded.json'
        bounded_command([sys.executable,str(Path(__file__).resolve()),
                         '--decode-primary',str(path),expected_sha256],output,
                        min(deadline,time.monotonic()+60),limit=64*1024**2)
        values=load_json(output,64*1024**2)
    if time.monotonic()>=deadline:
        raise TimeoutError('primary decode exceeded original observer budget')
    if not isinstance(values,list) or len(values)>200000:
        raise ValueError('invalid bounded primary decoder output')
    packages={}
    for entry in values:
        if not isinstance(entry,dict) or set(entry)!={'identity','package'}:
            raise ValueError('invalid primary decoder record')
        identity=entry['identity']; package=entry['package']
        if (not isinstance(identity,list) or len(identity)!=5 or
            any(not isinstance(v,str) or not v for v in identity) or
            tuple(identity) in packages or not isinstance(package,dict)):
            raise ValueError('invalid or duplicate primary decoder identity')
        packages[tuple(identity)]=package
    return packages


def validate_provider_artifacts(report, root, policy, *, deadline):
    """All 33 suppliers bind exact bytes/header and actual signed-index membership."""
    root=Path(root);artifacts=report['artifacts']; primary_cache={}
    for requirement, observed in report['dependencies'].items():
        if len(observed)!=1:raise ValueError('ambiguous supplier observation')
        entry=observed[0];selected=entry['selected']
        def one(kind, field, value):
            found=[a for a in artifacts if a.get('kind')==kind and a.get(field)==value]
            if len(found)!=1:raise ValueError('missing/duplicate exact provider artifact')
            return found[0]
        header_art=one('provider-headers','require',requirement)
        header=load_json(root/safe_member(header_art['name']))
        if header.get('require')!=requirement or header.get('nevra')!=selected['nevra']:
            raise ValueError('actual header artifact names another provider')
        data=header.get('header',{})
        if data.get('sha256header')!=selected['sha256header'] or data.get('evr')!=selected['evr'] or data.get('arch')!=selected['arch']:
            raise ValueError('actual RPM header differs from selected supplier')
        origin_art=one('provider-origin','require',requirement)
        origin=load_json(root/safe_member(origin_art['name']))
        snapshot=origin.get('snapshot',{})
        if origin.get('require')!=requirement or origin.get('arch')!=report.get('arch'):
            raise ValueError('original DNF query belongs to another require/architecture')
        installed=[v for v in snapshot.get('installed',[]) if v.get('arch') in (report['arch'],'noarch')]
        if len(installed)!=1 or installed[0].get('repoid') in (None,'','@System','@commandline'):
            raise ValueError('original installed supplier origin unknown or ambiguous')
        supplier=installed[0]
        fields=('name','epoch','version','release','arch','repoid')
        candidates=[v for v in snapshot.get('available',[]) if all(v.get(k)==supplier.get(k) for k in fields)]
        if len(candidates)!=1 or candidates[0].get('nevra')!=selected['nevra']:
            raise ValueError('original native supplier not bound to one exact candidate origin')
        candidate=candidates[0]
        if (candidate['repoid']!=selected['repoid'] or
            any(candidate.get(k)!=data.get(k) for k in ('name','epoch','version','release','arch'))):
            raise ValueError('original DNF origin differs from selected actual RPM header')
        rpm_art=one('provider-rpm','require',requirement)
        if rpm_art.get('nevra')!=selected['nevra'] or rpm_art.get('repoid')!=selected['repoid'] or rpm_art['sha256']!=selected['rpm_sha256']:
            raise ValueError('actual provider RPM bytes differ from selection')
        signature_art=one('provider-signature','require',requirement)
        signature=load_json(root/safe_member(signature_art['name']))
        if (signature.get('require')!=requirement or signature.get('nevra')!=selected['nevra'] or
            signature.get('rpm_sha256')!=selected['rpm_sha256'] or
            signature.get('signature')!=selected['signature']):
            raise ValueError('original RPM verifier bytes differ from provider signature verdict')
        url=selected['repourl'];primary_art=one('primary-xml','index_url',url)
        raw=(root/safe_member(primary_art['name'])).read_bytes()
        provenance=entry['index_provenance']
        if hashlib.sha256(raw).hexdigest()!=provenance['primary_sha256'] or len(raw)!=provenance['primary_bytes']:
            raise ValueError('actual primary artifact differs from claimed provenance')
        identity=(data.get('name'),data.get('epoch'),data.get('version'),data.get('release'),data.get('arch'))
        cache_key=primary_art['sha256']
        if cache_key not in primary_cache:
            primary_cache[cache_key]=primary_packages_supervised(root/safe_member(primary_art['name']),deadline,primary_art['sha256'])
        packages=primary_cache[cache_key]
        package=packages.get(identity)
        if package is None:raise ValueError('actual selected provider absent from signed primary')
        h=hashlib.new(package['algorithm'])
        with (root/safe_member(rpm_art['name'])).open('rb') as stream:
            while chunk:=stream.read(128*1024):h.update(chunk)
        if h.hexdigest()!=package['checksum']:raise ValueError('provider bytes differ from signed primary checksum')
        if candidate.get('checksum_algorithm') not in ('sha256','sha512'):
            raise ValueError('original DNF checksum missing or weak')
        checksum=hashlib.new(candidate['checksum_algorithm'])
        with (root/safe_member(rpm_art['name'])).open('rb') as stream:
            while chunk:=stream.read(128*1024):checksum.update(chunk)
        if checksum.hexdigest()!=candidate.get('checksum'):
            raise ValueError('original DNF checksum differs from exact provider bytes')
        repoid=selected['repoid']
        allowed=([k['fingerprint'] for k in policy['trusted_keys']]
                 if repoid.startswith('tunaos-published-') else policy['system_keys'].get(repoid,[]))
        if not allowed:raise ValueError('selected provider has no independent signing policy')
        state_art=one('repomd-gpg-status','index_url',url)
        status=read_regular(root/safe_member(state_art['name']),1024*1024).decode('utf-8')
        signer=valid_signature_status(status,allowed)
        if report['signatures']['repomd_asc'][url].get('signer_fingerprint')!=signer:
            raise ValueError('actual cryptographic index verdict differs from report')
        # Original API/source custody admits execution of the fixed GPG verifier.
        # Retain its actual detached signature, not a success-only string.
        asc=one('repomd-asc','index_url',url)
        if not read_regular(root/safe_member(asc['name']),65536):
            raise ValueError('original detached index signature missing')
        repomd_art=one('repomd-xml','index_url',url)
        import xml.etree.ElementTree as ET
        repomd=(root/safe_member(repomd_art['name'])).read_bytes()
        if len(repomd)>4*1024**2 or b'<!DOCTYPE' in repomd or b'<!ENTITY' in repomd:
            raise ValueError('unsafe repomd artifact')
        ns='{http://linux.duke.edu/metadata/repo}'
        primary=[v for v in ET.fromstring(repomd).findall(ns+'data') if v.get('type')=='primary']
        if len(primary)!=1:raise ValueError('signed repomd primary ambiguous')
        checksum=primary[0].find(ns+'checksum');location=primary[0].find(ns+'location')
        if checksum is None or location is None or checksum.get('type') not in ('sha256','sha512'):
            raise ValueError('signed repomd checksum missing/weak')
        if (provenance.get('primary_checksum_algorithm')!=checksum.get('type') or
            provenance.get('primary_checksum_declared')!=checksum.text):
            raise ValueError('reported primary descriptor differs from actual signed repomd')
        if (hashlib.new(checksum.get('type'),raw).hexdigest()!=checksum.text or
            location.get('href')!=provenance['primary_href']):
            raise ValueError('primary bytes/href differ from signed repomd')
    return True


if __name__ == '__main__':
    if len(sys.argv)!=4 or sys.argv[1]!='--decode-primary' or not re.fullmatch('[0-9a-f]{64}',sys.argv[3]):
        raise SystemExit('private bounded primary decoder invocation required')
    raw=read_regular(sys.argv[2],128*1024**2)
    if hashlib.sha256(raw).hexdigest()!=sys.argv[3]:
        raise ValueError('primary bytes changed before bounded decoding')
    packages=primary_packages(raw)
    print(json.dumps([{'identity':list(identity),'package':package}
                      for identity,package in packages.items()],separators=(',',':')))
