"""Private consistent inputs for the selected backup producer, not a scheduler.

SQLite backup gives per-database consistency. Only an externally established
writer stop can provide a cross-database/file cohort; the flag records that
claim, not evidence of stopping services. Restore never activates anything.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import uuid

from shared.runtime_bundle import _entries, _rename_noreplace


class RecoveryError(ValueError):
    pass


_MANIFEST = 'recovery-bundle.json'
_DBS = ('conversations.sqlite3', 'auth.sqlite3', 'workbench-settings.sqlite3',
        'dot-mailbox.sqlite3', 'dot-events.sqlite3', 'dot-delivery.sqlite3')
_FILES = ('accounts.json', 'session_secret', 'totp_secret', 'totp_enabled', 'audit.jsonl')
_DIRS = ('message-attachments', 'conversation-artifacts', 'dot-relay', 'uploads', 'edits', 'rule-history')


def _abs(path):
    return Path(os.path.abspath(os.fspath(path)))


def _inside(path, root):
    return path.is_relative_to(root)


def _dir(path):
    path = _abs(path)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        item = current.lstat()
        if not stat.S_ISDIR(item.st_mode):
            raise RecoveryError('input has a non-directory or link ancestor')
    return path


def _regular(path):
    _dir(path.parent)
    item = path.lstat()
    if not stat.S_ISREG(item.st_mode) or item.st_nlink != 1:
        raise RecoveryError('input is not a single-link regular file')
    return item


def _stable(item):
    return (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns,
            item.st_mode, item.st_nlink)


def _identity(path):
    item = _regular(path)
    return [item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns]


def _hash(path):
    value = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def _safe_name(value, *, component=False):
    if (not isinstance(value, str) or not value or '\\' in value or '\0' in value
            or PurePosixPath(value).is_absolute()
            or any(part in ('', '.', '..') for part in value.split('/'))
            or (component and '/' in value)):
        raise RecoveryError('unsafe relative path')
    return value


def _copy_file(source, target, budget=None):
    before = _regular(source)
    digest = _hash(source)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if budget is None:
        with source.open('rb') as reader, target.open('xb') as writer:
            shutil.copyfileobj(reader, writer)
    else:budget.copy(source,target,before.st_size)
    if (_stable(before) != _stable(_regular(source))
            or digest != _hash(source) or digest != _hash(target)):
        raise RecoveryError('source changed while copying')
    target.chmod(0o400)
    return {'sha256': digest, 'size': before.st_size,
            'source_identity': [before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns]}


def _copy_tree(source, target, files, directories, prefix, budget=None):
    _dir(source)
    target.mkdir(mode=0o700)
    for relative, path, item in _entries(source):
        key = prefix + '/' + relative
        if stat.S_ISDIR(item.st_mode):
            (target / relative).mkdir(mode=0o700)
            directories.append(key)
        elif stat.S_ISREG(item.st_mode) and item.st_nlink == 1:
            files[key] = _copy_file(path, target / relative, budget)
        else:
            raise RecoveryError('unsafe source tree entry')


def _connect(path, *, immutable=False):
    # Path.as_uri quotes #, ? and spaces: SQLite must not treat a filename as URI options.
    return sqlite3.connect(path.as_uri() + '?mode=ro' + ('&immutable=1' if immutable else ''), uri=True)


def _check_db(path):
    connection = _connect(path, immutable=True)
    try:
        if (connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok'
                or connection.execute('PRAGMA foreign_key_check').fetchone() is not None):
            raise RecoveryError('SQLite integrity or foreign-key failure')
    finally:
        connection.close()


def _backup_db(source, target, budget=None):
    before = _regular(source)
    reader = _connect(source)
    writer = None
    try:
        if budget is not None:
            reader.execute('BEGIN')
            pages=reader.execute('PRAGMA page_count').fetchone()[0];page_size=reader.execute('PRAGMA page_size').fetchone()[0]
            budget.take(2*pages*page_size+1024*1024)
        writer = sqlite3.connect(target)
        if budget is None:reader.backup(writer)
        else:reader.backup(writer,pages=256,progress=lambda status,remaining,total:budget.guard(2*256*page_size))
        # Only the private destination is normalized to a standalone database.
        # Never checkpoint the live source, including its committed WAL.
        writer.execute('PRAGMA journal_mode=DELETE')
    finally:
        if writer is not None:writer.close()
        reader.close()
    if budget is not None:budget.refresh()
    _check_db(target)
    if _stable(before) != _stable(_regular(source)):
        raise RecoveryError('SQLite source identity changed during backup')
    target.chmod(0o400)
    return {'sha256': _hash(target), 'size': target.stat().st_size,
            'source_identity': [before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns],
            'consistent': True}


def _walk(bundle):
    files, directories = {}, []
    for relative, path, item in _entries(bundle):
        if stat.S_ISDIR(item.st_mode):
            if stat.S_IMODE(item.st_mode) != 0o700:
                raise RecoveryError('bundle directory is not private')
            directories.append(relative)
        elif stat.S_ISREG(item.st_mode) and item.st_nlink == 1:
            if stat.S_IMODE(item.st_mode) != 0o400:
                raise RecoveryError('bundle file is not private readonly')
            if relative != _MANIFEST:
                files[relative] = {'sha256': _hash(path), 'size': item.st_size}
        else:
            raise RecoveryError('unsafe bundle entry')
    return files, sorted(directories)


def _closure_check(database, source_state, files):
    """Checks copied DB against copied bytes/recorded identities, never live sources."""
    connection = _connect(database, immutable=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        registry = {}
        binding_pairs = ({(row[0], row[1]) for row in connection.execute(
            'SELECT attachment_id,conversation_id FROM message_attachment_bindings')}
            if 'message_attachment_bindings' in tables else set())
        artifacts = commands = 0
        if 'message_attachments' in tables:
            for row in connection.execute('SELECT * FROM message_attachments'):
                record = json.loads(row['record_json'])
                if not isinstance(record, dict):
                    raise RecoveryError('invalid attachment registry JSON')
                for field in ('id', 'batch_id', 'owner_account_id', 'scope_kind', 'scope_id'):
                    if record.get(field) != row[field]:
                        raise RecoveryError('attachment registry columns disagree with JSON')
                aid = _safe_name(record.get('id'), component=True)
                name = _safe_name(record.get('name'), component=True)
                key = f'state/message-attachments/{aid}/{name}'
                metadata = files.get(key)
                if (record.get('path') != str(source_state / 'message-attachments' / aid / name)
                        or not metadata or record.get('size') != metadata['size']
                        or record.get('sha256') != metadata['sha256']
                        or record.get('identity') != metadata['source_identity']):
                    raise RecoveryError('attachment closure path, hash or identity drift')
                if 'bound_once' in row.keys() and row['bound_once'] == 1 and not any(pair[0] == aid for pair in binding_pairs):
                    raise RecoveryError('formerly used attachment has no conversation binding')
                registry[aid] = record
        if 'artifacts' in tables:
            for row in connection.execute('SELECT storage_relpath,size,sha256 FROM artifacts'):
                relative = _safe_name(row['storage_relpath'])
                metadata = files.get('state/conversation-artifacts/' + relative)
                if (not metadata or row['size'] != metadata['size']
                        or row['sha256'] != metadata['sha256']):
                    raise RecoveryError('Artifact closure mismatch')
                artifacts += 1
        if 'commands' in tables:
            for row in connection.execute('SELECT * FROM commands'):
                payload = json.loads(row['payload_json'])
                if not isinstance(payload, dict) or not isinstance(payload.get('attachments', []), list):
                    raise RecoveryError('invalid command attachment payload')
                for reference in payload.get('attachments', []):
                    record = registry.get(reference.get('id')) if isinstance(reference, dict) else None
                    if not record or any(reference.get(field) != record.get(field) for field in
                                         ('name', 'size', 'sha256', 'path', 'identity', 'batch_id',
                                          'owner_account_id', 'scope_kind', 'scope_id')):
                        raise RecoveryError('command attachment differs from registry')
                    if ('conversation_id' not in row.keys()
                            or (record['id'], row['conversation_id']) not in binding_pairs):
                        raise RecoveryError('command attachment conversation binding is missing')
                    commands += 1
        return {'attachments': len(registry), 'artifacts': artifacts, 'bindings': len(binding_pairs),
                'command_references': commands, 'checked': True}
    except (sqlite3.Error, ValueError, TypeError, KeyError) as error:
        raise RecoveryError('invalid recovery reference closure') from error
    finally:
        connection.close()


def _destination(destination, sources):
    destination = _abs(destination)
    _dir(destination.parent)
    if os.path.lexists(destination) or any(_inside(destination, root) or _inside(root, destination) for root in sources):
        raise RecoveryError('destination must be absent and disjoint from inputs')
    return destination


def build_recovery_bundle(state, workspace, destination, *, unit_inputs=None, writers_stopped=False, budget=None):
    state, workspace = _dir(state), _dir(workspace)
    destination = _destination(destination, (state, workspace))
    stage = destination.parent / f'.{destination.name}.building-{uuid.uuid4().hex}'
    files, directories, databases, units = {}, ['state'], {}, {}
    try:
        stage.mkdir(mode=0o700)
        outstate = stage / 'state'
        outstate.mkdir(mode=0o700)
        for name in _DBS:
            source = state / name
            if os.path.lexists(source):
                databases[name] = _backup_db(source, outstate / name, budget)
                files['state/' + name] = databases[name]
            elif name == 'conversations.sqlite3':
                raise RecoveryError('conversations.sqlite3 is required')
            else:
                databases[name] = {'missing': True}
        for name in _FILES:
            if os.path.lexists(state / name):
                files['state/' + name] = _copy_file(state / name, outstate / name, budget)
        for name in _DIRS:
            if os.path.lexists(state / name):
                directories.append('state/' + name)
                _copy_tree(state / name, outstate / name, files, directories, 'state/' + name, budget)
        directories.append('workspace')
        _copy_tree(workspace, stage / 'workspace', files, directories, 'workspace', budget)
        for name, source in (unit_inputs or {}).items():
            _safe_name(name, component=True)
            source = _abs(source)
            files['units/' + name] = _copy_file(source, stage / 'units' / name, budget)
            units[name] = {'path': str(source), 'sha256': files['units/' + name]['sha256']}
        if units:
            directories.append('units')
        closure = _closure_check(outstate / 'conversations.sqlite3', state, files)
        manifest = {'format_version': 1, 'writers_stopped': bool(writers_stopped),
                    'databases': databases, 'files': files, 'directories': sorted(directories),
                    'units': units, 'closure': closure, 'sources': {'state': str(state), 'workspace': str(workspace)}}
        raw=(json.dumps(manifest,sort_keys=True)+'\n').encode()
        if budget is None:(stage/_MANIFEST).write_bytes(raw)
        else:budget.write(stage/_MANIFEST,raw)
        (stage / _MANIFEST).chmod(0o400)
        verify_recovery_bundle(stage)
        _rename_noreplace(stage, destination)
        return manifest
    except BaseException:
        if stage.exists():
            shutil.rmtree(stage)
        raise


def verify_recovery_bundle(bundle):
    bundle = _dir(bundle)
    if stat.S_IMODE(bundle.stat().st_mode) != 0o700:
        raise RecoveryError('bundle root is not private')
    _regular(bundle / _MANIFEST)
    try:
        manifest = json.loads((bundle / _MANIFEST).read_text())
        if (not isinstance(manifest, dict) or set(manifest) != {'format_version', 'writers_stopped',
                'databases', 'files', 'directories', 'units', 'closure', 'sources'}
                or type(manifest['format_version']) is not int or manifest['format_version'] != 1
                or type(manifest['writers_stopped']) is not bool
                or set(manifest['databases']) != set(_DBS)
                or manifest['databases']['conversations.sqlite3'].get('missing')
                or set(manifest['sources']) != {'state', 'workspace'}):
            raise RecoveryError('invalid manifest fields')
        for name in ('state', 'workspace'):
            if str(_abs(manifest['sources'][name])) != manifest['sources'][name]:
                raise RecoveryError('manifest source is not an absolute canonical path')
        for relative, metadata in manifest['files'].items():
            _safe_name(relative)
            if (not isinstance(metadata, dict) or len(metadata.get('source_identity', [])) != 4
                    or not all(type(value) is int for value in metadata['source_identity'])
                    or type(metadata.get('size')) is not int):
                raise RecoveryError('invalid file manifest identity')
            top, *parts = relative.split('/')
            if not (top in {'workspace', 'units'} and parts or top == 'state' and parts and parts[0] in {*_DBS, *_FILES, *_DIRS}):
                raise RecoveryError('unexpected recovery scope')
        files, directories = _walk(bundle)
        if (set(files) != set(manifest['files']) or directories != manifest['directories']):
            raise RecoveryError('bundle entries differ from manifest')
        for relative, metadata in files.items():
            if any(manifest['files'][relative].get(key) != value for key, value in metadata.items()):
                raise RecoveryError('bundle hash mismatch')
        for name, entry in manifest['databases'].items():
            if entry == {'missing': True}:
                if 'state/' + name in files:
                    raise RecoveryError('unexpected database declared missing')
            else:
                if entry != manifest['files'].get('state/' + name) or entry.get('consistent') is not True:
                    raise RecoveryError('database manifest disagrees')
                _check_db(bundle / 'state' / name)
        if set(manifest['units']) != {key.split('/')[1] for key in files if key.startswith('units/')}:
            raise RecoveryError('unit mapping is incomplete')
        for name, unit in manifest['units'].items():
            _safe_name(name, component=True)
            if unit.get('sha256') != files['units/' + name]['sha256']:
                raise RecoveryError('unit digest disagrees')
        if _closure_check(bundle / 'state/conversations.sqlite3', Path(manifest['sources']['state']), manifest['files']) != manifest['closure']:
            raise RecoveryError('reference closure summary disagrees')
        return manifest
    except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError) as error:
        raise RecoveryError('invalid recovery bundle') from error


def restore_recovery_bundle(bundle, destination, *, rebind_attachments=False):
    manifest = verify_recovery_bundle(bundle)
    bundle = _dir(bundle)
    destination = _destination(destination, (bundle, Path(manifest['sources']['state']), Path(manifest['sources']['workspace'])))
    stage = destination.parent / f'.{destination.name}.restore-{uuid.uuid4().hex}'
    connection = None
    try:
        # copytree changes inode identity; all updates below affect this NEW tree only.
        stage.mkdir(mode=0o700)
        shutil.copytree(bundle, stage, dirs_exist_ok=True)
        # Validate the ACTUAL copied closure against the already verified cohort
        # before deriving any new inode identities or changing a database.
        if verify_recovery_bundle(stage) != manifest:
            raise RecoveryError('bundle changed while restoring')
        (stage / _MANIFEST).unlink()
        database = stage / 'state/conversations.sqlite3'
        database.chmod(0o600)
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        registry, unresolved, mapped, external = {}, [], [], []
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        with connection:
            if rebind_attachments and 'message_attachments' in tables:
                for row in connection.execute('SELECT id,record_json FROM message_attachments'):
                    record = json.loads(row['record_json'])
                    relative = Path('message-attachments') / record['id'] / record['name']
                    record['path'] = str(destination / 'state' / relative)
                    record['identity'] = _identity(stage / 'state' / relative)
                    registry[record['id']] = record
                    connection.execute('UPDATE message_attachments SET record_json=? WHERE id=?',
                                       (json.dumps(record, sort_keys=True), row['id']))
            if 'commands' in tables:
                for row in connection.execute('SELECT id,state,reason,payload_json FROM commands'):
                    payload = json.loads(row['payload_json'])
                    references = payload.get('attachments', [])
                    if not references:
                        continue
                    if rebind_attachments and row['state'] in {'completed', 'rejected'}:
                        for reference in references:
                            record = registry[reference['id']]
                            reference['path'], reference['identity'] = record['path'], record['identity']
                        connection.execute('UPDATE commands SET payload_json=? WHERE id=?',
                                           (json.dumps(payload, sort_keys=True), row['id']))
                    else:
                        unresolved.append({'id': row['id'], 'state': row['state'], 'reason': row['reason']})
            if 'conversations' in tables and 'cwd' in {row[1] for row in connection.execute('PRAGMA table_info(conversations)')}:
                original_workspace = Path(manifest['sources']['workspace'])
                for row in connection.execute('SELECT id,cwd FROM conversations'):
                    path = Path(row['cwd'])
                    if path.is_absolute() and path.is_relative_to(original_workspace) and '..' not in path.parts:
                        relative = path.relative_to(original_workspace)
                        if (stage / 'workspace' / relative).is_dir():
                            connection.execute('UPDATE conversations SET cwd=? WHERE id=?',
                                               (str(destination / 'workspace' / relative), row['id']))
                            mapped.append(row['id'])
                            continue
                    external.append(row['id'])
        connection.close()
        connection = None
        _check_db(database)
        report = {'format_version': 1, 'activated': False, 'commands_replayed': False,
                  'rebind_attachments': bool(rebind_attachments),
                  'attachments': 'rebound' if rebind_attachments else 'bytes_verified_identity_rebind_required',
                  'rebound_attachment_ids': sorted(registry), 'unresolved_commands': unresolved,
                  'cwd_mapped': mapped, 'cwd_not_materialized': external,
                  'plan_context_digests': 'unchanged; must be revalidated before activation',
                  'original_bundle_digest': _hash(bundle / _MANIFEST),
                  'restored_conversations_sha256': _hash(database)}
        (stage / 'restore-report.json').write_text(json.dumps(report, sort_keys=True) + '\n')
        (stage / 'restore-report.json').chmod(0o400)
        # No service activation, login, scheduler, native replay or notification.
        _rename_noreplace(stage, destination)
        return report
    except BaseException:
        if connection is not None:
            connection.close()
        if stage.exists():
            shutil.rmtree(stage)
        raise
