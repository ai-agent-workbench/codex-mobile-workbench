"""Offline source-only package integrity; no Git history, state or credentials."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat

from shared import instance_config as cfg
from shared import runtime_bundle as bundle

MANIFEST = 'package-source.json'
PANEL = 'CodexWorkbenchPanel.exe'


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda:source.read(1024*1024), b''):
            value.update(block)
    return value.hexdigest()


def content_files(root):
    result = {}
    for name,path,item in bundle._entries(root):
        if stat.S_ISDIR(item.st_mode):
            continue
        if not stat.S_ISREG(item.st_mode) or item.st_nlink != 1:
            raise cfg.InstanceError('安装包包含链接或特殊文件，未导入。', 'invalid_source_package')
        result[name] = {'sha256':digest(path),'size':item.st_size,'executable':bool(item.st_mode & 0o111)}
    return result


def export(source: Path, target: Path, panel: Path) -> dict:
    commit = bundle._git_source(source)
    if target.exists():
        raise cfg.InstanceError('打包目标已存在，未覆盖。', 'already_exists')
    target.mkdir(mode=0o700)
    bundle._archive_source(source,target,commit)
    bundle._assert_archive_complete(source,target,commit)
    files = content_files(target)
    if PANEL in files or MANIFEST in files:
        raise cfg.InstanceError('源码包含保留的成品路径。', 'invalid_source_package')
    if panel.is_symlink() or not panel.is_file() or panel.read_bytes()[:2] != b'MZ':
        raise cfg.InstanceError('需要已实际构建的 Windows 原生面板。', 'invalid_panel')
    shutil.copyfile(panel,target/PANEL)
    value = {'format_version':1,'source_commit':commit,'files':files,'panel_sha256':digest(target/PANEL)}
    cfg.write_json(target/MANIFEST,value,replace=False)
    verify(target)
    return value


def verify(root: Path) -> dict:
    path = root / MANIFEST
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2*1024*1024:
        raise cfg.InstanceError('缺少固定安装包清单，未猜测导入任意目录。', 'invalid_source_package')
    value=json.loads(path.read_text(encoding='utf-8'))
    if (type(value) is not dict or set(value) != {'format_version','source_commit','files','panel_sha256'}
            or type(value['format_version']) is not int or value['format_version'] != 1
            or not re.fullmatch('[0-9a-f]{40,64}',str(value['source_commit'])) or type(value['files']) is not dict
            or not re.fullmatch('[0-9a-f]{64}',str(value['panel_sha256']))):
        raise cfg.InstanceError('不支持或损坏的安装包清单。', 'invalid_source_package')
    expected = value['files']
    observed = content_files(root)
    if set(observed) != set(expected) | {PANEL,MANIFEST}:
        raise cfg.InstanceError('安装包含缺失或额外文件，未导入。', 'invalid_source_package')
    for name,info in expected.items():
        bundle._safe_tar_name(name)
        if (type(info) is not dict or set(info) != {'sha256','size','executable'}
                or not re.fullmatch('[0-9a-f]{64}',str(info['sha256'])) or type(info['size']) is not int
                or type(info['executable']) is not bool
                or observed[name]['sha256'] != info['sha256'] or observed[name]['size'] != info['size']):
            raise cfg.InstanceError('源码安装包校验失败，未运行或导入。', 'invalid_source_package')
    # Windows extraction may not preserve POSIX execute bits. The verified
    # source manifest, not the Windows mount mode, restores these in WSL.
    if observed[PANEL]['sha256'] != value['panel_sha256']:
        raise cfg.InstanceError('Windows 面板校验失败，未导入。', 'invalid_source_package')
    return value


def materialize(source: Path, target: Path) -> str:
    value = verify(source)
    target.mkdir(mode=0o700)
    for name,info in value['files'].items():
        path = target / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(source / name,path)
        path.chmod(0o700 if info['executable'] else 0o600)
        if digest(path) != info['sha256'] or digest(source/name) != info['sha256']:
            raise cfg.InstanceError('源码在导入时改变，未继续安装。', 'source_changed')
    return value['source_commit']
