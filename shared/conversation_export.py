"""Pure formatting helpers for downloadable conversation transcripts."""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePath
from typing import Any
from urllib.parse import quote


_COMMAND_STATES = {
    "queued": "已排队，尚未送达模型",
    "rejected": "已拒绝，未送达模型",
    "delivery_unknown": "送达状态无法确认",
}
_ACTIVE_TURN_STATUSES = {"inProgress", "active", "queued", "stopping"}


def export_markdown(
    snapshot: dict[str, Any],
    *,
    project_name: str,
    exported_at: datetime,
) -> str:
    """Render a public conversation snapshot as a UTF-8 Markdown transcript.

    ``snapshot`` must already be the browser-safe projection.  Deliberately
    accepting only that projection ensures native protocol details, local
    paths, and managed artifact URLs cannot enter an export by accident.
    """
    conversation = _mapping(snapshot.get("conversation"))
    title = _one_line(conversation.get("title"), "未命名对话")
    state = _one_line(conversation.get("state"), "未知")
    lines = [
        "# " + title,
        "",
        "- 所属项目：" + _one_line(project_name, "项目名称不可用"),
        "- 导出时间：" + exported_at.astimezone().isoformat(timespec="seconds"),
        "- 对话状态：" + state,
        "- 导出范围：仅包含你的提问与中途补充、Codex 的最终回答，以及生成文件的安全摘要；不包含过程消息、思考、计划、工具参数、执行日志、路径或下载链接。",
    ]

    if snapshot.get("questions"):
        lines.append("- 中途提问：包含原生结构化问题及回答回执；秘密回答始终隐藏。")

    rendered_command_ids: set[str] = set()
    turns = sorted(
        (_mapping(turn) for turn in _list(snapshot.get("turns"))),
        key=lambda turn: _ordinal(turn.get("ordinal")),
    )
    for position, turn in enumerate(turns, start=1):
        ordinal = _ordinal(turn.get("ordinal")) or position
        lines.extend(("", "## 第 " + str(ordinal) + " 轮"))
        command_lines, command_ids = _render_commands(_list(turn.get("commands")))
        rendered_command_ids.update(command_ids)
        lines.extend(command_lines)
        lines.extend(_render_questions([
            _mapping(question) for question in _list(snapshot.get("questions"))
            if _mapping(question).get("turn_id") == turn.get("id")
        ]))

        final_items = [
            _mapping(item) for item in _list(turn.get("items"))
            if _mapping(item).get("kind") == "agent_message"
            and _mapping(item).get("phase") == "final_answer"
            and isinstance(_mapping(item).get("text"), str)
        ]
        if final_items:
            lines.extend(("", "### Codex", "", final_items[-1]["text"]))
        else:
            fallback = turn.get("final_text")
            if isinstance(fallback, str) and fallback:
                lines.extend(("", "### Codex", "", fallback))
            elif turn.get("status") in _ACTIVE_TURN_STATUSES:
                lines.extend(("", "### Codex", "", "该轮仍在运行，尚无最终回答。"))
            else:
                lines.extend(("", "### Codex", "", "该轮没有可导出的最终回答。"))
        lines.extend(_render_artifacts(_list(turn.get("artifacts"))))

    pending = [
        _mapping(command) for command in _list(snapshot.get("messages"))
        if _command_id(_mapping(command)) not in rendered_command_ids
    ]
    if pending:
        lines.extend(("", "## 尚未归入轮次的用户消息"))
        command_lines, _ignored = _render_commands(pending)
        lines.extend(command_lines)
    lines.extend(_render_artifacts(_list(snapshot.get("artifacts"))))
    return "\n".join(lines).rstrip() + "\n"


def content_disposition_filename(title: Any) -> str:
    """Return a safe attachment header without exposing title header syntax."""
    safe_title = _safe_filename_component(title)
    filename = safe_title + "-完整对话.md"
    # The ASCII fallback prevents legacy clients from interpreting raw Unicode
    # or punctuation as header syntax.  filename* preserves a readable name.
    return (
        'attachment; filename="conversation-export.md"; filename*=UTF-8\'\''
        + quote(filename, safe="")
    )


def _render_commands(commands: list[Any]) -> tuple[list[str], set[str]]:
    lines: list[str] = []
    command_ids: set[str] = set()
    for raw in commands:
        command = _mapping(raw)
        if command.get("kind") not in {"start", "steer"}:
            continue
        text = command.get("text")
        if not isinstance(text, str):
            continue
        command_id = _command_id(command)
        if command_id:
            command_ids.add(command_id)
        if command.get("origin") == "scheduled":
            heading = "### 系统 · 定时执行"
        elif command.get("origin") == "system":
            heading = "### 系统 · 自动继续"
        else:
            heading = "### 你" if command.get("kind") == "start" else "### 你 · 中途补充"
        lines.extend(("", heading, "", text))
        state = _COMMAND_STATES.get(command.get("state"))
        if state:
            lines.extend(("", "_消息状态：" + state + "_"))
    return lines, command_ids


def _render_questions(requests: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for request in requests:
        lines.extend(("", "### Codex · 中途提问", ""))
        answers = _mapping(request.get("answers_masked"))
        for raw in _list(request.get("questions")):
            question = _mapping(raw)
            lines.append(_one_line(question.get("question"), "问题"))
            for option in _list(question.get("options")):
                option = _mapping(option)
                lines.append("- " + _one_line(option.get("label"), "选项") + "：" + _one_line(option.get("description"), ""))
            if question.get("id") in answers:
                answer = "[秘密回答已隐藏]" if question.get("isSecret") else str(answers[question["id"]])
                lines.extend(("", "你的回答：" + answer))
        state = request.get("state")
        if state == "answered":
            status = "已按推荐项自动继续" if request.get("answer_source") == "deadline" else "已回答，模型已继续"
        else:
            status = {
                "awaiting_answer": "等待回答", "answer_claimed": "回答处理中",
                "response_sent": "回答已发送，等待确认", "resolved_before_send": "等待发送确认",
                "awaiting_activity": "等待模型继续", "closed": "问题已关闭",
                "expired": "问题已过期", "awaiting_reissue": "连接已中断，等待原生请求恢复",
                "delivery_unknown": "送达状态无法确认",
            }.get(state, "状态未知")
        lines.extend(("", "_问题状态：" + status + "_"))
    return lines


def _render_artifacts(artifacts: list[Any]) -> list[str]:
    safe = [_safe_artifact(_mapping(item)) for item in artifacts]
    safe = [item for item in safe if item is not None]
    if not safe:
        return []
    lines = ["", "### 生成文件"]
    for name, mime_type, size in safe:
        lines.append("- " + name + "（类型：" + mime_type + "；大小：" + size + " 字节）")
    return lines


def _safe_artifact(value: dict[str, Any]) -> tuple[str, str, str] | None:
    name = value.get("name")
    mime_type = value.get("mime_type")
    size = value.get("size")
    if not isinstance(name, str):
        return None
    clean_name = _safe_artifact_name(name)
    clean_type = _one_line(mime_type, "未知")
    clean_size = str(size) if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else "未知"
    return clean_name, clean_type, clean_size


def _safe_filename_component(value: Any) -> str:
    label = _one_line(value, "对话")
    label = label.replace("/", "-").replace("\\", "-")
    label = label.replace('"', "'")
    label = "".join(char for char in label if ord(char) >= 32 and ord(char) != 127)
    label = label.strip(" .") or "对话"
    return label[:96]


def _safe_artifact_name(value: str) -> str:
    normalized = value.replace("\\", "/")
    name = PurePath(normalized).name
    name = _one_line(name, "未命名文件")
    return name.replace("/", "-").replace("\\", "-")


def _one_line(value: Any, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    result = " ".join(value.splitlines()).strip()
    return result or fallback


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _command_id(value: dict[str, Any]) -> str:
    command_id = value.get("id")
    return command_id if isinstance(command_id, str) else ""


def _ordinal(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0
