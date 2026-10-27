import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from routes import conversation_api, conversations, projects as project_routes


def _snapshot():
    return {
        "schema_version": 4,
        "conversation": {
            "id": "conversation-ui",
            "thread_id": "thread-ui",
            "title": "手机产物测试",
            "project_id": None,
            "state": "idle",
            "active_turn_id": None,
            "thread_status": {"type": "idle"},
            "settings": {
                "model": "fixture-model",
                "effort": "medium",
                "permission": "danger-full-access",
                "service_tier": "priority",
            },
            "recovery_state": None,
            "connection_state": "connected",
            "completion_notification": True,
            "pinned": False,
            "client_origin": "mobile_web",
            "created_at": "2026-08-24T10:00:00+00:00",
            "updated_at": "2026-08-24T10:01:00+00:00",
            "activity_at": "2026-08-24T10:01:00+00:00",
            "terminal_generation": 0,
            "last_terminal_turn_id": None,
            "last_terminal_at": None,
            "revision": 4,
            "usage": {
                "context_used": 420,
                "context_limit": 1000,
                "context_percent": 42.0,
                "input_tokens": 900,
                "cached_input_tokens": 600,
                "output_tokens": 120,
                "reasoning_output_tokens": 30,
                "updated_at": "2026-08-24T10:01:00+00:00",
            },
        },
        "turns": [],
        "messages": [],
        "queue": [],
        "artifacts": [],
    }


class ConversationPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_attachment_owner_failure_keeps_readable_history_and_text_composer(self):
        from engine.codex_app_server import AppServerProtocolError
        service = mock.Mock()
        service.public_browser_view.return_value = _snapshot()
        service.upload_context.return_value = {"cwd": "/home/workbench", "project_id": ""}
        service.catalog = mock.AsyncMock(return_value={"models": [], "skills": []})
        with mock.patch.object(conversations, "require_auth", return_value=None), \
             mock.patch.object(conversations, "logged_in", return_value="current-owner"), \
             mock.patch.object(conversations, "conversation_service", return_value=service), \
             mock.patch("shared.message_attachment_uploads.upload_context",
                        side_effect=AppServerProtocolError("无权向此对话添加附件。")):
            response = await conversations.conversation_page(SimpleNamespace(), "conversation-ui")
        self.assertEqual(response.status_code, 200)
        source = response.body.decode()
        self.assertIn("手机产物测试", source)
        self.assertIn('id="conversation-composer"', source)
        self.assertIn("历史和文字对话仍可使用", source)
        self.assertNotIn("/files/upload", source)

    async def test_mobile_picker_exposes_public_models_and_preserves_saved_selection(self):
        from tests.test_conversation_catalog import LEGACY_MODELS, model_row
        models = ("gpt-6-astra", "gpt-5.6-sol", *sorted(LEGACY_MODELS))
        for current in ("gpt-5.6-sol", *sorted(LEGACY_MODELS)):
            with self.subTest(current=current):
                snapshot = _snapshot()
                snapshot["conversation"]["settings"].update(model=current, effort="high")
                service = mock.MagicMock()
                service.public_browser_view.return_value = snapshot
                service.upload_context.return_value = {"cwd": "/tmp", "project_id": ""}
                service.catalog = mock.AsyncMock(return_value={"models": [
                    model_row(model) for model in models
                ]})
                with mock.patch.object(conversations, "require_auth", return_value=None), \
                     mock.patch.object(conversations, "logged_in", return_value="account-a"), \
                     mock.patch.object(conversations, "conversation_service", return_value=service):
                    response = await conversations.conversation_page(SimpleNamespace(), "conversation-ui")
                body = response.body.decode("utf-8")
                for model in models:
                    self.assertIn('value="' + model + '"', body)
                import re
                option = re.search(r'<option value="' + re.escape(current) + r'"[^>]*>', body)
                self.assertIsNotNone(option)
                self.assertIn("selected", option.group())
                self.assertEqual(snapshot["conversation"]["settings"]["model"], current)

    async def test_page_is_native_app_server_shell_without_backend_switch(self):
        service = mock.MagicMock()
        service.public_browser_view.return_value = _snapshot()
        service.upload_context.return_value = {
            "cwd": "/home/workbench/Inbox/conversations/conversation-ui",
            "project_id": "",
        }
        service.catalog = mock.AsyncMock(return_value={
            "models": [{
                "id": "fixture", "model": "fixture-model",
                "displayName": "Fixture", "defaultReasoningEffort": "medium",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "medium", "description": "Balanced"},
                ],
                "serviceTiers": [
                    {"id": "priority", "name": "Fast", "description": "Fast"},
                ],
            }],
            "skills": [{
                "name": "fixture-skill", "description": "Fixture",
                "enabled": True,
            }],
        })
        with mock.patch.object(conversations, "require_auth", return_value=None), \
             mock.patch.object(conversations, "logged_in", return_value="account-a"), \
             mock.patch.object(conversations, "conversation_service", return_value=service):
            response = await conversations.conversation_page(
                SimpleNamespace(), "conversation-ui"
            )
        body = response.body.decode("utf-8")
        self.assertIn('id="native-conversation"', body)
        self.assertIn("对话设置", body)
        self.assertNotIn("这里是真正的切换入口", body)
        self.assertIn('class="compact-control-grid native-settings-controls"', body)
        self.assertIn("fixture-skill", body)
        self.assertIn("/static/conversation.js", body)
        self.assertIn('id="diagnose-conversation"', body)
        self.assertIn('id="conversation-diagnostics"', body)
        self.assertIn('id="conversation-context-toggle"', body)
        self.assertIn('id="conversation-artifact-viewer"', body)
        self.assertIn('id="reconcile-conversation"', body)
        self.assertIn('id="conversation-notify"', body)
        self.assertIn('id="copy-conversation-export"', body)
        self.assertIn('id="download-conversation-export"', body)
        self.assertIn('id="conversation-export-dialog"', body)
        self.assertIn('id="context-compaction-count"', body)
        self.assertIn('id="context-compaction-breakdown"', body)
        self.assertIn('<details class="native-context-details" id="context-details">', body)
        context_card = body.split('<div class="native-context-card">', 1)[1].split(
            '<div class="native-context-summary-list">', 1
        )[0]
        context_details = body.split('id="context-details">', 1)[1].split(
            '</details>', 1
        )[0]
        self.assertIn('<dl class="native-context-tokens">', context_card)
        for token_id in ("context-input", "context-cached", "context-output"):
            self.assertIn(f'id="{token_id}"', context_card)
            self.assertNotIn(f'id="{token_id}"', context_details)
            self.assertEqual(body.count(f'id="{token_id}"'), 1)
        self.assertIn('id="context-switch-card"', body)
        self.assertIn('id="context-switch-details"', body)
        self.assertIn('id="context-switch-events"', body)
        self.assertIn("已记录压缩", body)
        self.assertIn("暂无记录", body)
        self.assertIn("本轮 Token 消耗与当前上下文占用不是同一指标", body)
        self.assertIn("仅统计已收到的完成事件；旧历史或离线缺失未补计", body)
        self.assertIn("其他压缩来源未明", body)
        self.assertIn("记录可能重叠，不相加", body)
        self.assertIn("内部换窗", body)
        self.assertNotIn("实验性观测", body)
        self.assertNotIn('id="pin-conversation"', body)
        self.assertNotIn("只导出你的全部提问", body)
        self.assertIn('class="native-diagnostics-project"', body)
        self.assertIn("复制内容包含项目名称", body)
        self.assertNotIn("不包含消息、附件、项目名", body)
        self.assertIn('>停止</button>', body)
        self.assertIn("复制内容包含项目名称", body)
        self.assertNotIn("执行后端", body)
        self.assertNotIn("Windows 高风险", body)
        composer = body.split('<form class="native-composer"', 1)[1].split(
            "</form>", 1
        )[0]
        self.assertIn('id="conversation-composer"', composer)
        self.assertIn(
            'data-draft-key="codex-conversation-draft:conversation-ui"', composer
        )
        self.assertNotIn('maxlength=', composer)
        self.assertIn('id="conversation-message-length"', composer)
        self.assertIn('name="collaboration_mode"', composer)
        self.assertNotIn("<form", composer)
        self.assertNotIn("type='file'", composer)
        self.assertIn("conversation-upload", composer)
        self.assertIn("<dialog id='conversation-upload' data-light-dismiss='empty-upload'>", body)
        self.assertNotIn("草稿保存在本机", body)
        self.assertIn("conversation-round-nav", body)
        self.assertIn("conversation-audio-bar", body)
        self.assertIn("conversation-enhancements.js", body)
        self.assertIn('data-conversation-module="markdown"', body)
        self.assertIn("conversation-round-navigation.js", body)
        self.assertIn("conversation-tts.js", body)
        self.assertIn('id="conversation-initial-snapshot"', body)
        self.assertIn('"id": "conversation-ui"', body)
        self.assertIn('data-segment-source="model"', body)
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(response.headers["pragma"], "no-cache")
        service.mark_read.assert_called_once_with("conversation-ui", "account-a")
        service.public_browser_view.assert_called_once_with("conversation-ui", include_text_previews=True)
        service.public_snapshot.assert_not_called()

    async def test_initial_snapshot_json_cannot_break_out_of_script(self):
        service = mock.MagicMock()
        snapshot = _snapshot()
        snapshot["conversation"]["title"] = "</script><script>alert(1)</script>"
        service.public_browser_view.return_value = snapshot
        service.upload_context.return_value = {"cwd": "/home/workbench", "project_id": ""}
        service.catalog = mock.AsyncMock(return_value={})
        with mock.patch.object(conversations, "require_auth", return_value=None), \
             mock.patch.object(conversations, "logged_in", return_value="account-a"), \
             mock.patch.object(conversations, "conversation_service", return_value=service):
            response = await conversations.conversation_page(
                SimpleNamespace(), "conversation-ui"
            )
        body = response.body.decode("utf-8")
        snapshot_script = body.split(
            'id="conversation-initial-snapshot"', 1
        )[1].split("</script>", 1)[0]
        self.assertNotIn("<script>alert(1)", snapshot_script)
        self.assertIn(r"\u003c/script\u003e", snapshot_script)

    async def test_page_survives_catalog_outage_with_saved_settings(self):
        service = mock.MagicMock()
        service.public_browser_view.return_value = _snapshot()
        service.upload_context.return_value = {"cwd": "/home/workbench", "project_id": ""}
        service.catalog = mock.AsyncMock(side_effect=OSError("socket unavailable"))
        with mock.patch.object(conversations, "require_auth", return_value=None), \
             mock.patch.object(conversations, "logged_in", return_value="account-a"), \
             mock.patch.object(conversations, "conversation_service", return_value=service):
            response = await conversations.conversation_page(
                SimpleNamespace(), "conversation-ui"
            )
        body = response.body.decode("utf-8")
        self.assertIn("fixture-model", body)
        self.assertIn('data-current="medium"', body)
        self.assertIn("模型目录暂时不可用", body)

    async def test_project_conversation_upload_uses_project_upload_directory(self):
        service = mock.MagicMock()
        snapshot = _snapshot()
        snapshot["conversation"]["project_id"] = "fixture-project"
        service.public_browser_view.return_value = snapshot
        service.upload_context.return_value = {
            "cwd": "/home/workbench/Projects/fixture-project",
            "project_id": "fixture-project",
        }
        service.catalog = mock.AsyncMock(return_value={})
        with mock.patch.object(conversations, "require_auth", return_value=None), \
             mock.patch.object(conversations, "logged_in", return_value="account-a"), \
             mock.patch.object(conversations, "conversation_service", return_value=service), \
             mock.patch("projects.get", return_value={"name": "Fixture"}), \
             mock.patch(
                 "projects.upload_directory",
                 return_value="/home/workbench/Projects/fixture-project/手机上传资料",
             ):
            response = await conversations.conversation_page(
                SimpleNamespace(), "conversation-ui"
            )
        body = response.body.decode("utf-8")
        self.assertIn(
            "name='directory' value='/home/workbench/Projects/fixture-project/手机上传资料'",
            body,
        )
        self.assertNotIn(
            "name='directory' value='/home/workbench/Projects/fixture-project'", body
        )

class ConversationApiInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_draft_defaults_completion_notification_on_for_owner(self):
        service = mock.MagicMock()
        service.create_draft.return_value = {"id": "conversation-default"}
        service.public_snapshot.return_value = _snapshot()
        request = SimpleNamespace(
            cookies={}, client=SimpleNamespace(host="127.0.0.1")
        )
        with (
            mock.patch.object(conversation_api, "_auth", return_value=None),
            mock.patch.object(
                conversation_api, "conversation_service", return_value=service
            ),
            mock.patch.object(
                conversation_api,
                "classify_client_origin",
                return_value=("mobile_web", {}, {}),
            ),
            mock.patch.object(
                conversation_api, "logged_in", return_value="admin-a"
            ),
            mock.patch.object(conversation_api, "audit"),
        ):
            response = await conversation_api.create_conversation_draft(
                request,
                project_id="",
                model="fixture-model",
                effort="medium",
                permission="danger-full-access",
                service_tier="",
                completion_notification=None,
            )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(
            service.create_draft.call_args.kwargs["completion_notification"]
        )
        self.assertEqual(
            service.create_draft.call_args.kwargs["owner_account_id"],
            "admin-a",
        )

    def test_browser_api_stays_under_public_conversation_namespace(self):
        paths = {route.path for route in conversation_api.router.routes}
        self.assertIn("/conversation/draft", paths)
        self.assertIn("/conversation/{conversation_id}/snapshot", paths)
        self.assertIn("/conversation/{conversation_id}/export", paths)
        self.assertIn("/conversation/{conversation_id}/read", paths)
        self.assertIn("/conversation/{conversation_id}/messages", paths)
        self.assertIn("/conversation/{conversation_id}/metadata", paths)
        self.assertIn(
            "/conversation/{conversation_id}/artifact/{artifact_id}", paths
        )
        self.assertIn(
            "/conversation/{conversation_id}/artifact/{artifact_id}/preview", paths
        )
        self.assertFalse(any(path.startswith("/api/") for path in paths))

    async def test_read_receipt_uses_authenticated_account(self):
        service = mock.MagicMock()
        request = SimpleNamespace()
        with mock.patch.object(conversation_api, "_auth", return_value=None), \
             mock.patch.object(conversation_api, "logged_in", return_value="account-a"), \
             mock.patch.object(conversation_api, "conversation_service", return_value=service):
            response = await conversation_api.mark_conversation_read(
                request, "conversation-ui"
            )
        self.assertEqual(response.status_code, 200)
        service.mark_read.assert_called_once_with("conversation-ui", "account-a")

    def test_export_response_is_private_download_with_safe_headers(self):
        service = mock.MagicMock()
        service.export_transcript.return_value = {
            "title": 'evil"\r\nX-Injected: yes',
            "markdown": "# 导出\n",
        }
        with mock.patch.object(conversation_api, "_auth", return_value=None), \
             mock.patch.object(conversation_api, "conversation_service", return_value=service):
            response = conversation_api.conversation_export(
                SimpleNamespace(), "conversation-ui"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, "# 导出\n".encode("utf-8"))
        self.assertTrue(response.headers["content-type"].startswith("text/markdown"))
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertNotIn("\r", response.headers["content-disposition"])
        self.assertNotIn("\n", response.headers["content-disposition"])
        service.export_transcript.assert_called_once_with("conversation-ui")

    def test_export_auth_denial_never_reaches_service(self):
        denied = PlainTextResponse("locked", 403)
        with mock.patch.object(conversation_api, "_auth", return_value=denied), \
             mock.patch.object(conversation_api, "conversation_service") as service:
            response = conversation_api.conversation_export(SimpleNamespace(), "c1")
        self.assertIs(response, denied)
        service.assert_not_called()

    def test_export_missing_conversation_is_not_found(self):
        service = mock.MagicMock()
        service.export_transcript.side_effect = ValueError("conversation does not exist")
        with mock.patch.object(conversation_api, "_auth", return_value=None), \
             mock.patch.object(conversation_api, "conversation_service", return_value=service):
            response = conversation_api.conversation_export(SimpleNamespace(), "missing")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.headers["cache-control"], "private, no-store")

    def test_html_viewer_uses_server_shell_without_reading_artifact_bytes(self):
        service = mock.MagicMock()
        service.artifact_record.return_value = {"name": "草图.html"}
        service.artifacts.html_previewable.return_value = True
        shell = mock.MagicMock()
        shell.render.return_value = "<!doctype html><title>受限预览</title>"
        with mock.patch.object(conversation_api, "_auth", return_value=None), \
             mock.patch.object(conversation_api, "conversation_service", return_value=service), \
             mock.patch.object(conversation_api.templates, "get_template", return_value=shell):
            response = conversation_api.conversation_html_artifact_preview(
                SimpleNamespace(), "conversation-ui", "artifact-ui", viewer=True
            )
        self.assertIsInstance(response, HTMLResponse)
        self.assertIn("受限预览".encode("utf-8"), response.body)
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertIn("frame-src 'self'", response.headers["content-security-policy"])
        shell.render.assert_called_once_with(artifact_name="草图.html")
        service.artifacts.path_for.assert_not_called()

    async def test_start_forwards_native_skill_selection(self):
        service = mock.MagicMock()
        service.start = mock.AsyncMock()
        service.public_snapshot.return_value = _snapshot()
        request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        with mock.patch.object(conversation_api, "_auth", return_value=None), \
             mock.patch.object(conversation_api, "conversation_service", return_value=service), \
             mock.patch.object(conversation_api, "audit"):
            response = await conversation_api.start_conversation(
                request,
                "conversation-ui",
                command_id="message-one",
                text="use the skill",
                skills=["fixture-skill"],
            )
        self.assertEqual(response.status_code, 200)
        service.start.assert_awaited_once_with(
            "conversation-ui",
            command_id="message-one",
            text="use the skill",
            skill_selectors=["fixture-skill"],
        )

    async def test_start_rejected_command_returns_conflict_with_snapshot(self):
        service = mock.MagicMock()
        service.start = mock.AsyncMock(return_value={
            "command": {
                "id": "message-one", "kind": "start", "state": "rejected",
                "reason": "thread_exists",
            },
            "snapshot": {},
        })
        service.public_snapshot.return_value = _snapshot()
        request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        with mock.patch.object(conversation_api, "_auth", return_value=None), \
             mock.patch.object(conversation_api, "conversation_service", return_value=service), \
             mock.patch.object(conversation_api, "audit"):
            response = await conversation_api.start_conversation(
                request, "conversation-ui", command_id="message-one", text="try"
            )

        self.assertEqual(response.status_code, 409)
        body = json.loads(response.body)
        self.assertEqual(body["error"]["code"], "command_rejected")
        self.assertEqual(body["command"]["state"], "rejected")
        self.assertEqual(body["snapshot"], _snapshot())

    async def test_empty_service_tier_form_switches_fast_back_to_standard(self):
        service = mock.MagicMock()
        service.update_settings = mock.AsyncMock(return_value={
            "command": {"id": "settings-one", "state": "completed"},
            "snapshot": _snapshot(),
        })
        service.public_snapshot.return_value = _snapshot()
        request = SimpleNamespace(form=mock.AsyncMock(return_value={
            "command_id": "settings-one",
            "model": "fixture-model",
            "effort": "medium",
            "permission": "danger-full-access",
            "service_tier": "",
        }))
        with mock.patch.object(conversation_api, "_auth", return_value=None), \
             mock.patch.object(conversation_api, "conversation_service", return_value=service), \
             mock.patch.object(conversation_api, "audit"):
            response = await conversation_api.update_conversation_settings(
                request,
                "conversation-ui",
                command_id="settings-one",
                model="fixture-model",
                effort="medium",
                permission="danger-full-access",
                service_tier=None,
                completion_notification=None,
            )

        self.assertEqual(response.status_code, 200)
        service.update_settings.assert_awaited_once_with(
            "conversation-ui",
            command_id="settings-one",
            updates={
                "model": "fixture-model",
                "effort": "medium",
                "permission": "danger-full-access",
                "service_tier": None,
            },
            completion_notification=None,
        )

    async def test_omitted_service_tier_form_leaves_fast_unchanged(self):
        service = mock.MagicMock()
        service.update_settings = mock.AsyncMock(return_value={
            "command": None,
            "snapshot": _snapshot(),
        })
        service.public_snapshot.return_value = _snapshot()
        request = SimpleNamespace(form=mock.AsyncMock(return_value={
            "command_id": "settings-notification",
            "completion_notification": "1",
        }))
        with mock.patch.object(conversation_api, "_auth", return_value=None), \
             mock.patch.object(conversation_api, "conversation_service", return_value=service), \
             mock.patch.object(conversation_api, "audit"):
            response = await conversation_api.update_conversation_settings(
                request,
                "conversation-ui",
                command_id="settings-notification",
                model=None,
                effort=None,
                permission=None,
                service_tier=None,
                completion_notification="1",
            )

        self.assertEqual(response.status_code, 200)
        service.update_settings.assert_awaited_once_with(
            "conversation-ui",
            command_id="settings-notification",
            updates={},
            completion_notification=True,
        )

    def test_json_auth_converts_redirect_and_forbidden_responses(self):
        request = SimpleNamespace()
        for original, expected_status, expected_code in (
            (RedirectResponse("/login", 303), 401, "authentication_required"),
            (RedirectResponse("/setup", 303), 401, "setup_required"),
            (PlainTextResponse("locked", 403), 403, "authorization_denied"),
        ):
            with mock.patch.object(
                conversation_api, "require_auth", return_value=original
            ):
                response = conversation_api._auth(request)
            self.assertEqual(response.status_code, expected_status)
            self.assertEqual(json.loads(response.body)["error"]["code"], expected_code)
            self.assertEqual(response.headers["cache-control"], "private, no-store")

    async def test_tree_stop_route_accepts_no_unique_current_turn_and_returns_verified_snapshot(self):
        service = mock.Mock()
        service.interrupt = mock.AsyncMock(return_value={'command':{'state':'completed'}})
        value = _snapshot()
        value['stop'] = {'status':'verified', 'threads':3}
        service.public_snapshot.return_value = value
        with mock.patch.object(conversation_api,'_auth',return_value=None), \
             mock.patch.object(conversation_api,'conversation_service',return_value=service), \
             mock.patch.object(conversation_api,'audit'):
            response = await conversation_api.interrupt_conversation(SimpleNamespace(), 'c1',
                command_id='stop', expected_turn_id='', scope='conversation_tree')
        self.assertEqual(response.status_code,200)
        service.interrupt.assert_awaited_once_with('c1', command_id='stop',
            expected_turn_id='', scope='conversation_tree')
        self.assertEqual(json.loads(response.body)['stop']['status'],'verified')

    async def test_every_json_endpoint_stops_at_json_auth_boundary(self):
        denied = JSONResponse({"error": {"code": "authentication_required"}}, 401)
        request = SimpleNamespace()
        calls = [
            conversation_api.create_conversation_draft(request),
            conversation_api.conversation_snapshot(request, "c1"),
            conversation_api.start_conversation(
                request, "c1", command_id="m1", text="start"
            ),
            conversation_api.send_conversation_message(
                request, "c1", command_id="m2", text="next"
            ),
            conversation_api.interrupt_conversation(
                request, "c1", command_id="i1", expected_turn_id="t1"
            ),
            conversation_api.reconcile_conversation(request, "c1"),
            conversation_api.update_conversation_settings(
                request, "c1", command_id="s1"
            ),
            conversation_api.update_conversation_metadata(
                request, "c1", title="新标题"
            ),
            conversation_api.compact_conversation(
                request, "c1", command_id="x1"
            ),
            conversation_api.delete_conversation(request, "c1"),
        ]
        with mock.patch.object(conversation_api, "_auth", return_value=denied), \
             mock.patch.object(conversation_api, "conversation_service") as service:
            for call in calls:
                self.assertIs(await call, denied)
        service.assert_not_called()

    async def test_metadata_update_forwards_title_and_pin(self):
        service = mock.MagicMock()
        service.update_metadata = mock.AsyncMock(return_value=_snapshot())
        request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        with mock.patch.object(conversation_api, "_auth", return_value=None), \
             mock.patch.object(conversation_api, "conversation_service", return_value=service), \
             mock.patch.object(conversation_api, "audit"):
            response = await conversation_api.update_conversation_metadata(
                request, "conversation-ui", title="新标题", pinned="1"
            )
        self.assertEqual(response.status_code, 200)
        service.update_metadata.assert_awaited_once_with(
            "conversation-ui", title="新标题", pinned=True
        )


class ProjectConversationComposerTests(unittest.IsolatedAsyncioTestCase):
    def project_module(self):
        return SimpleNamespace(
            get=mock.Mock(return_value={
                "id": "project-one", "name": "测试项目", "path": "/home/workbench/fixture"
            }),
            upload_directory=mock.Mock(return_value="/home/workbench/fixture"),
        )

    async def render_project(self, *, catalog_error=False):
        service = mock.MagicMock()
        service.list_public_page.return_value = {"values": [], "next_cursor": None}
        service.catalog = mock.AsyncMock(
            side_effect=OSError("catalog unavailable") if catalog_error else None,
            return_value={"models": [{
                "model": "gpt-5.6-sol",
                "displayName": "GPT-5.6-Sol",
                "defaultReasoningEffort": "medium",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "medium"}, {"reasoningEffort": "ultra"},
                ],
                "serviceTiers": [{"id": "priority", "name": "Fast"}],
            }]},
        )
        with mock.patch.object(project_routes, "require_auth", return_value=None), \
             mock.patch.object(project_routes, "logged_in", return_value="account-a"), \
             mock.patch.object(project_routes, "conversation_service", return_value=service), \
             mock.patch.dict(sys.modules, {"projects": self.project_module()}):
            return await project_routes.project_view(SimpleNamespace(), "project-one")

    async def test_project_start_exposes_four_catalog_driven_settings(self):
        response = await self.render_project()
        body = response.body.decode("utf-8")
        self.assertIn('id="project-conversation-model"', body)
        self.assertIn('name="effort"', body)
        self.assertIn('name="service_tier"', body)
        self.assertIn('name="permission"', body)
        self.assertIn("GPT-5.6-Sol", body)
        self.assertIn('<option value="high" selected>high（不可用）</option>', body)
        self.assertIn('<option value="ultra" >ultra</option>', body)
        self.assertIn('<option value="" selected>标准</option>', body)
        self.assertIn('<option value="priority"', body)
        project_form = body.split('<form id="project-send"', 1)[1].split(
            "</form>", 1
        )[0]
        self.assertNotIn("<form", project_form)
        self.assertNotIn("type='file'", project_form)
        self.assertIn(
            'data-draft-key="codex-project-conversation-draft:project-one"',
            project_form,
        )
        self.assertNotIn('maxlength=', project_form)
        self.assertIn('id="project-message-length"', project_form)
        self.assertIn('name="collaboration_mode"', project_form)
        self.assertIn("<dialog id='project-upload' data-light-dismiss='empty-upload'>", body)

    async def test_project_start_survives_catalog_outage_with_warning(self):
        response = await self.render_project(catalog_error=True)
        body = response.body.decode("utf-8")
        self.assertIn("gpt-5.6-sol", body)
        self.assertIn("模型目录暂时不可用", body)


class DirectStartStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_quick_conversation_defaults_notification_on_for_owner(self):
        service = mock.MagicMock()
        service.create_draft.return_value = {"id": "conversation-quick"}
        service.catalog = mock.AsyncMock(return_value={"models": []})
        request = SimpleNamespace(cookies={})
        with (
            mock.patch.object(conversations, "require_auth", return_value=None),
            mock.patch.object(
                conversations, "conversation_service", return_value=service
            ),
            mock.patch.object(
                conversations,
                "classify_client_origin",
                return_value=("mobile_web", {}, {}),
            ),
            mock.patch.object(conversations, "logged_in", return_value="admin-a"),
        ):
            response = await conversations.quick_conversation(request)
        self.assertEqual(response.status_code, 303)
        self.assertTrue(
            service.create_draft.call_args.kwargs["completion_notification"]
        )
        self.assertEqual(
            service.create_draft.call_args.kwargs["owner_account_id"],
            "admin-a",
        )

    async def test_rejected_and_unknown_start_are_not_audited_as_started(self):
        for state in ("rejected", "delivery_unknown"):
            service = mock.MagicMock()
            service.create_draft.return_value = {"id": "conversation-direct"}
            service.start = mock.AsyncMock(return_value={
                "command": {"state": state}, "snapshot": {},
            })
            project_module = SimpleNamespace(
                get=mock.Mock(return_value={"path": "/home/workbench/fixture"}),
                touch=mock.Mock(),
            )
            request = SimpleNamespace(cookies={})
            with self.subTest(state=state), \
                 mock.patch.object(conversations, "require_auth", return_value=None), \
                 mock.patch.object(conversations, "conversation_service", return_value=service), \
                 mock.patch.object(
                     conversations, "classify_client_origin",
                     return_value=("mobile_web", {}, {}),
                 ), \
                 mock.patch.object(
                     conversations, "logged_in", return_value="admin-a"
                 ), \
                 mock.patch.object(conversations, "audit") as audit, \
                 mock.patch.dict(sys.modules, {"projects": project_module}):
                response = await conversations.create_and_start_conversation(
                    request,
                    prompt="first",
                    project_id="project-one",
                    model="gpt-5.6-sol",
                    effort="ultra",
                    permission="danger-full-access",
                    service_tier="priority",
                    completion_notification=None,
                )
            self.assertEqual(response.status_code, 303)
            self.assertEqual(
                response.headers["location"], "/conversation/conversation-direct"
            )
            project_module.touch.assert_not_called()
            self.assertTrue(
                service.create_draft.call_args.kwargs[
                    "completion_notification"
                ]
            )
            self.assertEqual(
                service.create_draft.call_args.kwargs["owner_account_id"],
                "admin-a",
            )
            self.assertEqual(
                audit.call_args.args[1], "conversation_start_" + state
            )

    async def test_invalid_start_result_is_not_audited_as_started(self):
        service = mock.MagicMock()
        service.create_draft.return_value = {"id": "conversation-direct"}
        service.start = mock.AsyncMock(return_value={"snapshot": {}})
        request = SimpleNamespace()
        with mock.patch.object(conversations, "require_auth", return_value=None), \
             mock.patch.object(conversations, "conversation_service", return_value=service), \
             mock.patch.object(
                 conversations, "classify_client_origin",
                 return_value=("mobile_web", {}, {}),
             ), \
             mock.patch.object(conversations, "audit") as audit:
            response = await conversations.create_and_start_conversation(
                request,
                prompt="first",
                project_id="",
                model="gpt-5.6-sol",
                effort="medium",
                permission="danger-full-access",
                service_tier="",
                completion_notification="",
                skills=[],
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("无法开始对话", response.body.decode("utf-8"))
        self.assertFalse(
            service.create_draft.call_args.kwargs["completion_notification"]
        )
        audit.assert_not_called()


class RetiredBackendTests(unittest.TestCase):
    def test_retired_execution_backends_stay_removed(self):
        retired = [
            "tasks.py", "windows_bridge.py", "engine/codex_exec.py",
            "engine/kimi_acp.py", "engine/codex_app_server_runtime.py",
            "routes/task.py", "routes/takeover.py",
        ]
        self.assertEqual([value for value in retired if Path(value).exists()], [])


if __name__ == "__main__":
    unittest.main()
