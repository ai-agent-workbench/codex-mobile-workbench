"""Synthetic history-search fixture, never connected to production or a model."""
from contextlib import ExitStack, contextmanager
from pathlib import Path
import tempfile
from unittest import mock

from engine.conversation_service import ConversationService
from shared.conversation_artifacts import ConversationArtifactService
from tests.store_fixtures import seeded_store


class NoRuntime:
    def __getattr__(self, name):
        raise AssertionError("search fixture must not call runtime: " + name)


@contextmanager
def search_fixture(count=45):
    import projects
    from routes import home, projects as project_routes, conversations, conversation_api

    with tempfile.TemporaryDirectory(prefix="codex-history-search-", dir=Path.home()) as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        store = seeded_store(root / "state" / "conversations.sqlite3")
        service = ConversationService(
            store, ConversationArtifactService(store, root / "artifacts"), NoRuntime(),
        )
        service.catalog = mock.AsyncMock(return_value={})
        settings = {"model": "fixture-model", "effort": "medium", "permission": "workspace-write", "service_tier": None}

        def add(cid, project, label, number, *, long=False):
            store.create_conversation(cid, title=label, cwd=str(workspace), project_id=project, settings=settings)
            store.bind_thread(cid, {"id": "thread-" + cid}, server_version="fixture")
            user = "共同关键词 提问专属 " + number
            final = "共同关键词 回答专属 " + number
            if long:
                user += "\n" + ("历史提问的长段落，仅用于测试展开。\n" * 180)
                final += "\n" + ("历史回复的长段落，仅用于测试展开。\n" * 180)
                final += "\n```text\n" + ("synthetic long code line\n" * 160) + "```"
            command = "command-" + cid
            turn = "turn-" + cid
            store.begin_command(cid, command, kind="start", text=user, payload={})
            store._db.execute(
                "INSERT INTO turns(id,conversation_id,ordinal,status,started_at,completed_at,final_text) "
                "VALUES(?,?,1,'completed','2026-10-01T00:00:00+00:00','2026-10-01T00:01:00+00:00',?)",
                (turn, cid, final),
            )
            store._db.execute("UPDATE commands SET state='completed',result_turn_id=? WHERE id=?", (turn, command))
            store._db.execute(
                "INSERT INTO items(conversation_id,turn_id,native_item_id,kind,status,phase,text,created_at,updated_at) "
                "VALUES(?,?,?,'agent_message','completed','final_answer',?,'2026-10-01T00:01:00+00:00','2026-10-01T00:01:00+00:00')",
                (cid, turn, "item-" + cid, final),
            )
            store._db.execute("UPDATE conversations SET state='idle' WHERE id=?", (cid,))

        for number in range(count):
            add("search-" + format(number, "03d"), "fixture", "手机搜索示例 " + format(number, "03d"),
                format(number, "03d"), long=number == 0)
        add("outside", "outside", "其他项目", "outside")
        add("temporary", None, "临时对话", "temporary")
        for cid, title in (("search-feff", "\ufeff字面字符检索\ufeff"),
                           ("search-no-feff", "字面字符检索")):
            store.create_conversation(cid, title=title, cwd=str(workspace), project_id="fixture", settings=settings)
            store.bind_thread(cid, {"id": "thread-" + cid}, server_version="fixture")
        store._db.execute(
            "INSERT INTO turns(id,conversation_id,ordinal,status,started_at,completed_at,final_text) "
            "VALUES('newest-turn','search-000',2,'completed','2026-10-01T00:02:00+00:00','2026-10-01T00:03:00+00:00','最新一轮短回答')"
        )
        fallback_id="search-fallback-proof"
        fallback_turn="turn-"+fallback_id
        fallback_text="缓存命中专用短语\n"+("保存的历史终稿长段落。\n"*180)+"\n```text\n"+("preserved historical source code\n"*160)+"```"
        store.create_conversation(fallback_id,title="独立来源缓存定位示例",cwd=str(workspace),project_id="fixture",settings=settings)
        store.bind_thread(fallback_id,{"id":"thread-"+fallback_id},server_version="fixture")
        store._db.execute(
            "INSERT INTO turns(id,conversation_id,ordinal,status,started_at,completed_at,final_text) VALUES(?,?,1,'completed','2026-10-01T00:00:00+00:00','2026-10-01T00:01:00+00:00',?)",
            (fallback_turn,fallback_id,fallback_text),
        )
        for item in ("proof-A","proof-B"):
            store._upsert_item_locked(fallback_id,fallback_turn,{"id":item,"type":"agentMessage","phase":"final_answer","text":fallback_text},completed=True,source="fixture",now="2026-10-01T00:01:00+00:00")
        store._db.execute("DELETE FROM items WHERE turn_id=? AND native_item_id='proof-A'",(fallback_turn,))
        store._upsert_item_locked(fallback_id,fallback_turn,{"id":"proof-B","type":"agentMessage","phase":"final_answer","text":"被更正的不同正文，不能替代缓存"},completed=True,source="fixture",now="2026-10-01T00:02:00+00:00")
        store._upsert_item_locked(fallback_id,fallback_turn,{"id":"proof-B","type":"plan","text":"只是计划"},completed=True,source="fixture",now="2026-10-01T00:03:00+00:00")
        store._db.execute("UPDATE conversations SET state='idle' WHERE id=?",(fallback_id,))
        copy_id="search-duplicate-final"
        copy_turn="turn-"+copy_id
        copy_text="副本精确定位关键词\n"+("通知与历史读取的同一终稿。\n"*180)+"\n```text\n"+("canonical native message code\n"*160)+"```"
        store.create_conversation(copy_id,title="通知与历史读取终稿副本",cwd=str(workspace),project_id="fixture",settings=settings)
        store.bind_thread(copy_id,{"id":"thread-"+copy_id},server_version="fixture")
        store._db.execute(
            "INSERT INTO turns(id,conversation_id,ordinal,status,started_at,completed_at,final_text) VALUES(?,?,1,'completed','2026-10-01T00:00:00+00:00','2026-10-01T00:01:00+00:00',?)",
            (copy_turn,copy_id,copy_text),
        )
        store._upsert_item_locked(copy_id,copy_turn,{"id":"msg_final","type":"agentMessage","phase":"final_answer","text":"修复前仅增量后缀"},completed=True,source="notification_delta",now="2026-10-01T00:01:00+00:00")
        store.hydrate_delta_baseline(copy_id,copy_turn,"msg_final",expected_revision=1,fresh_native_item={"id":"msg_final","type":"agentMessage","phase":"final_answer","text":copy_text})
        store._upsert_item_locked(copy_id,copy_turn,{"id":"item-1","type":"agentMessage","phase":"final_answer","text":copy_text},completed=True,source="thread_read",now="2026-10-01T00:01:00+00:00")
        store._db.execute("UPDATE conversations SET state='idle' WHERE id=?",(copy_id,))
        distinct_id="search-distinct-source"
        distinct_turn="turn-"+distinct_id
        distinct_text=copy_text.replace("副本精确定位关键词","独立消息定位关键词")
        store.create_conversation(distinct_id,title="不同原生来源消息",cwd=str(workspace),project_id="fixture",settings=settings)
        store.bind_thread(distinct_id,{"id":"thread-"+distinct_id},server_version="fixture")
        store._db.execute("INSERT INTO turns(id,conversation_id,ordinal,status,final_text) VALUES(?,?,1,'completed',?)",(distinct_turn,distinct_id,distinct_text))
        for item,source in (("msg_native","reconcile"),("item-1","thread_read")):
            store._upsert_item_locked(distinct_id,distinct_turn,{"id":item,"type":"agentMessage","phase":"final_answer","text":distinct_text},completed=True,source=source,now="2026-10-01T00:01:00+00:00")
        store._db.execute("UPDATE conversations SET state='idle' WHERE id=?",(distinct_id,))

        def get_project(pid):
            if pid not in {"fixture", "outside"}:
                raise ValueError("missing fixture project")
            return {"id": pid, "name": "搜索测试项目" if pid == "fixture" else "其他项目", "path": str(workspace)}

        try:
            with ExitStack() as patches:
                for module in (home, project_routes, conversations, conversation_api):
                    patches.enter_context(mock.patch.object(module, "require_auth", return_value=None))
                    patches.enter_context(mock.patch.object(module, "logged_in", return_value="fixture-admin"))
                    patches.enter_context(mock.patch.object(module, "conversation_service", return_value=service))
                patches.enter_context(mock.patch.object(projects, "recent", return_value=[get_project("fixture"), get_project("outside")]))
                patches.enter_context(mock.patch.object(projects, "get", side_effect=get_project))
                patches.enter_context(mock.patch.object(projects, "upload_directory", return_value=str(workspace)))
                settings_double = mock.Mock()
                settings_double.resolve.return_value = {"values": settings, "sources": {"model": "project"}}
                patches.enter_context(mock.patch.object(project_routes, "settings_store", return_value=settings_double))
                patches.enter_context(mock.patch("shared.security.notify_task_completion", side_effect=AssertionError("no notifications")))
                yield store, service
        finally:
            store.close()
