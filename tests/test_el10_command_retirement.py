"""Actual Linux command ownership controls, each in a private observer process."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]


class CommandRetirement(unittest.TestCase):
    def run_control(self, program, duration=.4, limit=4096):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            runner='''
import importlib.util,json,os,pathlib,sys,time
spec=importlib.util.spec_from_file_location('owned_el10',sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
root=pathlib.Path(sys.argv[2]);start=time.monotonic();error=None
try:m.bounded_command([sys.executable,'-c',sys.argv[3],str(root/'pid')],root/'output',start+float(sys.argv[4]),limit=int(sys.argv[5]))
except Exception as e:error=type(e).__name__
children=pathlib.Path('/proc/self/task/'+str(os.getpid())+'/children').read_text().strip()
pid=int((root/'pid').read_text()) if (root/'pid').exists() else None
alive=False
if pid is not None:
 try:os.kill(pid,0);alive=True
 except ProcessLookupError:pass
print(json.dumps(dict(error=error,elapsed=time.monotonic()-start,children=children,owned_child_alive=alive)))
'''
            result=subprocess.run([sys.executable,'-c',runner,str(ROOT/'scripts/el10-evidence.py'),
                                   str(root),program,str(duration),str(limit)],
                                  capture_output=True,text=True,timeout=8)
            self.assertEqual(result.returncode,0,result.stderr)
            receipt=json.loads(result.stdout)
            self.assertFalse(receipt['owned_child_alive'],receipt)
            self.assertEqual(receipt['children'],'',receipt)
            self.assertLess(receipt['elapsed'],6.5,receipt)
            return receipt

    def test_successful_owned_command(self):
        receipt=self.run_control("print('bounded')")
        self.assertIsNone(receipt['error'])

    def test_deadline_hang_retires_owned_child(self):
        receipt=self.run_control("import os,pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(60)")
        self.assertEqual(receipt['error'],'TimeoutError')

    def test_exited_leader_cannot_leave_same_group_stderr_writer(self):
        program="""import os,pathlib,sys,time
pid=os.fork()
if pid:
 pathlib.Path(sys.argv[1]).write_text(str(pid));time.sleep(.03);os._exit(0)
while True:
 os.write(2,b'writer\\n');time.sleep(.02)
"""
        receipt=self.run_control(program,duration=1)
        self.assertEqual(receipt['error'],'DescendantsRefused')

    def test_noisy_output_refuses_and_retires(self):
        receipt=self.run_control("import os,pathlib,sys;pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));\nwhile True:os.write(2,b'x'*65536)",limit=1024)
        self.assertIn(receipt['error'],('TimeoutError','CommandFailed'))

    def test_structured_stdout_separate_bounded_stderr_retained(self):
        helper=str(ROOT/'scripts/el10-evidence.py')
        program=f"""import importlib.util,json,pathlib,sys
spec=importlib.util.spec_from_file_location('io',{helper!r});m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
root=pathlib.Path(sys.argv[1]).parent;execution=m.Execution(root/'separate')
value=execution.command([sys.executable,'-c','import sys;print(42);print("diagnostic",file=sys.stderr)'],timeout=1)
assert json.loads(value)==42
assert (root/'separate/command-00001.stderr.log').read_text()=='diagnostic'+chr(10)
"""
        self.assertIsNone(self.run_control(program,duration=2)['error'])

    def test_actual_primary_decoder_executes_in_owned_child(self):
        helper=str(ROOT/'scripts/el10-evidence.py')
        program=f"""import gzip,importlib.util,pathlib,sys,time
spec=importlib.util.spec_from_file_location('decoder',{helper!r});m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
root=pathlib.Path(sys.argv[1]).parent;path=root/'primary.gz'
xml=b'<metadata xmlns="http://linux.duke.edu/metadata/common"><package><name>fixture</name><arch>noarch</arch><version epoch="0" ver="1" rel="1"/><checksum type="sha256">'+b'a'*64+b'</checksum><location href="fixture.rpm"/></package></metadata>'
path.write_bytes(gzip.compress(xml))
parsed=m.primary_packages_supervised(path,time.monotonic()+2,m.digest_file(path))
assert parsed[('fixture','0','1','1','noarch')]['checksum']=='a'*64
path.write_bytes(gzip.compress(b'<!DOCTYPE bad><metadata/>'))
try:m.primary_packages_supervised(path,time.monotonic()+2,m.digest_file(path))
except m.CommandFailed:pass
else:raise AssertionError('unsafe XML decoder admitted')
"""
        self.assertIsNone(self.run_control(program,duration=3)['error'])

    def test_failed_leader_remains_failure_and_retires_descendant(self):
        program="""import os,pathlib,sys,time
pid=os.fork()
if pid:
 pathlib.Path(sys.argv[1]).write_text(str(pid));time.sleep(.03);os._exit(7)
time.sleep(60)
"""
        receipt=self.run_control(program,duration=1)
        self.assertEqual(receipt['error'],'CommandFailed')


if __name__=='__main__':unittest.main()
