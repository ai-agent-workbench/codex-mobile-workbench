import tempfile
import json
import unittest
import re
import os
from pathlib import Path
from unittest import mock

from engine.codex_app_server import AppServerProtocolError
from shared.conversation_artifacts import ConversationArtifactService, tool_success, tool_failure
from tests.store_fixtures import seeded_store


class ConversationArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="codex-conversation-artifacts-", dir=Path.home()
        )
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.store = seeded_store(self.root / "state" / "conversations.sqlite3")
        self.store.create_conversation(
            "conversation-1",
            title="artifacts",
            cwd=str(self.workspace),
            settings={"model": "fixture", "effort": "medium"},
        )
        self.store.bind_thread(
            "conversation-1", {"id": "thread-1"}, server_version="fixture"
        )
        self.store.begin_command(
            "conversation-1", "message-1", kind="start", text="make files"
        )
        self.store.bind_turn(
            "conversation-1",
            {"id": "turn-1", "status": "inProgress"},
            command_id="message-1",
        )
        self.service = ConversationArtifactService(
            self.store, self.root / "managed-artifacts"
        )

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def publish(self, source, key, **values):
        return self.service.publish(
            "conversation-1",
            turn_id="turn-1",
            source_path=str(source),
            idempotency_key=key,
            origin="dynamic_tool",
            **values,
        )

    def test_model_visible_success_contains_authenticated_download_route(self):
        source = self.workspace / "验收成功.txt"
        source.write_text("手机预览测试成功", encoding="utf-8")
        record = self.publish(source, "visible-receipt")
        response = tool_success(record)
        visible = json.loads(response["contentItems"][0]["text"])
        self.assertIs(response["success"], True)
        self.assertIs(visible["success"], True)
        self.assertEqual(visible["artifact_id"], record["id"])
        self.assertEqual(visible["sha256"], record["sha256"])
        self.assertEqual(visible["size"], 24)
        self.assertEqual(visible["download_url"],
                         "/conversation/conversation-1/artifact/" + record["id"] + "?download=1")
        self.assertNotIn(str(source), str(visible))
        self.assertNotIn("storage_relpath", visible)

    def test_failure_is_visible_false_without_mislabelling_other_tools(self):
        response = tool_failure("标题未保存，任务可继续。")
        visible = json.loads(response["contentItems"][0]["text"])
        self.assertIs(response["success"], False)
        self.assertIs(visible["success"], False)
        self.assertEqual(visible["error"], "标题未保存，任务可继续。")
        self.assertNotIn("Artifact publishing failed", str(response))

    def test_image_is_copied_to_managed_storage_and_is_idempotent(self):
        source = self.workspace / "chart.png"
        source.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        record = self.publish(source, "call-1")
        replay = self.publish(source, "call-1")

        self.assertEqual(record["id"], replay["id"])
        self.assertEqual(record["preview_kind"], "image")
        self.assertEqual(record["mime_type"], "image/png")
        self.assertRegex(record["id"], re.compile(r"^[a-f0-9]{36}$"))
        managed = self.service.path_for(record)
        self.assertNotEqual(managed, source)
        self.assertEqual(managed.read_bytes(), source.read_bytes())
        self.assertNotIn(str(source), str(record))

    def test_text_uses_plain_browser_preview_and_is_bounded(self):
        source = self.workspace / "notes.md"
        source.write_text("# Title\n\nplain text", encoding="utf-8")
        record = self.publish(source, "call-text")
        self.assertEqual(record["preview_kind"], "text")
        self.assertEqual(self.service.text_preview(record), "# Title\n\nplain text")

    def test_pdf_is_native_preview_and_office_is_download(self):
        pdf = self.workspace / "report.pdf"
        pdf.write_bytes(b"%PDF-1.7\nfixture")
        xlsx = self.workspace / "report.xlsx"
        xlsx.write_bytes(b"PK\x03\x04fixture")
        self.assertEqual(self.publish(pdf, "pdf")["preview_kind"], "pdf")
        self.assertEqual(self.publish(xlsx, "xlsx")["preview_kind"], "download")

    def test_html_gets_restricted_preview_while_svg_stays_download_only(self):
        html = self.workspace / "interactive.html"
        html.write_text("<button>可交互草图</button>", encoding="utf-8")
        svg = self.workspace / "active.svg"
        svg.write_text("<svg><script>alert(1)</script></svg>", encoding="utf-8")
        html_record = self.publish(html, "html")
        svg_record = self.publish(svg, "svg")
        self.assertEqual(html_record["preview_kind"], "html")
        self.assertEqual(html_record["mime_type"], "text/html")
        self.assertEqual(svg_record["preview_kind"], "download")
        self.assertTrue(self.service.html_previewable({
            "preview_kind": "download", "name": "旧草图.HTML",
        }))
        self.assertFalse(self.service.html_previewable({
            "preview_kind": "download", "name": "active.svg",
        }))

    def test_readable_regular_file_outside_conversation_can_be_published(self):
        with tempfile.TemporaryDirectory(prefix="artifact-source-outside-roots-") as root:
            outside = Path(root) / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            record = self.publish(outside, "outside")
            self.assertEqual(self.service.path_for(record).read_text(), "outside")

    def test_source_symlink_is_rejected(self):
        source = self.workspace / "real.txt"
        source.write_text("inside", encoding="utf-8")
        link = self.workspace / "link.txt"
        link.symlink_to(source)
        with self.assertRaisesRegex(AppServerProtocolError, "symlink"):
            self.publish(link, "symlink")

    def test_title_is_filename_only_and_preserves_source_extension(self):
        source = self.workspace / "table.csv"
        source.write_text("a,b\n1,2\n", encoding="utf-8")
        record = self.publish(source, "safe-name", title="../季度/汇总")
        self.assertEqual(record["name"], "汇总.csv")
        self.assertNotIn("..", record["storage_relpath"])

    def test_title_version_dot_cannot_hide_the_source_extension(self):
        source = self.workspace / "interactive.html"
        source.write_text("<!doctype html><title>草图</title>", encoding="utf-8")

        versioned = self.publish(
            source, "versioned-title", title="统一经营门户 v0.8 交互草图"
        )
        explicit = self.publish(
            source, "explicit-title", title="统一经营门户 v0.8 交互草图.html"
        )

        self.assertEqual(versioned["name"], "统一经营门户 v0.8 交互草图.html")
        self.assertEqual(explicit["name"], "统一经营门户 v0.8 交互草图.html")
        self.assertEqual(versioned["preview_kind"], "html")
        self.assertEqual(versioned["mime_type"], "text/html")

    def test_total_quota_rejects_without_orphaning_copies(self):
        limited = ConversationArtifactService(
            self.store,
            self.root / "limited-artifacts",
            max_count_per_conversation=2,
            max_total_bytes_per_conversation=7,
        )
        first = self.workspace / "first.txt"
        first.write_text("1234", encoding="utf-8")
        second = self.workspace / "second.txt"
        second.write_text("5678", encoding="utf-8")
        limited.publish(
            "conversation-1",
            turn_id="turn-1",
            source_path=str(first),
            idempotency_key="quota-first",
            origin="dynamic_tool",
        )
        # A replay is free and returns the already-managed object.
        replay = limited.publish(
            "conversation-1",
            turn_id="turn-1",
            source_path=str(first),
            idempotency_key="quota-first",
            origin="dynamic_tool",
        )
        self.assertEqual(replay["size"], 4)
        with self.assertRaisesRegex(AppServerProtocolError, "quota"):
            limited.publish(
                "conversation-1",
                turn_id="turn-1",
                source_path=str(second),
                idempotency_key="quota-second",
                origin="dynamic_tool",
            )
        managed_files = [path for path in limited.root.rglob("*") if path.is_file()]
        self.assertEqual(len(managed_files), 1)

    def test_count_quota_cannot_be_bypassed_with_new_idempotency_keys(self):
        limited = ConversationArtifactService(
            self.store,
            self.root / "count-limited-artifacts",
            max_count_per_conversation=1,
            max_total_bytes_per_conversation=1024,
        )
        source = self.workspace / "small.txt"
        source.write_text("one", encoding="utf-8")
        limited.publish(
            "conversation-1",
            turn_id="turn-1",
            source_path=str(source),
            idempotency_key="count-first",
            origin="dynamic_tool",
        )
        with self.assertRaisesRegex(AppServerProtocolError, "quota"):
            limited.publish(
                "conversation-1",
                turn_id="turn-1",
                source_path=str(source),
                idempotency_key="count-second",
                origin="dynamic_tool",
            )

    def test_source_replacement_between_validation_and_open_is_rejected(self):
        source = self.workspace / "changing.txt"
        source.write_text("original", encoding="utf-8")
        replacement = self.workspace / "replacement.txt"
        replacement.write_text("replacement", encoding="utf-8")
        real_open = os.open
        replaced = False

        def swap_then_open(path, flags, mode=0o777):
            nonlocal replaced
            if Path(path) == source and not replaced:
                replaced = True
                source.unlink()
                replacement.rename(source)
            return real_open(path, flags, mode)

        with mock.patch(
            "shared.conversation_artifacts.os.open", side_effect=swap_then_open
        ):
            with self.assertRaisesRegex(AppServerProtocolError, "changed"):
                self.publish(source, "replacement-race")
        self.assertEqual(
            [path for path in self.service.root.rglob("*") if path.is_file()], []
        )


if __name__ == "__main__":
    unittest.main()
