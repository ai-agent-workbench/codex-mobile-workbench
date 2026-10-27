import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from shared import runtime_processes


class RuntimeProcessBarrierTests(unittest.TestCase):
    def test_expired_pidfd_is_evicted_before_reused_owned_pid_is_signalled(self):
        alive = True
        def descendants():
            return {42} if alive else set()
        def send(fd,signum):
            nonlocal alive
            if fd == 101:
                raise ProcessLookupError('old pinned process exited')
            alive = False
        with mock.patch.object(runtime_processes,'_owner',os.getpid()), \
             mock.patch.object(runtime_processes,'_descendants',side_effect=descendants), \
             mock.patch.object(runtime_processes,'_reap'), \
             mock.patch.object(runtime_processes.os,'pidfd_open',side_effect=[101,102]) as opened, \
             mock.patch.object(runtime_processes.os,'close') as closed, \
             mock.patch.object(runtime_processes.signal,'pidfd_send_signal',side_effect=send) as sent, \
             mock.patch.object(runtime_processes.time,'sleep'):
            runtime_processes.stop_descendants(grace=1,kill_timeout=1)
        self.assertEqual(opened.call_count,2)
        self.assertEqual([c.args[0] for c in sent.call_args_list],[101,102])
        self.assertEqual([c.args[0] for c in closed.call_args_list],[101,102])

    def test_detached_orphan_writer_stops_before_compensation_and_peer_survives(self):
        source = Path(__file__).resolve().parents[1]
        # A sibling of the dedicated supervisor is outside its ownership.
        peer = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])
        self.addCleanup(lambda: (peer.kill(), peer.wait()) if peer.poll() is None else None)
        with tempfile.TemporaryDirectory() as temporary:
            code = r'''
import json,os,signal,subprocess,sys,time
from pathlib import Path
from shared.runtime_processes import enable_subreaper,stop_descendants
from shared.runtime_controller import stop_child
root=Path(sys.argv[1]);enable_subreaper()
writer="import os,signal,time;from pathlib import Path;p=Path(%r);p.write_text(str(os.getpid()));signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)" % str(root/'writer')
leader="import subprocess,sys;subprocess.Popen([sys.executable,'-c',%r],start_new_session=True)" % writer
child=subprocess.Popen([sys.executable,'-c',leader]);child.wait(timeout=5)
deadline=time.monotonic()+5
while not (root/'writer').exists():
    if time.monotonic()>deadline:raise RuntimeError('writer not ready')
    time.sleep(.01)
pid=int((root/'writer').read_text())
stop_descendants(grace=.1,kill_timeout=2)
stop_child(child) # already-exited leader must not hide an orphan
assert not Path('/proc/'+str(pid)).exists(), 'writer survived fallback barrier'
print(json.dumps({'writer_stopped':True,'parent_exited':True}))
'''
            result = subprocess.run([sys.executable, '-B', '-c', code, temporary],
                                    cwd=source, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('"writer_stopped": true', result.stdout)
            self.assertIsNone(peer.poll())
