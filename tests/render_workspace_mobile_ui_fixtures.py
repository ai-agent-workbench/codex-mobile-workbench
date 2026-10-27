"""Render real history, settings, and notifications routes for mobile browser checks."""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import projects
from routes import home, misc, projects as project_routes
from shared.conversation_views import conversation_cards


def render() -> dict[str, str]:
    now = datetime.now().astimezone().isoformat()
    with mock.patch.object(home, "require_auth", return_value=None), \
         mock.patch.object(home, "conversation_service") as service, \
         mock.patch.object(projects, "recent", return_value=[]):
        def values(prefix, count, activity_at):
            return [{
                "id": prefix + str(index),
                "title": "用于核对分页卡片 " + str(index), "state": "active" if index == 0 else "idle",
                "project_id": None, "pinned": False, "activity_at": activity_at,
            } for index in range(count)]
        initial = values("initial-", 20, now)
        service.return_value.list_public_page.return_value = {"values": initial, "next_cursor": "fixture-next-1"}
        history = home.history(None).body.decode("utf-8")
        history_more_1 = conversation_cards(
            values("more-", 20, now), "history-conversation-manage", show_project=True,
            group_dates=True, visual_status=True,
        )
        history_more_2 = conversation_cards(
            values("last-today-", 1, now)
            + values("last-yesterday-", 2, (datetime.now().astimezone() - timedelta(days=1)).isoformat())
            + values("last-old-", 2, (datetime.now().astimezone() - timedelta(days=5)).isoformat()),
            "history-conversation-manage", show_project=True, group_dates=True,
            visual_status=True,
        )
        service.return_value.list_public_page.return_value = {"values": [], "next_cursor": None}
        history_empty = home.history(None).body.decode("utf-8")

    with mock.patch.object(project_routes, "require_auth", return_value=None), \
         mock.patch.object(project_routes, "logged_in", return_value="account-a"), \
         mock.patch.object(project_routes, "conversation_service") as service, \
         mock.patch.object(projects, "get", return_value={"id": "fixture", "name": "分页测试项目", "path": "/home/workbench/fixture"}), \
         mock.patch.object(projects, "upload_directory", return_value="/home/workbench/fixture"):
        service.return_value.list_public_page.return_value = {"values": initial, "next_cursor": "fixture-next-1"}
        service.return_value.catalog = mock.AsyncMock(return_value={})
        project = asyncio.run(project_routes.project_view(object(), "fixture")).body.decode("utf-8")
        project_more_1 = conversation_cards(
            values("pmore-", 20, now), "project-conversation-manage",
            group_dates=True, visual_status=True,
        )
        project_more_2 = conversation_cards(
            values("plast-today-", 1, now)
            + values("plast-yesterday-", 2, (datetime.now().astimezone() - timedelta(days=1)).isoformat())
            + values("plast-old-", 2, (datetime.now().astimezone() - timedelta(days=5)).isoformat()),
            "project-conversation-manage",
            group_dates=True, visual_status=True,
        )

    request = object()
    with mock.patch.object(misc, "require_auth", return_value=None), \
         mock.patch.object(misc, "logged_in", return_value="admin"), \
         mock.patch.object(misc, "load_accounts", return_value={"admin": {"role": "admin"}}):
        settings_admin = misc.settings(request).body.decode("utf-8")
    with mock.patch.object(misc, "require_auth", return_value=None), \
         mock.patch.object(misc, "logged_in", return_value="user"), \
         mock.patch.object(misc, "load_accounts", return_value={"user": {"role": "user"}}):
        settings_user = misc.settings(request).body.decode("utf-8")

    topic_file = mock.Mock()
    topic_file.read_text.return_value = ""
    with mock.patch.object(misc, "require_auth", return_value=None), \
         mock.patch.object(misc, "logged_in", return_value="admin"), \
         mock.patch.object(misc, "load_accounts", return_value={"admin": {"role": "admin"}}), \
         mock.patch.object(misc, "NTFY_TOPIC_FILE", topic_file):
        notifications_admin = misc.notifications(request).body.decode("utf-8")
    with mock.patch.object(misc, "require_auth", return_value=None), \
         mock.patch.object(misc, "logged_in", return_value="user"), \
         mock.patch.object(misc, "load_accounts", return_value={"user": {"role": "user"}}), \
         mock.patch.object(misc, "NTFY_TOPIC_FILE", topic_file):
        notifications_user = misc.notifications(request).body.decode("utf-8")
    with mock.patch.object(home, "require_auth", return_value=None), \
         mock.patch.object(home, "conversation_service") as service, \
         mock.patch.object(home, "dot_available", return_value=True), \
         mock.patch.object(projects, "recent", return_value=[{"id": "fixture", "name": "用于检查两行标题的手机工作台测试项目"}]):
        service.return_value.list_public.return_value = []
        homepage = home.home(None).body.decode("utf-8")
    with mock.patch.object(misc, "require_auth", return_value=None):
        tts = misc.tts_settings(None).body.decode("utf-8")
        sound = misc.completion_sound_settings(None).body.decode("utf-8")
    return {
        "home": homepage, "tts": tts, "sound": sound,
        "project": project, "project_more_1": project_more_1, "project_more_2": project_more_2,
        "history": history, "history_more_1": history_more_1, "history_more_2": history_more_2, "history_empty": history_empty,
        "settings_admin": settings_admin, "settings_user": settings_user,
        "notifications_admin": notifications_admin, "notifications_user": notifications_user,
    }


if __name__ == "__main__":
    print(json.dumps(render()))
