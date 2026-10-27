import tempfile
import unittest
from pathlib import Path

from engine.codex_app_server import AppServerProtocolError
from engine.conversation_questions import (
    answer_digest, normalize_request, rpc_identity, validate_answers,
)
from tests.store_fixtures import seeded_store


def request(*, blocking=False, auto=None, questions=None):
    value = {
        "threadId": "thread-1", "turnId": "turn-1", "itemId": "item-1",
        "isBlocking": blocking,
        "questions": questions or [{
            "id": "choice", "header": "Choose", "question": "Choose one",
            "options": [{"label": "A", "description": "first"}],
            "isOther": False, "isSecret": False,
        }],
    }
    if auto is not None:
        value["autoResolutionMs"] = auto
    return value


class ConversationQuestionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.store = seeded_store(Path(self.tmp.name) / "db.sqlite3")
        self.store.create_conversation("conversation-1", title="x", cwd="/tmp", settings={
            "model": "fixture", "effort": "medium", "permission": "danger-full-access",
        })
        self.store.bind_thread("conversation-1", {"id": "thread-1"}, server_version="fixture")
        self.store.begin_command("conversation-1", "command-start", kind="start", text="x")
        self.store.bind_turn("conversation-1", {"id": "turn-1", "status": "inProgress"}, command_id="command-start")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _register(self, value=None, rpc=0, connection="instance:1"):
        parsed = normalize_request(value or request())
        rpc_json, rpc_type = rpc_identity(rpc)
        return self.store.register_question_request(
            "conversation-1", request_id_json=rpc_json, request_id_type=rpc_type,
            connection_epoch=connection, **parsed,
        )[0]

    def test_multi_text_secret_and_public_receipt(self):
        item = self._register(request(questions=[
            {"id": "choice", "header": "C", "question": "C", "options": [{"label": "A", "description": "a"}], "isOther": False, "isSecret": False},
            {"id": "text", "header": "T", "question": "T", "options": None, "isOther": False, "isSecret": False},
            {"id": "secret", "header": "S", "question": "S", "options": None, "isOther": False, "isSecret": True},
        ]))
        answers = validate_answers(item["questions"], {"choice": "A", "text": "hello", "secret": "token"})
        saved, won = self.store.claim_question_answer("conversation-1", item["id"], command_id="answer-1", answers=answers, source="user", answers_digest="digest")
        self.assertTrue(won)
        self.assertEqual(saved["answers"], {"choice": "A", "text": "hello", "secret": None})

    def test_native_large_questions_options_and_answers_are_not_web_capped(self):
        values=[{"id": "q"+str(i), "header": "H", "question": "长"*6001,
                 "options": None, "isOther": False, "isSecret": False} for i in range(9)]
        normalized=normalize_request(request(questions=values))
        answers=validate_answers(normalized["questions"], {"q"+str(i):"答"*6001 for i in range(9)})
        self.assertEqual(len(answers),9)
        values[0]["options"]=[{"label":str(i),"description":"描述"*3000} for i in range(13)]
        self.assertEqual(len(normalize_request(request(questions=values))["questions"][0]["options"]),13)

    def test_optional_null_default_and_zero_native_interval(self):
        optional = normalize_request(request(blocking=False))
        self.assertEqual(optional["deadline_source"], "host_optional_default")
        self.assertIsNotNone(optional["deadline_at"])
        zero = normalize_request(request(blocking=False, auto=0))
        self.assertEqual(zero["deadline_source"], "native_interval")
        self.assertIsNotNone(zero["deadline_at"])
        blocking = normalize_request(request(blocking=True))
        self.assertIsNone(blocking["deadline_at"])

    def test_command_idempotence_cross_conversation_and_connection_match(self):
        item = self._register()
        first, won = self.store.claim_question_answer("conversation-1", item["id"], command_id="answer-1", answers={"choice": "A"}, source="user", answers_digest="one")
        self.assertTrue(won)
        replay, won = self.store.claim_question_answer("conversation-1", item["id"], command_id="answer-1", answers={"choice": "A"}, source="user", answers_digest="one")
        self.assertFalse(won)
        self.assertEqual(first["id"], replay["id"])
        with self.assertRaises(AppServerProtocolError):
            self.store.claim_question_answer("conversation-1", item["id"], command_id="answer-1", answers={"choice": "other"}, source="user", answers_digest="two")
        self.assertIsNone(self.store.mark_question_resolved("thread-1", "0", "integer", "other:1"))

    def test_click_racing_deadline_records_recommendation_digest(self):
        item = self._register(request(questions=[{
            "id": "choice", "header": "Choose", "question": "Choose one",
            "options": [
                {"label": "A", "description": "recommended"},
                {"label": "B", "description": "manual"},
            ], "isOther": False, "isSecret": False,
        }]))
        self.store._db.execute(
            "UPDATE question_requests SET deadline_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", item["id"]),
        )
        saved, won = self.store.claim_question_answer(
            "conversation-1", item["id"], command_id="late-click",
            answers={"choice": "B"}, source="user",
            answers_digest=answer_digest({"choice": "B"}),
        )
        self.assertTrue(won)
        self.assertEqual(saved["answer_source"], "deadline")
        self.assertEqual(saved["answers"], {"choice": "A"})
        self.assertEqual(saved["answers_digest"], answer_digest({"choice": "A"}))

    def test_resolved_before_send_requires_later_activity(self):
        item = self._register()
        self.store.claim_question_answer("conversation-1", item["id"], command_id="answer-1", answers={"choice": "A"}, source="user", answers_digest="one")
        self.assertEqual(self.store.mark_question_resolved("thread-1", "0", "integer", "instance:1")["state"], "resolved_before_send")
        self.assertEqual(self.store.mark_question_response_sent(item["id"])["state"], "awaiting_activity")
        self.assertEqual(self.store.note_question_turn_activity("thread-1", "turn-1")[0]["state"], "answered")

    def test_restart_never_replays_and_reissue_keeps_deadline(self):
        item = self._register(request(blocking=False, auto=0))
        original_deadline = item["deadline_at"]
        self.assertEqual(self.store.recover_question_requests()[0]["state"], "expired")
        rebound = self._register(request(blocking=False, auto=0), rpc="new", connection="instance:2")
        self.assertEqual(rebound["id"], item["id"])
        self.assertEqual(rebound["deadline_at"], original_deadline)
        self.assertEqual(rebound["connection_epoch"], "instance:2")

    def test_disconnect_marks_unwritten_answer_delivery_unknown(self):
        item = self._register()
        self.store.claim_question_answer("conversation-1", item["id"], command_id="answer-1", answers={"choice": "A"}, source="user", answers_digest="one")
        lost = self.store.mark_questions_connection_lost("instance:1")
        self.assertEqual(lost[0]["state"], "delivery_unknown")
