import html
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch, AsyncMock

from itsdangerous import URLSafeTimedSerializer
from starlette.requests import Request

from engine.codex_app_server import AppServerDisconnected, AppServerProtocolError, CodexAppServerClient
from engine.conversation_service import ConversationService
from routes import conversation_api, conversations, files
from shared import file_utils, message_attachment_uploads
from shared.message_attachments import MessageAttachmentService, attachment_context, public_attachment, quarantine_before_rollback
from shared.conversation_artifacts import ConversationArtifactService
from tests import test_conversation_runtime as runtime_fixtures
from tests.store_fixtures import seeded_store


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path.home(), prefix="local-message-attachment-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.store = seeded_store(self.root / "state" / "conversations.sqlite3")
        self.addCleanup(self.store.close)
        self.attachments = MessageAttachmentService(self.store)
        self.scope = self.attachments.scope(owner="owner", kind="project", scope_id="project-one",
                                            directory=str(self.workspace))

    def register(self, name="说明.txt", data=b"UNIQUE_PRIVATE_FILE_CONTENT"):
        source = self.workspace / name
        source.write_bytes(data)
        records = self.attachments.prepare(self.scope, [(source, name)])
        self.attachments.commit(records)
        return source, records[0]

    def conversation(self, conversation_id):
        return self.store.create_conversation(conversation_id, title="fixture", cwd=str(self.workspace),
            project_id="project-one", owner_account_id="owner", settings={"model": "fixture",
                "effort": "medium", "permission": "danger-full-access", "service_tier": None})

    def test_original_stays_local_and_same_name_replacement_does_not_change_version(self):
        source, record = self.register()
        source.write_bytes(b"new same-name content")
        self.assertEqual(self.attachments.path_for(record).read_bytes(), b"UNIQUE_PRIVATE_FILE_CONTENT")
        self.assertEqual(source.read_bytes(), b"new same-name content")
        self.assertEqual(self.attachments.path_for(record).stat().st_mode & 0o777, 0o400)
        self.assertEqual(self.attachments.root.stat().st_mode & 0o777, 0o700)

    def test_reference_is_plain_text_paths_not_embedded_file_or_native_image(self):
        _, record = self.register("图片.png", b"\x89PNG\r\n\x1a\nPRIVATE_IMAGE_DATA")
        context = attachment_context({"payload": {"attachments": [record]}})
        client = CodexAppServerClient(self.root / "unused.sock")
        params = client._turn_params("thread-1", "请看这张图", "message-1", leading_context=context)
        self.assertEqual({item["type"] for item in params["input"]}, {"text"})
        self.assertIn(record["path"], context)
        self.assertNotIn(str(self.workspace / "图片.png"), context)
        self.assertNotIn("PRIVATE_IMAGE_DATA", context)
        self.assertNotIn("localImage", json.dumps(params))

    def test_legacy_rollback_quarantines_only_pending_attachment_commands(self):
        source, record = self.register()
        self.conversation("rollback-fixture")
        self.attachments.admit_command("rollback-fixture", "file-queued", kind="steer", text="file",
            payload={"attachments": [record]})
        self.store.update_command("file-queued", "queued")
        self.store.begin_command("rollback-fixture", "plain-queued", kind="steer", text="plain")
        self.store.update_command("plain-queued", "queued")
        self.attachments.admit_command("rollback-fixture", "file-sending", kind="steer", text="uncertain",
            payload={"attachments": [record]})
        self.store.update_command("file-sending", "submitted", reason="native_rpc_sending")
        legacy = self.root / "legacy-release"; (legacy / "app").mkdir(parents=True)
        self.assertEqual(quarantine_before_rollback(legacy, database_path=self.store.path), 2)
        self.assertEqual(self.store.command("file-queued")["state"], "rejected")
        self.assertEqual(self.store.command("plain-queued")["state"], "queued")
        self.assertEqual(self.store.command("file-sending")["state"], "delivery_unknown")
        self.assertEqual(self.store.get_conversation("rollback-fixture")["state"], "needs_user")
        # Even a coroutine that captured a queue entry before quarantine can't
        # put it back into a queue understood differently by the old runtime.
        self.store.update_command("file-queued", "queued", reason="native_goal_next_turn")
        self.assertEqual(self.store.command("file-queued")["state"], "rejected")
        with self.assertRaises(AppServerProtocolError):
            self.attachments.admit_command("rollback-fixture", "new-file", kind="steer", text="new",
                payload={"attachments": [record]})
        self.attachments.resume_admission()
        self.assertEqual(self.store.command("file-sending")["state"], "delivery_unknown")
        self.assertEqual(self.store.command("file-queued")["state"], "rejected")
        self.assertTrue(Path(record["path"]).exists()); self.assertTrue(source.exists())

    def test_capable_rollback_target_preserves_queue(self):
        _, record = self.register(); self.conversation("rollback-fixture")
        self.attachments.admit_command("rollback-fixture", "file-queued", kind="steer", text="file",
            payload={"attachments": [record]})
        self.store.update_command("file-queued", "queued")
        capable = self.root / "capable-release" / "app" / "shared"; capable.mkdir(parents=True)
        (capable / "message_attachments.py").write_text("ATTACHMENT_DELIVERY_VERSION = 1\n")
        self.assertEqual(quarantine_before_rollback(capable.parent.parent, database_path=self.store.path), 0)
        self.assertEqual(self.store.command("file-queued")["state"], "queued")
        self.assertIsNone(self.store._db.execute("SELECT 1 FROM schema_meta WHERE key='message_attachments_rollback_barrier'").fetchone())

    def test_public_projection_has_no_machine_path_or_private_identity(self):
        _, record = self.register()
        value = public_attachment(record, conversation_id="conversation-1")
        self.assertNotIn(str(self.root), json.dumps(value))
        self.assertNotIn("original_path", value)
        self.assertNotIn("sha256", value)
        self.assertIn("conversation_id=conversation-1", value["content_url"])

    def test_owner_and_project_scope_are_both_checked(self):
        _, record = self.register()
        with self.assertRaises(AppServerProtocolError):
            self.attachments.resolve([record["id"]], owner="other", project_id="project-one")
        with self.assertRaises(AppServerProtocolError):
            self.attachments.resolve([record["id"]], owner="owner", project_id="project-two")
        self.assertEqual(self.attachments.resolve([record["id"]], owner="owner", project_id="project-one")[0], record)

    def test_copy_rejects_symlinks_and_does_not_delete_source(self):
        source = self.workspace / "source.txt"
        source.write_bytes(b"original")
        link = self.workspace / "link.txt"
        link.symlink_to(source)
        with self.assertRaises(OSError):
            self.attachments.prepare(self.scope, [(link, "link.txt")])
        self.assertEqual(source.read_bytes(), b"original")
        self.assertEqual(list(self.attachments.root.iterdir()), [])

    def test_changed_private_version_is_rejected_instead_of_silently_rebound(self):
        _, record = self.register()
        path = Path(record["path"])
        path.chmod(0o600)
        path.write_bytes(b"changed")
        with self.assertRaises(AppServerProtocolError):
            self.attachments.path_for(record)
        with self.assertRaises(AppServerProtocolError):
            attachment_context({"payload": {"attachments": [record]}})

    def test_svg_and_html_stay_downloads_not_active_preview(self):
        for name in ("active.html", "active.svg"):
            _, record = self.register(name, b"<script>alert(1)</script>")
            self.assertEqual(public_attachment(record)["preview_kind"], "download")

    def test_core_schema_version_and_old_empty_commands_are_unchanged(self):
        self.assertEqual(self.store._db.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()["value"], "6")
        self.assertIsNone(attachment_context({"payload": {}}))
        self.assertNotIn("attachments", ConversationService._public_command({"payload": {}}))

    def test_registry_reopens_without_losing_scope_or_content(self):
        _, record = self.register()
        reopened = MessageAttachmentService(self.store)
        value = reopened.resolve([record["id"]], owner="owner", project_id="project-one")[0]
        self.assertEqual(reopened.path_for(value).read_bytes(), b"UNIQUE_PRIVATE_FILE_CONTENT")

    def test_project_reference_is_cleaned_when_its_last_conversation_is_deleted(self):
        source, record = self.register()
        self.conversation("first"); self.conversation("second")
        self.attachments.bind("first", [record]); self.attachments.bind("second", [record])
        self.attachments.delete_conversation("first")
        self.assertTrue(Path(record["path"]).exists())
        self.attachments.delete_conversation("second")
        self.assertFalse(Path(record["path"]).exists())
        self.assertTrue(source.exists())

    def test_old_release_core_delete_is_cleaned_on_next_registry_open(self):
        source, record = self.register()
        self.conversation("first")
        self.attachments.bind("first", [record])
        self.store.delete_conversation("first")
        self.assertTrue(Path(record["path"]).exists())
        MessageAttachmentService(self.store)
        self.assertFalse(Path(record["path"]).exists())
        self.assertTrue(source.exists())

    def test_project_delete_removes_pending_copies_but_preserves_message_history_versions(self):
        pending_source, pending = self.register("pending.txt")
        history_source, history = self.register("history.txt")
        self.conversation("first")
        self.attachments.bind("first", [history])
        self.attachments.delete_project("project-one")
        self.assertFalse(Path(pending["path"]).exists())
        self.assertTrue(Path(history["path"]).exists())
        self.assertTrue(pending_source.exists() and history_source.exists())

    def test_binding_does_not_change_canonical_command_payload_on_replay(self):
        _, reference = self.register()
        self.conversation("first")
        before = self.attachments.record(reference["id"], owner="owner")
        self.attachments.bind("first", [reference])
        self.assertEqual(before, self.attachments.record(reference["id"], owner="owner"))

    def test_service_real_project_delete_cleans_pending_copy_not_other_sources(self):
        source, record = self.register()
        artifacts = ConversationArtifactService(self.store, self.root / "artifacts")
        service = ConversationService(self.store, artifacts, type("Runtime", (), {})())
        service.delete_projects(["project-one"], lambda _: __import__("shutil").rmtree(self.workspace))
        self.assertFalse(source.exists())
        self.assertFalse(Path(record["path"]).exists())

    def test_missing_marked_table_is_integrity_error_not_empty_history(self):
        self.store._db.execute("DROP TABLE message_attachments")
        with self.assertRaises(AppServerProtocolError):
            MessageAttachmentService(self.store)

    def test_content_and_list_require_authentication_and_correct_scope(self):
        _, record = self.register()
        service = type("Service", (), {"attachments": self.attachments, "store": self.store})()
        request = Request({"type": "http", "method": "GET", "path": "/message-attachments", "headers": []})
        with patch.object(conversation_api, "require_auth", return_value=None), \
                patch.object(conversation_api, "logged_in", return_value="owner"), \
                patch.object(conversation_api, "conversation_service", return_value=service):
            response = conversation_api.message_attachment_content(request, record["id"], project_id="project-one")
            self.assertEqual(response.body, b"UNIQUE_PRIVATE_FILE_CONTENT")
            denied = conversation_api.message_attachment_content(request, record["id"], project_id="foreign")
            self.assertEqual(denied.status_code, 404)
            listing = conversation_api.message_attachment_list(request, [record["id"]], project_id="project-one")
            self.assertEqual(json.loads(listing.body)["attachments"][0]["id"], record["id"])
            with patch.object(conversation_api, "logged_in", return_value="other"):
                other = conversation_api.message_attachment_list(request, [record["id"]], project_id="project-one")
                self.assertEqual(json.loads(other.body)["attachments"], [])

    def test_gateway_conversation_namespace_routes_and_public_links(self):
        import asyncio
        from fastapi import FastAPI
        from starlette.responses import JSONResponse
        _, record = self.register()
        service = type("Service", (), {"attachments": self.attachments, "store": self.store})()
        app = FastAPI()
        # The same inclusion order as production: /conversation/{id} is first.
        app.include_router(conversations.router)
        app.include_router(conversation_api.router)
        metadata = "/conversation/attachments/list"
        content = "/conversation/attachments/content/" + record["id"]
        async def get(url):
            parsed, messages = urlsplit(url), []
            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}
            async def send(message):
                messages.append(message)
            await app({
                "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                "method": "GET", "scheme": "http", "path": parsed.path,
                "raw_path": parsed.path.encode(), "root_path": "", "query_string": parsed.query.encode(),
                "headers": [], "client": ("127.0.0.1", 1), "server": ("fixture", 80),
            }, receive, send)
            status = next(item["status"] for item in messages if item["type"] == "http.response.start")
            body = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
            return status, body
        with patch.object(conversation_api, "require_auth", return_value=None), \
                patch.object(conversation_api, "logged_in", return_value="owner"), \
                patch.object(conversation_api, "conversation_service", return_value=service):
            status, body = asyncio.run(get(metadata + "?ids=" + record["id"] + "&project_id=project-one"))
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["missing_ids"], [])
            public = json.loads(body)["attachments"][0]
            self.assertEqual(urlsplit(public["content_url"]).path, content)
            self.assertEqual(asyncio.run(get(public["content_url"])), (200, b"UNIQUE_PRIVATE_FILE_CONTENT"))
            self.assertEqual(asyncio.run(get(content + "?project_id=foreign"))[0], 404)
            with patch.object(conversation_api, "require_auth", return_value=JSONResponse({}, status_code=401)):
                self.assertEqual(asyncio.run(get(metadata))[0], 401)
                self.assertEqual(asyncio.run(get(content))[0], 401)


class UploadBindingTests(unittest.TestCase):
    def setUp(self):
        RegistryTests.setUp(self)
        self.request = Request({"type": "http", "method": "POST", "path": "/files/upload", "headers": []})
        self.signer = URLSafeTimedSerializer("unit-test-only", salt="unit-message-upload")
        self.service = type("Service", (), {"attachments": self.attachments, "store": self.store})()
        self.token = self.signer.dumps({"purpose": "message_upload", **self.scope})
        for context in (
            patch.object(files, "require_auth", return_value=None),
            patch.object(files, "audit"),
            patch.object(files, "confirmation_signer", return_value=self.signer),
            patch.object(message_attachment_uploads, "confirmation_signer", return_value=self.signer),
            patch.object(message_attachment_uploads, "logged_in", return_value="owner"),
            patch("engine.conversation_service.conversation_service", return_value=self.service),
            patch("projects.upload_directory", return_value=str(self.workspace)),
            patch.object(file_utils, "FILE_ROOTS", (self.root,)),
            patch.object(file_utils, "SENSITIVE_PATHS", ()),
            patch.object(file_utils, "UPLOADS", self.root / "staging"),
            patch.object(files, "UPLOADS", self.root / "staging"),
        ):
            context.start()
            self.addCleanup(context.stop)

    def upload(self, names):
        values = [type("Upload", (), {"filename": name, "file": io.BytesIO(data)})()
                  for name, data in names]
        return files.upload(self.request, str(self.workspace), values,
                            "/project/project-one", self.token)

    def test_direct_upload_returns_bound_batch_without_starting_ai(self):
        result = self.upload([("first.txt", b"first"), ("second.txt", b"second")])
        self.assertEqual(result.status_code, 303)
        batch = parse_qs(urlsplit(result.headers["location"]).query)["attachment_batch"][0]
        values = self.attachments.batch(batch, scope=self.scope)
        self.assertEqual([value["name"] for value in values], ["first.txt", "second.txt"])
        self.assertEqual(self.store._db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 0)
        self.assertEqual((self.workspace / "first.txt").read_bytes(), b"first")
        self.assertEqual(Path(values[0]["path"]).read_bytes(), b"first")

    def test_mixed_overwrite_batch_waits_for_confirmation_then_keeps_uploaded_versions(self):
        (self.workspace / "old.txt").write_bytes(b"old")
        result = self.upload([("new.txt", b"new"), ("old.txt", b"replacement")])
        self.assertEqual(result.status_code, 200)
        self.assertFalse((self.workspace / "new.txt").exists())
        self.assertEqual(list(self.attachments.root.iterdir()), [])
        token = html.unescape(re.search(r"name='token' value='([^']+)'", result.body.decode())[1])
        saved = files.upload_confirm(self.request, token)
        batch = parse_qs(urlsplit(saved.headers["location"]).query)["attachment_batch"][0]
        records = self.attachments.batch(batch, scope=self.scope)
        (self.workspace / "old.txt").write_bytes(b"later")
        self.assertEqual(Path(records[1]["path"]).read_bytes(), b"replacement")

    def test_tampered_scope_fails_before_saving_originals(self):
        self.token = self.signer.dumps({"purpose": "message_upload", **self.scope, "owner": "foreign"})
        result = self.upload([("new.txt", b"new")])
        self.assertEqual(result.status_code, 200)
        self.assertFalse((self.workspace / "new.txt").exists())

    def test_registry_failure_reports_original_already_saved_not_retry_upload(self):
        with patch.object(self.attachments, "commit", side_effect=OSError("unavailable")):
            result = self.upload([("saved.txt", b"saved")])
        self.assertEqual(result.status_code, 500)
        self.assertIn("不要重复上传", result.body.decode())
        self.assertEqual((self.workspace / "saved.txt").read_bytes(), b"saved")
        self.assertEqual(list(self.attachments.root.iterdir()), [])

    def test_partial_install_keeps_only_saved_local_versions_and_reports_actual_count(self):
        from shared.file_operations import check_destination
        original = files.install_upload
        count = 0
        def partial(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("test partial install")
            return original(*args, **kwargs)
        with patch.object(files, "install_upload", side_effect=partial):
            result = self.upload([("first.txt", b"first"), ("second.txt", b"second")])
        self.assertIn("已保存 1 个文件", result.body.decode())
        self.assertTrue((self.workspace / "first.txt").exists())
        self.assertFalse((self.workspace / "second.txt").exists())
        self.assertEqual(self.store._db.execute("SELECT COUNT(*) FROM message_attachments").fetchone()[0], 1)

    def test_scope_token_does_not_replace_current_login_or_overwrite_confirmation_expiry(self):
        with patch.object(message_attachment_uploads, "logged_in", return_value="foreign"):
            result = self.upload([("new.txt", b"new")])
        self.assertIn("上传失败", result.body.decode())
        self.assertFalse((self.workspace / "new.txt").exists())
        (self.workspace / "old.txt").write_bytes(b"old")
        confirmation = self.upload([("old.txt", b"new")])
        token = html.unescape(re.search(r"name='token' value='([^']+)'", confirmation.body.decode())[1])
        import time
        now = time.time()
        with patch("shared.upload_confirmation.time.time", return_value=now + 3602):
            rejected = files.upload_confirm(self.request, token)
        self.assertIn("超过 1 小时", rejected.body.decode())
        self.assertEqual((self.workspace / "old.txt").read_bytes(), b"old")


class RuntimeBindingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = runtime_fixtures.ConversationRuntimeTests.asyncSetUp
    asyncTearDown = runtime_fixtures.ConversationRuntimeTests.asyncTearDown
    start = runtime_fixtures.ConversationRuntimeTests.start

    def reference(self, name="local.txt"):
        manager = MessageAttachmentService(self.store)
        source = self.workspace / name
        source.write_bytes(b"KEEP_FILE_BYTES_LOCAL")
        scope = manager.scope(owner="owner", kind="conversation", scope_id="conversation-1",
                              directory=str(self.workspace))
        values = manager.prepare(scope, [(source, name)])
        manager.commit(values)
        return values[0]

    async def test_first_turn_stores_refs_and_only_delivers_text_paths(self):
        reference = self.reference()
        result = await self.runtime.start_conversation("conversation-1", command_id="message-1",
            text="看看文件", attachment_refs=[reference])
        self.assertEqual(result["command"]["state"], "accepted")
        payload = self.store.command("message-1")["payload"]
        self.assertEqual(payload["attachments"][0]["id"], reference["id"])
        call = [value for method, value in self.factory.clients[0].calls if method == "turn/start"][0]
        self.assertIn(reference["path"], call["leading_context"])
        self.assertNotIn("KEEP_FILE_BYTES_LOCAL", str(call))
        public = ConversationService(self.store, self.artifacts, self.runtime).public_snapshot("conversation-1")
        self.assertEqual(public["messages"][0]["attachments"][0]["id"], reference["id"])
        self.assertNotIn(reference["path"], json.dumps(public))

    async def test_active_steer_uses_the_same_durable_file_reference(self):
        await self.start()
        reference = self.reference()
        await self.runtime.send_message("conversation-1", command_id="message-2", text="看附件",
                                        attachment_refs=[reference])
        call = [value for method, value in self.factory.clients[0].calls if method == "turn/steer"][0]
        self.assertIn(reference["path"], call["leading_context"])

    async def test_queued_next_turn_uses_persisted_refs_not_current_upload_filename(self):
        await self.start()
        reference = self.reference()
        result = await self.runtime.send_message("conversation-1", command_id="message-2", text="下一轮",
                                                 force_new_turn=True, attachment_refs=[reference])
        self.assertEqual(result["command"]["state"], "queued")
        (self.workspace / reference["name"]).write_bytes(b"newer original")
        self.factory.threads["thread-1"]["turns"][0]["status"] = "completed"
        self.factory.threads["thread-1"]["status"] = {"type": "idle"}
        self.store.set_conversation_state("conversation-1", "idle", active_turn_id=None)
        await self.runtime._drain_queue("conversation-1")
        starts = [value for method, value in self.factory.clients[0].calls if method == "turn/start"]
        self.assertEqual(len(starts), 2)
        self.assertIn(reference["path"], starts[-1]["leading_context"])
        self.assertEqual(Path(reference["path"]).read_bytes(), b"KEEP_FILE_BYTES_LOCAL")

    async def test_unknown_delivery_does_not_replay_file_reference(self):
        await self.start()
        reference = self.reference()
        self.factory.clients[0].steer_error = AppServerDisconnected("test uncertain delivery")
        result = await self.runtime.send_message("conversation-1", command_id="message-2", text="看看",
                                                 attachment_refs=[reference])
        self.assertEqual(result["command"]["state"], "delivery_unknown")
        await self.runtime.reconcile("conversation-1")
        self.assertEqual(sum(method == "turn/steer" for client in self.factory.clients for method, _ in client.calls), 1)
        self.assertEqual(self.store.command("message-2")["payload"]["attachments"][0]["id"], reference["id"])

    async def test_missing_queued_version_rejects_before_native_delivery(self):
        await self.start()
        reference = self.reference()
        result = await self.runtime.send_message("conversation-1", command_id="message-2", text="下一轮",
                                                 force_new_turn=True, attachment_refs=[reference])
        Path(reference["path"]).unlink()
        self.factory.threads["thread-1"]["turns"][0]["status"] = "completed"
        self.factory.threads["thread-1"]["status"] = {"type": "idle"}
        self.store.set_conversation_state("conversation-1", "idle", active_turn_id=None)
        await self.runtime._drain_queue("conversation-1")
        self.assertEqual(self.store.command("message-2")["reason"], "attachment_unavailable")
        self.assertEqual(sum(method == "turn/start" for method, _ in self.factory.clients[0].calls), 1)

    async def test_rollback_between_skill_resolution_and_rpc_cannot_deliver_file(self):
        await self.start()
        reference = self.reference()
        legacy = self.root / "legacy-release"; (legacy / "app").mkdir(parents=True)
        async def skills(*args, **kwargs):
            quarantine_before_rollback(legacy, database_path=self.store.path)
            return []
        with patch.object(self.runtime, "_resolve_skills", side_effect=skills):
            result = await self.runtime.send_message("conversation-1", command_id="file-race", text="see file",
                attachment_refs=[reference])
        self.assertEqual(result["command"]["state"], "rejected")
        self.assertFalse(any(method == "turn/steer" for method, _ in self.factory.clients[0].calls))
        self.assertTrue(Path(reference["path"]).exists())

    async def test_post_mark_rollback_preserves_one_inflight_delivery_with_exact_refs(self):
        await self.start(); reference = self.reference()
        legacy = self.root / "legacy-release"; (legacy / "app").mkdir(parents=True)
        original = self.runtime._mark_command_sending
        def mark_then_quarantine(command_id):
            admitted = original(command_id)
            quarantine_before_rollback(legacy, database_path=self.store.path)
            self.assertEqual(self.store.command(command_id)["state"], "delivery_unknown")
            return admitted
        with patch.object(self.runtime, "_mark_command_sending", side_effect=mark_then_quarantine):
            result = await self.runtime.send_message("conversation-1", command_id="file-admitted", text="see file",
                attachment_refs=[reference])
        calls = [value for method, value in self.factory.clients[0].calls if method == "turn/steer"]
        self.assertEqual(len(calls), 1)
        self.assertIn(reference["path"], calls[0]["leading_context"])
        self.assertEqual(result["command"]["state"], "accepted")
        self.assertEqual(self.store.queued_commands("conversation-1"), [])

    async def test_same_command_id_cannot_be_reused_with_different_file(self):
        first, second = self.reference("first.txt"), self.reference("second.txt")
        await self.runtime.start_conversation("conversation-1", command_id="message-1", text="一样文字",
                                              attachment_refs=[first])
        with self.assertRaises(AppServerProtocolError):
            await self.runtime.start_conversation("conversation-1", command_id="message-1", text="一样文字",
                                                  attachment_refs=[second])

    async def test_changed_retry_does_not_bind_new_attachment(self):
        first, second = self.reference("first.txt"), self.reference("second.txt")
        service = ConversationService(self.store, self.artifacts, self.runtime)
        await service.start("conversation-1", command_id="message-1", text="same",
            attachment_ids=[first["id"]], attachment_owner_id="owner")
        with self.assertRaises(AppServerProtocolError):
            await service.start("conversation-1", command_id="message-1", text="same",
                attachment_ids=[second["id"]], attachment_owner_id="owner")
        bindings = self.store._db.execute("SELECT attachment_id FROM message_attachment_bindings").fetchall()
        self.assertEqual([row[0] for row in bindings], [first["id"]])
        self.assertEqual(self.store._db.execute("SELECT bound_once FROM message_attachments WHERE id=?",
            (second["id"],)).fetchone()[0], 0)

    async def test_binding_failure_rolls_back_command_before_native_rpc(self):
        reference = self.reference()
        service = ConversationService(self.store, self.artifacts, self.runtime)
        with patch.object(service.attachments, "_bind_locked", side_effect=OSError("fixture binding failed")):
            with self.assertRaises(OSError):
                await service.start("conversation-1", command_id="message-1", text="read",
                    attachment_ids=[reference["id"]], attachment_owner_id="owner")
        self.assertIsNone(self.store._db.execute("SELECT id FROM commands WHERE id='message-1'").fetchone())
        self.assertFalse(self.factory.clients)

    async def test_only_attachment_start_has_no_invented_user_text_in_history(self):
        reference = self.reference()
        service = ConversationService(self.store, self.artifacts, self.runtime)
        result = await service.start("conversation-1", command_id="message-1", text="",
            attachment_ids=[reference["id"]], attachment_owner_id="owner")
        self.assertEqual(result["command"]["state"], "accepted")
        self.assertEqual(self.store.command("message-1")["text"], "")
        self.assertEqual(service.public_snapshot("conversation-1")["messages"][0]["attachments"][0]["id"], reference["id"])

    async def test_delete_conversation_removes_only_owned_copy_not_original(self):
        reference = self.reference()
        service = ConversationService(self.store, self.artifacts, self.runtime)
        await service.delete("conversation-1")
        self.assertTrue((self.workspace / reference["name"]).exists())
        self.assertFalse(Path(reference["path"]).exists())


class FirstRequestDedupeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = runtime_fixtures.ConversationRuntimeTests.asyncSetUp
    asyncTearDown = runtime_fixtures.ConversationRuntimeTests.asyncTearDown

    def setup_request(self):
        service = ConversationService(self.store, self.artifacts, self.runtime)
        source = self.workspace / "first.txt"; source.write_bytes(b"first local file")
        scope = service.attachments.scope(owner="owner", kind="project", scope_id="project-one", directory=str(self.workspace))
        records = service.attachments.prepare(scope, [(source, source.name)])
        service.attachments.commit(records)
        request = Request({"type": "http", "method": "POST", "path": "/conversation/new", "headers": []})
        for item in (
            patch.object(conversations, "require_auth", return_value=None),
            patch.object(conversations, "logged_in", return_value="owner"),
            patch.object(conversations, "audit"),
            patch.object(conversations, "conversation_service", return_value=service),
            patch.object(conversations, "classify_client_origin", return_value=("mobile_web", None, None)),
            patch("projects.get", return_value={"path": str(self.workspace)}),
            patch("projects.touch"),
            patch.object(conversations, "new_defaults", new=AsyncMock(return_value={
                "model": "fixture", "effort": "medium", "permission": "danger-full-access",
                "completion_notification": False,
            })),
        ):
            item.start(); self.addCleanup(item.stop)
        return request, records[0], {"prompt": "看这个文件", "project_id": "project-one",
            "skills": [], "skills_explicit": "1", "attachment_ids": [records[0]["id"]],
            "client_command_id": "first-request-1"}

    async def test_same_first_post_returns_original_conversation_without_another_native_turn(self):
        request, record, values = self.setup_request()
        first = await conversations.create_and_start_conversation(request, **values)
        second = await conversations.create_and_start_conversation(request, **values)
        self.assertEqual(first.status_code, 303)
        self.assertEqual(first.headers["location"], second.headers["location"])
        self.assertEqual(sum(method == "turn/start" for client in self.factory.clients for method, _ in client.calls), 1)
        self.assertEqual(self.store.command("first-request-1")["payload"]["attachments"][0]["id"], record["id"])

    async def test_concurrent_first_posts_admit_one_conversation(self):
        import asyncio
        request, record, values = self.setup_request()
        async def defaults(*args, **kwargs):
            await asyncio.sleep(0)
            return {"model": "fixture", "effort": "medium", "permission": "danger-full-access",
                "completion_notification": False}
        with patch.object(conversations, "new_defaults", side_effect=defaults):
            results = await asyncio.gather(*[
                conversations.create_and_start_conversation(request, **values) for _ in range(4)])
        self.assertEqual({result.status_code for result in results}, {303})
        self.assertEqual(len({result.headers["location"] for result in results}), 1)
        self.assertEqual(sum(method == "turn/start" for client in self.factory.clients for method, _ in client.calls), 1)
        # The fixture already contains conversation-1; admission creates exactly one more.
        self.assertEqual(self.store._db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 2)

    async def test_unknown_first_post_is_not_replayed(self):
        request, record, values = self.setup_request()
        first = await conversations.create_and_start_conversation(request, **values)
        self.store.update_command("first-request-1", "delivery_unknown", reason="test uncertain response")
        second = await conversations.create_and_start_conversation(request, **values)
        self.assertEqual(first.headers["location"], second.headers["location"])
        self.assertEqual(self.store.command("first-request-1")["state"], "delivery_unknown")
        self.assertEqual(sum(method == "turn/start" for client in self.factory.clients for method, _ in client.calls), 1)

    async def test_reused_first_identity_with_changed_text_is_rejected_before_creating_draft(self):
        request, record, values = self.setup_request()
        await conversations.create_and_start_conversation(request, **values)
        count = self.store._db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        result = await conversations.create_and_start_conversation(request, **{**values, "prompt": "changed request"})
        self.assertIn("内容不同", result.body.decode())
        self.assertEqual(self.store._db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], count)
