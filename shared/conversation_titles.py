"""First-turn title metadata; generation stays in the conversation's turn."""

TITLE_TOOL_NAME = "set_conversation_title"
TITLE_CONTEXT = "请为当前对话生成一个标题。"
TITLE_DESCRIPTION = (TITLE_CONTEXT + "仅用于新对话首轮仍在执行时的自动命名；已有手动标题或首轮结束后不会覆盖。"
                     "返回的 success/changed 只表示标题是否保存，不影响任务执行或文件交付。")
TITLE_SETTING = "_ai_initial_title"


def conversation_title_dynamic_tool():
    return {
        "type": "function",
        "name": TITLE_TOOL_NAME,
        "description": TITLE_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
            "additionalProperties": False,
        },
    }
