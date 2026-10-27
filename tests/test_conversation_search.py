import sqlite3
import tempfile
import unittest
from pathlib import Path

from engine.codex_app_server import AppServerProtocolError
from engine.conversation_search import ConversationSearchQueryError, normalize_search_query
from engine.conversation_service import ConversationService
from engine.conversation_store import ConversationStore
from shared.conversation_artifacts import ConversationArtifactService
from shared.conversation_pagination import ConversationPageCursorError


class FakeRuntime:
    pass


class ConversationSearchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-search-", dir=Path.home())
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.path = self.root / "state" / "conversations.sqlite3"
        self.store = ConversationStore(self.path)
        self.service = ConversationService(
            self.store,
            ConversationArtifactService(self.store, self.root / "artifacts"),
            FakeRuntime(),
        )

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def add(self, value, *, project=None, title=None):
        self.store.create_conversation(
            value, title=title or value, cwd=str(self.workspace),
            project_id=project, settings={},
        )
        self.store.bind_thread(value, {"id": "thread-" + value}, server_version="fixture")

    def command(self, conversation_id, command_id, text, *, kind="start"):
        self.store.begin_command(
            conversation_id, command_id, kind=kind, text=text, payload={}
        )

    def item(self, conversation_id, turn_id, item_id, text, *, phase="final_answer",
             delivery=None, status="completed", final_text=None):
        self.store._db.execute(
            "INSERT INTO turns(id,conversation_id,ordinal,status,final_text,started_at) "
            "VALUES(?,?,1,'completed',?,'2026-10-01T00:00:00+00:00') "
            "ON CONFLICT(id) DO NOTHING",
            (turn_id, conversation_id, final_text),
        )
        self.store._db.execute(
            "INSERT INTO items(conversation_id,turn_id,native_item_id,kind,status,phase,text,delivery,"
            "created_at,updated_at) VALUES(?,?,?,'agent_message',?,?,?,?,?,?)",
            (conversation_id, turn_id, item_id, status, phase, text, delivery,
             "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"),
        )

    def test_title_user_final_and_safe_exclusions(self):
        self.add("c1", title="标题 needle")
        self.command("c1", "cmd-start", "用户 needle")
        self.command("c1", "cmd-steer", "补充 needle", kind="steer")
        self.store._db.execute(
            "INSERT INTO turns(id,conversation_id,ordinal,status,final_text) "
            "VALUES('turn-one','c1',1,'completed','回退 needle')"
        )
        self.item("c1", "turn-one", "final-one", "最终 needle")
        self.store._db.execute(
            "INSERT INTO items(conversation_id,turn_id,native_item_id,kind,status,phase,text,detail,delivery,"
            "created_at,updated_at) VALUES('c1','turn-one','reason','reasoning_summary','completed',"
            "NULL,'私密 reasoning needle','工具 needle',NULL,'x','x')"
        )
        self.item("c1", "turn-one", "async", "异步 needle", delivery="async")
        self.store._db.execute(
            "UPDATE commands SET payload_json='{\"secret\":\"payload needle\"}' WHERE id='cmd-start'"
        )
        self.assertEqual(self.store.search_conversation_ids("needle"), ["c1"])
        hit = self.store.search_matches_by_ids("needle", ["c1"])[0]
        self.assertEqual(hit["kind"], "title")
        self.assertEqual(hit["snippet"], "标题 needle")
        self.assertEqual(self.store.search_conversation_ids("reasoning"), [])
        self.assertEqual(self.store.search_conversation_ids("payload"), [])
        self.assertEqual(self.store.search_conversation_ids("异步"), [])
        self.assertEqual(self.store.search_conversation_ids("最终"), ["c1"])
        self.assertEqual(self.store.search_matches_by_ids("最终", ["c1"])[0]["item_id"], "final-one")

    def test_literal_query_chinese_case_and_injection(self):
        self.add("c1")
        self.command("c1", "cmd", "AbC 中文测试")
        for query in ("abc", "ABC", "中", "中文", "中文测", 'Ab"C', "%_", "select *"):
            # Punctuation and SQL/FTS-looking input remains a literal and never errors.
            self.store.search_conversation_ids(query)
        self.assertEqual(self.store.search_conversation_ids("ABC"), ["c1"])
        self.assertEqual(self.store.search_conversation_ids("中文 测试"), ["c1"])
        self.assertEqual(self.store.search_conversation_ids("中文 缺失"), [])
        with self.assertRaises(ConversationSearchQueryError) as raised:
            normalize_search_query("x" * 121)
        self.assertEqual(raised.exception.code, "invalid_query")
        with self.assertRaises(ConversationSearchQueryError):
            normalize_search_query(" ".join("x" for _ in range(9)))
        for control in ("bad\x00query", "bad\x1bquery"):
            with self.assertRaises(ConversationSearchQueryError) as raised:
                normalize_search_query(control)
            self.assertEqual(raised.exception.code, "invalid_query")

    def test_legacy_async_fallback_is_rejected_and_safe_legacy_answer_survives(self):
        self.add("c1")
        self.item("c1", "t1", "async-old", "异步题目绝不索引", phase=None,
                  delivery="async", final_text="异步题目绝不索引")
        self.assertIsNone(self.store._final_text_locked("t1"))
        self.assertEqual(self.store.search_conversation_ids("异步题目"), [])
        # SQLite trim does not strip this Unicode whitespace, whereas the old
        # final_text producer did. Rejection must not depend on text equality.
        self.add("c2")
        self.item("c2", "t2", "async-space", "\u2003异步隐私字串\u2003", phase=None,
                  delivery="async", final_text="异步隐私字串")
        self.assertEqual(self.store.search_conversation_ids("异步隐私"), [])
        self.item("c1", "t1", "sync-legacy", "同步兼容回答", phase=None)
        safe = self.store._final_text_locked("t1")
        self.assertEqual(safe, "同步兼容回答")
        self.store._db.execute("UPDATE turns SET final_text=? WHERE id='t1'", (safe,))
        self.assertEqual(self.store.search_conversation_ids("兼容回答"), ["c1"])
        self.store._db.execute("UPDATE items SET delivery='async' WHERE native_item_id='sync-legacy'")
        self.assertEqual(self.store.search_conversation_ids("兼容回答"), [])

    def test_literal_matching_after_nul_is_not_lost_by_fts_candidates(self):
        for cid in ("nul-title","nul-user","nul-final","nul-fallback"):
            self.add(cid,title="early\x00HiddenNeedle" if cid=="nul-title" else cid)
        self.command("nul-user","nul-command","early\x00HiddenNeedle")
        self.item("nul-final","nul-turn","nul-item","early\x00HiddenNeedle")
        self.store._db.execute(
            "INSERT INTO turns(id,conversation_id,ordinal,status,final_text) VALUES('nul-fallback-turn','nul-fallback',1,'completed',?)",
            ("early\x00HiddenNeedle",),
        )
        expected={"nul-title","nul-user","nul-final","nul-fallback"}
        for query in ("hiddenneedle","HiddenNeedle","early needle"):
            self.assertEqual(set(self.store.search_conversation_ids(query)),expected)
        for hit in self.store.search_matches_by_ids("needle",list(expected)):
            start,end=hit["highlights"][0]
            self.assertEqual(hit["snippet"][start:end].lower(),"needle")

    def test_thread_read_copy_search_targets_the_visible_native_message(self):
        self.add("copy-c")
        self.item("copy-c","copy-t","msg_final","副本映射\n合法终稿")
        self.store._db.execute("UPDATE items SET source='notification_item' WHERE native_item_id='msg_final'")
        self.item("copy-c","copy-t","item-1"," 副本映射\t合法终稿\n")
        self.store._db.execute("UPDATE items SET source='thread_read' WHERE native_item_id='item-1'")
        for rebuild in (False,True):
            if rebuild: self.store._backfill_conversation_search_locked()
            self.assertEqual(self.store._db.execute(
                "SELECT native_item_id FROM conversation_search_documents WHERE kind='assistant' AND source_id='copy-t'"
            ).fetchone()[0],"item-1")
            self.assertEqual(self.store.search_matches_by_ids("副本映射 合法终稿",["copy-c"])[0]["item_id"],"msg_final")
            self.assertEqual([i["native_item_id"] for i in self.service.public_snapshot("copy-c")["turns"][0]["items"]],["msg_final"])

    def test_async_message_does_not_hide_a_matching_synchronous_final_copy(self):
        self.add("delivery-c")
        self.item("delivery-c","delivery-t","msg_async","不同投递方式的正文",delivery="async")
        self.item("delivery-c","delivery-t","item-2","不同投递方式的正文")
        self.store._db.execute("UPDATE items SET source='thread_read' WHERE native_item_id='item-2'")
        ids=[i["native_item_id"] for i in self.service.public_snapshot("delivery-c")["turns"][0]["items"]]
        self.assertEqual(ids,["msg_async","item-2"])
        self.assertEqual(self.store.search_matches_by_ids("不同投递",["delivery-c"])[0]["item_id"],"item-2")

    def test_hydrated_delta_baseline_and_search_share_visible_canonical_identity(self):
        self.add("hydrate-c")
        self.item("hydrate-c","hydrate-t","msg_hydrated","仅增量后缀")
        self.store._db.execute("UPDATE items SET source='notification_delta' WHERE native_item_id='msg_hydrated'")
        self.assertTrue(self.store.hydrate_delta_baseline(
            "hydrate-c","hydrate-t","msg_hydrated",expected_revision=1,
            fresh_native_item={"id":"msg_hydrated","type":"agentMessage","phase":"final_answer","text":"基线修复完整终稿"},
        ))
        self.item("hydrate-c","hydrate-t","item-1","基线修复完整终稿")
        self.store._db.execute("UPDATE items SET source='thread_read' WHERE native_item_id='item-1'")
        public=self.service.public_snapshot("hydrate-c")["turns"][0]["items"]
        self.assertEqual([(i["native_item_id"],i["dedup_role"]) for i in public],[("msg_hydrated","realtime")])
        self.assertEqual(self.store.search_matches_by_ids("基线修复",["hydrate-c"])[0]["item_id"],"msg_hydrated")

    def test_noncanonical_sources_remain_visible_and_search_targets_are_not_hidden(self):
        self.add("source-c")
        self.item("source-c","source-t","msg_native","独立来源同文终稿")
        self.store._db.execute("UPDATE items SET source='reconcile' WHERE native_item_id='msg_native'")
        self.item("source-c","source-t","item-1","独立来源同文终稿")
        self.store._db.execute("UPDATE items SET source='thread_read' WHERE native_item_id='item-1'")
        public=self.service.public_snapshot("source-c")["turns"][0]["items"]
        self.assertEqual([(i["native_item_id"],i["dedup_role"]) for i in public],[("msg_native","native"),("item-1","legacy-copy")])
        self.assertEqual(self.store.search_matches_by_ids("独立来源",["source-c"])[0]["item_id"],"item-1")

    def test_backfill_does_not_trust_historical_async_final_text(self):
        self.add("c1")
        self.item("c1", "t1", "old-question", "旧异步问题禁止回填", phase=None,
                  delivery="async", final_text="旧异步问题禁止回填")
        self.store.close()
        with sqlite3.connect(self.path) as connection:
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'conversation_search%'"
            ).fetchall():
                connection.execute("DROP TRIGGER " + row[0])
            connection.execute("DROP TABLE conversation_search_fts")
            connection.execute("DROP TABLE conversation_search_documents")
            connection.execute("DROP TABLE conversation_search_fallback_provenance")
            connection.execute("DROP TABLE conversation_search_item_sources")
            connection.execute("DELETE FROM schema_meta WHERE key='conversation_search_version'")
        self.store = ConversationStore(self.path)
        self.assertEqual(self.store.search_conversation_ids("旧异步"), [])
        self.assertIsNone(self.store._db.execute(
            "SELECT text FROM conversation_search_documents WHERE kind='assistant' AND source_id='t1'"
        ).fetchone())

    def test_first_backfill_rejects_known_nonfinal_cached_bodies_without_erasing_valid_sources(self):
        body = "非最终缓存检索"
        unsafe = ("commentary", "unknown-phase", "null-cache")
        for cid in unsafe:
            self.add(cid)
            self.item(cid, "t-" + cid, "source-" + cid, body,
                      phase="commentary" if cid == "null-cache" else cid,
                      final_text=None if cid == "null-cache" else body)
        self.add("valid-independent")
        self.item("valid-independent", "t-valid", "real-final", body, final_text=body)
        self.item("valid-independent", "t-valid", "progress", body, phase="commentary")
        self.add("valid-legacy")
        self.item("valid-legacy", "t-legacy", "legacy-final", body, phase=None, final_text=body)
        self.item("valid-legacy", "t-legacy", "legacy-progress", body, phase="commentary")
        self.add("valid-cache-only")
        self.store._db.execute(
            "INSERT INTO turns(id,conversation_id,ordinal,status,final_text) VALUES('t-cache','valid-cache-only',1,'completed',?)", (body,),
        )
        # Remove the unpublished extension to emulate the actual old v6 data.
        self.store.close()
        with sqlite3.connect(self.path) as db:
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'conversation_search%'").fetchall():
                db.execute("DROP TRIGGER " + row[0])
            for name in ("fts", "documents", "fallback_provenance", "item_sources"):
                db.execute("DROP TABLE conversation_search_" + name)
            db.execute("DELETE FROM schema_meta WHERE key='conversation_search_version'")
        self.store = ConversationStore(self.path)
        valid = {"valid-independent", "valid-legacy", "valid-cache-only"}
        self.assertEqual(set(self.store.search_conversation_ids(body)), valid)
        # Rejection survives deletion/NULLs/rebuild, while independent real
        # answers remain usable after their item has disappeared.
        self.store._db.execute("DELETE FROM items")
        for cid in unsafe:
            self.store._db.execute("UPDATE turns SET final_text=NULL WHERE id=?", ("t-" + cid,))
        self.store._backfill_conversation_search_locked()
        for cid in unsafe:
            self.store._db.execute("UPDATE turns SET final_text=? WHERE id=?", (body, "t-" + cid))
        self.assertEqual(set(self.store.search_conversation_ids(body)), valid)
        self.assertEqual(self.store._db.execute(
            "SELECT final_text FROM turns WHERE id='t-commentary'"
        ).fetchone()[0], body)

    def test_rejected_fallback_survives_source_deletion_null_and_index_rebuild(self):
        self.add("c1")
        self.item("c1", "t1", "async-old", "持久排除异步语料", phase=None,
                  delivery="async", final_text="持久排除异步语料")
        self.store._db.execute("DELETE FROM items WHERE native_item_id='async-old'")
        self.assertEqual(self.store.search_conversation_ids("排除异步"), [])
        self.store._db.execute("UPDATE turns SET final_text=NULL WHERE id='t1'")
        self.store._db.execute("UPDATE turns SET final_text='\u2003持久排除异步语料\n' WHERE id='t1'")
        self.assertEqual(self.store.search_conversation_ids("排除异步"), [])
        self.store.close()
        with sqlite3.connect(self.path) as connection:
            connection.execute("DROP TABLE conversation_search_fts")
        self.store = ConversationStore(self.path)
        self.assertEqual(self.store.search_conversation_ids("排除异步"), [])
        self.assertEqual(self.store._db.execute(
            "SELECT trusted FROM conversation_search_fallback_provenance WHERE turn_id='t1'"
        ).fetchone()[0], 0)
        # A real, synchronous answer is positive evidence for the same text.
        self.item("c1", "t1", "sync-proof", "持久排除异步语料", phase=None)
        self.assertEqual(self.store.search_conversation_ids("排除异步"), ["c1"])
        self.store._db.execute("DELETE FROM items WHERE native_item_id='sync-proof'")
        self.assertEqual(self.store.search_conversation_ids("排除异步"), ["c1"])
        self.store.delete_conversation("c1")
        self.assertEqual(self.store._db.execute(
            "SELECT count(*) FROM conversation_search_fallback_provenance"
        ).fetchone()[0], 0)
        self.assertEqual(self.store._db.execute(
            "SELECT count(*) FROM conversation_search_item_sources"
        ).fetchone()[0], 0)

    def test_sync_legacy_whitespace_is_normalized_like_python_strip(self):
        self.add("c1")
        self.item("c1", "t1", "async-old", "异步问题", phase=None,
                  delivery="async", final_text="异步问题")
        whitespace = "".join(chr(value) for value in range(0x3100) if chr(value).isspace())
        answer = whitespace + "同步空白归一" + whitespace
        self.item("c1", "t1", "sync-legacy", answer, phase=None)
        safe = self.store._final_text_locked("t1")
        self.assertEqual(safe, "同步空白归一")
        self.store._db.execute("UPDATE turns SET final_text=? WHERE id='t1'", (safe,))
        self.assertEqual(self.store.search_conversation_ids("空白归一"), ["c1"])
        self.store._backfill_conversation_search_locked()
        self.assertEqual(self.store.search_conversation_ids("空白归一"), ["c1"])

    def test_missing_provenance_fails_closed_instead_of_forgetting_rejections(self):
        self.store.close()
        with sqlite3.connect(self.path) as connection:
            connection.execute("DROP TABLE conversation_search_fallback_provenance")
        with self.assertRaisesRegex(AppServerProtocolError, "provenance is missing"):
            ConversationStore(self.path)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='conversation_search_fallback_provenance'"
            ).fetchone()[0], 0)

    def test_source_correction_revokes_all_historical_proofs_during_null_final(self):
        for changed_text in (False, True):
            conversation, turn, item = (f"c-null-{changed_text}", f"t-null-{changed_text}", f"item-null-{changed_text}")
            with self.subTest(changed_text=changed_text):
                self.add(conversation)
                self.item(conversation, turn, item, "历史来源独立撤销", phase=None,
                          final_text="历史来源独立撤销")
                self.assertIn(conversation, self.store.search_conversation_ids("独立撤销"))
                self.store._db.execute("UPDATE turns SET final_text=NULL WHERE id=?", (turn,))
                if changed_text:
                    self.store._db.execute("UPDATE items SET text='修订后的另一段异步题目' WHERE native_item_id=?", (item,))
                self.store._db.execute("UPDATE items SET delivery='async' WHERE native_item_id=?", (item,))
                self.store._db.execute("DELETE FROM items WHERE native_item_id=?", (item,))
                self.store._db.execute("UPDATE turns SET final_text='历史来源独立撤销' WHERE id=?", (turn,))
                self.assertNotIn(conversation, self.store.search_conversation_ids("独立撤销"))
                self.assertEqual(self.store._db.execute(
                    "SELECT trusted FROM conversation_search_fallback_provenance WHERE turn_id=?", (turn,)
                ).fetchone()[0], 0)

    def test_final_reselection_update_delete_and_fallback(self):
        self.add("c1")
        self.store._db.execute(
            "INSERT INTO turns(id,conversation_id,ordinal,status,final_text) "
            "VALUES('t1','c1',1,'completed','fallback word')"
        )
        self.assertEqual(self.store.search_conversation_ids("fallback"), ["c1"])
        self.item("c1", "t1", "one", "first word")
        self.item("c1", "t1", "two", "second word")
        self.assertEqual(self.store.search_matches_by_ids("word", ["c1"])[0]["item_id"], "two")
        self.store._db.execute("UPDATE items SET phase='commentary' WHERE native_item_id='two'")
        self.assertEqual(self.store.search_matches_by_ids("word", ["c1"])[0]["item_id"], "one")
        self.store._db.execute("DELETE FROM items WHERE native_item_id='one'")
        hit = self.store.search_matches_by_ids("fallback", ["c1"])[0]
        self.assertEqual(hit["kind"], "assistant")
        self.assertIsNone(hit["item_id"])
        self.store._db.execute("UPDATE turns SET final_text='changed fallback' WHERE id='t1'")
        self.assertEqual(self.store.search_conversation_ids("fallback"), ["c1"])
        self.assertEqual(self.store.search_conversation_ids("changed"), ["c1"])

    def native_item(self, conversation, turn, item, *, kind="agentMessage", phase="final_answer", text="类型纠正旧稿"):
        return self.store._upsert_item_locked(
            conversation, turn, {"id": item, "type": kind, "phase": phase, "text": text},
            completed=True, source="fixture", now="2026-10-02T00:00:00+00:00",
        )

    def test_superseded_native_kind_never_restores_cached_or_direct_answer(self):
        for phase in (None, "final_answer"):
            for saved in (False, True):
                suffix=f"{phase}-{saved}"
                c,t,i="kind-c-"+suffix,"kind-t-"+suffix,"kind-i-"+suffix
                with self.subTest(phase=phase,saved=saved):
                    self.add(c)
                    self.store._db.execute(
                        "INSERT INTO turns(id,conversation_id,ordinal,status,final_text) VALUES(?,?,1,'completed',?)",
                        (t,c,"类型纠正旧稿" if saved else None),
                    )
                    self.native_item(c,t,i,phase=phase)
                    self.store._db.execute("UPDATE turns SET final_text=NULL WHERE id=?",(t,))
                    self.native_item(c,t,i,kind="plan",phase=None,text="只是计划")
                    self.assertIsNone(self.store._final_text_locked(t))
                    self.assertNotIn(c,self.store.search_conversation_ids("类型纠正"))
                    self.store._db.execute("UPDATE turns SET final_text='类型纠正旧稿' WHERE id=?",(t,))
                    self.assertEqual(self.store._db.execute(
                        "SELECT trusted FROM conversation_search_fallback_provenance WHERE turn_id=? AND text='类型纠正旧稿'",(t,),
                    ).fetchone()[0],0)
                    self.store._db.execute("DELETE FROM items WHERE turn_id=? AND kind='plan'",(t,))
                    self.store._backfill_conversation_search_locked()
                    self.assertNotIn(c,self.store.search_conversation_ids("类型纠正"))
                    # Same fields as the original row: the native identity has
                    # changed even though the per-kind row is byte-identical.
                    self.assertTrue(self.native_item(c,t,i,phase=phase))
                    self.assertIn(c,self.store.search_conversation_ids("类型纠正"))
                    self.assertFalse(self.native_item(c,t,i,phase=phase))

    def test_alternate_effective_proof_survives_other_native_kind_correction(self):
        self.add("proof-c")
        self.store._db.execute(
            "INSERT INTO turns(id,conversation_id,ordinal,status,final_text) VALUES('proof-t','proof-c',1,'completed','类型纠正旧稿')"
        )
        self.native_item("proof-c","proof-t","first",phase=None)
        self.native_item("proof-c","proof-t","second",phase=None)
        self.native_item("proof-c","proof-t","second",kind="plan",phase=None,text="只是计划")
        self.assertEqual(self.store.search_conversation_ids("类型纠正"),["proof-c"])
        self.assertEqual(self.store._db.execute(
            "SELECT source_item_id FROM conversation_search_fallback_provenance WHERE turn_id='proof-t' AND trusted=1"
        ).fetchone()[0],"first")
        self.native_item("proof-c","proof-t","first",kind="plan",phase=None,text="只是计划")
        self.assertEqual(self.store.search_conversation_ids("类型纠正"),[])

    def test_deleted_independent_proof_survives_another_source_correction(self):
        for phase in (None,"final_answer"):
            for correction in ("plan","async"):
                suffix=f"{phase}-{correction}"
                c,t="independent-c-"+suffix,"independent-t-"+suffix
                with self.subTest(phase=phase,correction=correction):
                    self.add(c)
                    self.store._db.execute(
                        "INSERT INTO turns(id,conversation_id,ordinal,status,final_text) VALUES(?,?,1,'completed','类型纠正旧稿')",(t,c),
                    )
                    self.native_item(c,t,"A",phase=phase)
                    self.native_item(c,t,"B",phase=phase)
                    self.store._db.execute("DELETE FROM items WHERE turn_id=? AND native_item_id='A'",(t,))
                    if correction=="plan":
                        self.native_item(c,t,"B",kind="plan",phase=None,text="只是计划")
                    else:
                        self.store._upsert_item_locked(
                            c,t,{"id":"B","type":"agentMessage","phase":phase,"delivery":"async","text":"异步题目"},
                            completed=True,source="fixture",now="2026-10-02T00:01:00+00:00",
                        )
                    self.assertIn(c,self.store.search_conversation_ids("类型纠正"))
                    self.assertEqual(dict(self.store._db.execute(
                        "SELECT source_item_id,trusted FROM conversation_search_fallback_provenance WHERE turn_id=? AND text='类型纠正旧稿' AND source_item_id != ''",(t,),
                    ).fetchall()),{"A":1,"B":0})
                    self.store._db.execute("DELETE FROM items WHERE turn_id=? AND (kind='plan' OR delivery='async')",(t,))
                    self.store._backfill_conversation_search_locked()
                    self.assertIn(c,self.store.search_conversation_ids("类型纠正"))
                    # A has disappeared, but a later correction of its native
                    # identity can still revoke A's independent historical proof.
                    self.native_item(c,t,"A",kind="plan",phase=None,text="只是计划")
                    self.assertNotIn(c,self.store.search_conversation_ids("类型纠正"))

    def test_source_identity_backfill_and_deleted_superseder_do_not_revive_old_kind(self):
        self.add("old-kind-c")
        self.store._db.execute(
            "INSERT INTO turns(id,conversation_id,ordinal,status,final_text) VALUES('old-kind-t','old-kind-c',1,'completed','类型纠正旧稿')"
        )
        self.native_item("old-kind-c","old-kind-t","same")
        self.native_item("old-kind-c","old-kind-t","same",kind="plan",phase=None,text="只是计划")
        self.store.close()
        with sqlite3.connect(self.path) as db:
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'conversation_search%'").fetchall():
                db.execute("DROP TRIGGER "+row[0])
            for name in ("fts","documents","fallback_provenance","item_sources"):
                db.execute("DROP TABLE conversation_search_"+name)
            db.execute("DELETE FROM schema_meta WHERE key='conversation_search_version'")
        self.store=ConversationStore(self.path)
        self.assertEqual(self.store.search_conversation_ids("类型纠正"),[])
        self.store._db.execute("DELETE FROM items WHERE kind='plan'")
        self.store.close()
        self.store=ConversationStore(self.path)
        self.store._backfill_conversation_search_locked()
        self.assertEqual(self.store.search_conversation_ids("类型纠正"),[])

    def test_missing_source_identity_fails_closed(self):
        self.store.close()
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TABLE conversation_search_item_sources")
        with self.assertRaisesRegex(AppServerProtocolError,"source identities are missing"):
            ConversationStore(self.path)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='conversation_search_item_sources'"
            ).fetchone()[0],0)

    def test_scope_title_rename_reassignment_and_delete(self):
        self.add("ca", project="project-a", title="old name")
        self.add("cb", project="project-b", title="old name")
        self.assertEqual(set(self.store.search_conversation_ids("old")), {"ca", "cb"})
        self.assertEqual(self.store.search_conversation_ids("old", project_id="project-a"), ["ca"])
        self.assertEqual(self.store.search_conversation_ids("old", project_id=None), [])
        self.store.update_conversation_title("ca", "renamed")
        self.assertEqual(self.store.search_conversation_ids("old", project_id="project-a"), [])
        self.store._db.execute("UPDATE conversations SET project_id='project-b' WHERE id='ca'")
        self.assertEqual(set(self.store.search_conversation_ids("renamed", project_id="project-b")), {"ca"})
        self.store.delete_conversation("ca")
        self.assertEqual(self.store.search_conversation_ids("renamed"), [])

    def test_metadata_only_document_updates_do_not_rewrite_fts_text(self):
        self.add("c1", title="stable title")
        before = self.store._db.total_changes
        self.store._db.execute(
            "UPDATE conversations SET activity_at='2030-01-01T00:00:00+00:00' WHERE id='c1'"
        )
        # One derived-document metadata UPDATE; an FTS delete+insert would
        # add at least two more writes.  This protects streaming/list churn.
        self.assertEqual(self.store._db.total_changes - before, 2)
        self.assertEqual(self.store.search_conversation_ids("stable"), ["c1"])

    def test_first_backfill_reopen_and_plain_sql_writer(self):
        self.add("c1", title="legacy title")
        self.command("c1", "cmd", "legacy command")
        self.store.close()
        connection = sqlite3.connect(self.path)
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'conversation_search%'"
        ).fetchall():
            connection.execute("DROP TRIGGER " + row[0])
        connection.execute("DROP TABLE conversation_search_fts")
        connection.execute("DROP TABLE conversation_search_documents")
        connection.execute("DROP TABLE conversation_search_fallback_provenance")
        connection.execute("DROP TABLE conversation_search_item_sources")
        connection.execute("DELETE FROM schema_meta WHERE key='conversation_search_version'")
        connection.commit()
        connection.close()
        self.store = ConversationStore(self.path)
        self.assertEqual(self.store.search_conversation_ids("legacy"), ["c1"])
        direct = sqlite3.connect(self.path)
        direct.execute(
            "INSERT INTO commands(id,conversation_id,kind,state,text,payload_json,origin,created_at,updated_at) "
            "VALUES('plain','c1','steer','queued','plain writer','{}','user','z','z')"
        )
        direct.commit()
        direct.close()
        self.assertEqual(self.store.search_conversation_ids("writer"), ["c1"])
        self.store.close()
        self.store = ConversationStore(self.path)
        self.assertEqual(self.store.search_conversation_ids("writer"), ["c1"])

    def test_marker_with_missing_derived_tables_rebuilds(self):
        self.add("c1", title="recoverable search")
        self.command("c1", "cmd", "recoverable body")
        self.store.close()
        connection = sqlite3.connect(self.path)
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'conversation_search%'"
        ).fetchall():
            connection.execute("DROP TRIGGER " + row[0])
        # Keep conversation_search_version to exercise the damaged-marker path.
        connection.execute("DROP TABLE conversation_search_fts")
        connection.execute("DROP TABLE conversation_search_documents")
        connection.commit()
        connection.close()
        self.store = ConversationStore(self.path)
        self.assertEqual(self.store.search_conversation_ids("recoverable"), ["c1"])
        self.assertEqual(
            self.store._db.execute(
                "SELECT value FROM schema_meta WHERE key='conversation_search_version'"
            ).fetchone()["value"],
            "1",
        )

    def test_service_pages_are_bound_and_refiltered(self):
        for number in range(25):
            conversation_id = "c" + format(number, "03d")
            self.add(conversation_id, project="project-a")
            self.command(conversation_id, "cmd" + format(number, "03d"), "page needle")
        first = self.service.search_public_page(
            "needle", account_id="account-a", project_id="project-a"
        )
        self.assertEqual(len(first["values"]), 20)
        self.assertTrue(all("search_hit" in value for value in first["values"]))
        with self.assertRaises(ConversationPageCursorError):
            self.service.search_public_page(
                "other", account_id="account-a", project_id="project-a",
                cursor=first["next_cursor"],
            )
        with self.assertRaises(ConversationPageCursorError):
            self.service.search_public_page(
                "needle", account_id="account-b", project_id="project-a",
                cursor=first["next_cursor"],
            )
        removed = first["values"][0]["id"]
        self.store.update_conversation_title(removed, "unrelated")
        self.store._db.execute(
            "UPDATE commands SET text='unrelated' WHERE conversation_id=?", (removed,)
        )
        second = self.service.search_public_page(
            "needle", account_id="account-a", project_id="project-a",
            cursor=first["next_cursor"],
        )
        values = first["values"] + second["values"]
        self.assertNotIn(removed, [value["id"] for value in second["values"]])
        self.assertEqual(len({value["id"] for value in values}), len(values))
