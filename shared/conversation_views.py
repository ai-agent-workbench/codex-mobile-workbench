"""Small HTML helpers for native App Server conversation indexes."""

from __future__ import annotations

import html
from datetime import datetime, timedelta
from urllib.parse import quote


_STATE_LABELS = {
    "new": "新对话",
    "starting_thread": "正在开始",
    "starting_turn": "正在发送",
    "active": "执行中",
    "interrupting": "正在停止",
    "reconciling": "正在核对",
    "needs_user": "需要核对",
    "idle": "已就绪",
}
_ACTIVE_STATES = {
    "starting_thread", "starting_turn", "active", "interrupting", "reconciling"
}


def _time(value: str | None) -> str:
    try:
        return datetime.fromisoformat(str(value)).astimezone().strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return ""


def conversation_cards(
    values: list[dict],
    form_id: str,
    *,
    show_project: bool = False,
    project_names: dict[str, str] | None = None,
    group_dates: bool = False,
    visual_status: bool = False,
) -> str:
    project_names = project_names or {}
    cards: list[tuple[str, str]] = []
    for value in values:
        conversation_id = str(value.get("id") or "")
        state = str(value.get("state") or "")
        terminal_status = str(value.get("last_terminal_status") or "")
        failed = state == "idle" and terminal_status == "failed"
        interrupted = state == "idle" and terminal_status == "interrupted"
        completed = state == "idle" and bool(
            terminal_status == "completed" or value.get("last_terminal_at")
            or value.get("last_terminal_turn_id")
        )
        unread = bool(value.get("unread")) and state == "idle" and not failed and not interrupted
        time_value = _card_time(value)
        time_label = _time(time_value)
        if not visual_status and state == "idle" and value.get("last_terminal_at") and time_label:
            time_label = "完成 " + time_label
        meta = ([] if visual_status else [_STATE_LABELS.get(state, state)]) + [time_label]
        if show_project:
            project_id = str(value.get("project_id") or "")
            meta.insert(
                0,
                project_names.get(project_id, "项目对话")
                if project_id else "临时对话",
            )
        visual_class = (
            " running" if state in _ACTIVE_STATES
            else " needs" if state == "needs_user"
            else " failed" if failed
            else " stopped" if interrupted
            else " complete" if state == "idle"
            else ""
        ) if visual_status else ""
        if visual_status and unread:
            visual_class += " unread"
        visible_status = ("执行失败" if failed else "已停止" if interrupted else
                          "完成未读" if unread else "已完成" if completed else
                          _STATE_LABELS.get(state, state))
        card = (
            "<label class='manage-card conversation-index-card" + visual_class + "' data-id='"
            + html.escape(conversation_id, quote=True) + "'>"
            + ("<span class='conversation-state-rail' aria-hidden='true'></span>" if visual_status else "")
            + "<input class='manage-check' type='checkbox' form='" + html.escape(form_id, quote=True)
            + "' name='ids' value='" + html.escape(conversation_id, quote=True) + "'>"
            + "<a class='card' href='/conversation/" + quote(conversation_id) + "'"
            + (" data-conversation-state='" + html.escape(state, quote=True)
               + "' data-conversation-revision='" + html.escape(str(value.get("revision") or 0), quote=True) + "'" if visual_status else "")
            + ">"
            + ("<span class='conversation-card-copy'><span class='conversation-card-title'>" if visual_status else "")
            + ("<span class='pin-mark'>置顶</span>" if value.get("pinned") else "")
            + "<b>" + html.escape(str(value.get("title") or "新对话")) + "</b>"
            + ("</span>" if visual_status else "")
            + "<small>" + html.escape(" · ".join(item for item in meta if item)) + "</small>"
            + ("</span><span class='conversation-card-status'><i aria-hidden='true'></i>"
               + html.escape(visible_status) + "</span>" if visual_status else "")
            + "</a></label>"
        )
        group = ("置顶" if value.get("pinned") else
                 "已完成未读" if unread and terminal_status == "completed" else
                 _date_group(time_value))
        cards.append((group, card))
    if not group_dates:
        return "".join(card for _group, card in cards)
    sections: list[str] = []
    current = None
    for group, card in cards:
        if group != current:
            if current is not None:
                sections.append("</div></section>")
            sections.append(
                "<section class='conversation-date-group'><h2>"
                + html.escape(group) + "</h2><div class='conversation-date-cards'>"
            )
            current = group
        sections.append(card)
    if current is not None:
        sections.append("</div></section>")
    return "".join(sections)


def _card_time(value: dict) -> str | None:
    if value.get("state") == "idle" and value.get("last_terminal_at"):
        return value.get("last_terminal_at")
    return value.get("activity_at") or value.get("created_at")


def _date_group(value: str | None) -> str:
    try:
        day = datetime.fromisoformat(str(value)).astimezone().date()
    except (TypeError, ValueError):
        return "更早"
    today = datetime.now().astimezone().date()
    if day == today:
        return "今天"
    if day == today - timedelta(days=1):
        return "昨天"
    return "更早"


def pagination_controls(
    endpoint: str, next_cursor: str | None, *, group_dates: bool = False,
) -> str:
    if not next_cursor:
        return ""
    return (
        "<div class='conversation-pagination' data-conversation-pagination "
        "data-group-dates='" + ("true" if group_dates else "false") + "' data-endpoint='"
        + html.escape(endpoint, quote=True) + "' data-cursor='"
        + html.escape(next_cursor, quote=True) + "'>"
        "<button type='button' class='secondary' data-load-more>加载更多（再显示 20 条）</button>"
        "<p data-pagination-status role='status' aria-live='polite'></p></div>"
    )


def manage_bar(form_id: str, action: str) -> str:
    return (
        "<form id='" + html.escape(form_id, quote=True) + "' class='manage-bar' action='"
        + html.escape(action, quote=True) + "' method='post'>"
        "<button type='button' class='secondary' data-all>全选</button>"
        "<button name='action' value='pin'>置顶</button>"
        "<button name='action' value='unpin' class='secondary'>取消置顶</button>"
        "<button name='action' value='delete' class='danger'>删除</button>"
        "<button type='button' class='secondary' data-cancel><span data-count>0 项</span> 取消</button>"
        "</form>"
    )
