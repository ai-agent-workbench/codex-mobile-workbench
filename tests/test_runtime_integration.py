"""Real processes and regular-file bundles, synthetic HTTP/DB only."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

from shared.runtime_bundle import verify_bundle, _remove_owned_tree
from shared.runtime_controller import command_for, child_environment
from shared.runtime_release import (prepare_runtime_release, select_healthy_runtime,
                                    write_runtime_pending, handoff_runtime, accept_runtime,
                                    selected_runtime)

SERVER = '''
import functools,http.server,os,subprocess,sys,time
from pathlib import Path
def run(app,host,port,**kwargs):
    if os.environ.get('RUNTIME_PROBE_WRITER_PID') and (Path.cwd()/'writer').exists():
        code="import os,time;from pathlib import Path;p=Path(%r);t=p.with_suffix('.writing');t.write_text(str(os.getpid()));t.replace(p);time.sleep(60)" % os.environ['RUNTIME_PROBE_WRITER_PID']
        subprocess.Popen([sys.executable,'-c',code],start_new_session=True)
        deadline=time.monotonic()+10
        while not Path(os.environ['RUNTIME_PROBE_WRITER_PID']).exists():
            if time.monotonic()>deadline:raise RuntimeError('synthetic writer did not become ready')
            time.sleep(.01)
    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path=='/login':self.path='/templates/x.txt'
            super().do_GET()
        def log_message(self,*args):pass
    class Server(http.server.HTTPServer):allow_reuse_address=True
    Server((host,port),functools.partial(Handler,directory=str(Path.cwd()))).serve_forever()
'''


class RuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root/'fixture'; self.source.mkdir()
        self.state = self.root/'state'; self.releases = self.state/'releases'
        self.releases.mkdir(parents=True)
        self.children = []
        self.addCleanup(self._cleanup)
        (self.source/'.gitignore').write_text('.venv/\n')
        (self.source/'app.py').write_text('app=object()\n')
        (self.source/'uvicorn.py').write_text(SERVER)
        (self.source/'smoke_test.sh').write_text('exit 0\n')
        for name in ('templates','static'):
            (self.source/name).mkdir(); (self.source/name/'x.txt').write_text('old')
        self.git('init','-q'); self.git('config','user.name','Runtime test')
        self.git('config','user.email','runtime@example.invalid')
        subprocess.run([sys.executable,'-m','venv','--without-pip','--copies',str(self.source/'.venv')],check=True)
        self.commit()
        self.old = '20261008-080000'; self.new = '20261008-090000'
        snapshot=self.releases/self.old; snapshot.mkdir()
        prepare_runtime_release(self.source,snapshot,None)
        (snapshot/'status').write_text('healthy\n')
        select_healthy_runtime(self.releases,self.old)

    def git(self,*args):
        return subprocess.run(['git','-C',str(self.source),*args],check=True,
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE)

    def commit(self):
        self.git('add','.'); self.git('commit','-qm','synthetic runtime')

    def _cleanup(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
                try: child.wait(timeout=5)
                except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=5)
        for snapshot in self.releases.iterdir():
            if snapshot.is_dir() and not snapshot.is_symlink() and (snapshot/'runtime').exists():
                _remove_owned_tree(snapshot/'runtime')

    def port(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0)); return listener.getsockname()[1]

    def get(self,port,path='/login',expected=None):
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{port}{path}',timeout=1) as response:
                    data=response.read().decode()
                    if expected is None or expected==data:return data
            except OSError:pass
            time.sleep(.05)
        self.fail('synthetic runtime did not become ready')

    def prepare_new(self,*,failure=None):
        for name in ('templates','static'):(self.source/name/'x.txt').write_text('new')
        if failure=='import':(self.source/'app.py').write_text("raise RuntimeError('synthetic import failure')\n")
        if failure=='smoke':
            (self.source/'smoke_test.sh').write_text('exit 1\n')
            (self.source/'writer').write_text('synthetic detached writer')
        self.commit()
        snapshot=self.releases/self.new; snapshot.mkdir()
        prepare_runtime_release(self.source,snapshot,self.old)
        write_runtime_pending(self.releases,self.new,self.old)
        handoff_runtime(self.releases,self.new)

    def test_pointer_and_development_changes_cannot_change_old_process_static_or_templates(self):
        old_bundle=selected_runtime(self.releases)
        old_manifest=verify_bundle(old_bundle)
        port_old=self.port()
        old=subprocess.Popen(command_for(old_bundle,'web',port=port_old),cwd=old_bundle/'app',
                             env=child_environment(old_bundle,self.state),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.children.append(old); self.assertEqual(self.get(port_old),'old')
        self.prepare_new(); accept_runtime(self.releases,self.new)
        new_bundle=selected_runtime(self.releases); port_new=self.port()
        new=subprocess.Popen(command_for(new_bundle,'web',port=port_new),cwd=new_bundle/'app',
                             env=child_environment(new_bundle,self.state),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.children.append(new); self.assertEqual(self.get(port_new),'new')
        for name in ('templates','static'):(self.source/name/'x.txt').write_text('unpublished developer bytes')
        for path in ('/login','/static/x.txt'):
            self.assertEqual(self.get(port_old,path),'old')
            self.assertEqual(self.get(port_new,path),'new')
        self.assertEqual(verify_bundle(old_bundle),old_manifest)
        verify_bundle(new_bundle)

    def test_failed_smoke_compensates_after_detached_writer_is_gone(self):
        self._failure('smoke')

    def test_import_failure_compensates_without_application_lifespan(self):
        self._failure('import')

    def _failure(self,kind):
        self.prepare_new(failure=kind)
        port=self.port(); pidfile=self.root/'writer-pid'
        env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',RUNTIME_PROBE_WRITER_PID=str(pidfile))
        owner=subprocess.Popen([sys.executable,'-B','-m','shared.runtime_controller','--state',str(self.state),
                                '--port',str(port),'web'],cwd=Path(__file__).resolve().parents[1],env=env,
                               stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.children.append(owner)
        self.assertEqual(self.get(port,expected='old'),'old')
        self.assertEqual(os.readlink(self.releases/'runtime-current'),self.old+'/runtime')
        self.assertFalse((self.releases/'pending-publish').exists())
        self.assertEqual((self.releases/self.new/'status').read_text(),'failed\n')
        if kind=='smoke':
            self.assertTrue(pidfile.exists())
            self.assertFalse(Path('/proc'/Path(pidfile.read_text())).exists())
        verify_bundle(self.releases/self.old/'runtime')
        verify_bundle(self.releases/self.new/'runtime')
