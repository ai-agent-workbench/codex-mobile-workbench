"""Safe HTML for local, scoped conversation-history search."""
from __future__ import annotations

import html
import re
from urllib.parse import quote, urlencode

from engine.conversation_search import (
    ConversationSearchQueryError, normalize_search_query,
)
from shared.conversation_views import _time
from shared.conversation_pagination import ConversationPageCursorError


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")
_KINDS = {"title": "标题", "user": "提问", "assistant": "最终回答"}
_SEARCH_ICON = (
    "<svg class='search-icon' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2'"
    " stroke-linecap='round' aria-hidden='true'><circle cx='10.5' cy='10.5' r='6.5'/>"
    "<path d='m16 16 4.5 4.5'/></svg>"
)


def initial_search(service, query: str, *, account_id, project_id=..., cursor=None):
    """Render an initial search without failing the underlying history page."""
    if not query.strip():
        return {"values": [], "next_cursor": None}, ""
    try:
        parsed = normalize_search_query(query)
        return service.search_public_page(
            parsed, account_id=account_id,
            project_id=project_id, cursor=cursor,
        ), ""
    except (ConversationSearchQueryError, ConversationPageCursorError) as error:
        return {"values": [], "next_cursor": None}, str(error)


def highlighted_text(text: str, ranges) -> str:
    """Escape all content before adding trusted highlight markup."""
    intervals = []
    for pair in ranges if isinstance(ranges, list) else []:
        if (isinstance(pair, (list, tuple)) and len(pair) == 2
                and all(type(value) is int for value in pair)
                and 0 <= pair[0] < pair[1] <= len(text)):
            intervals.append((pair[0], pair[1]))
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    parts, position = [], 0
    for start, end in merged:
        parts.extend((html.escape(text[position:start]),
                      "<mark>" + html.escape(text[start:end]) + "</mark>"))
        position = end
    parts.append(html.escape(text[position:]))
    return "".join(parts)


def search_cards(values: list[dict], *, project_names=None, show_project=False) -> str:
    project_names = project_names or {}
    cards = []
    for value in values:
        conversation_id = str(value.get("id") or "")
        if not _ID.fullmatch(conversation_id):
            continue
        hit = value.get("search_hit") or {}
        kind = hit.get("kind")
        path = "/conversation/" + quote(conversation_id, safe="._:-")
        turn = str(hit.get("turn_id") or "")
        command = str(hit.get("command_id") or "")
        item = str(hit.get("item_id") or "")
        if kind == "user" and _ID.fullmatch(command):
            path += "?command=" + quote(command, safe="._:-")
        elif kind == "assistant" and _ID.fullmatch(turn):
            path += "?turn=" + quote(turn, safe="._:-")
            if _ID.fullmatch(item):
                path += "&item=" + quote(item, safe="._:-")
            elif not item:
                # The timeline gives a saved turn.final_text the same stable
                # fallback marker, so it can also be opened as an exact hit.
                path += "&item=fallback"
        meta = [_KINDS.get(kind, "匹配内容"), _time(value.get("activity_at"))]
        if show_project:
            project_id = value.get("project_id")
            meta.insert(0, project_names.get(project_id, "项目对话") if project_id else "临时对话")
        snippet = str(hit.get("snippet") or "")
        cards.append(
            "<a class='card history-search-card' href='" + html.escape(path, quote=True)
            + "' data-search-hit-kind='" + html.escape(str(kind), quote=True) + "'>"
            + "<b>" + html.escape(str(value.get("title") or "新对话")) + "</b>"
            + "<small>" + html.escape(" · ".join(part for part in meta if part)) + "</small>"
            + "<span class='history-search-snippet'>"
            + highlighted_text(snippet, hit.get("highlights")) + "</span></a>"
        )
    return "".join(cards)


def search_controls(action: str, endpoint: str, query: str, page: dict, *,
                    error="", project_names=None, show_project=False,
                    compact=False) -> str:
    active = bool(query.strip())
    try:
        # Use the same query that produced the results and bound the cursor.
        # Raw edge whitespace can exceed maxlength without making it invalid.
        query = normalize_search_query(query).text
    except ConversationSearchQueryError:
        query = query.strip()[:120]
    values = page["values"]
    cursor = page["next_cursor"] or ""
    status = error or ("已显示 " + str(len(values)) + " 条匹配对话" if values
                       else "没有找到匹配的对话" if active else "")
    clear = (
        "<a href='" + html.escape(action, quote=True) + "' data-search-clear>"
        + ("取消" if compact else "清除搜索") + "</a>"
    )
    form = (
        "<form method='get' action='" + html.escape(action, quote=True)
        + "' class='history-search-form' data-search-form role='search'>"
        + "<div class='history-search-field search-field'>"
        + "<input type='search' name='q' maxlength='120' enterkeyhint='search'"
        + " aria-label='搜索标题、提问和回答' placeholder='"
        + ("搜索对话" if compact else "搜索标题、提问和回答") + "' value='"
        + html.escape(query, quote=True) + "' data-history-search-input>"
        + "<button type='submit' aria-label='提交搜索' title='搜索'>" + _SEARCH_ICON
        + "</button></div>" + (clear if compact else "")
        + "</form>"
    )
    if compact:
        # Native details keeps the low-frequency search usable without JavaScript.
        form = (
            "<header class='history-page-head'><h1>历史对话</h1>"
            "<div class='history-page-actions'><details class='history-search-disclosure'"
            " data-search-disclosure" + (" open" if active else "") + ">"
            "<summary aria-label='搜索对话' title='搜索对话' data-search-toggle>"
            + _SEARCH_ICON + "</summary>" + form + "</details>"
            "<a class='history-new-conversation' href='/quick' aria-label='新对话'>"
            "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2'"
            " stroke-linecap='round' aria-hidden='true'><path d='M12 5v14M5 12h14'/></svg>"
            "<span>新对话</span></a></div></header>"
        )
    return (
        "<div class='history-search" + (" history-search-compact" if compact else "")
        + "' data-history-search data-search-endpoint='"
        + html.escape(endpoint, quote=True) + "' data-search-query='"
        + html.escape(query, quote=True) + "'>"
        + form
        + "<div class='history-search-panel' data-search-panel" + ("" if active else " hidden") + ">"
        + "<div class='history-search-summary'><p data-search-status role='status' aria-live='polite'>"
        + html.escape(status) + "</p>" + ("" if compact else clear) + "</div>"
        + "<section class='history-search-results' data-search-results aria-label='搜索结果'>"
        + search_cards(values, project_names=project_names, show_project=show_project) + "</section>"
        + "<a class='button secondary history-search-more' data-search-more href='"
        + html.escape(action + "?" + urlencode({"q": query, "search_cursor": cursor}), quote=True)
        + "' data-cursor='"
        + html.escape(cursor, quote=True) + "'" + ("" if cursor else " hidden")
        + ">加载更多</a></div></div>"
    )
