"""Local manager contract. No production units, real login, network or models."""
import copy
import io
import json
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import tarfile
import unittest
from unittest import mock

from shared import instance_config as cfg
from shared import instance_manager as manager
from shared import instance_runtime as runtime
from shared import instance_native as native
from shared import instance_recovery as recovery
from shared import runtime_release as release
from engine.conversation_store import ConversationStore
from shared.message_attachments import MessageAttachmentService
from shared.conversation_artifacts import ConversationArtifactService

SOURCE = Path(__file__).resolve().parents[1]
OLD = '20261018-080000'
NEW = '20261018-090000'


class ManagerContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='local-manager-', dir=Path.home())
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = mock.patch.dict(os.environ, {'HOME': str(self.home)})
        self.env.start(); self.addCleanup(self.env.stop)
        self.root = self.home / '中文 $ 实例 % &'
        self.path = self.root / 'instance.json'
        self.config = cfg.initialize(self.path, {'root': str(self.root), 'account': 'fixture-owner'})

    def test_missing_inspect_has_no_side_effect_and_allows_prepare(self):
        missing = self.home / 'missing/instance.json'
        with mock.patch.object(manager, 'user_environment', side_effect=cfg.InstanceError('no bus')):
            result = manager.inspect(missing)
        self.assertFalse(result['instance_found'])
        self.assertEqual(result['revision'], 'absent')
        self.assertIsNone(result['phone_entry'])
        self.assertFalse(missing.parent.exists())

    def test_configure_cas_and_bound_origin_are_not_overwritten(self):
        values = {**self.config['settings'], 'phone_origin': 'https://phone.example.invalid',
                  'expected_revision': manager.revision(self.config)}
        original = self.path.read_bytes()
        with mock.patch.object(manager, 'credential_exists', return_value=True):
            with self.assertRaises(cfg.InstanceError) as error:
                manager.configure(self.path, values)
        self.assertEqual(error.exception.code, 'bound_origin_change')
        self.assertEqual(self.path.read_bytes(), original)
        with mock.patch.object(manager, 'credential_exists', return_value=False):
            manager.configure(self.path, values)
            with self.assertRaises(cfg.InstanceError) as stale:
                manager.configure(self.path, {**values, 'automations': True})
        self.assertEqual(stale.exception.code, 'revision_conflict')
        self.assertFalse(cfg.read(self.path)['settings']['automations'])

    def test_failed_service_query_is_unknown_not_a_stopped_or_missing_service(self):
        values = [(1,'','not_checked'),(0,'','not_checked'),
            (0,'LoadState=not-found\nActiveState=inactive\n','not_installed'),
            (0,'LoadState=loaded\nActiveState=inactive\n','inactive'),
            (0,'LoadState=loaded\nActiveState=active\n','active')]
        with mock.patch.object(manager,'user_environment',return_value={}):
            for code,output,expected in values:
                with self.subTest(code=code,output=output), mock.patch.object(manager.subprocess,'run',
                    return_value=subprocess.CompletedProcess([],code,output,'synthetic bus failure' if code else '')):
                    self.assertEqual(manager.unit_status('fixture.service'),expected)

    def test_credential_metadata_can_read_database_larger_than_profile_limit(self):
        auth = self.root / 'state/auth.sqlite3'
        with sqlite3.connect(auth) as db:
            db.execute('CREATE TABLE passkey_credentials(revoked_at INTEGER, padding BLOB)')
            db.execute('INSERT INTO passkey_credentials VALUES(NULL, zeroblob(80000))')
        auth.chmod(0o600)
        self.assertGreater(auth.stat().st_size, 65536)
        self.assertTrue(manager.credential_exists(self.config))

    def test_environment_does_not_import_credentials_or_other_deployment(self):
        with mock.patch.dict(os.environ, {'CODEX_WEB_DOT_ENABLED': '1', 'OPENAI_API_KEY': 'not-a-real-key',
                                          'CODEX_HOME': '/unrelated', 'no_proxy': 'internal.example'}):
            environment = runtime.instance_environment(self.config)
        self.assertNotIn('OPENAI_API_KEY', environment)
        self.assertEqual(environment['CODEX_WEB_DOT_ENABLED'], '0')
        self.assertEqual(environment['CODEX_HOME'], str(self.root / 'native-home'))
        self.assertIn('127.0.0.1', environment['NO_PROXY'])
        self.assertIn('internal.example', environment['no_proxy'])

    def test_native_record_cannot_authorize_self_consistent_arbitrary_executable(self):
        unexpected = self.root / 'unexpected-executable'
        unexpected.write_text('#!/bin/sh\nexit 0\n'); unexpected.chmod(0o700)
        metadata = native.metadata()
        with mock.patch.object(runtime.subprocess, 'run') as run, self.assertRaises(cfg.InstanceError) as error:
            runtime.verify_native(unexpected, metadata)
        self.assertEqual(error.exception.code, 'native_digest_mismatch')
        run.assert_not_called()

    def test_pinned_full_native_manifest_includes_host_and_licensing_resources(self):
        members = {name for name, _, _, _ in native.MEMBERS}
        self.assertEqual(native.VERSION, '0.160.0')
        self.assertEqual(native.ARCHIVE_SHA256, '4fcc47ab57f52ff75363951a8761146cd10c8288bd86fed45487dbb204a16b71')
        self.assertTrue({'bin/codex', 'bin/codex-code-mode-host', 'codex-path/rg',
                         'codex-resources/voice/NOTICE.md',
                         'codex-resources/voice/licenses/LGPL-2.1.txt'} <= members)
        self.assertTrue({'bin/codex', 'bin/codex-code-mode-host'} <= native.EXECUTABLES)
        self.assertNotIn('codex-package.json', native.EXECUTABLES)
        self.assertEqual(native.metadata()['manifest_sha256'], native.manifest_identity())

    def test_native_unpack_rejects_duplicate_extra_and_link_members(self):
        contents = b'fixture codex'
        members = (('bin', '5', 0, None),
                   ('bin/codex', '0', len(contents), __import__('hashlib').sha256(contents).hexdigest()))
        def archive_with(items):
            path = self.home / ('native-' + str(len(items)) + '.tar.gz')
            with tarfile.open(path, 'w:gz') as output:
                for info, body in items:
                    output.addfile(info, io.BytesIO(body) if body is not None else None)
            return path
        directory = tarfile.TarInfo('bin'); directory.type = tarfile.DIRTYPE
        file = tarfile.TarInfo('bin/codex'); file.size = len(contents)
        duplicate = tarfile.TarInfo('bin/codex'); duplicate.size = len(contents)
        link = tarfile.TarInfo('bin/codex'); link.type = tarfile.SYMTYPE; link.linkname = '/outside'
        invalid_archives = [
            [(directory, None), (file, contents), (duplicate, contents)],
            [(directory, None), (link, None)],
        ]
        with mock.patch.object(native, 'MEMBERS', members), mock.patch.object(native, 'EXECUTABLES', frozenset({'bin/codex'})):
            for index, items in enumerate(invalid_archives, 1):
                path = archive_with(items)
                destination = self.home / ('unpack-' + str(index)); destination.mkdir(mode=0o700)
                with self.assertRaises(cfg.InstanceError) as error:
                    native.unpack(path, destination)
                self.assertEqual(error.exception.code, 'unsafe_native_archive')

    def test_native_stage_failure_never_deletes_existing_untrusted_tree(self):
        target = self.root / 'native'
        target.mkdir(mode=0o700)
        old = target / 'keep-unknown'; old.write_bytes(b'preserve this exact tree'); old.chmod(0o600)
        archive = self.root / 'codex-package-0.160.0-linux-x64.tar.gz'
        archive.write_bytes(b'fixture archive'); archive.chmod(0o600)
        with mock.patch.object(native, 'ARCHIVE_SIZE', archive.stat().st_size), \
                mock.patch.object(native, 'ARCHIVE_SHA256', runtime.digest(archive)), \
                mock.patch.object(runtime.native, 'unpack', side_effect=cfg.InstanceError('synthetic stage failure')):
            with self.assertRaises(cfg.InstanceError):
                runtime.install_native(self.config)
        self.assertEqual(old.read_bytes(), b'preserve this exact tree')
        self.assertEqual({item.name for item in target.iterdir()}, {'keep-unknown'})

    def test_missing_proxy_inherit_allows_direct_network_attempt_without_global_mutation(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            environment = runtime.instance_environment(self.config)
        self.assertEqual(environment['HTTPS_PROXY'], '')
        self.assertIn('127.0.0.1', environment['NO_PROXY'])

    def test_owned_services_are_quoted_and_no_credentials_are_materialized(self):
        directory = self.home / 'user-units'
        directory.mkdir(mode=0o700)
        def local_step(args, **kwargs):
            if args[:3] == ['systemctl','--user','show']:
                return ('FragmentPath=' + str(directory / args[3]) + '\nDropInPaths=\nLoadState=loaded\nEnvironmentFiles='
                        + str(self.root / 'service.env') + ' (ignore_errors=no)\nWorkingDirectory=' + str(self.root / 'home') + '\n')
            return ''
        with mock.patch.object(runtime, 'unit_directory', return_value=directory), \
                mock.patch.object(runtime, 'user_environment', return_value={}), \
                mock.patch.object(runtime, 'command', side_effect=local_step) as run, \
                mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'not-a-real-key'}):
            value = runtime.write_services(self.config, {'controller': 'controllers/' + 'a' * 16})
        self.assertEqual(set(value), set(manager.units(self.config)))
        web = (directory / manager.units(self.config)[0]).read_text()
        self.assertIn('ExecStart=:', web)
        self.assertIn('%%', web)
        self.assertIn('$', web)
        self.assertNotIn('sudo', web)
        self.assertIn('UnsetEnvironment=OPENAI_API_KEY CODEX_API_KEY OPENAI_ACCESS_TOKEN PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONUSERBASE\n', web)
        self.assertIn('CODEX_WEB_DOT_ENABLED="0"', (self.root / 'service.env').read_text())
        self.assertNotIn('codex-web.service', web)
        self.assertNotIn('not-a-real-key', (self.root / 'service.env').read_text())
        self.assertEqual((self.root / 'service.env').stat().st_mode & 0o777, 0o600)
        self.assertEqual(run.call_args_list[0].args[0][:3], ['systemd-analyze','--user','verify'])
        self.assertEqual(run.call_args_list[1].args[0], ['systemctl','--user','daemon-reload'])
        self.assertIn('WorkingDirectory=' + str(self.root / 'home').replace('%','%%') + '\n', web)
        self.assertIn('EnvironmentFile=' + str(self.root / 'service.env').replace('%','%%') + '\n', web)
        self.assertEqual(len(run.call_args_list), 4)
        foreign = directory / manager.units(self.config)[1]
        foreign.write_text('unrelated service')
        with mock.patch.object(runtime, 'unit_directory', return_value=directory), self.assertRaises(cfg.InstanceError):
            runtime.write_services(self.config, {'controller': 'controllers/' + 'a' * 16})
        self.assertEqual(foreign.read_text(), 'unrelated service')

    @unittest.skipUnless(shutil.which('systemd-analyze'), 'target unit parser unavailable')
    def test_generated_units_pass_real_target_systemd_parser_with_spaced_paths(self):
        directory = self.home / 'parser-units'
        directory.mkdir(mode=0o700)
        native_command = self.root / 'native/bin/codex'
        native_command.parent.mkdir(mode=0o700, parents=True)
        shutil.copyfile('/usr/bin/true', native_command); native_command.chmod(0o700)
        def local_step(args, **kwargs):
            if args[:3] == ['systemd-analyze','--user','verify']:
                result = subprocess.run(args, capture_output=True, text=True, **kwargs)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout
            if args[:3] == ['systemctl','--user','show']:
                return ('FragmentPath=' + str(directory / args[3]) + '\nDropInPaths=\nLoadState=loaded\nEnvironmentFiles='
                        + str(self.root / 'service.env') + ' (ignore_errors=no)\nWorkingDirectory=' + str(self.root / 'home') + '\n')
            return ''
        with mock.patch.object(runtime,'unit_directory',return_value=directory), \
                mock.patch.object(runtime,'user_environment',return_value={**os.environ, 'XDG_RUNTIME_DIR':f'/run/user/{os.getuid()}'}), \
                mock.patch.object(runtime,'command',side_effect=local_step):
            runtime.write_services(self.config, {'controller':'controllers/'+'a'*16})

    def test_effective_unit_without_its_environment_is_never_accepted(self):
        directory = self.home / 'unloaded-environment-units'
        directory.mkdir(mode=0o700)
        with mock.patch.object(runtime,'unit_directory',return_value=directory), \
                mock.patch.object(runtime,'user_environment',return_value={}), \
                mock.patch.object(runtime,'command',return_value=''), self.assertRaises(cfg.InstanceError) as error:
            runtime.write_services(self.config, {'controller':'controllers/'+'a'*16})
        self.assertEqual(error.exception.code, 'service_changed')

    def test_service_directory_respects_private_ancestor_without_chmod(self):
        parent = self.home / '.config'
        parent.mkdir(mode=0o700)
        systemd = parent / 'systemd'
        systemd.mkdir(mode=0o775); systemd.chmod(0o775)
        self.assertEqual(runtime.unit_directory(), systemd / 'user')
        self.assertEqual(systemd.stat().st_mode & 0o777, 0o775)
        parent.chmod(0o755)
        # The temporary HOME is private too; remove that barrier as well.
        self.home.chmod(0o755)
        # The test runner may itself be under an owner-only task directory.
        # Model ambient ancestors as public, without chmod'ing the real runner
        # or weakening the product's private-ancestor traversal protection.
        real_stat = Path.stat
        def ambient_public_stat(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if path != self.home and self.home not in path.parents:
                fields = list(info)
                fields[0] = (fields[0] & ~0o777) | 0o755
                return os.stat_result(fields)
            return info
        with mock.patch.object(Path, 'stat', ambient_public_stat), \
                self.assertRaises(cfg.InstanceError) as error:
            runtime.unit_directory()
        self.assertEqual(error.exception.code, 'unsafe_user_services')

    def test_busy_stop_never_calls_systemctl_stop(self):
        with mock.patch.object(runtime, 'unit_status', return_value='active'), \
                mock.patch.object(runtime, 'software', return_value={}), \
                mock.patch.object(runtime, 'verify_service_binding'), \
                mock.patch.object(runtime, 'runtime_json', return_value={'idle': False}), \
                mock.patch.object(runtime, 'command') as run, self.assertRaises(cfg.InstanceError) as error:
            runtime.stop(self.config)
        self.assertEqual(error.exception.code, 'instance_busy')
        run.assert_not_called()

    def test_missing_native_login_cannot_claim_started(self):
        value = copy.deepcopy(self.config)
        value['settings']['phone_origin'] = 'https://phone.example.invalid'
        with mock.patch.object(runtime, 'software', return_value={}), \
                mock.patch.object(runtime, 'unit_status', return_value='inactive'), \
                mock.patch.object(runtime, 'native_login_ready', return_value=False), \
                mock.patch.object(runtime, 'command') as run, self.assertRaises(cfg.InstanceError) as error:
            runtime.start(value)
        self.assertEqual(error.exception.code, 'native_login_required')
        run.assert_not_called()

    def test_historical_healthy_bytes_do_not_prove_current_startup_is_ready(self):
        value=copy.deepcopy(self.config)
        value['settings']['phone_origin']='https://phone.example.invalid'
        installed={'release':OLD,'unit_sha256':{}}
        states=iter(['inactive','active'])
        def state(name):return next(states,'active')
        with mock.patch.object(runtime,'software',return_value=installed), \
                mock.patch.object(runtime,'unit_status',side_effect=state), \
                mock.patch.object(runtime,'native_login_ready',return_value=True), \
                mock.patch.object(runtime.release,'_current_stamp',return_value=OLD), \
                mock.patch.object(runtime.release,'_status',return_value='healthy'), \
                mock.patch.object(runtime,'write_services',return_value={}), \
                mock.patch.object(runtime,'verify_service_binding'), \
                mock.patch.object(runtime,'command'), \
                mock.patch.object(runtime,'runtime_json',return_value={'ready':False}) as probe, \
                mock.patch.object(runtime.time,'sleep'), \
                mock.patch.object(runtime.time,'monotonic',side_effect=[0,1,200]):
            with self.assertRaises(cfg.InstanceError) as error:runtime.start(value)
        self.assertEqual(error.exception.code,'startup_not_verified')
        probe.assert_called_once()

    def test_cli_inspection_is_single_json_and_no_runtime_is_created(self):
        missing = self.home / 'cli-missing/instance.json'
        result = subprocess.run([sys.executable, str(SOURCE / 'scripts/manage-instance.py'),
            '--config', str(missing), 'inspect'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['ok'])
        self.assertFalse(missing.parent.exists())


class IsolatedRecoveryTests(unittest.TestCase):
    setUp = ManagerContractTests.setUp
    def prepare(self):
        source = self.home / 'source'
        source.mkdir(mode=0o700)
        for origin in runtime.CONTROLLER_FILES:
            target = source / origin
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SOURCE / origin, target)
        (source / 'app.py').write_text('# inert isolated software fixture\n')
        (source / '.gitignore').write_text('.venv/\n')
        env = {**os.environ, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'}
        for args in (['init', '-q'], ['add', '.'], ['-c','user.name=Fixture', '-c','user.email=fixture@example.invalid',
                    '-c','commit.gpgsign=false','commit','-qm','fixture']):
            subprocess.run(['git', *args], cwd=source, env=env, check=True, capture_output=True)
        subprocess.run([sys.executable, '-I', '-m', 'venv', '--without-pip', str(source / '.venv')], check=True, capture_output=True)
        releases = self.root / 'state/releases'
        releases.mkdir(mode=0o700)
        (releases / OLD).mkdir(mode=0o700)
        metadata = release.prepare_runtime_release(source, releases / OLD, None)
        release.write_runtime_pending(releases, OLD, None)
        release.handoff_runtime(releases, OLD)
        release.accept_runtime(releases, OLD)  # inert unit fixture, not production health evidence
        controller, hashes = runtime.install_controller(source, self.root)
        # Tiny complete bundle fixture: all production pins remain in the source
        # module; only this process replaces them with a checked miniature kit.
        fixture_files = {
            'bin/codex': b'#!/bin/sh\necho codex-cli 0.160.0\n',
            'bin/codex-code-mode-host': b'#!/bin/sh\nexit 0\n',
            'codex-package.json': b'{}\n',
            'codex-resources/NOTICE.md': b'fixture notice\n',
        }
        members = []
        directories = {'bin', 'codex-resources'}
        for name in sorted(directories):
            members.append((name, '5', 0, None))
        for name, contents in fixture_files.items():
            members.append((name, '0', len(contents), __import__('hashlib').sha256(contents).hexdigest()))
        patch = mock.patch.object(native, 'MEMBERS', tuple(members))
        patch.start(); self.addCleanup(patch.stop)
        executable_patch = mock.patch.object(native, 'EXECUTABLES', frozenset({'bin/codex', 'bin/codex-code-mode-host'}))
        executable_patch.start(); self.addCleanup(executable_patch.stop)
        native_root = self.root / 'native'
        native_root.mkdir(mode=0o700)
        for name, kind, _, _ in native.MEMBERS:
            path = native_root / name
            if kind == '5':
                path.mkdir(mode=0o700, exist_ok=True)
            else:
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                path.write_bytes(fixture_files[name])
                path.chmod(0o700 if name in native.EXECUTABLES else 0o600)
        license_bytes = b'fixture license\n'
        license_size = mock.patch.object(native, 'LICENSE_SIZE', len(license_bytes))
        license_sha = mock.patch.object(native, 'LICENSE_SHA256', __import__('hashlib').sha256(license_bytes).hexdigest())
        license_size.start(); self.addCleanup(license_size.stop)
        license_sha.start(); self.addCleanup(license_sha.stop)
        (native_root / 'LICENSE.txt').write_bytes(license_bytes)
        (native_root / 'LICENSE.txt').chmod(0o600)
        installed = {'format_version':1,'source_commit':metadata['commit'],'runtime_commit':metadata['commit'],
            'release':OLD,'controller':controller,'controller_sha256':hashes,'unit_sha256':{},
            'native': native.metadata()}
        cfg.write_json(self.root / 'installed.json', installed)
        runtime.bootstrap_account(self.config)
        project = self.root / 'workspace/projects/project'
        project.mkdir(mode=0o700)
        (project / 'keep.txt').write_text('用户资料')
        (project / 'run.sh').write_text('#!/bin/sh\nexit 0\n'); (project / 'run.sh').chmod(0o700)
        store = ConversationStore(self.root / 'state/conversations.sqlite3')
        store.create_conversation('c1', title='合成恢复', cwd=str(project), owner_account_id=self.config['account'], settings={})
        source_file=project/'input.txt';source_file.write_text('合成附件')
        attachments=MessageAttachmentService(store,self.root/'state/message-attachments')
        scope=attachments.scope(owner=self.config['account'],kind='conversation',scope_id='c1',directory=str(project))
        records=attachments.prepare(scope,[(source_file,source_file.name)])
        attachments.commit(records)
        attachments.admit_command('c1','first',kind='start',text='synthetic completed command',payload={'attachments':records})
        store.bind_thread('c1',{'id':'thread-fixture'},server_version='fixture')
        store.bind_turn('c1',{'id':'turn-fixture','status':'inProgress'},command_id='first')
        store.apply_notification({'method':'turn/completed','params':{'threadId':'thread-fixture',
            'turn':{'id':'turn-fixture','status':'completed','items':[]}}})
        output=project/'output.txt';output.write_text('合成文件成果')
        artifacts=ConversationArtifactService(store,self.root/'state/conversation-artifacts')
        artifacts.publish('c1',turn_id='turn-fixture',source_path=str(output),idempotency_key='fixture-output',origin='tool')
        store.begin_command('c1','queued',kind='start',text='must not replay')
        store.update_command('queued','queued')
        store.close()
        from shared.passkey_auth import _init_schema
        with sqlite3.connect(self.root / 'state/auth.sqlite3') as db:
            _init_schema(db)
            db.execute("INSERT INTO passkey_users VALUES('fixture-owner',?,1)",(b'fixture-user',))
            db.execute("INSERT INTO passkey_credentials(account,user_handle,credential_id,public_key,created_at) VALUES('fixture-owner',?,?,?,1)",
                (b'fixture-user',b'fixture-credential',b'inert-public-key'))
            db.execute("INSERT INTO admin_sessions(token_hash,account,credential_id,created_at,last_used_at,absolute_expires_at,reauthenticated_at) VALUES(?,'fixture-owner',?,1,1,2099999999,1)",
                ('c'*64,b'fixture-credential'))
            db.execute("INSERT INTO webauthn_flows(selector_hash,flow_type,challenge,account,user_handle,created_at,expires_at) VALUES(?,'registration',?,'fixture-owner',?,1,2099999999)",
                ('d'*64,b'synthetic-challenge',b'fixture-user'))
            db.execute("INSERT INTO registration_authorizations VALUES(?,'fixture-owner',1,2099999999,NULL)",('e'*64,))
        (self.root / 'state/auth.sqlite3').chmod(0o600)
        (self.root / 'native-home/keep-local.txt').write_text('not login data; prove excluded')
        return releases, installed

    def test_stopped_backup_and_isolated_restore_do_not_transfer_sessions_or_replay(self):
        self.prepare()
        original = (self.root / 'state/session_secret').read_bytes()
        with mock.patch.object(recovery, 'unit_status', return_value='inactive'):
            captured = recovery.backup(self.config)
            target = self.home / 'restored'
            result = recovery.restore(self.config, {'backup':captured['backup'],'destination':str(target)})
        self.assertFalse(result['activated'])
        self.assertEqual((target / 'workspace/projects/project/keep.txt').read_text(), '用户资料')
        self.assertTrue((target / 'workspace/projects/project/run.sh').stat().st_mode & 0o100)
        self.assertFalse(any((target / 'native-home').iterdir()))
        self.assertFalse(cfg.read(target / 'instance.json')['settings']['automations'])
        self.assertNotEqual((target / 'state/session_secret').read_bytes(), original)
        self.assertEqual((self.root / 'state/session_secret').read_bytes(), original)
        with sqlite3.connect(target / 'state/conversations.sqlite3') as db:
            self.assertEqual(db.execute("SELECT state FROM commands WHERE id='queued'").fetchone()[0], 'delivery_unknown')
            self.assertEqual(db.execute("SELECT cwd FROM conversations WHERE id='c1'").fetchone()[0], str(target / 'workspace/projects/project'))
        with sqlite3.connect(target / 'state/auth.sqlite3') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM admin_sessions').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM webauthn_flows').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM registration_authorizations').fetchone()[0],0)
            self.assertIsNotNone(db.execute('SELECT revoked_at FROM passkey_credentials').fetchone()[0])
        restored_store=ConversationStore(target/'state/conversations.sqlite3')
        try:
            attachments=MessageAttachmentService(restored_store,target/'state/message-attachments')
            row=restored_store._db.execute('SELECT record_json FROM message_attachments LIMIT 1').fetchone()
            record=json.loads(row[0])
            self.assertTrue(Path(record['path']).is_relative_to(target/'state/message-attachments'))
            self.assertEqual(attachments.text_preview(record),'合成附件')
            artifacts=ConversationArtifactService(restored_store,target/'state/conversation-artifacts')
            self.assertEqual(artifacts.text_preview(restored_store.artifact_for_key('c1','fixture-output')),'合成文件成果')
        finally:
            restored_store.close()
        with self.assertRaises(cfg.InstanceError):
            recovery.restore(self.config, {'backup':captured['backup'],'destination':str(target)})

    def test_restored_two_version_snapshot_can_rollback_and_be_backed_up_again(self):
        releases, installed = self.prepare()
        source = self.home / 'source'
        newest = '20261018-100000'
        previous = OLD
        for version in (NEW, newest):
            (releases / version).mkdir(mode=0o700)
            metadata = release.prepare_runtime_release(source, releases / version, previous)
            release.write_runtime_pending(releases, version, previous)
            release.handoff_runtime(releases, version)
            release.accept_runtime(releases, version)
            previous = version
        cfg.write_json(self.root / 'installed.json', {**installed, 'release': newest,
            'runtime_commit': metadata['commit']})
        with mock.patch.object(recovery, 'unit_status', return_value='inactive'):
            captured = recovery.backup(self.config)
            backup = Path(captured['backup'])
            target = self.home / 'restored-three-version-history'
            recovery.restore(None, {'backup': str(backup), 'destination': str(target)})
            restored = cfg.read(target / 'instance.json')
            self.assertEqual(recovery.rollback(restored)['release'], NEW)
            second = recovery.backup(restored)
            self.assertEqual(recovery.verify(Path(second['backup']))['versions'], [NEW])
        self.assertFalse((target / 'state/releases' / OLD).exists())
        self.assertIsNone(release._read_meta(target / 'state/releases', NEW)['previous'])
        self.assertEqual(release._read_meta(releases, NEW)['previous'], OLD)
        self.assertEqual(release._read_meta(backup / 'releases', NEW)['previous'], OLD)
        self.assertTrue(json.loads((target / 'restore-report.json').read_text())['runtime_lineage_truncated'])

    def test_native_transaction_rollback_restores_predecessor_after_later_failure(self):
        self.prepare()
        target = self.root / 'native'
        original = (target / 'bin/codex').read_bytes()
        previous = self.root / ('.native-previous-' + '3' * 32)
        stage = self.root / ('.native-stage-' + '3' * 32)
        target.rename(previous)
        native.copy_bundle(previous, target)
        # Match the real installer's durable receipt, not a speculative
        # directory-only fallback that could undo committed metadata.
        runtime._write_receipt(self.root, self._native_receipt(
            previous=previous, stage=stage, old_native=native.metadata()))
        runtime.rollback_native_transaction(self.config, previous)
        self.assertEqual((target / 'bin/codex').read_bytes(), original)
        self.assertFalse(previous.exists())
        self.assertFalse((self.root / runtime.RECEIPT_NAME).exists())
        native.verify(target, native.metadata())

    def test_install_controller_failure_restores_native_transaction(self):
        self.prepare()
        target = self.root / 'native'
        original = (target / 'bin/codex').read_bytes()
        previous = self.root / ('.native-previous-' + '4' * 32)
        stage = self.root / ('.native-stage-' + '4' * 32)

        def install_native_with_receipt(config):
            target.rename(previous)
            native.copy_bundle(previous, target)
            runtime._write_receipt(self.root, self._native_receipt(
                previous=previous, stage=stage, old_native=native.metadata()))
            return native.metadata(), previous

        with mock.patch.object(runtime, 'user_environment', return_value={}), \
                mock.patch.object(runtime, 'unit_directory', return_value=self.home), \
                mock.patch.object(runtime, 'unit_status', return_value='inactive'), \
                mock.patch.object(runtime.release, '_pending', return_value=None), \
                mock.patch.object(runtime.release, '_current_stamp', return_value=None), \
                mock.patch.object(runtime, 'snapshot_source', return_value=('source', 'runtime')), \
                mock.patch.object(runtime, 'command', return_value=''), \
                mock.patch.object(runtime, 'install_native', side_effect=install_native_with_receipt), \
                mock.patch.object(runtime, 'install_controller', side_effect=OSError('synthetic controller failure')):
            with self.assertRaises(OSError):
                runtime.install(self.config, self.home / 'unused-source')
        self.assertEqual((target / 'bin/codex').read_bytes(), original)
        self.assertFalse(previous.exists())
        self.assertFalse((self.root / runtime.RECEIPT_NAME).exists())
        native.verify(target, native.metadata())

    def _native_receipt(self, *, previous, stage, old_native, installed_sha=None):
        return {'format_version': 1, 'operation': 'a' * 32,
                'installed_sha256': runtime._installed_bytes(self.root) if installed_sha is None else installed_sha,
                'old_native': old_native, 'new_native': native.metadata(),
                'previous': previous.name, 'stage': stage.name, 'phase': 'prepared'}

    def test_receipt_before_first_rename_allows_safe_retry(self):
        self.prepare()
        target = self.root / 'native'
        previous = self.root / ('.native-previous-' + '1' * 32)
        stage = self.root / ('.native-stage-' + '1' * 32)
        native.copy_bundle(target, stage)
        metadata = json.loads((self.root / 'installed.json').read_text())['native']
        original = (target / 'bin/codex').read_bytes()
        runtime._write_receipt(self.root, self._native_receipt(previous=previous, stage=stage, old_native=metadata))
        runtime.reconcile_native_transaction(self.config)
        self.assertEqual((target / 'bin/codex').read_bytes(), original)
        self.assertTrue(stage.exists())
        self.assertFalse((self.root / runtime.RECEIPT_NAME).exists())

    def test_receipt_first_install_resets_prepared_or_uncommitted_new_kit(self):
        self.prepare()
        (self.root / 'installed.json').unlink()
        target = self.root / 'native'
        previous = self.root / ('.native-previous-' + '2' * 32)
        stage = self.root / ('.native-stage-' + '2' * 32)
        # New kit was placed but installation metadata was never written.
        runtime._write_receipt(self.root, self._native_receipt(previous=previous, stage=stage, old_native=None))
        runtime.reconcile_native_transaction(self.config)
        self.assertFalse(target.exists())
        native.verify(stage, native.metadata())
        # Process can also die after intent, before its first rename.
        runtime._write_receipt(self.root, self._native_receipt(previous=previous, stage=stage, old_native=None))
        runtime.reconcile_native_transaction(self.config)
        self.assertFalse(target.exists())
        self.assertFalse((self.root / runtime.RECEIPT_NAME).exists())
        self.assertFalse((self.root / 'installed.json').exists())

    def test_broken_receipt_symlink_is_not_ignored(self):
        self.prepare()
        receipt = self.root / runtime.RECEIPT_NAME
        receipt.symlink_to(self.home / 'nonexistent-receipt')
        with self.assertRaises(cfg.InstanceError):
            runtime.reconcile_native_transaction(self.config)
        with self.assertRaises(cfg.InstanceError):
            runtime.software(self.config)
        self.assertTrue(receipt.is_symlink())

    def test_receipt_recovers_process_exit_after_old_rename(self):
        self.prepare()
        target = self.root / 'native'; previous = self.root / ('.native-previous-' + 'a' * 32)
        stage = self.root / ('.native-stage-' + 'a' * 32)
        old_meta = json.loads((self.root / 'installed.json').read_text())['native']
        target.rename(previous)
        runtime._write_receipt(self.root, self._native_receipt(previous=previous, stage=stage, old_native=old_meta))
        runtime.reconcile_native_transaction(self.config)
        self.assertTrue((target / 'bin/codex-code-mode-host').is_file())
        self.assertFalse(previous.exists()); self.assertFalse((self.root / runtime.RECEIPT_NAME).exists())

    def test_receipt_recovers_process_exit_after_new_rename_before_metadata(self):
        self.prepare()
        target = self.root / 'native'; previous = self.root / ('.native-previous-' + 'b' * 32)
        stage = self.root / ('.native-stage-' + 'b' * 32)
        old_meta = json.loads((self.root / 'installed.json').read_text())['native']
        target.rename(previous); native.copy_bundle(previous, target)
        runtime._write_receipt(self.root, self._native_receipt(previous=previous, stage=stage, old_native=old_meta))
        runtime.reconcile_native_transaction(self.config)
        self.assertTrue((target / 'bin/codex').is_file())
        self.assertTrue((stage / 'bin/codex').is_file())
        self.assertFalse((self.root / runtime.RECEIPT_NAME).exists())

    def test_receipt_commits_after_metadata_was_written_before_receipt_clear(self):
        self.prepare()
        target = self.root / 'native'; previous = self.root / ('.native-previous-' + 'c' * 32)
        stage = self.root / ('.native-stage-' + 'c' * 32)
        old_meta = json.loads((self.root / 'installed.json').read_text())['native']
        target.rename(previous); native.copy_bundle(previous, target)
        installed = json.loads((self.root / 'installed.json').read_text()); installed['native'] = native.metadata(); installed['unit_sha256'] = {'synthetic': 'new-metadata'}
        cfg.write_json(self.root / 'installed.json', installed)
        runtime._write_receipt(self.root, self._native_receipt(previous=previous, stage=stage, old_native=old_meta, installed_sha='0' * 64))
        runtime.reconcile_native_transaction(self.config)
        self.assertTrue(previous.exists())
        self.assertFalse((self.root / runtime.RECEIPT_NAME).exists())

    def test_receipt_concurrent_target_or_metadata_change_fails_closed(self):
        self.prepare()
        target = self.root / 'native'; previous = self.root / ('.native-previous-' + 'd' * 32)
        stage = self.root / ('.native-stage-' + 'd' * 32)
        old_meta = json.loads((self.root / 'installed.json').read_text())['native']
        target.rename(previous); target.mkdir(mode=0o700); foreign = target / 'foreign'; foreign.write_bytes(b'concurrent'); foreign.chmod(0o600)
        runtime._write_receipt(self.root, self._native_receipt(previous=previous, stage=stage, old_native=old_meta))
        with self.assertRaises(cfg.InstanceError): runtime.reconcile_native_transaction(self.config)
        self.assertEqual(foreign.read_bytes(), b'concurrent'); self.assertTrue(previous.exists())

    def test_native_predecessor_extra_file_is_retained_after_commit(self):
        self.prepare()
        target = self.root / 'native'; previous = self.root / ('.native-previous-' + 'e' * 32)
        target.rename(previous); native.copy_bundle(previous, target)
        extra = previous / 'owner-extra'; extra.write_bytes(b'keep verbatim'); extra.chmod(0o600)
        runtime.commit_native_transaction(previous)
        self.assertEqual(extra.read_bytes(), b'keep verbatim')
        self.assertTrue(previous.exists())

    def test_complete_native_fixture_rejects_missing_host_link_or_wide_mode(self):
        self.prepare()
        root = self.root / 'native'
        native.verify(root, native.metadata())
        host = root / 'bin/codex-code-mode-host'
        host.unlink()
        with self.assertRaises(cfg.InstanceError) as missing:
            native.verify(root, native.metadata())
        self.assertEqual(missing.exception.code, 'native_digest_mismatch')
        host.write_bytes(b'broken host\n'); host.chmod(0o700)
        with self.assertRaises(cfg.InstanceError):
            native.verify(root, native.metadata())
        # Restore the pinned fixture bytes, then prove type and mode checks remain closed.
        host.write_bytes(b'#!/bin/sh\nexit 0\n'); host.chmod(0o700)
        resource = root / 'codex-resources/NOTICE.md'
        resource.chmod(0o644)
        with self.assertRaises(cfg.InstanceError):
            native.verify(root, native.metadata())
        resource.chmod(0o600)
        host.unlink(); host.symlink_to('../codex-package.json')
        with self.assertRaises(cfg.InstanceError):
            native.verify(root, native.metadata())

    def test_complete_backup_restore_preserves_host_and_resources(self):
        self.prepare()
        with mock.patch.object(recovery, 'unit_status', return_value='inactive'):
            captured = recovery.backup(self.config)
            target = self.home / 'full-native-restored'
            recovery.restore(None, {'backup': captured['backup'], 'destination': str(target)})
        self.assertTrue((target / 'native/bin/codex').is_file())
        self.assertTrue((target / 'native/bin/codex-code-mode-host').is_file())
        self.assertTrue((target / 'native/codex-resources/NOTICE.md').is_file())
        self.assertEqual((target / 'native/bin/codex').stat().st_mode & 0o777, 0o700)
        self.assertEqual((target / 'native/bin/codex-code-mode-host').stat().st_mode & 0o777, 0o700)
        self.assertTrue(os.access(target / 'native/bin/codex', os.X_OK))
        self.assertTrue(os.access(target / 'native/bin/codex-code-mode-host', os.X_OK))
        self.assertEqual((target / 'native/codex-resources/NOTICE.md').stat().st_mode & 0o777, 0o600)
        self.assertFalse(any(path.suffix == '.gz' for path in (target / 'native').iterdir()))

    def test_legacy_native_requires_explicit_migration_and_restores_data_only(self):
        self.prepare()
        full = self.root / 'native'
        shutil.rmtree(full)
        full.mkdir(mode=0o700)
        old = full / 'codex'
        old.write_text('# inert legacy fixture\n'); old.chmod(0o700)
        for name, value in (('NATIVE_BINARY_SIZE', old.stat().st_size),
                            ('NATIVE_BINARY_SHA256', runtime.digest(old))):
            patch = mock.patch.object(runtime, name, value); patch.start(); self.addCleanup(patch.stop)
        installed = json.loads((self.root / 'installed.json').read_text())
        # Model a real pre-native-bundle controller: exact former fileset, not
        # a modern controller made artificially acceptable by a mock.
        controller = self.root / installed['controller']
        shared = controller / 'shared'
        controller.chmod(0o700); shared.chmod(0o700)
        (shared / 'instance_native.py').unlink()
        installed['controller_sha256'].pop('shared/instance_native.py')
        shared.chmod(0o500); controller.chmod(0o500)
        installed['native'] = runtime.legacy_native_metadata()
        cfg.write_json(self.root / 'installed.json', installed)
        with self.assertRaises(cfg.InstanceError) as strict:
            runtime.software(self.config)
        self.assertEqual(strict.exception.code, 'legacy_native_incomplete')
        with mock.patch.object(recovery, 'unit_status', return_value='inactive'):
            with self.assertRaises(cfg.InstanceError):
                recovery.backup(self.config)
            captured = recovery.backup(self.config, legacy_native=True)
            self.assertTrue(captured['legacy_native'])
            with self.assertRaises(cfg.InstanceError):
                recovery.verify(Path(captured['backup']))
            target = self.home / 'legacy-rebuilt'
            restored = recovery.restore(None, {'backup': captured['backup'], 'destination': str(target)})
        self.assertTrue(restored['rebuild_required'])
        self.assertFalse((target / 'native').exists())
        self.assertFalse((target / 'installed.json').exists())

    def test_legacy_running_instance_can_drain_stop_but_cannot_start_or_backup(self):
        self.prepare()
        full = self.root / 'native'; shutil.rmtree(full); full.mkdir(mode=0o700)
        old = full / 'codex'; old.write_text('# inert legacy fixture\n'); old.chmod(0o700)
        for name, value in (('NATIVE_BINARY_SIZE', old.stat().st_size), ('NATIVE_BINARY_SHA256', runtime.digest(old))):
            patch = mock.patch.object(runtime, name, value); patch.start(); self.addCleanup(patch.stop)
        installed = json.loads((self.root / 'installed.json').read_text())
        controller = self.root / installed['controller']; shared = controller / 'shared'
        controller.chmod(0o700); shared.chmod(0o700); (shared / 'instance_native.py').unlink()
        installed['controller_sha256'].pop('shared/instance_native.py'); shared.chmod(0o500); controller.chmod(0o500)
        installed['native'] = runtime.legacy_native_metadata(); cfg.write_json(self.root / 'installed.json', installed)
        with self.assertRaises(cfg.InstanceError): runtime.start(self.config)
        with mock.patch.object(recovery, 'unit_status', return_value='inactive'), self.assertRaises(cfg.InstanceError):
            recovery.backup(self.config)
        states = iter(['active', 'active', 'inactive', 'inactive'])
        executed = []

        def wire(args, **kwargs):
            executed.append(args)
            if args[0] == 'systemctl':
                return ''
            # Exercise runtime_json -> installed_python -> verified legacy
            # release rather than mocking away the nested strict/legacy proof.
            self.assertTrue(str(args[0]).endswith('/runtime/.venv/bin/python'))
            self.assertIn('-c', args)
            return '{"idle":true}'

        with mock.patch.object(runtime, 'unit_status', side_effect=lambda name: next(states)), \
                mock.patch.object(runtime, 'verify_service_binding'), \
                mock.patch.object(runtime, 'user_environment', return_value={}), \
                mock.patch.object(runtime, 'command', side_effect=wire):
            result = runtime.stop(self.config)
        self.assertIn('停止', result['note'])
        self.assertEqual(len(executed), 3)
        self.assertEqual(sum(call[0] == 'systemctl' for call in executed), 1)

    def test_legacy_install_reaches_build_after_real_migration_backup(self):
        self.prepare()
        root = self.root / 'native'
        shutil.rmtree(root); root.mkdir(mode=0o700)
        old = root / 'codex'; old.write_bytes(b'# retired CLI fixture\n'); old.chmod(0o700)
        for name, value in (('NATIVE_BINARY_SIZE', old.stat().st_size), ('NATIVE_BINARY_SHA256', runtime.digest(old))):
            patch = mock.patch.object(runtime, name, value); patch.start(); self.addCleanup(patch.stop)
        installed = json.loads((self.root / 'installed.json').read_text())
        controller = self.root / installed['controller']; shared = controller / 'shared'
        controller.chmod(0o700); shared.chmod(0o700)
        (shared / 'instance_native.py').unlink(); installed['controller_sha256'].pop('shared/instance_native.py')
        shared.chmod(0o500); controller.chmod(0o500)
        installed['native'] = runtime.legacy_native_metadata(); cfg.write_json(self.root / 'installed.json', installed)
        with mock.patch.object(runtime, 'user_environment', return_value={}), \
                mock.patch.object(runtime, 'unit_directory', return_value=self.home), \
                mock.patch.object(runtime, 'unit_status', return_value='inactive'), \
                mock.patch.object(recovery, 'unit_status', return_value='inactive'), \
                mock.patch.object(runtime, 'snapshot_source', side_effect=RuntimeError('reached build boundary')):
            with self.assertRaisesRegex(RuntimeError, 'reached build boundary'):
                runtime.install(self.config, self.home / 'not-read-source')
        captures = list((self.root / 'backups').iterdir())
        self.assertEqual(len(captures), 1)
        self.assertTrue(recovery.verify(captures[0], allow_rebuild=True)['_needs_rebuild'])
        self.assertEqual((root / 'codex').read_bytes(), b'# retired CLI fixture\n')

    def changed_host_backup(self):
        self.prepare()
        with mock.patch.object(recovery,'unit_status',return_value='inactive'):
            captured=recovery.backup(self.config)
        source=Path(captured['backup'])
        manifest=source/'releases'/OLD/'runtime/runtime-bundle.json'
        value=json.loads(manifest.read_text())
        # Same trusted canonical path, changed OS package bytes; never replace
        # or execute the real host interpreter to simulate a different machine.
        value['python']['base_interpreter']['sha256']='0'*64
        manifest.chmod(0o600);manifest.write_text(json.dumps(value));manifest.chmod(0o400)
        return source

    def test_losing_concurrent_restore_does_not_mark_winner_incomplete(self):
        source=self.changed_host_backup()
        target=self.home/'concurrent-winner'
        def competing_restore(*args, **kwargs):
            target.mkdir(mode=0o700)
            (target/'winner.txt').write_text('owned by another restore')
            raise recovery.data.RecoveryError('destination was concurrently published')
        with mock.patch.object(recovery.data,'restore_recovery_bundle',side_effect=competing_restore), \
                mock.patch.object(cfg,'exclusive',side_effect=AssertionError('loser must not lock winner')), \
                self.assertRaises(recovery.data.RecoveryError):
            recovery.restore(None,{'backup':str(source),'destination':str(target)})
        self.assertEqual([path.name for path in target.iterdir()],['winner.txt'])
        self.assertEqual((target/'winner.txt').read_text(),'owned by another restore')

    def test_owned_restore_failure_is_marked_incomplete_under_target_lock(self):
        source=self.changed_host_backup()
        target=self.home/'owned-incomplete'
        with mock.patch.object(cfg,'_ensure_directories',side_effect=OSError('synthetic failure')), \
                self.assertRaises(OSError):
            recovery.restore(None,{'backup':str(source),'destination':str(target)})
        self.assertFalse(json.loads((target/'restore-incomplete.json').read_text())['complete'])
        with self.assertRaises(cfg.InstanceError) as error:
            runtime.start(cfg.read(target/'instance.json'))
        self.assertEqual(error.exception.code,'restore_incomplete')

    def test_changed_host_restores_only_data_without_running_or_copying_old_code(self):
        source=self.changed_host_backup()
        target=self.home/'new-machine-data'
        with mock.patch.object(runtime.bundle,'_run',side_effect=AssertionError('no backup probe needed')), \
                mock.patch.object(recovery,'copy_tree',side_effect=AssertionError('never copy old programs')):
            result=recovery.restore(None,{'backup':str(source),'destination':str(target)})
        self.assertTrue(result['rebuild_required']);self.assertFalse(result['software_restored'])
        for name in ('installed.json','controllers','native','state/releases'):
            self.assertFalse((target/name).exists(),name)
        self.assertFalse(any((target/'native-home').iterdir()))
        self.assertFalse(cfg.read(target/'instance.json')['settings']['automations'])
        self.assertEqual((target/'workspace/projects/project/keep.txt').read_text(),'用户资料')
        with sqlite3.connect(target/'state/auth.sqlite3') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM webauthn_flows').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM admin_sessions').fetchone()[0],0)
            self.assertIsNotNone(db.execute('SELECT revoked_at FROM passkey_credentials').fetchone()[0])
        with sqlite3.connect(target/'state/conversations.sqlite3') as db:
            self.assertEqual(db.execute("SELECT state FROM commands WHERE id='queued'").fetchone()[0],'delivery_unknown')
        with self.assertRaises(runtime.bundle.HostPythonMismatch):recovery.verify(source)

    def test_fresh_machine_cli_restores_with_only_system_python_no_placeholder_profile(self):
        source=self.changed_host_backup()
        target=self.home/'cli-restored-machine'
        selected=self.home/'never-created-selection/instance.json'
        result=subprocess.run(['/usr/bin/python3','-B',str(SOURCE/'scripts/manage-instance.py'),
            '--config',str(selected),'restore'],input=json.dumps({'backup':str(source),'destination':str(target)}),
            capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+' '+result.stderr)
        response=json.loads(result.stdout)
        self.assertTrue(response['ok']);self.assertTrue(response['rebuild_required'])
        self.assertFalse(selected.parent.exists())
        self.assertTrue((target/'instance.json').is_file())
        self.assertFalse((target/'installed.json').exists())

    def test_changed_host_does_not_hide_data_corruption(self):
        source=self.changed_host_backup()
        file=source/'data/workspace/projects/project/keep.txt'
        file.chmod(0o600);file.write_text('damaged backup data')
        target=self.home/'must-not-create-corrupt-data'
        with self.assertRaises(ValueError):recovery.restore(None,{'backup':str(source),'destination':str(target)})
        self.assertFalse(target.exists())

    def test_backup_controller_target_cannot_escape_new_instance(self):
        self.prepare()
        with mock.patch.object(recovery, 'unit_status', return_value='inactive'):
            captured = recovery.backup(self.config)
        path = Path(captured['backup']) / 'backup.json'
        original = json.loads(path.read_text())
        target = self.home / 'not-created'
        for malicious in (str(self.home / 'outside-controller'), '../outside-controller'):
            value = copy.deepcopy(original)
            value['installation']['controller'] = malicious
            cfg.write_json(path, value)
            with self.assertRaises(cfg.InstanceError) as error:
                recovery.restore(self.config, {'backup':captured['backup'],'destination':str(target)})
            self.assertEqual(error.exception.code, 'invalid_installation')
            self.assertFalse(target.exists())
            self.assertFalse((self.home / 'outside-controller').exists())

    def test_running_backup_and_corrupt_restore_fail_before_destination(self):
        self.prepare()
        with mock.patch.object(recovery, 'unit_status', return_value='active'), self.assertRaises(cfg.InstanceError):
            recovery.backup(self.config)
        self.assertFalse(any((self.root / 'backups').iterdir()))
        with mock.patch.object(recovery, 'unit_status', return_value='inactive'):
            captured = recovery.backup(self.config)
        backup = Path(captured['backup'])
        path = backup / 'native/bin/codex'
        path.chmod(0o700); path.write_text('tampered')
        target = self.home / 'must-not-create'
        with self.assertRaises(cfg.InstanceError):
            recovery.restore(self.config, {'backup':str(backup),'destination':str(target)})
        self.assertFalse(target.exists())

    def test_untrusted_backup_cannot_choose_a_verification_executable(self):
        self.prepare()
        with mock.patch.object(recovery,'unit_status',return_value='inactive'):
            captured=recovery.backup(self.config)
        manifest=Path(captured['backup'])/'releases'/OLD/'runtime/runtime-bundle.json'
        value=json.loads(manifest.read_text())
        value['python']['base_interpreter']={'path':'/usr/bin/true','sha256':runtime.digest(Path('/usr/bin/true'))}
        manifest.chmod(0o600);manifest.write_text(json.dumps(value));manifest.chmod(0o400)
        with mock.patch.object(runtime.bundle,'_run',wraps=runtime.bundle._run) as probe:
            with self.assertRaises(ValueError):
                recovery.verify(Path(captured['backup']))
        self.assertFalse(any(call.args and call.args[0][0]=='/usr/bin/true' for call in probe.call_args_list))
        target=self.home/'noncanonical-must-not-create'
        with self.assertRaises(ValueError):recovery.restore(None,{'backup':captured['backup'],'destination':str(target)})
        self.assertFalse(target.exists())

    def test_uninstall_retires_programs_without_deleting_state_or_login_home(self):
        self.prepare()
        original = (self.root / 'state/session_secret').read_bytes()
        with mock.patch.object(recovery,'unit_status',return_value='inactive'):
            result = recovery.uninstall(self.config)
        self.assertTrue(Path(result['retired_software']).is_dir())
        self.assertFalse((self.root / 'installed.json').exists())
        self.assertEqual((self.root / 'workspace/projects/project/keep.txt').read_text(), '用户资料')
        self.assertTrue((self.root / 'native-home/keep-local.txt').exists())
        self.assertEqual((self.root / 'state/session_secret').read_bytes(), original)
        self.assertTrue(Path(result['retired_software'],'installed.json').is_file())
        self.assertTrue(self.path.is_file())

    def test_first_pending_candidate_can_be_withdrawn_but_healthy_cannot(self):
        releases, _ = self.prepare()
        with self.assertRaises(release.RuntimeReleaseError):
            release.withdraw_initial_runtime(releases, OLD)
        self.assertEqual(release._current_stamp(releases), OLD)
        isolated = self.home / 'initial-releases'
        isolated.mkdir(mode=0o700)
        shutil.copytree(releases / OLD, isolated / OLD)
        (isolated / OLD / 'status').chmod(0o600)
        (isolated / OLD / 'status').write_text('candidate\n')
        release.write_runtime_pending(isolated, OLD, None)
        release.handoff_runtime(isolated, OLD)
        release.withdraw_initial_runtime(isolated, OLD)
        self.assertIsNone(release._current_stamp(isolated))
        self.assertEqual(release._status(isolated,OLD),'failed')
        self.assertIsNone(release._pending(isolated))
