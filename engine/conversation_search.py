"""Small, local-only helpers shared by conversation-history search paths."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass


SEARCH_TEXT_LIMIT = 120
SEARCH_TOKEN_LIMIT = 8
SNIPPET_LIMIT = 180
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


class ConversationSearchQueryError(ValueError):
    """A browser supplied search expression is not a supported literal query."""

    code = "invalid_query"


@dataclass(frozen=True)
class SearchQuery:
    text: str
    tokens: tuple[str, ...]

    @property
    def scope_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:24]


def normalize_search_query(value: object) -> SearchQuery:
    """Accept only a short, whitespace-separated literal AND query.

    SQL ``lower`` is intentionally used by the store for matching: its ASCII
    behaviour is exactly the case folding promised for English, while Chinese
    is compared as-is.  No FTS syntax is exposed to callers.
    """
    if not isinstance(value, str):
        raise ConversationSearchQueryError("搜索词必须是文本。")
    if any(
        unicodedata.category(character) == "Cc" and not character.isspace()
        for character in value
    ):
        raise ConversationSearchQueryError("搜索关键词包含不支持的控制字符。")
    text = value.strip()
    if not text:
        raise ConversationSearchQueryError("请输入搜索关键词。")
    if len(text) > SEARCH_TEXT_LIMIT:
        raise ConversationSearchQueryError("搜索关键词不能超过120个字符。")
    tokens = tuple(text.split())
    if len(tokens) > SEARCH_TOKEN_LIMIT:
        raise ConversationSearchQueryError("搜索关键词最多包含8个词。")
    return SearchQuery(text=" ".join(tokens), tokens=tokens)


def agent_message_text_key(value) -> tuple[str, str, str] | None:
    """Shared search-target/public-timeline equivalence for native copies."""
    text, phase = value.get("text"), value.get("phase")
    if not isinstance(text, str) or not isinstance(phase, str):
        return None
    return phase, "async" if value.get("delivery") == "async" else "sync", " ".join(text.split())


def realtime_agent_message(value) -> bool:
    item_id = value.get("native_item_id")
    return isinstance(item_id,str) and item_id.startswith("msg_") and value.get("source") in {
        "notification_delta","notification_item","item_read",None,
    }


def thread_read_agent_copy(value) -> bool:
    item_id = value.get("native_item_id")
    return isinstance(item_id,str) and re.fullmatch(r"item-\d+",item_id) is not None and value.get("source") in {"thread_read",None}


def _literal_ranges(text: str, tokens: tuple[str, ...]) -> list[tuple[int, int]]:
    """Find literal matches, merging overlap so a renderer cannot double-wrap."""
    # Match SQLite lower() exactly, without Unicode expansions shifting the
    # character offsets used for escaping/highlighting the original text.
    haystack = text.translate(_ASCII_LOWER)
    found: list[tuple[int, int]] = []
    for token in tokens:
        needle = token.translate(_ASCII_LOWER)
        start = 0
        while True:
            index = haystack.find(needle, start)
            if index < 0:
                break
            found.append((index, index + len(token)))
            start = index + max(1, len(token))
    found.sort()
    merged: list[tuple[int, int]] = []
    for start, end in found:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def safe_search_snippet(text: object, query: SearchQuery) -> tuple[str, list[list[int]]]:
    """Return a bounded plain-text excerpt and offsets relative to that excerpt."""
    value = text if isinstance(text, str) else ""
    ranges = _literal_ranges(value, query.tokens)
    if not ranges:
        return "", []
    if len(value) <= SNIPPET_LIMIT:
        return value, [[start, end] for start, end in ranges]
    first = ranges[0][0]
    start = max(0, first - 55)
    end = min(len(value), start + SNIPPET_LIMIT)
    start = max(0, end - SNIPPET_LIMIT)
    prefix = "…" if start else ""
    suffix = "…" if end < len(value) else ""
    snippet = prefix + value[start:end] + suffix
    offset = len(prefix) - start
    visible = [
        [max(start_at, start) + offset, min(end_at, end) + offset]
        for start_at, end_at in ranges
        if end_at > start and start_at < end
    ]
    return snippet, visible
