"""Bounded project documents with optimistic edits and private recovery points."""
from __future__ import annotations

import difflib
import fcntl
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

import projects
from shared.config import STATE
from shared.file_utils import allowed_path
from shared.workbench_settings import SettingsConflict

DOCUMENTS = ('AGENTS.md', '项目说明.md')
MAX_BYTES = 128 * 1024


def _target(project_id, name):
    if name not in DOCUMENTS:
        raise ValueError('只能编辑项目规则或项目说明。')
    root = allowed_path(projects.get(project_id)['path'], require_directory=True)
    path = root / name
    if path.is_symlink() or path.resolve().parent != root:
        raise ValueError('不允许编辑符号链接或项目外文件。')
    return path


def _read(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {'content': '', 'version': 'absent', 'exists': False, 'mode': 0o600}
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
            raise ValueError('规则必须是大小不超过 128 KB 的独立普通文件。')
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('规则文件过大。')
    try:
        content = raw.decode('utf-8')
    except UnicodeError as exc:
        raise ValueError('规则文件不是 UTF-8 文本。') from exc
    return {'content': content, 'version': hashlib.sha256(raw).hexdigest(),
            'exists': True, 'mode': stat.S_IMODE(info.st_mode)}


def _bucket(path):
    bucket = STATE / 'rule-history' / hashlib.sha256(str(path).encode()).hexdigest()
    if any(component.is_symlink() for component in (bucket, *bucket.parents)):
        raise ValueError('规则恢复目录路径不安全。')
    return bucket


def read_rule(project_id, name):
    path = _target(project_id, name)
    result = _read(path)
    result.pop('mode')
    result['previous'] = None
    backup = _bucket(path) / 'previous.json'
    if backup.exists():
        if backup.is_symlink() or backup.stat().st_size > MAX_BYTES * 8:
            raise ValueError('规则恢复点不可用。')
        try:
            old = json.loads(backup.read_text())
            if (not isinstance(old, dict) or type(old.get('existed')) is not bool
                    or not isinstance(old.get('content'), str) or not isinstance(old.get('result_version'), str)):
                raise ValueError('invalid history')
            _content(old['content'])
        except (ValueError, UnicodeError, TypeError) as exc:
            raise ValueError('规则恢复点损坏，未自动恢复。') from exc
        if old['result_version'] == result['version']:
            result['previous'] = {'content': old['content'], 'exists': old['existed']}
    return result


def _content(value):
    if not isinstance(value, str) or '\0' in value or len(value.encode('utf-8')) > MAX_BYTES:
        raise ValueError('内容必须是 128 KB 以内的文本，不能包含空字符。')


def preview_rule(project_id, name, version, content, *, remove=False):
    _content(content)
    current = _read(_target(project_id, name))
    if current['version'] != version:
        raise SettingsConflict('文件已被其他操作修改；输入已保留，请重新读取后比较。')
    if remove:
        previous = read_rule(project_id, name)['previous']
        if previous is None or previous['exists']:
            raise ValueError('只能将本功能创建的文件恢复到不存在状态。')
        content = ''
    return ('恢复前文件不存在：确认保存将删除此项目的 ' + name + '。\n' if remove else '') + ''.join(difflib.unified_diff(current['content'].splitlines(keepends=True),
                                      content.splitlines(keepends=True), fromfile='保存前', tofile='保存后'))


def _atomic(path, raw, mode=0o600):
    fd, temporary = tempfile.mkstemp(prefix='.codex-rule-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_rule(project_id, name, version, content, *, remove=False):
    _content(content)
    path = _target(project_id, name)
    bucket = _bucket(path)
    if bucket.is_symlink() or bucket.parent.is_symlink():
        raise ValueError('恢复目录路径不安全。')
    STATE.mkdir(parents=True, mode=0o700, exist_ok=True)
    bucket.parent.mkdir(mode=0o700, exist_ok=True)
    bucket.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(bucket / 'lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = _read(_target(project_id, name))
        if current['version'] != version:
            raise SettingsConflict('文件已被其他操作修改，未覆盖；请重新读取后比较。')
        if remove:
            previous = read_rule(project_id, name)['previous']
            if previous is None or previous['exists']:
                raise ValueError('只能将本功能创建的文件恢复到不存在状态。')
        backup = bucket / 'previous.json'
        if backup.is_symlink():
            raise ValueError('恢复点路径不安全。')
        old_backup = backup.read_bytes() if backup.exists() else None
        result_version = 'absent' if remove else hashlib.sha256(content.encode()).hexdigest()
        _atomic(backup, json.dumps({'content': current['content'], 'existed': current['exists'],
                                   'result_version': result_version}, ensure_ascii=False).encode())
        try:
            if _read(_target(project_id, name))['version'] != version:
                raise SettingsConflict('文件刚刚发生变化，未覆盖。')
            if remove:
                path.unlink()
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            else:
                _atomic(path, content.encode('utf-8'), current['mode'])
        except BaseException:
            # A failure before replacement must not replace the last valid recovery point.
            if _read(_target(project_id, name))['version'] != result_version:
                if old_backup is None:
                    backup.unlink(missing_ok=True)
                else:
                    _atomic(backup, old_backup)
            raise
    return read_rule(project_id, name)
