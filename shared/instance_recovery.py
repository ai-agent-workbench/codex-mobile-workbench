"""Stopped-instance backups and isolated restores. No credential transplantation.

Core data recovery and immutable-runtime validators remain authoritative. A
restore always creates a NEW offline instance; never overwrites or auto-runs a
conversation, a scheduler, a browser session or native credentials.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import uuid

from shared import instance_config as cfg
from shared import runtime_release as release
from shared.instance_manager import unit_status, units
from shared import instance_runtime as runtime
from shared import instance_native as native
from shared import workbench_recovery as data
from shared.instance_primitives import copy_tree


def stopped(config):
    if any(unit_status(name) not in {'inactive', 'failed', 'not_installed'} for name in units(config)):
        raise cfg.InstanceError('备份/恢复版本需要先安全停止此实例，未强行停写或覆盖。', 'instance_running')


_V1_MANIFEST_KEYS = {'format_version', 'profile', 'installation', 'selected', 'versions',
                     'data_manifest_sha256', 'workspace_executables'}
_V2_MANIFEST_KEYS = _V1_MANIFEST_KEYS | {'created_at'}


def _created_at(value):
    """Accept only the v2 UTC wire format; never infer a time from filesystem metadata."""
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', value):
        raise cfg.InstanceError('备份创建时间格式无效，未恢复。', 'invalid_backup')
    try:
        parsed = datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    except ValueError:
        raise cfg.InstanceError('备份创建时间格式无效，未恢复。', 'invalid_backup') from None
    if parsed.strftime('%Y-%m-%dT%H:%M:%SZ') != value:
        raise cfg.InstanceError('备份创建时间格式无效，未恢复。', 'invalid_backup')
    return value


def _backup_summary(value, cohort):
    """Bounded facts from this operation's already verified manifest, without I/O."""
    files = cohort['files']
    database_count = sum(1 for item in cohort['databases'].values() if item != {'missing': True})
    byte_count = sum(item['size'] for item in files.values())
    needs_rebuild = value.get('_needs_rebuild', False)
    return {
        'format_version': value['format_version'],
        'created_at': value.get('created_at', 'unknown'),
        'created_at_kind': 'capture_utc' if value['format_version'] == 2 else 'unknown_legacy_v1',
        'included': ['所选实例资料配置', 'state 白名单（含状态数据库和认证资料）', 'workspace',
                     '当前健康程序及最多一个健康回退程序', '控制器和原生程序包'],
        'excluded': ['原生登录缓存', 'workspace 外工程和文件', '主机实际网关和组网环境', '其他历史程序版本'],
        'sensitive_notice': '备份含敏感明文资料（包括认证资料和所选实例配置），请妥善保管；它不是“完全不含凭据”的备份。',
        'data': {'status': 'verified', 'files': len(files), 'databases': database_count,
                 'bytes': byte_count},
        'software': {'status': 'rebuild_required' if needs_rebuild else 'verified',
                     'versions': len(value['versions']), 'rebuild_required': needs_rebuild},
    }


def _restore_summary(value, report, target, source_summary):
    """Report only bounded post-restore facts; never return report internals or IDs."""
    if (type(report) is not dict or type(report.get('rebound_attachment_ids')) is not list
            or type(report.get('cwd_not_materialized')) is not list
            or report.get('activated') is not False or report.get('commands_replayed') is not False):
        raise cfg.InstanceError('恢复报告无效，未报告成功。', 'invalid_restore_report')
    database = target / 'state/conversations.sqlite3'
    try:
        with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as connection:
            unknown_count = connection.execute(
                "SELECT count(*) FROM commands WHERE state='delivery_unknown'").fetchone()[0]
        if report.get('restored_conversations_sha256') != runtime.digest(database):
            raise ValueError('restore report does not match final database')
    except (sqlite3.Error, ValueError) as error:
        raise cfg.InstanceError('恢复后的命令状态无法核验，未报告成功。', 'invalid_restore_report') from error
    needs_rebuild = value['_needs_rebuild']
    return {
        'restored_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'backup': source_summary,
        'data': {'status': 'verified_and_restored', 'attachments_rebound': len(report['rebound_attachment_ids']),
                 'external_cwd_not_materialized': len(report['cwd_not_materialized']),
                 'delivery_unknown_commands': unknown_count},
        'software': {'status': 'rebuild_required' if needs_rebuild else 'restored_verified',
                     'rebuild_required': needs_rebuild, 'versions': len(value['versions'])},
        'activated': False,
        'commands_replayed': False,
        'next_steps': ['需要重建时先安装当前程序', '本人重新原生登录、配置手机 HTTPS 并绑定手机',
                       '检查新实例按默认重建的配置、工程与计划上下文', '核对未知投递，不自动续跑旧任务'],
    }


def backup(config, *, legacy_native=False):
    stopped(config)
    installed = runtime.software(config, legacy_native=legacy_native)
    root = Path(config['root'])
    releases = root / 'state/releases'
    if release._pending(releases) is not None:
        raise cfg.InstanceError('版本事务尚未完成，不能把它标记为可恢复备份。', 'pending_runtime')
    selected = release._current_stamp(releases)
    if selected is None or release._status(releases, selected) != 'healthy':
        raise cfg.InstanceError('尚无已核验的健康版本；保留原数据，未生成假恢复点。', 'no_healthy_runtime')
    metadata = release._read_meta(releases, selected)
    versions = [selected] + ([metadata['previous']] if metadata['previous'] else [])
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
    destination = root / 'backups' / stamp
    destination.mkdir(mode=0o700)
    try:
        data.build_recovery_bundle(root / 'state', root / 'workspace', destination / 'data', writers_stopped=True)
        (destination / 'releases').mkdir(mode=0o700)
        for version in versions:
            if release._status(releases, version) != 'healthy':
                raise cfg.InstanceError('回退版本不是健康版本，未产生不完整恢复点。', 'no_healthy_fallback')
            release._verify_release(releases, version, trusted_base_interpreter=runtime.HOST_PYTHON)
            copy_tree(releases / version, destination / 'releases' / version)
            release._verify_release(destination / 'releases', version, trusted_base_interpreter=runtime.HOST_PYTHON)
        copy_tree(root / installed['controller'], destination / 'controller')
        legacy = runtime.is_legacy_native_metadata(installed['native'])
        runtime.verify_controller(destination / 'controller', installed['controller_sha256'], legacy=legacy)
        if legacy:
            (destination / 'native').mkdir(mode=0o700)
            # Migration captures the exact retired program only as evidence; it
            # is never accepted as a runnable complete kit after restore.
            shutil.copyfile(root / 'native/codex', destination / 'native/codex')
            (destination / 'native/codex').chmod(0o700)
            license_source = root / 'native/LICENSE.txt'
            if license_source.exists():
                shutil.copyfile(license_source, destination / 'native/LICENSE.txt')
                (destination / 'native/LICENSE.txt').chmod(0o600)
        else:
            native.copy_bundle(root / 'native', destination / 'native')
        cfg.write_json(destination / 'backup.json', {'format_version': 2,
            'created_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'profile': config,
            'installation': installed, 'selected': selected, 'versions': versions,
            'data_manifest_sha256': runtime.digest(destination / 'data/recovery-bundle.json'),
            'workspace_executables': sorted(name for name,path,item in runtime.bundle._entries(root / 'workspace')
                if item.st_mode & 0o111 and not path.is_dir())}, replace=False)
        note = '仅备份所选实例资料配置、state 白名单、workspace 和健康程序；不包含原生登录缓存、workspace 外项目、主机实际网关或组网环境及其他历史版本。备份含敏感明文资料，请妥善保管。'
        if legacy:
            note += ' 此迁移恢复点包含旧版不完整单文件原生程序证据；恢复后必须重建当前完整原生包。'
        _, summary = _verify_details(destination, allow_rebuild=legacy)
        return {'backup': str(destination), 'legacy_native': legacy, 'backup_summary': summary, 'note': note}
    except BaseException:
        # Keep failed capture separate and explicit; do not delete user data or
        # present a partial tree as a valid backup.
        failed = destination.with_name('.incomplete-' + destination.name)
        destination.rename(failed)
        raise


def verify(path, *, allow_rebuild=False):
    """Keep the existing metadata-only validation contract for callers."""
    return _verify_details(path, allow_rebuild=allow_rebuild)[0]


def inspect_backup(path):
    """Explicit read-only check; return only bounded, path-free verified facts."""
    return _verify_details(path, allow_rebuild=True)[1]


def _verify_details(path, *, allow_rebuild=False):
    path = cfg.absolute_path(str(path))
    cfg.private_directory(path)
    cfg.private_file(path / 'backup.json', max_size=2*1024*1024)
    value = json.loads((path / 'backup.json').read_text())
    if type(value) is not dict or type(value.get('format_version')) is not int:
        raise cfg.InstanceError('备份清单不完整，未恢复。', 'invalid_backup')
    version = value['format_version']
    if ((version == 1 and set(value) != _V1_MANIFEST_KEYS)
            or (version == 2 and set(value) != _V2_MANIFEST_KEYS)
            or version not in {1, 2}
            or type(value.get('versions')) is not list or len(value['versions']) not in {1, 2}
            or any(not re.fullmatch(r'\d{8}-\d{6}', str(item)) for item in value['versions'])
            or value.get('selected') != value['versions'][0]):
        raise cfg.InstanceError('备份清单不完整，未恢复。', 'invalid_backup')
    if version == 2:
        _created_at(value['created_at'])
    # Use the same bounded metadata contract as installed instances BEFORE
    # any destination is created or the controller's target path is joined.
    runtime.validate_installation(value['installation'])
    legacy = runtime.is_legacy_native_metadata(value['installation']['native'])
    if {p.name for p in path.iterdir()} != {'backup.json', 'data', 'releases', 'controller', 'native'}:
        raise cfg.InstanceError('备份包含缺失或额外项目，未恢复。', 'invalid_backup')
    if runtime.digest(path / 'data/recovery-bundle.json') != value['data_manifest_sha256']:
        raise cfg.InstanceError('备份数据清单校验失败，未恢复。', 'invalid_backup')
    cohort = data.verify_recovery_bundle(path / 'data')
    executable = value['workspace_executables']
    if (type(executable) is not list or any(not isinstance(name,str) or 'workspace/' + name not in cohort['files'] for name in executable)):
        raise cfg.InstanceError('备份执行位元信息无效，未恢复。', 'invalid_backup')
    if {p.name for p in (path / 'releases').iterdir()} != set(value['versions']):
        raise cfg.InstanceError('备份程序版本不完整，未恢复。', 'invalid_backup')
    for version in value['versions']:
        release._read_meta(path / 'releases', version)
        if release._status(path / 'releases', version) != 'healthy':
            raise cfg.InstanceError('备份版本未经健康核验，未恢复。', 'invalid_backup')
    previous = release._read_meta(path / 'releases', value['selected'])['previous']
    if value['versions'][1:] != ([previous] if previous else []):
        raise cfg.InstanceError('备份回退链不完整，未恢复。', 'invalid_backup')
    cfg.validate(value['profile'], Path(value['profile']['root']) / 'instance.json')
    needs_rebuild = legacy
    if legacy and not allow_rebuild:
        raise cfg.InstanceError('旧版单文件原生程序不是完整可恢复套件；只能执行资料重建恢复。', 'legacy_native_incomplete')
    for version in value['versions']:
        try:
            release._verify_release(path / 'releases', version, trusted_base_interpreter=runtime.HOST_PYTHON)
        except runtime.bundle.HostPythonMismatch:
            if not allow_rebuild:
                raise
            # Only independently verified DATA will be copied. Check every
            # version nevertheless: a non-canonical interpreter stays an error.
            needs_rebuild = True
    installation = value['installation']
    if legacy:
        runtime.verify_controller(path / 'controller', installation['controller_sha256'], legacy=True)
        runtime.verify_legacy_native(path / 'native/codex', installation['native'])
    elif not needs_rebuild:
        runtime.verify_controller(path / 'controller', installation['controller_sha256'])
        native.verify(path / 'native', installation['native'])
    if allow_rebuild:
        value = {**value, '_needs_rebuild': needs_rebuild}
    return value, _backup_summary(value, cohort)


def restore(config, payload):
    if type(payload) is not dict or set(payload) != {'backup', 'destination'}:
        raise cfg.InstanceError('恢复需要明确的备份目录和新的独立目录；不会覆盖原实例。', 'invalid_restore_input')
    source = cfg.absolute_path(payload['backup'])
    target = cfg.absolute_path(payload['destination'])
    value, source_summary = _verify_details(source, allow_rebuild=True)  # Validate BEFORE target creation.
    if target.exists():
        raise cfg.InstanceError('恢复目标已存在；请选择新目录，未覆盖或删除任何内容。', 'restore_target_exists')
    original = value['profile']
    profile = cfg.new_profile(target / 'instance.json', {'root': str(target), 'account': original['account']})
    # The target gets a fresh id/port, but its enrollment semantics belong to
    # the backed-up profile.  A new physical directory must not turn a v1
    # account into a first-choice account (or vice versa).
    if original['format_version'] == 1:
        profile.pop('permission_choice_required')
        profile['format_version'] = 1
        profile = cfg.validate(profile, target / 'instance.json')
    # Restore at the FINAL root so rebound attachment identities do not refer
    # to a temporary subtree. Only successful publication proves target ownership.
    # A losing concurrent restore must never write into the winner's directory.
    data.restore_recovery_bundle(source / 'data', target, rebind_attachments=True)
    with cfg.exclusive(target / 'instance.json'):
        try:
            cfg.write_json(target / 'instance.json', profile, replace=False)
            cfg._ensure_directories(target)
            for scope in ('state', 'workspace'):
                for relative,path,item in runtime.bundle._entries(target / scope):
                    if path.is_dir():
                        path.chmod(0o700)
                    else:
                        path.chmod(0o700 if scope == 'workspace' and relative in value['workspace_executables'] else 0o600)
            database = target / 'state/conversations.sqlite3'
            with sqlite3.connect(database) as db:
                # A new native HOME cannot prove old delivery. Leave unknown turns
                # behind the explicit reconciliation gate, never replay commands.
                db.execute("UPDATE commands SET state='delivery_unknown',reason='isolated_restore_requires_reconcile' WHERE state IN ('submitted','queued')")
                db.execute("UPDATE conversations SET state='needs_user',recovery_state='isolated_restore_requires_reconcile',connection_state='disconnected'")
            auth = target / 'state/auth.sqlite3'
            if auth.exists():
                with sqlite3.connect(auth) as db:
                    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    for table in ('admin_sessions', 'registration_authorizations', 'webauthn_flows'):
                        if table in tables:
                            db.execute('DELETE FROM ' + table)
                    if 'passkey_credentials' in tables:
                        db.execute("UPDATE passkey_credentials SET revoked_at=CAST(strftime('%s','now') AS INTEGER),revoked_reason='isolated_restore' WHERE revoked_at IS NULL")
            (target / 'state/session_secret').write_text(runtime.secrets.token_urlsafe(48))
            (target / 'state/session_secret').chmod(0o600)
            (target / 'state/totp_enabled').unlink(missing_ok=True)
            lineage_truncated = False
            if not value['_needs_rebuild']:
                # Restore the ACTUAL pinned supervisor, not one inferred from app code
                # after a previous software-only rollback.
                controller = value['installation']['controller']
                controllers = target / 'controllers'
                controllers.mkdir(mode=0o700)
                copy_tree(source / 'controller', target / controller)
                hashes = value['installation']['controller_sha256']
                runtime.verify_controller(target / controller, hashes)
                releases = target / 'state/releases'
                releases.mkdir(mode=0o700)
                for version in value['versions']:
                    copy_tree(source / 'releases' / version, releases / version)
                # The portable snapshot includes the current release and ONE
                # fallback, not the entire historical chain. Close only this NEW
                # copy's tail so rollback -> backup never follows an absent version.
                oldest = value['versions'][-1]
                metadata = release._read_meta(releases, oldest)
                if metadata['previous'] is not None:
                    container = releases / oldest
                    mode = stat.S_IMODE(container.stat().st_mode)
                    container.chmod(mode | 0o200)
                    try:
                        release._replace(container / release._META,
                            json.dumps({**metadata, 'previous': None}, sort_keys=True) + '\n')
                    finally:
                        container.chmod(mode)
                    lineage_truncated = True
                release.select_healthy_runtime(releases, value['selected'], trusted_base_interpreter=runtime.HOST_PYTHON)
                native.copy_bundle(source / 'native', target / 'native')
                installation = {**value['installation'], 'release': value['selected'],
                                'runtime_commit': release._read_meta(releases, value['selected'])['commit'],
                                'controller': controller, 'controller_sha256': hashes, 'unit_sha256': {}}
                cfg.write_json(target / 'installed.json', installation)
            report = json.loads((target / 'restore-report.json').read_text())
            cfg.write_json(target / 'restore-report.json', {**report, 'activated': False, 'native_login_copied': False,
                'modules_enabled': False, 'phone_identity_restored': False,
                'software_restored': not value['_needs_rebuild'], 'rebuild_required': value['_needs_rebuild'],
                'runtime_lineage_truncated': lineage_truncated,
                'restored_conversations_sha256': runtime.digest(database)})
            note = ('已恢复到新目录，未覆盖原实例、未启动或重放；附件副本身份已重新绑定，外部目录和计划上下文仍需核验，原生与手机登录需本人重做。')
            if value['_needs_rebuild']:
                note += '电脑 Python 环境与备份不同：只恢复了资料，旧程序未复制或运行。请为此新实例点“安装”重建当前软件。'
            restore_summary = _restore_summary(value, json.loads((target / 'restore-report.json').read_text()), target, source_summary)
            return {'root': str(target), 'activated': False,
                    'software_restored': not value['_needs_rebuild'], 'rebuild_required': value['_needs_rebuild'],
                    'restore_summary': restore_summary, 'note': note}
        except BaseException:
            if target.exists():
                cfg.write_json(target / 'restore-incomplete.json', {'activated': False, 'complete': False})
            raise


def rollback(config):
    stopped(config)
    installed = runtime.software(config)
    root = Path(config['root'])
    releases = root / 'state/releases'
    if release._pending(releases) is not None:
        raise cfg.InstanceError('有未决版本核验，先检查该结果，未强改指针。', 'pending_runtime')
    selected = release._current_stamp(releases)
    if not selected:
        raise cfg.InstanceError('尚无可回退版本。', 'no_healthy_fallback')
    previous = release._read_meta(releases, selected)['previous']
    if not previous:
        raise cfg.InstanceError('这是首个健康版本，没有更早的本实例恢复点。', 'no_healthy_fallback')
    backup(config)
    release.select_healthy_runtime(releases, previous, trusted_base_interpreter=runtime.HOST_PYTHON)
    installed['release'] = previous
    installed['runtime_commit'] = release._read_meta(releases, previous)['commit']
    # Keep the verified independent supervisor, native protocol version and data;
    # rollback selects program bytes, never rolls the conversation DB backward.
    cfg.write_json(root / 'installed.json', installed)
    return {'release': previous, 'started': False,
            'note': '仅选择了此实例上一健康程序，数据未倒退，也未自动启动；旧版本功能需重新核对。'}


def uninstall(config):
    """Unregister only this instance; retain data AND retired software bytes."""
    stopped(config)
    installed = runtime.software(config)
    root = Path(config['root'])
    if installed['unit_sha256']:
        runtime.verify_service_binding(config, installed)
    paths = [root / name for name in ('installed.json', 'service.env', 'native', 'controllers', 'build', 'state/releases')
             if (root / name).exists()]
    for path in paths:
        cfg.absolute_path(str(path))
    # No caller data is deleted, including files unexpectedly placed inside an
    # old build: software is retired intact, never recursively purged.
    retired = root / 'retired-software'
    retired.mkdir(mode=0o700, exist_ok=True)
    cfg.private_directory(retired)
    destination = retired / (datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
    destination.mkdir(mode=0o700)
    if installed['unit_sha256']:
        directory = cfg.absolute_path(str(Path.home() / '.config/systemd/user'))
        for name in units(config):
            path = directory / name
            if runtime.digest(path) != installed['unit_sha256'][name]:
                raise cfg.InstanceError('服务在卸载前改变，未删除。', 'service_changed')
        for name in units(config):
            (directory / name).unlink()
        runtime.command(['systemctl','--user','daemon-reload'],env=runtime.user_environment())
    for path in paths:
        path.rename(destination / path.name)
    return {'root': str(root), 'retired_software': str(destination),
            'note': '仅注销此实例服务和运行入口；状态、项目、原生本人登录及备份均保留。旧程序也完整移入 retired-software，未清理磁盘或删除资料。'}
