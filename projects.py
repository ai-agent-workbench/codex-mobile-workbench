import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from shared.config import PROJECTS_DIR


ROOT = PROJECTS_DIR
META_FILE = ".codex-project.json"
UPLOAD_DIR = "手机上传资料"


def ensure_root():
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)


def _safe_name(name):
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", name.strip()).strip(" .")
    if not cleaned or cleaned in {".", ".."} or len(cleaned) > 80:
        raise ValueError("项目名称无效或过长。")
    return cleaned


def create(name, template="blank"):
    ensure_root()
    safe_name = _safe_name(name)
    directory = ROOT / safe_name
    if directory.exists():
        raise ValueError("同名项目已经存在。")
    directory.mkdir(mode=0o700)
    now = datetime.now(timezone.utc).isoformat()
    metadata = {"id": safe_name, "name": name.strip(), "template": template, "created": now, "updated": now}
    (directory / META_FILE).write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / "项目说明.md").write_text("# " + name.strip() + "\n\n## 目标\n\n请在这里记录项目目标和重要背景。\n", encoding="utf-8")
    (directory / "进度与待办.md").write_text("# 进度与待办\n\n## 当前进展\n\n项目已创建。\n\n## 下一步\n\n- 补充项目目标或上传资料\n", encoding="utf-8")
    (directory / "工作记录.md").write_text("# 工作记录\n\n- " + now + " 创建项目。\n", encoding="utf-8")
    (directory / UPLOAD_DIR).mkdir()
    if template == "document":
        (directory / "资料").mkdir()
        (directory / "成果").mkdir()
    elif template == "organize":
        (directory / "待整理").mkdir()
        (directory / "已整理").mkdir()
    metadata["path"] = str(directory)
    return metadata


def get(project_id):
    ensure_root()
    directory = (ROOT / project_id).resolve()
    if directory.parent != ROOT.resolve() or not directory.is_dir():
        raise ValueError("项目不存在。")
    metadata = json.loads((directory / META_FILE).read_text(encoding="utf-8"))
    metadata["path"] = str(directory)
    return metadata


def recent():
    ensure_root()
    items = []
    for directory in ROOT.iterdir():
        meta = directory / META_FILE
        if directory.is_dir() and meta.is_file():
            try:
                item = json.loads(meta.read_text(encoding="utf-8"))
                item["path"] = str(directory)
                items.append(item)
            except (OSError, json.JSONDecodeError):
                continue
    items.sort(key=lambda item: item.get("updated", item["created"]), reverse=True)
    return sorted(items, key=lambda item: (not item.get("pinned", False), item.get("sort_order", 0)))


def upload_directory(project_id):
    directory = Path(get(project_id)["path"]) / UPLOAD_DIR
    directory.mkdir(mode=0o700, exist_ok=True)
    return str(directory)


def update_order(project_ids, pinned=None):
    for index, project_id in enumerate(project_ids):
        metadata = get(project_id)
        directory = Path(metadata.pop("path"))
        metadata["sort_order"] = index
        if pinned is not None:
            metadata["pinned"] = pinned
        (directory / META_FILE).write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")


def delete(project_id):
    directory = Path(get(project_id)["path"])
    shutil.rmtree(directory)


def touch(project_id, task_id=None, prompt=""):
    """Update project recency and its latest task pointer.

    Task prompts already live in the task store.  Do not duplicate them into
    工作记录.md: that file is reserved for structured publish/maintenance
    records and may not receive raw or credential-bearing user input.
    ``prompt`` remains accepted for compatibility with existing callers.
    """
    metadata = get(project_id)
    directory = Path(metadata["path"])
    metadata.pop("path", None)
    metadata["updated"] = datetime.now(timezone.utc).isoformat()
    if task_id:
        metadata["last_task"] = task_id
    (directory / META_FILE).write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
