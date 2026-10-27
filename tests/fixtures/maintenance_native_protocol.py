"""Exact installed native CLI, isolated HOME/CODEX_HOME and two readonly clients.

Run explicitly through scripts/run_tests.sh fixtures.maintenance_native_protocol.
No real account, model turn, production state, unit or native session is used.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from engine.codex_app_server import CodexAppServerClient, PROTOCOL_FIXTURE_0_160_0
from shared.maintenance_native import busy_counts


class NativeLoadedProtocolFixture(unittest.IsolatedAsyncioTestCase):
    async def test_global_loaded_inventory_and_metadata_reads_do_not_unload_other_client_thread(self):
        fixture=json.loads(PROTOCOL_FIXTURE_0_160_0.read_text())
        binary=(Path.home()/'.local/share/codex-cli/current/bin/codex').resolve(strict=True)
        with binary.open('rb') as stream:
            self.assertEqual(fixture['codexSha256'],hashlib.file_digest(stream,'sha256').hexdigest())
        with tempfile.TemporaryDirectory(prefix='native-drain-fixture-') as directory:
            root=Path(directory);home=root/'home';home.mkdir();codex_home=root/'codex';codex_home.mkdir()
            (codex_home/'config.toml').write_text('''model="fixture"
model_provider="fixture"
[analytics]
enabled=false
[model_providers.fixture]
name="SyntheticFixture"
base_url="http://127.0.0.1:9/v1"
wire_api="responses"
requires_openai_auth=false
''')
            socket=root/'native.sock'
            env={'HOME':str(home),'CODEX_HOME':str(codex_home),'PATH':'/usr/bin:/bin','LANG':'C.UTF-8',
                 'NO_PROXY':'127.0.0.1,localhost','no_proxy':'127.0.0.1,localhost'}
            log=(root/'native.log').open('wb')
            process=subprocess.Popen([str(binary),'app-server','--listen','unix://'+str(socket)],
                cwd=root,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
            first=CodexAppServerClient(socket);second=CodexAppServerClient(socket)
            try:
                for _ in range(100):
                    if process.poll() is not None:self.fail('isolated native process failed before socket ready')
                    if socket.exists():break
                    await asyncio.sleep(.05)
                self.assertTrue(socket.exists())
                await first.connect();await second.connect()
                started=await first.request('thread/start',{'model':'fixture','modelProvider':'fixture',
                    'cwd':str(root),'approvalPolicy':'never','sandbox':'read-only'})
                tid=started['thread']['id']
                self.assertIn(tid,await first.list_loaded_thread_ids())
                self.assertIn(tid,await second.list_loaded_thread_ids())
                self.assertEqual([],await second.list_background_terminals(tid))
                self.assertFalse(any((await busy_counts(second)).values()))
                self.assertIn(tid,await first.list_loaded_thread_ids())
                self.assertEqual('idle',(await first.read_thread(tid,include_turns=False))['thread']['status']['type'])
                self.assertIsNone(await first.get_thread_goal(tid))
                # Only thread/start makes a tiny synthetic session, never turn/start/resume or a Goal.
            finally:
                await first.close();await second.close()
                if process.poll() is None:
                    process.terminate()
                    try:await asyncio.to_thread(process.wait,timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill();await asyncio.to_thread(process.wait,timeout=10)
                log.close()


if __name__=='__main__':unittest.main()
