"""Small, account-scoped durable store for workbench settings.

This store is deliberately separate from the conversation projection.  Reads
are side-effect free: in particular, an account which has never saved a
setting does not cause a state directory or SQLite file to be created.
"""

from __future__ import annotations

import copy
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

from shared.config import (
    NEW_CONVERSATION_MODEL,
    NEW_CONVERSATION_PERMISSION,
    NEW_CONVERSATION_REASONING,
    STATE,
)


_DEFAULT_KEYS = {
    "model",
    "effort",
    "permission",
    "service_tier",
    "collaboration_mode",
    "followup",
    "completion_notification",
    "skills",
}
_PERMISSIONS = {"read-only", "workspace-write", "danger-full-access"}
_FOLLOWUPS = {"steer", "queue"}
_COLLABORATION_MODES = {"default", "plan"}
_ACCOUNT_LIMIT = 256
_PROJECT_LIMIT = 256
_SHORT_TEXT_LIMIT = 128
_JSON_LIMIT = 64 * 1024
_SAFE_PRESET_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


class SettingsConflict(ValueError):
    """Raised when a settings write uses an out-of-date revision."""


def system_defaults() -> dict[str, Any]:
    """Return a fresh copy of the built-in new-conversation defaults."""
    return {
        "model": NEW_CONVERSATION_MODEL,
        "effort": NEW_CONVERSATION_REASONING,
        "permission": NEW_CONVERSATION_PERMISSION,
        "service_tier": None,
        "collaboration_mode": "default",
        "followup": "steer",
        "completion_notification": True,
        "skills": [],
    }


def _error(message: str) -> ValueError:
    return ValueError(f"工作台设置无效：{message}")


def _identifier(value: Any, *, label: str, limit: int) -> str:
    if type(value) is not str or not value or len(value) > limit:
        raise _error(f"{label}必须是非空短文本")
    if value != value.strip() or any(ord(char) < 32 for char in value):
        raise _error(f"{label}格式不正确")
    return value


def _revision(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise _error("版本号不正确")
    return value


def _short_string(value: Any, *, label: str) -> str:
    if type(value) is not str or not value.strip() or len(value) > _SHORT_TEXT_LIMIT:
        raise _error(f"{label}必须是非空短文本")
    return value


def _defaults(value: Any) -> dict[str, Any]:
    """Validate and copy a partial defaults object."""
    if type(value) is not dict:
        raise _error("默认设置必须是对象")
    unknown = set(value) - _DEFAULT_KEYS
    if unknown:
        raise _error("默认设置包含不支持的字段")

    result: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"model", "effort"}:
            result[key] = _short_string(item, label=key)
        elif key == "permission":
            if type(item) is not str or item not in _PERMISSIONS:
                raise _error("权限设置不正确")
            result[key] = item
        elif key == "service_tier":
            if item is None:
                result[key] = None
            else:
                result[key] = _short_string(item, label="服务层级")
        elif key == "collaboration_mode":
            if type(item) is not str or item not in _COLLABORATION_MODES:
                raise _error("协作模式不正确")
            result[key] = item
        elif key == "followup":
            if type(item) is not str or item not in _FOLLOWUPS:
                raise _error("后续消息方式不正确")
            result[key] = item
        elif key == "completion_notification":
            if type(item) is not bool:
                raise _error("完成通知必须是布尔值")
            result[key] = item
        elif key == "skills":
            if type(item) is not list or len(item) > 32:
                raise _error("技能列表不正确")
            skills: list[str] = []
            for skill in item:
                if type(skill) is not str or not skill.strip() or len(skill) > 128:
                    raise _error("技能名称不正确")
                skills.append(skill)
            if len(set(skills)) != len(skills):
                raise _error("技能名称不能重复")
            result[key] = skills
    return result


def _personal(value: Any) -> str:
    if type(value) is not str or len(value) > 4000:
        raise _error("个人说明必须是不超过4000字符的文本")
    return value


def _presets(value: Any) -> list[dict[str, Any]]:
    if type(value) is not list or len(value) > 20:
        raise _error("预设列表不正确")
    result: list[dict[str, Any]] = []
    ids: set[str] = set()
    names: set[str] = set()
    for preset in value:
        if type(preset) is not dict or set(preset) != {"id", "name", "defaults"}:
            raise _error("预设格式不正确")
        preset_id = preset["id"]
        name = preset["name"]
        if type(preset_id) is not str or not _SAFE_PRESET_ID.fullmatch(preset_id):
            raise _error("预设标识不正确")
        if type(name) is not str or not name.strip() or len(name) > 24:
            raise _error("预设名称不正确")
        if preset_id in ids or name in names:
            raise _error("预设标识和名称不能重复")
        ids.add(preset_id)
        names.add(name)
        result.append({"id": preset_id, "name": name, "defaults": _defaults(preset["defaults"])})
    return result


def _json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > _JSON_LIMIT:
        raise _error("设置内容过大")
    return encoded


def _json_object(value: Any, *, fallback: dict[str, Any]) -> dict[str, Any]:
    if type(value) is not str:
        raise _error("已保存的数据损坏，未自动恢复默认")
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        raise _error("已保存的数据损坏，未自动恢复默认")
    if type(parsed) is not dict:
        raise _error("已保存的数据损坏")
    return _defaults(parsed)


def _json_list(value: Any, *, fallback: list[Any]) -> list[Any]:
    if type(value) is not str:
        raise _error("已保存的数据损坏，未自动恢复默认")
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        raise _error("已保存的数据损坏，未自动恢复默认")
    if type(parsed) is not list:
        raise _error("已保存的数据损坏")
    return _presets(parsed)



def _mode(value: Any) -> str:
    if type(value) is not str or value not in _COLLABORATION_MODES:
        raise _error("协作模式不正确")
    return value


def _native_account_modes(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if type(value) is not str:
        raise _error("原生执行模式数据损坏")
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise _error("原生执行模式数据损坏") from error
    if type(parsed) is not dict or set(parsed) - {"defaults", "presets"}:
        raise _error("原生执行模式数据损坏")
    result: dict[str, Any] = {}
    if "defaults" in parsed:
        result["defaults"] = _mode(parsed["defaults"])
    if "presets" in parsed:
        if type(parsed["presets"]) is not dict:
            raise _error("原生执行模式数据损坏")
        modes: dict[str, str] = {}
        for preset_id, mode in parsed["presets"].items():
            if type(preset_id) is not str or not _SAFE_PRESET_ID.fullmatch(preset_id):
                raise _error("原生执行模式数据损坏")
            modes[preset_id] = _mode(mode)
        result["presets"] = modes
    return result


def _without_mode(defaults: dict[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(value) for key, value in defaults.items() if key != "collaboration_mode"}


def _account_modes(defaults: dict[str, Any], presets: list[dict[str, Any]]) -> dict[str, Any] | None:
    result: dict[str, Any] = {}
    if "collaboration_mode" in defaults:
        result["defaults"] = defaults["collaboration_mode"]
    preset_modes = {preset["id"]: preset["defaults"]["collaboration_mode"] for preset in presets if "collaboration_mode" in preset["defaults"]}
    if preset_modes:
        result["presets"] = preset_modes
    return result or None


def _storage_presets(presets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"id": preset["id"], "name": preset["name"], "defaults": _without_mode(preset["defaults"])} for preset in presets]


class SettingsStore:
    """SQLite settings storage with optimistic (revision) concurrency control."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def close(self) -> None:
        """Compatibility no-op; every operation closes its own connection."""

    def _read_connection(self) -> sqlite3.Connection | None:
        # Do not use sqlite connect in normal mode for reads: it would create a
        # missing database.  Symlinks are never trusted for private state.
        if any(parent.is_symlink() for parent in self.path.parents):
            raise _error("设置目录路径不安全")
        try:
            self.path.lstat()
        except FileNotFoundError:
            return None
        if not self.path.is_file() or self.path.is_symlink():
            raise _error("设置数据库路径不安全")
        uri = f"file:{quote(str(self.path))}?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)

    def _ensure_private_parent(self) -> None:
        parent = self.path.parent
        if any(directory.is_symlink() for directory in self.path.parents):
            raise _error("设置目录路径不安全")
        # We only create parents which are absent.  Existing deployment state
        # is deliberately left untouched by this independent store.
        missing: list[Path] = []
        probe = parent
        while not probe.exists():
            missing.append(probe)
            probe = probe.parent
        if probe.is_symlink() or not probe.is_dir():
            raise _error("设置目录路径不安全")
        for directory in reversed(missing):
            directory.mkdir(mode=0o700)
        # Check every relevant component after creation; an attacker cannot
        # redirect the database through a symlinked directory.
        current = parent
        while current != probe.parent:
            if current.is_symlink() or not current.is_dir():
                raise _error("设置目录路径不安全")
            if current == probe:
                break
            current = current.parent

    def _write_connection(self) -> sqlite3.Connection:
        self._ensure_private_parent()
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise _error("设置数据库路径不安全")
        # Pre-create with an explicit restrictive mode before SQLite opens it.
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        if self.path.is_symlink() or not self.path.is_file():
            raise _error("设置数据库路径不安全")
        os.chmod(self.path, 0o600)
        connection = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS workbench_account_settings (
                    account TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,
                    defaults_json TEXT NOT NULL,
                    personal TEXT NOT NULL,
                    previous_personal TEXT,
                    presets_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workbench_project_settings (
                    account TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    defaults_json TEXT NOT NULL,
                    PRIMARY KEY (account, project_id)
                );
                """
            )
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(workbench_account_settings)"
                )
            }
            # Additive migration: deployments with the first store revision
            # keep their data, and older code ignores this extra column.
            if "previous_personal" not in columns:
                connection.execute(
                    "ALTER TABLE workbench_account_settings "
                    "ADD COLUMN previous_personal TEXT"
                )
            if "native_modes_json" not in columns:
                connection.execute("ALTER TABLE workbench_account_settings ADD COLUMN native_modes_json TEXT")
            if "native_modes_revision" not in columns:
                connection.execute("ALTER TABLE workbench_account_settings ADD COLUMN native_modes_revision INTEGER")
            project_columns = {row[1] for row in connection.execute("PRAGMA table_info(workbench_project_settings)")}
            if "native_mode" not in project_columns:
                connection.execute("ALTER TABLE workbench_project_settings ADD COLUMN native_mode TEXT")
            if "native_mode_revision" not in project_columns:
                connection.execute("ALTER TABLE workbench_project_settings ADD COLUMN native_mode_revision INTEGER")
        except BaseException:
            connection.close()
            raise
        return connection

    @staticmethod
    def _account_snapshot(row: sqlite3.Row | tuple[Any, ...] | None) -> dict[str, Any]:
        if row is None:
            return {"revision": 0, "defaults": {}, "personal": "", "previous_personal": None, "presets": []}
        revision = _revision(row[0])
        defaults = _json_object(row[1], fallback={})
        presets = _json_list(row[4], fallback=[])
        metadata_revision = row[6] if len(row) > 6 else None
        if metadata_revision is not None and _revision(metadata_revision) == revision:
            modes = _native_account_modes(row[5])
            if modes is not None:
                if "defaults" in modes:
                    defaults["collaboration_mode"] = modes["defaults"]
                by_id = modes.get("presets", {})
                for preset in presets:
                    if preset["id"] in by_id:
                        preset["defaults"]["collaboration_mode"] = by_id[preset["id"]]
        return {"revision": revision, "defaults": defaults, "personal": _personal(row[2]),
                "previous_personal": None if row[3] is None else _personal(row[3]), "presets": presets}

    @staticmethod
    def _project_snapshot(row: sqlite3.Row | tuple[Any, ...] | None) -> dict[str, Any]:
        if row is None:
            return {"revision": 0, "defaults": {}}
        revision = _revision(row[0])
        defaults = _json_object(row[1], fallback={})
        metadata_revision = row[3] if len(row) > 3 else None
        if metadata_revision is not None and _revision(metadata_revision) == revision and row[2] is not None:
            defaults["collaboration_mode"] = _mode(row[2])
        return {"revision": revision, "defaults": defaults}

    @staticmethod
    def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}

    def read_account(self, account: str | None) -> dict[str, Any]:
        if account is None:
            return self._account_snapshot(None)
        account = _identifier(account, label="账号", limit=_ACCOUNT_LIMIT)
        connection = self._read_connection()
        if connection is None:
            return self._account_snapshot(None)
        try:
            columns = self._columns(connection, "workbench_account_settings")
            metadata_json = "native_modes_json" if "native_modes_json" in columns else "NULL"
            metadata_revision = "native_modes_revision" if "native_modes_revision" in columns else "NULL"
            row = connection.execute(
                f"SELECT revision, defaults_json, personal, previous_personal, presets_json, {metadata_json}, {metadata_revision} "
                "FROM workbench_account_settings WHERE account = ?", (account,),
            ).fetchone()
            return copy.deepcopy(self._account_snapshot(row))
        finally:
            connection.close()

    def read_project(self, account: str | None, project_id: str) -> dict[str, Any]:
        if account is None:
            return {"revision": 0, "defaults": {}}
        account = _identifier(account, label="账号", limit=_ACCOUNT_LIMIT)
        project_id = _identifier(project_id, label="项目标识", limit=_PROJECT_LIMIT)
        connection = self._read_connection()
        if connection is None:
            return {"revision": 0, "defaults": {}}
        try:
            columns = self._columns(connection, "workbench_project_settings")
            native_mode = "native_mode" if "native_mode" in columns else "NULL"
            native_revision = "native_mode_revision" if "native_mode_revision" in columns else "NULL"
            row = connection.execute(
                f"SELECT revision, defaults_json, {native_mode}, {native_revision} FROM workbench_project_settings "
                "WHERE account = ? AND project_id = ?", (account, project_id),
            ).fetchone()
            return copy.deepcopy(self._project_snapshot(row))
        finally:
            connection.close()

    def save_account(self, account: str, expected_revision: int, changes: dict[str, Any]) -> dict[str, Any]:
        account = _identifier(account, label="账号", limit=_ACCOUNT_LIMIT)
        expected_revision = _revision(expected_revision)
        if type(changes) is not dict or not changes:
            raise _error("账号设置更新必须是非空对象")
        if set(changes) - {"defaults", "personal", "presets"}:
            raise _error("账号设置包含不支持的字段")
        normalized: dict[str, Any] = {}
        if "defaults" in changes:
            normalized["defaults"] = _defaults(changes["defaults"])
        if "personal" in changes:
            normalized["personal"] = _personal(changes["personal"])
        if "presets" in changes:
            normalized["presets"] = _presets(changes["presets"])

        connection = self._write_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision, defaults_json, personal, previous_personal, presets_json, native_modes_json, native_modes_revision "
                "FROM workbench_account_settings WHERE account = ?", (account,)
            ).fetchone()
            current = self._account_snapshot(row)
            if current["revision"] != expected_revision:
                raise SettingsConflict("设置已被其他操作更新，请刷新后重试")
            defaults = normalized.get("defaults", current["defaults"])
            personal = normalized.get("personal", current["personal"])
            presets = normalized.get("presets", current["presets"])
            next_value = {
                "revision": current["revision"] + 1,
                "defaults": defaults,
                "personal": personal,
                "previous_personal": (
                    current["personal"]
                    if "personal" in normalized and personal != current["personal"]
                    else current["previous_personal"]
                ),
                "presets": presets,
            }
            native_modes = _account_modes(defaults, presets)
            storage_defaults = _without_mode(defaults)
            storage_presets = _storage_presets(presets)
            connection.execute(
                "INSERT INTO workbench_account_settings "
                "(account, revision, defaults_json, personal, previous_personal, presets_json, native_modes_json, native_modes_revision) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(account) DO UPDATE SET revision = excluded.revision, "
                "defaults_json = excluded.defaults_json, personal = excluded.personal, "
                "previous_personal = excluded.previous_personal, presets_json = excluded.presets_json, "
                "native_modes_json = excluded.native_modes_json, native_modes_revision = excluded.native_modes_revision",
                (account, next_value["revision"], _json(storage_defaults), personal,
                 next_value["previous_personal"], _json(storage_presets),
                 None if native_modes is None else _json(native_modes), next_value["revision"] if native_modes is not None else None),
            )
            connection.execute("COMMIT")
            os.chmod(self.path, 0o600)
            return copy.deepcopy(next_value)
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def save_project(
        self, account: str, project_id: str, expected_revision: int, defaults: dict[str, Any]
    ) -> dict[str, Any]:
        account = _identifier(account, label="账号", limit=_ACCOUNT_LIMIT)
        project_id = _identifier(project_id, label="项目标识", limit=_PROJECT_LIMIT)
        expected_revision = _revision(expected_revision)
        defaults = _defaults(defaults)

        connection = self._write_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision, defaults_json, native_mode, native_mode_revision FROM workbench_project_settings "
                "WHERE account = ? AND project_id = ?", (account, project_id),
            ).fetchone()
            current = self._project_snapshot(row)
            if current["revision"] != expected_revision:
                raise SettingsConflict("设置已被其他操作更新，请刷新后重试")
            merged = defaults
            next_value = {"revision": current["revision"] + 1, "defaults": merged}
            native_mode = merged.get("collaboration_mode")
            connection.execute(
                "INSERT INTO workbench_project_settings (account, project_id, revision, defaults_json, native_mode, native_mode_revision) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(account, project_id) DO UPDATE SET "
                "revision = excluded.revision, defaults_json = excluded.defaults_json, native_mode = excluded.native_mode, "
                "native_mode_revision = excluded.native_mode_revision",
                (account, project_id, next_value["revision"], _json(_without_mode(merged)), native_mode,
                 next_value["revision"] if native_mode is not None else None),
            )
            connection.execute("COMMIT")
            os.chmod(self.path, 0o600)
            return copy.deepcopy(next_value)
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def resolve(self, account: str | None, project_id: str | None = None) -> dict[str, Any]:
        values = system_defaults()
        sources = {key: "system" for key in values}
        if account is None:
            return {
                "values": copy.deepcopy(values),
                "sources": copy.deepcopy(sources),
                "personal": "",
                "account_revision": 0,
                "project_revision": 0,
            }
        account_value = self.read_account(account)
        for key, value in account_value["defaults"].items():
            # Persisted data was written through _defaults.  Keep resolution
            # defensive if a manually-corrupted database contains extra keys.
            if key in values:
                values[key] = copy.deepcopy(value)
                sources[key] = "account"
        project_value = {"revision": 0, "defaults": {}}
        if project_id is not None:
            project_value = self.read_project(account, project_id)
            for key, value in project_value["defaults"].items():
                if key in values:
                    values[key] = copy.deepcopy(value)
                    sources[key] = "project"
        return copy.deepcopy({
            "values": values,
            "sources": sources,
            "personal": account_value["personal"],
            "account_revision": account_value["revision"],
            "project_revision": project_value["revision"],
        })


def settings_store() -> SettingsStore:
    """Build the default store without caching, so callers/tests can patch state."""
    return SettingsStore(STATE / "workbench-settings.sqlite3")
