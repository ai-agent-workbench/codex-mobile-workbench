"""Isolated real persistence and route tests; no live socket or notification."""
import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from itsdangerous import URLSafeTimedSerializer
from starlette.requests import Request
from starlette.responses import Response, RedirectResponse

import projects
from engine.conversation_runtime import ConversationRuntime
from engine.conversation_service import ConversationService
from engine.conversation_store import ConversationStore
from routes import settings_center as api, conversations, conversation_api, projects as project_routes
from shared import conversation_defaults, project_rules
from shared.permission_choice import PermissionChoiceRequired
from shared.conversation_artifacts import ConversationArtifactService
from shared.workbench_settings import SettingsConflict, SettingsStore
from tests.test_conversation_runtime import FakeFactory

CATALOG = {'models': [
    {'model': name, 'displayName': name, 'supportedReasoningEfforts':[{'reasoningEffort':v} for v in ('medium','high')],
     'defaultReasoningEffort':'high', 'serviceTiers':[{'id':'priority','name':'Fast'}]}
    for name in ('gpt-6-sol','gpt-6-astra')],
    'skills':[{'name':'fixture-skill','description':'Fixture', 'enabled':True, 'scope':'user', 'path':'/private/not-exposed'}]}


def request(data=None, *, origin='https://fixture.test', content_type='application/json'):
    raw = json.dumps(data).encode() if data is not None else b''
    async def receive():
        return {'type':'http.request','body':raw,'more_body':False}
    return Request({'type':'http','method':'POST' if data is not None else 'GET','scheme':'https',
                    'path':'/settings/api/config','root_path':'','query_string':b'',
                    'headers':[(b'host',b'fixture.test'),(b'origin',origin.encode()),(b'content-type',content_type.encode())],
                    'server':('fixture.test',443),'client':('127.0.0.1',1234)}, receive)


class SettingsCenterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='settings-test-',dir=Path.home())
        self.root=Path(self.temporary.name)
        self.project=self.root/'projects'/'fixture';self.project.mkdir(parents=True)
        (self.project/projects.META_FILE).write_text(json.dumps({'id':'fixture','name':'测试项目','created':'2026'}))
        self.store=SettingsStore(self.root/'state'/'settings.sqlite3')
        self.service=mock.Mock(catalog=mock.AsyncMock(return_value=CATALOG))
        self.stack=ExitStack();self.addCleanup(self.stack.close);self.addCleanup(self.temporary.cleanup)
        for target,value in [
            ('projects.ROOT',self.root/'projects'),('shared.project_rules.STATE',self.root/'state'),
            ('routes.settings_center.settings_store',lambda:self.store),('shared.conversation_defaults.settings_store',lambda:self.store),
            ('shared.permission_choice.settings_store',lambda:self.store),
            ('routes.settings_center.require_auth',lambda _:None),('routes.settings_center.logged_in',lambda _:'account'),
            ('routes.settings_center.conversation_service',lambda:self.service),('routes.settings_center.audit',mock.Mock()),
            ('routes.settings_center.confirmation_signer',lambda:URLSafeTimedSerializer('test-only-secret')),
        ]: self.stack.enter_context(mock.patch(target,value))

    async def save(self, changes, revision=0, **extra):
        return await api.save_configuration(request({'revision':revision,'changes':changes,**extra}))

    async def test_bootstrap_is_private_no_paths_or_read_side_effects(self):
        value=await api.configuration(request())
        self.assertEqual(value.status_code,200)
        self.assertEqual(value.headers['cache-control'],'private, no-store')
        self.assertNotIn('/private',value.body.decode())
        self.assertNotIn(str(self.root),value.body.decode())
        self.assertFalse(self.store.path.exists())
        self.assertEqual(json.loads(value.body)['effective']['permission'],'danger-full-access')

    async def test_auth_and_origin_gate_all_mutations(self):
        for origin in ('','https://evil.test','null'):
            result=await api.save_configuration(request({'revision':0,'changes':{'personal':'x'}},origin=origin))
            self.assertEqual(result.status_code,400)
        result=await api.save_configuration(request({'revision':0,'changes':{'personal':'x'}},content_type='text/plain'))
        self.assertEqual(result.status_code,400)
        with mock.patch.object(api,'require_auth',return_value=Response(status_code=403)):
            self.assertEqual((await api.configuration(request())).status_code,403)
            self.assertEqual((await self.save({'personal':'x'})).status_code,403)
        self.assertFalse(self.store.path.exists())

    async def test_public_port_origin_can_save_and_denials_leave_revision_unchanged(self):
        with mock.patch('shared.config.PASSKEY_ORIGIN','https://fixture.test:18443'):
            good = await api.save_configuration(request(
                {'revision':0,'changes':{'personal':'经代理保存'}}, origin='https://fixture.test:18443'))
            self.assertEqual(good.status_code, 200)
            rejected = await api.save_configuration(request(
                {'revision':1,'changes':{'personal':'不能写入'}}, origin='https://fixture.test'))
            self.assertEqual(rejected.status_code, 400)
            saved = self.store.read_account('account')
            self.assertEqual(saved['revision'], 1)
            self.assertEqual(saved['personal'], '经代理保存')

    async def test_account_project_defaults_reset_and_permission_confirmation(self):
        self.assertEqual((await self.save({'defaults':{'permission':'read-only','completion_notification':False}})).status_code,200)
        self.assertEqual((await self.save({'defaults':{}},1)).status_code,409)
        self.assertEqual((await self.save({'defaults':{}},1,confirm_permission=True)).status_code,200)
        result=await self.save({'defaults':{'model':'gpt-6-astra','permission':'read-only'}},0,project_id='fixture',account_revision=2)
        self.assertEqual(result.status_code,200)
        resolved=self.store.resolve('account','fixture')
        self.assertEqual(resolved['values']['model'],'gpt-6-astra')
        self.assertEqual(resolved['sources']['model'],'project')
        self.assertEqual((await self.save({'defaults':{}},1,project_id='fixture',account_revision=2)).status_code,409)
        self.assertEqual((await self.save({'defaults':{}},1,project_id='fixture',account_revision=2,confirm_permission=True)).status_code,200)
        self.assertEqual(self.store.read_project('account','fixture')['defaults'],{})
        self.assertEqual(self.store.read_account('account')['defaults'],{})

    async def test_cas_personal_recovery_and_presets(self):
        self.assertEqual((await self.save({'personal':'旧规则'})).status_code,200)
        self.assertEqual((await self.save({'personal':'新规则'},1)).status_code,200)
        self.assertEqual(self.store.read_account('account')['previous_personal'],'旧规则')
        self.assertEqual((await self.save({'personal':'stale'},1)).status_code,409)
        presets=[{'id':'daily','name':'日常','defaults':{'model':'gpt-6-sol','effort':'high'}}]
        self.assertEqual((await self.save({'presets':presets},2)).status_code,200)
        self.assertEqual(self.store.read_account('account')['presets'],presets)
        self.assertEqual(self.store.read_account('other')['revision'],0)

    async def test_invalid_live_combinations_cannot_be_saved(self):
        for defaults in ({'model':'missing'},{'effort':'ultra'},{'service_tier':'invalid'},
                         {'skills':['missing']},{'completion_notification':1},{'permission':'root'}, {'unsafe':1}):
            with self.subTest(defaults=defaults):
                self.assertEqual((await self.save({'defaults':defaults})).status_code,400)
                self.assertEqual(self.store.read_account('account')['revision'],0)
        self.service.catalog.side_effect=OSError('private failure')
        result=await self.save({'defaults':{'effort':'high'}})
        self.assertEqual(result.status_code,503)
        self.assertNotIn('private failure',result.body.decode())
        self.assertEqual((await self.save({'personal':'目录故障不妨碍编辑个人偏好'})).status_code,200)

    async def test_rule_preview_binding_conflict_restore_and_creation(self):
        data={'project_id':'fixture','name':'AGENTS.md','version':'absent','content':'第一版\n'}
        self.assertEqual((await api.change_rule(request(data),'save')).status_code,400)
        preview=await api.change_rule(request(data),'preview');self.assertEqual(preview.status_code,200)
        token=json.loads(preview.body)['token']
        self.assertEqual((await api.change_rule(request({**data,'content':'篡改','token':token}),'save')).status_code,400)
        saved=await api.change_rule(request({**data,'token':token}),'save')
        self.assertEqual(saved.status_code,200)
        self.assertEqual((self.project/'AGENTS.md').read_text(),'第一版\n')
        version=json.loads(saved.body)['version']
        data.update(version=version,content='第二版\n')
        preview=await api.change_rule(request(data),'preview');data['token']=json.loads(preview.body)['token']
        (self.project/'AGENTS.md').write_text('外部修改')
        self.assertEqual((await api.change_rule(request(data),'save')).status_code,409)
        self.assertEqual((self.project/'AGENTS.md').read_text(),'外部修改')
        latest=project_rules.read_rule('fixture','AGENTS.md')
        data.update(version=latest['version'])
        preview=await api.change_rule(request(data),'preview');data['token']=json.loads(preview.body)['token']
        saved=json.loads((await api.change_rule(request(data),'save')).body)
        self.assertEqual(saved['previous'],{'content':'外部修改','exists':True})
        restored=project_rules.write_rule('fixture','AGENTS.md',saved['version'],saved['previous']['content'])
        self.assertEqual(restored['content'],'外部修改')
        self.assertEqual(restored['previous'],{'content':'第二版\n','exists':True})

    def test_rule_paths_symlinks_size_and_unknown_documents(self):
        for name in ('../AGENTS.md','/etc/passwd','.codex-project.json'):
            with self.assertRaises(ValueError):project_rules.read_rule('fixture',name)
        outside=self.root/'outside';outside.write_text('secret')
        (self.project/'AGENTS.md').symlink_to(outside)
        with self.assertRaises(ValueError):project_rules.read_rule('fixture','AGENTS.md')
        self.assertEqual(outside.read_text(),'secret')
        with self.assertRaises(ValueError):project_rules.read_rule('../outside','项目说明.md')
        with self.assertRaises(ValueError):project_rules.preview_rule('fixture','项目说明.md','absent','x'*(project_rules.MAX_BYTES+1))

    async def test_rule_restore_absence_and_corrupt_recovery_fail_closed(self):
        saved=project_rules.write_rule('fixture','AGENTS.md','absent','created')
        self.assertEqual(saved['previous'],{'content':'','exists':False})
        diff=project_rules.preview_rule('fixture','AGENTS.md',saved['version'],'',remove=True)
        self.assertIn('删除',diff)
        restored=project_rules.write_rule('fixture','AGENTS.md',saved['version'],'',remove=True)
        self.assertFalse((self.project/'AGENTS.md').exists())
        self.assertEqual(restored['previous'],{'content':'created','exists':True})
        with self.assertRaises(ValueError):project_rules.write_rule('fixture','AGENTS.md','absent','',remove=True)
        backup=project_rules._bucket(self.project/'AGENTS.md')/'previous.json'
        backup.write_text('{}')
        response=api.rules(request(),'fixture','AGENTS.md')
        self.assertEqual(response.status_code,400)
        self.assertIn('恢复点损坏',response.body.decode())

    def test_rule_late_conflict_preserves_old_recovery(self):
        saved=project_rules.write_rule('fixture','AGENTS.md','absent','original')
        backup=project_rules._bucket(self.project/'AGENTS.md')/'previous.json'
        original_backup=backup.read_bytes()
        atomic=project_rules._atomic
        changed=False
        def external_change(path,raw,mode=0o600):
            nonlocal changed
            atomic(path,raw,mode)
            if path==backup and not changed:
                changed=True
                (self.project/'AGENTS.md').write_text('external')
        with mock.patch.object(project_rules,'_atomic',side_effect=external_change):
            with self.assertRaises(SettingsConflict):
                project_rules.write_rule('fixture','AGENTS.md',saved['version'],'not saved')
        self.assertEqual((self.project/'AGENTS.md').read_text(),'external')
        self.assertEqual(backup.read_bytes(),original_backup)
        self.assertIsNone(project_rules.read_rule('fixture','AGENTS.md')['previous'])

    async def test_diagnostics_close_failure_stays_sanitized(self):
        client=mock.Mock(connect=mock.AsyncMock(side_effect=OSError('secret-path')),close=mock.AsyncMock(side_effect=OSError('secret-close')))
        with mock.patch.object(api,'CodexAppServerClient',return_value=client):
            response=await api.diagnostics(request())
        self.assertEqual(response.status_code,200)
        self.assertNotIn('secret',response.body.decode())
        self.assertEqual(json.loads(response.body)['model_count'],0)

    async def test_new_entrypoints_take_saved_defaults_not_constants(self):
        self.store.save_account('account',0,{'defaults':{'model':'gpt-6-astra','effort':'medium','permission':'read-only','completion_notification':False,'skills':['fixture-skill'],'followup':'queue'},'personal':'简体中文'})
        self.store.save_project('account','fixture',0,{'effort':'high'})
        for module,endpoint in [(conversations,conversations.quick_conversation),(conversation_api,conversation_api.create_conversation_draft),(conversations,conversations.create_and_start_conversation)]:
            service=mock.MagicMock(catalog=mock.AsyncMock(return_value=CATALOG))
            service.create_draft.return_value={'id':'draft'}
            service.public_snapshot.return_value={'conversation':{}}
            service.start=mock.AsyncMock(return_value={'command':{'state':'accepted'}})
            with mock.patch.object(module,'require_auth' if module is conversations else '_auth',return_value=None),mock.patch.object(module,'conversation_service',return_value=service),mock.patch.object(module,'logged_in',return_value='account'),mock.patch.object(module,'audit'),mock.patch.object(module,'classify_client_origin',return_value=('mobile_web',{},{})),mock.patch.object(projects,'touch'):
                params={'project_id':'fixture'}
                if endpoint is conversations.create_and_start_conversation:params.update(prompt='test',model='',skills=[])
                if endpoint is conversation_api.create_conversation_draft:params.update(model='')
                result=await endpoint(request(),**params)
            self.assertIn(result.status_code,(201,303))
            actual=service.create_draft.call_args.kwargs
            self.assertEqual((actual['model'],actual['effort'],actual['permission'],actual['completion_notification']),('gpt-6-astra','high','read-only',False))
            self.assertEqual(actual['web_preferences'],{'personal':'简体中文','followup':'queue','skills':['fixture-skill']})
        values=await conversation_defaults.new_defaults(self.service,'account','fixture',str(self.project),{'model':'gpt-6-sol','service_tier':'','effort':'medium'})
        self.assertEqual(values['model'],'gpt-6-sol');self.assertIsNone(values['service_tier'])
        self.store.save_account('mode-account',0,{'defaults':{'collaboration_mode':'plan'}})
        self.assertEqual(self.store.resolve('mode-account')['values']['collaboration_mode'],'plan')
        self.assertEqual((await conversation_defaults.new_defaults(self.service,'mode-account'))['collaboration_mode'],'plan')
        self.service.catalog.side_effect=OSError('offline')
        with self.assertRaises(ValueError):await conversation_defaults.new_defaults(self.service,'account','fixture')

    async def test_first_permission_choice_needs_explicit_selection_and_conflict_can_retry(self):
        with mock.patch('shared.permission_choice.PERMISSION_CHOICE_REQUIRED', True):
            with self.assertRaises(PermissionChoiceRequired):
                await conversation_defaults.new_defaults(self.service, 'account', explicit={'permission':'danger-full-access'})
            # No catalog call is needed to make the first selection, including
            # when the catalog is offline.
            self.service.catalog.side_effect=OSError('offline')
            self.store.save_account('account', 0, {'personal':'concurrent ordinary save'})
            stale=await api.save_permission_choice(request({'permission':'workspace-write','revision':0,'return_to':'https://evil.test'}))
            self.assertEqual(stale.status_code,409)
            conflict=json.loads(stale.body)
            self.assertEqual(conflict['code'],'conflict')
            self.assertEqual(conflict['revision'],1)
            completed=await api.save_permission_choice(request({'permission':'workspace-write','revision':conflict['revision'],'return_to':'/quick'}))
            self.assertEqual(completed.status_code,200)
            self.assertEqual(json.loads(completed.body)['redirect'],'/quick')
            self.assertTrue(self.store.permission_choice_completed('account'))
            self.assertEqual(self.store.resolve('account')['values']['permission'],'workspace-write')
            self.service.catalog.assert_not_called()
            # Completion unlocks the normal defaults resolver and a stale
            # request's explicit Full Access never became its selection.
            values=await conversation_defaults.new_defaults(self.service,'account')
            self.assertEqual(values['permission'],'workspace-write')
            self.store.save_project('account','fixture',0,{'permission':'read-only'})
            project_values=await conversation_defaults.new_defaults(self.service,'account','fixture',str(self.project))
            self.assertEqual(project_values['permission'],'read-only')

    async def test_api_draft_returns_actionable_409_before_persisting(self):
        service=mock.MagicMock()
        with mock.patch.object(conversation_api,'_auth',return_value=None), \
             mock.patch.object(conversation_api,'logged_in',return_value='account'), \
             mock.patch.object(conversation_api,'new_defaults',side_effect=PermissionChoiceRequired()), \
             mock.patch.object(conversation_api,'conversation_service',return_value=service):
            result=await conversation_api.create_conversation_draft(
                request(), project_id='', model='fixture', effort=None, permission=None,
                service_tier=None, collaboration_mode=None, completion_notification=None,
            )
        self.assertEqual(result.status_code,409)
        body=json.loads(result.body)
        self.assertEqual(body['code'],'permission_choice_required')
        self.assertIn('/settings/permission-choice',body['permission_choice_url'])
        service.create_draft.assert_not_called()

    def test_choice_return_targets_reject_browser_network_paths_and_keep_encoded_queries(self):
        from urllib.parse import urlencode
        for unsafe in ('//example.invalid', '///example.invalid', '////example.invalid',
                       ' ///example.invalid', '/\\example.invalid', '/%2fexample.invalid',
                       'https://example.invalid', '/home#fragment', '/a/../home'):
            with self.subTest(unsafe=unsafe):
                self.assertEqual(api._choice_return_to(unsafe), '/home')
        target = '/quick?' + urlencode({'project_id': 'fixture', 'workdir': '/tmp/中文 目录',
                                        'prompt': '百分比 50% & 反斜杠 \\n', 'quoted': '1'})
        self.assertEqual(api._choice_return_to(target), target)

    def test_choice_html_entry_preserves_expired_session_login_redirect(self):
        redirect = RedirectResponse('/login', 303)
        with mock.patch.object(api, 'require_auth', return_value=redirect), \
             mock.patch.object(api, 'logged_in') as account:
            self.assertIs(api.permission_choice_page(request()), redirect)
            account.assert_not_called()

    async def test_pending_quick_retains_all_context_for_the_same_entry(self):
        from urllib.parse import parse_qs, urlsplit
        parameters = {'project_id': 'fixture', 'workdir': '/tmp/中文 目录',
                      'prompt': '请保留 50% & 完整上下文', 'quoted': '1'}
        with mock.patch.object(conversations, 'require_auth', return_value=None), \
             mock.patch.object(conversations, 'logged_in', return_value='account'), \
             mock.patch('shared.permission_choice.PERMISSION_CHOICE_REQUIRED', True), \
             mock.patch.object(conversations, 'conversation_service') as service:
            response = await conversations.quick_conversation(request(), **parameters)
        target = parse_qs(urlsplit(response.headers['location']).query)['return_to'][0]
        self.assertEqual(api._choice_return_to(target), target)
        self.assertEqual(urlsplit(target).path, '/quick')
        self.assertEqual({k: v[0] for k, v in parse_qs(urlsplit(target).query).items()}, parameters)
        service.assert_not_called()

    async def test_choice_rejects_unauthorized_origin_and_missing_selection_without_state(self):
        with mock.patch('shared.permission_choice.PERMISSION_CHOICE_REQUIRED', True):
            for data, origin in (({'revision': 0}, 'https://fixture.test'),
                                 ({'revision': 0, 'permission': 'read-only'}, 'https://evil.test')):
                response = await api.save_permission_choice(request(data, origin=origin))
                self.assertEqual(response.status_code, 400)
                self.assertFalse(self.store.path.exists())
            with mock.patch.object(api, 'require_auth', return_value=Response(status_code=403)):
                response = await api.save_permission_choice(request({'revision': 0, 'permission': 'read-only'}))
                self.assertEqual(response.status_code, 403)
            self.assertFalse(self.store.path.exists())

    async def test_enrollment_blocks_new_drafts_not_existing_conversations(self):
        store = ConversationStore(self.root / 'isolated-native.sqlite3')
        artifacts = ConversationArtifactService(store, self.root / 'isolated-artifacts')
        runtime = ConversationRuntime(store, artifacts, FakeFactory())
        service = ConversationService(store, artifacts, runtime)
        try:
            with mock.patch('shared.permission_choice.PERMISSION_CHOICE_REQUIRED', False):
                old = service.create_draft(cwd=str(self.project), permission='danger-full-access', owner_account_id='account')
            with mock.patch('shared.permission_choice.PERMISSION_CHOICE_REQUIRED', True):
                for owner in ('account', None):
                    with self.assertRaises(PermissionChoiceRequired):
                        service.create_draft(cwd=str(self.project), permission='read-only', owner_account_id=owner)
                self.assertEqual(service.public_snapshot(old['id'])['conversation']['settings']['permission'], 'danger-full-access')
                self.store.complete_permission_choice('account', 0, 'read-only')
                self.assertEqual(service.public_snapshot(old['id'])['conversation']['settings']['permission'], 'danger-full-access')
                values = await conversation_defaults.new_defaults(self.service, 'account')
                created = service.create_draft(cwd=str(self.project), owner_account_id='account', **values)
                self.assertEqual(created['settings']['permission'], 'read-only')
        finally:
            await runtime.close()
            store.close()

    async def test_snapshot_personal_skills_queue_and_explicit_empty(self):
        store=ConversationStore(self.root/'native.sqlite3')
        artifacts=ConversationArtifactService(store,self.root/'artifacts')
        factory=FakeFactory();runtime=ConversationRuntime(store,artifacts,factory)
        service=ConversationService(store,artifacts,runtime)
        try:
            draft=service.create_draft(cwd=str(self.project),model='fixture-model',effort='medium',permission='read-only',client_origin='mobile_web',web_preferences={'personal':'简体中文，简洁','followup':'queue','skills':['fixture-skill']})
            self.assertNotIn('_personal_instructions',service.public_snapshot(draft['id'])['conversation']['settings'])
            await service.start(draft['id'],command_id='start-1',text='hello')
            start=next(value for name,value in factory.clients[0].calls if name=='turn/start')
            self.assertIn('简体中文，简洁',start['leading_context'])
            command=store.command('start-1')
            self.assertEqual(command['payload']['skills'],['fixture-skill'])
            result=await service.send(draft['id'],command_id='send-1',text='next',skill_selectors=[])
            self.assertEqual(result['command']['state'],'queued')
            self.assertEqual(store.command('send-1')['payload']['skills'],[])
            # Changed account defaults never retroactively rewrite a conversation.
            self.store.save_account('account',0,{'personal':'new personal','defaults':{'permission':'danger-full-access'}})
            self.assertEqual(store.get_conversation(draft['id'])['settings']['_personal_instructions'],'简体中文，简洁')
            self.assertEqual(store.get_conversation(draft['id'])['settings']['permission'],'read-only')
        finally:
            await runtime.close();store.close()

    def test_corrupted_and_broken_link_settings_fail_closed(self):
        self.store.save_account('account',0,{'defaults':{'permission':'read-only'}})
        with sqlite3.connect(self.store.path) as db:db.execute("UPDATE workbench_account_settings SET defaults_json = 'broken'")
        with self.assertRaises(ValueError):self.store.resolve('account')
        target=self.root/'nonexistent';link=self.root/'broken.sqlite3';link.symlink_to(target)
        with self.assertRaises(ValueError):SettingsStore(link).save_account('account',0,{'personal':'x'})
        self.assertFalse(target.exists())
