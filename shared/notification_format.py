"""Legacy compact completion notices, without final-reply excerpts."""
from __future__ import annotations

import re
from typing import Any

_LABEL_LIMIT = 48
_MARKDOWN_ESCAPE = re.compile(r"([\\`*_{}\[\]()<>#+\-.!|~])")
_STATUS = {
    "completed": ("回复已完成", "white_check_mark"),
    "failed": ("回复失败", "warning"),
    "interrupted": ("回复已停止", "stop_sign"),
}


def _label(value: Any, fallback: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip() or fallback
    if len(text) > _LABEL_LIMIT:
        text = text[:_LABEL_LIMIT - 1].rstrip() + "…"
    # The approved sender still uses Markdown; labels must remain literal.
    return _MARKDOWN_ESCAPE.sub(r"\\\1", text)


def format_completion_notification(
    *,
    status: Any,
    conversation_title: Any,
    project_name: Any,
    ordinal: Any,
    final_text: Any,
) -> tuple[str, str, str]:
    """Keep the caller contract, but deliberately never include final_text."""
    title, tag = _STATUS.get(str(status), ("回复已结束", "information_source"))
    try:
        round_number = int(ordinal)
    except (TypeError, ValueError):
        round_number = 0
    round_label = "第 " + str(round_number) + " 轮" if round_number else "当前轮次"
    body = "  \n".join((
        "项目：" + _label(project_name, "项目对话"),
        "任务：" + _label(conversation_title, "未命名任务"),
        "轮次：" + round_label,
        "请打开工作台查看详细结果。",
    ))
    return title, body, tag
