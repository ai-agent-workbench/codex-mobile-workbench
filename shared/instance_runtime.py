"""Local lifecycle for a new owner-owned instance, never the production units.

The window delegates here; bundles, native execution, admission and recovery
remain the existing implementations. No network listener or RPC control API is
added for management. Native credentials are created only by native login.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import uuid

from shared import instance_config as cfg
from shared import instance_network as network
from shared.instance_manager import units, user_environment, unit_status
from shared import runtime_bundle as bundle
from shared import runtime_release as release
from shared import instance_native as native

NATIVE_VERSION = '0.160.0'
LEGACY_NATIVE_URL = 'https://github.com/openai/codex/releases/download/rust-v0.160.0/codex-x86_64-unknown-linux-musl.tar.gz'
LEGACY_NATIVE_SIZE = 109304578
LEGACY_NATIVE_ARCHIVE_SHA256 = '306865417d4ee7a927785852910a527f41e1e159add390ac5ae3accb67d44a13'
# Compatibility aliases for source users; only legacy migration accepts these pins.
NATIVE_URL = native.URL
NATIVE_SIZE = native.ARCHIVE_SIZE
NATIVE_ARCHIVE_SHA256 = native.ARCHIVE_SHA256
HOST_PYTHON = Path('/usr/bin/python3.12')
NATIVE_BINARY_SIZE = 289101384
NATIVE_BINARY_SHA256 = '12eb3e81114588aca3b7998f4f19e8997b056aca08e57a7ca7c8a3ec8c652aad'
LEGACY_NATIVE_MEMBER = 'codex-x86_64-unknown-linux-musl'
CONTROLLER_FILES = {'scripts/runtime-launch.py': 'launcher.py', **{
    'shared/' + name + '.py': 'shared/' + name + '.py' for name in (
        '__init__', 'runtime_bundle', 'runtime_release', 'runtime_controller',
        'runtime_quarantine', 'runtime_processes', 'instance_native')}}
LEGACY_CONTROLLER_FILES = {origin: target for origin, target in CONTROLLER_FILES.items() if not origin.endswith('/instance_native.py')}


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open('rb') as source:
        for part in iter(lambda: source.read(1024 * 1024), b''):
            value.update(part)
    return value.hexdigest()


def command(args: list[str], *, env=None, cwd=None, timeout=30, stdin=None) -> str:
    """Never persist child output, environment, proxy URLs, or credentials."""
    result = subprocess.run(args, env=env, cwd=cwd, input=stdin, capture_output=True,
                            text=True, encoding='utf-8', timeout=timeout)
    if result.returncode:
        raise cfg.InstanceError('本机步骤未完成；请检查环境和实例状态，未报告成功。', 'local_step_failed')
    return result.stdout


def instance_environment(config: dict) -> dict[str, str]:
    """Environment for native, installer and runtime children only."""
    env = os.environ.copy()
    for key in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP', 'PYTHONUSERBASE',
                'OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_ACCESS_TOKEN'):
        env.pop(key, None)
    # Do not carry another deployment's flags/paths into this instance.
    for key in list(env):
        if key.startswith('CODEX_WEB_') or key == 'CODEX_APP_SERVER_SOCKET':
            env.pop(key)
    env.update(cfg.environment(config))
    try:
        env.update(network.policy(config['settings'], env))
    except ValueError:
        # Config validation normally catches this; never expose a private URL
        # if a caller handed us an unchecked in-memory object.
        raise cfg.InstanceError('网络设置无效，未启动或下载。', 'invalid_network_proxy') from None
    return env


def network_command(args: list[str], *, env: dict[str, str], timeout: int,
                    mode: str, phase: str, code: str, cwd=None) -> str:
    """Run one network stage exactly once without exposing child output/URLs."""
    try:
        return command(args, env=env, cwd=cwd, timeout=timeout)
    except (cfg.InstanceError, OSError, subprocess.TimeoutExpired):
        label = network.LABELS.get(mode, '未知')
        raise cfg.InstanceError(f'{phase}失败（网络方式：{label}）；未自动切换网络路径。', code) from None


def native_command(config: dict) -> Path:
    return native.command(Path(config['root']))


def legacy_native_metadata() -> dict:
    return {'version': NATIVE_VERSION, 'archive_sha256': LEGACY_NATIVE_ARCHIVE_SHA256,
            'binary_sha256': NATIVE_BINARY_SHA256, 'source': LEGACY_NATIVE_URL}


def is_legacy_native_metadata(value: object) -> bool:
    return value == legacy_native_metadata()


def verify_legacy_native(path: Path, metadata: dict | None = None) -> None:
    """Read-only migration proof for the one retired single-binary source."""
    if metadata is not None and not is_legacy_native_metadata(metadata):
        raise cfg.InstanceError('原生程序不符合已核验的官方固定版本，未执行。', 'native_digest_mismatch')
    cfg.private_file(path, max_size=None)
    if path.stat().st_size != NATIVE_BINARY_SIZE or digest(path) != NATIVE_BINARY_SHA256:
        raise cfg.InstanceError('原生程序不符合已核验的官方固定版本，未执行。', 'native_digest_mismatch')


RECEIPT_NAME = '.native-transaction.json'

def _receipt_path(root: Path) -> Path:
    return root / RECEIPT_NAME

def _present(path: Path) -> bool:
    return os.path.lexists(path)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_tree(root: Path) -> None:
    # Called only after the entire staging tree passed fixed manifest checks.
    for current, _, files in os.walk(root, topdown=False, followlinks=False):
        directory = Path(current)
        for name in files:
            descriptor = os.open(directory / name, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        _sync_directory(directory)


def _installed_bytes(root: Path) -> str | None:
    path = root / 'installed.json'
    if not _present(path):
        return None
    cfg.private_file(path, max_size=None)
    return digest(path)


def _write_receipt(root: Path, value: dict) -> None:
    cfg.write_json(_receipt_path(root), value)
    _sync_directory(root)


def _clear_receipt(root: Path) -> None:
    path = _receipt_path(root)
    if _present(path):
        cfg.private_file(path)
        path.unlink()
        _sync_directory(root)


def _pending_native() -> None:
    raise cfg.InstanceError('原生安装事务未决或对象已变化，保留全部证据未覆盖。', 'native_transaction_pending')


def _verify_previous_native(path: Path, metadata: dict) -> None:
    if is_legacy_native_metadata(metadata):
        cfg.private_directory(path)
        verify_legacy_native(path / 'codex', metadata)
    else:
        native.verify(path, metadata)


def reconcile_native_transaction(config: dict) -> None:
    """Reconcile physical states after a stopped install, including process death."""
    root = Path(config['root'])
    path = _receipt_path(root)
    if not _present(path):
        return
    cfg.private_file(path, max_size=65536)
    try:
        value = json.loads(path.read_text())
    except (ValueError, UnicodeError):
        _pending_native()
    required = {'format_version', 'operation', 'installed_sha256', 'old_native',
                'new_native', 'previous', 'stage', 'phase'}
    if (type(value) is not dict or set(value) != required
            or type(value['format_version']) is not int or value['format_version'] != 1
            or not isinstance(value['operation'], str)
            or not re.fullmatch(r'[0-9a-f]{32}', value['operation'])
            or value['phase'] not in {'prepared', 'old_moved', 'new_moved'}
            or value['new_native'] != native.metadata()
            or (value['old_native'] is not None
                and not is_legacy_native_metadata(value['old_native'])
                and value['old_native'] != native.metadata())
            or (value['installed_sha256'] is not None
                and (not isinstance(value['installed_sha256'], str)
                     or not re.fullmatch(r'[0-9a-f]{64}', value['installed_sha256'])))):
        _pending_native()
    for key in ('previous', 'stage'):
        if not isinstance(value[key], str) or not re.fullmatch(r'\.native-' + key + r'-[0-9a-f]{32}', value[key]):
            _pending_native()
    target = root / 'native'
    previous = root / value['previous']
    stage = root / value['stage']
    if any(candidate.is_symlink() for candidate in (target, previous, stage)):
        _pending_native()
    installed_sha = _installed_bytes(root)
    if installed_sha != value['installed_sha256']:
        installed = json.loads((root / 'installed.json').read_text()) if installed_sha else None
        if type(installed) is not dict or installed.get('native') != value['new_native']:
            _pending_native()
        validate_installation(installed)
        native.verify(target, value['new_native'])
        # Metadata committed: never remove or rewrite the retained old tree.
        _clear_receipt(root)
        return

    def unchanged():
        if _installed_bytes(root) != installed_sha:
            _pending_native()

    if _present(previous):
        if value['old_native'] is None:
            _pending_native()
        _verify_previous_native(previous, value['old_native'])
        if _present(target):
            native.verify(target, value['new_native'])
            if _present(stage):
                _pending_native()
            unchanged()
            os.replace(target, stage)
            _sync_directory(root)
        unchanged()
        os.replace(previous, target)
        _sync_directory(root)
        _clear_receipt(root)
        return
    if value['old_native'] is not None:
        # Receipt persisted, but the first rename never happened (or an
        # in-process exception already restored it). Do not wedge safe retries.
        if not _present(target):
            _pending_native()
        _verify_previous_native(target, value['old_native'])
        unchanged()
        _clear_receipt(root)
        return
    # First installation has no predecessor and no committed metadata. Reset
    # to an uninstalled state, retaining the exact new kit as task staging.
    if _present(target):
        native.verify(target, value['new_native'])
        if _present(stage):
            _pending_native()
        unchanged()
        os.replace(target, stage)
        _sync_directory(root)
    elif _present(stage):
        native.verify(stage, value['new_native'])
    unchanged()
    _clear_receipt(root)


def install_native(config: dict) -> tuple[dict, Path | None]:
    """Stage a complete kit; preserve any validated predecessor for install()."""
    root = Path(config['root'])
    archive = root / 'codex-package-0.160.0-linux-x64.tar.gz'
    if archive.exists() or archive.is_symlink():
        cfg.private_file(archive, max_size=None)
    if not archive.exists() or digest(archive) != native.ARCHIVE_SHA256 or archive.stat().st_size != native.ARCHIVE_SIZE:
        temporary = root / ('.download-' + uuid.uuid4().hex)
        try:
            env = instance_environment(config)
            network_command(['curl', '--disable', '--silent', '--show-error', '--fail', '--location', '--proto', '=https',
                '--proto-redir', '=https', '--tlsv1.2', '--max-time', '900', '--output', str(temporary), native.URL],
                env=env, timeout=930, mode=config['settings']['network_mode'],
                phase='原生程序下载', code='native_download_failed')
            temporary.chmod(0o600)
            if temporary.stat().st_size != native.ARCHIVE_SIZE or digest(temporary) != native.ARCHIVE_SHA256:
                raise cfg.InstanceError('官方固定版本的文件大小或校验值不匹配，未运行。', 'native_digest_mismatch')
            os.replace(temporary, archive)
        finally:
            temporary.unlink(missing_ok=True)
    stage = root / ('.native-stage-' + uuid.uuid4().hex)
    previous = root / ('.native-previous-' + uuid.uuid4().hex)
    target = root / 'native'
    moved_previous = False
    try:
        stage.mkdir(mode=0o700)
        native.unpack(archive, stage)
        native.add_license(stage, Path(__file__).resolve().parents[1] / 'desktop/licenses/codex-Apache-2.0.txt')
        native.verify(stage, native.metadata())
        if command([str(stage / 'bin/codex'), '--version'], env=instance_environment(config)).strip() != 'codex-cli ' + NATIVE_VERSION:
            raise cfg.InstanceError('原生程序版本不符，未激活。', 'native_version_mismatch')
        old_metadata = None
        installed_path = root / 'installed.json'
        if installed_path.exists():
            cfg.private_file(installed_path, max_size=None)
            prior_record = json.loads(installed_path.read_text())
            if type(prior_record) is dict: old_metadata = prior_record.get('native')
        if _present(_receipt_path(root)):
            _pending_native()
        if _present(previous):
            _pending_native()
        had_target = _present(target)
        if had_target:
            if target.is_symlink():
                raise cfg.InstanceError('原生目录不安全，未覆盖。', 'unsafe_permissions')
            if old_metadata is None:
                # An exact full kit is safe to retain even if a prior first
                # install stopped before creating its installation metadata.
                native.verify(target, native.metadata())
                old_metadata = native.metadata()
            else:
                _verify_previous_native(target, old_metadata)
        elif old_metadata is not None:
            _pending_native()
        _sync_tree(stage)
        receipt = {'format_version': 1, 'operation': uuid.uuid4().hex, 'installed_sha256': _installed_bytes(root),
                   'old_native': old_metadata, 'new_native': native.metadata(), 'previous': previous.name,
                   'stage': stage.name, 'phase': 'prepared'}
        _write_receipt(root, receipt)
        if had_target:
            os.replace(target, previous)
            moved_previous = True
            _sync_directory(root)
            _write_receipt(root, {**receipt, 'phase': 'old_moved'})
        # Stage has already been fully verified; directory replacement avoids a
        # partially assembled active kit.  The predecessor remains recoverable.
        os.replace(stage, target)
        _sync_directory(root)
        _write_receipt(root, {**receipt, 'phase': 'new_moved'})
        native.verify(target, native.metadata())
        return native.metadata(), (previous if moved_previous else None)
    except BaseException:
        # The same persistent path handles exceptions and restarts. Unknown
        # objects preserve the receipt instead of being recursively deleted.
        try:
            if _present(_receipt_path(root)):
                reconcile_native_transaction(config)
        except BaseException:
            pass
        raise
    finally:
        # Only this call's private temporary staging is cleanup-eligible. An
        # unresolved receipt retains its physical evidence for the next install.
        if stage.exists() and not stage.is_symlink() and not _present(_receipt_path(root)):
            shutil.rmtree(stage)


def commit_native_transaction(previous: Path | None) -> None:
    # Retain a predecessor verbatim: legacy trees may contain unknown owner data.
    return None


def rollback_native_transaction(config: dict, previous: Path | None) -> None:
    # Compatibility entry: never infer rollback from the mere existence of an
    # old directory. The receipt and installed-record digest are authoritative.
    reconcile_native_transaction(config)


def verify_native(path: Path, metadata: dict | None = None) -> None:
    # ``path`` is retained for callers but no longer allows a lone executable.
    root = path.parent.parent if path.name == 'codex' and path.parent.name == 'bin' else path.parent
    native.verify(root, metadata)

def snapshot_source(source: Path, destination: Path) -> tuple[str, str]:
    # The private local Git snapshot carries no upstream .git, user history or remotes.
    if (source / '.git').exists():
        source_sha = bundle._git_source(source)
        destination.mkdir(mode=0o700)
        bundle._archive_source(source, destination, source_sha)
        bundle._assert_archive_complete(source, destination, source_sha)
    else:
        from shared.instance_package import materialize
        source_sha = materialize(source, destination)
    env = {**os.environ, 'HOME': str(destination.parent), 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'}
    for key in list(env):
        if key.startswith('GIT_') and key not in {'GIT_CONFIG_NOSYSTEM', 'GIT_CONFIG_GLOBAL'}:
            env.pop(key)
    for args in (['init', '--quiet'], ['add', '--all'], ['-c', 'user.name=Local snapshot',
            '-c', 'user.email=local@localhost', '-c', 'commit.gpgsign=false', 'commit', '--quiet', '-m', 'Fixed local source snapshot']):
        command(['git', *args], env=env, cwd=destination)
    return source_sha, bundle._git_source(destination)


def install_controller(source: Path, root: Path) -> tuple[str, dict]:
    hashes = {target: digest(source / origin) for origin, target in CONTROLLER_FILES.items()}
    identity = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()[:16]
    parent = root / 'controllers'
    parent.mkdir(mode=0o700, exist_ok=True)
    cfg.private_directory(parent)
    target = parent / identity
    if not target.exists():
        target.mkdir(mode=0o700)
        (target / 'shared').mkdir(mode=0o700)
        for origin, relative in CONTROLLER_FILES.items():
            shutil.copyfile(source / origin, target / relative)
            (target / relative).chmod(0o400)
        (target / 'shared').chmod(0o500)
        target.chmod(0o500)
    verify_controller(target, hashes)
    return target.relative_to(root).as_posix(), hashes


def verify_controller(root: Path, expected: dict, *, legacy=False) -> None:
    cfg.absolute_path(str(root))
    actual = {name: digest(path) for name, path, item in bundle._entries(root)
              if stat.S_ISREG(item.st_mode) and item.st_nlink == 1}
    all_entries = list(bundle._entries(root))
    allowed = set(LEGACY_CONTROLLER_FILES.values()) if legacy else set(CONTROLLER_FILES.values())
    if (actual != expected or set(actual) != allowed
            or any(not (stat.S_ISREG(item.st_mode) or stat.S_ISDIR(item.st_mode)) for _, _, item in all_entries)):
        raise cfg.InstanceError('本地运行控制器校验失败，未继续。', 'controller_changed')


def quote_unit(value: str) -> str:
    if any(ord(c) < 32 for c in value):
        raise cfg.InstanceError('服务参数包含不支持的字符。', 'invalid_path')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def unit_directory() -> Path:
    root = cfg.absolute_path(str(Path.home() / '.config/systemd/user'))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    # A private owner-only ancestor blocks traversal by other users. A
    # group-writable descendant behind that boundary is not publicly writable;
    # do not chmod the user's existing ~/.config/systemd merely to install.
    shielded = False
    for path in reversed((root, *root.parents)):
        info = path.stat()
        if info.st_uid not in {0, os.getuid()} or (not shielded and info.st_mode & 0o022):
            raise cfg.InstanceError('用户服务目录不安全，未写入。', 'unsafe_user_services')
        if info.st_uid == os.getuid() and not info.st_mode & 0o077:
            shielded = True
    return root


def write_services(config: dict, installed: dict) -> dict:
    root = Path(config['root'])
    web, native = units(config)
    marker = '# CodexWorkbench local instance ' + config['instance_id'] + '\n'
    runtime_name = 'codex-web-' + config['instance_id']
    # Use the exact same policy as downloads and native login.  Every proxy
    # key is emitted (including empty values) so a user systemd manager cannot
    # leak stale global proxy values into this instance.
    environment = cfg.environment(config)
    try:
        environment.update(network.policy(config['settings'], os.environ))
    except ValueError:
        raise cfg.InstanceError('网络设置无效，未写入服务。', 'invalid_network_proxy') from None
    envpath = root / 'service.env'
    contents = ''.join(key + '=' + json.dumps(value, ensure_ascii=False) + '\n' for key, value in sorted(environment.items()))
    if envpath.exists():
        cfg.private_file(envpath)
    temporary = root / ('.service-env-' + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            output.write(contents); output.flush(); os.fsync(output.fileno())
        os.replace(temporary, envpath)
    finally:
        temporary.unlink(missing_ok=True)
    common = ('\n[Service]\nType=simple\nUMask=0077\nWorkingDirectory=' + str(root / 'home').replace('%', '%%')
              + '\nEnvironmentFile=' + str(envpath).replace('%', '%%')
              + '\nUnsetEnvironment=OPENAI_API_KEY CODEX_API_KEY OPENAI_ACCESS_TOKEN PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONUSERBASE'
              + '\nRestart=on-failure\nRestartSec=3\nTimeoutStopSec=90\n')
    native_text = marker + '[Unit]\nDescription=Codex Workbench local native instance\n' + common
    native_text += 'RuntimeDirectory=' + runtime_name + '\nRuntimeDirectoryMode=0700\nExecStart=:' + ' '.join(map(quote_unit,
        # systemd rejects certain characters in the executable token even
        # when quoted. env execs the pinned absolute path as an argument,
        # without a shell or an extra long-lived process/PID boundary.
        ['/usr/bin/env', '--', str(native_command(config)), 'app-server', '--listen', 'unix://' + environment['CODEX_APP_SERVER_SOCKET']])) + '\n'
    web_text = marker + '[Unit]\nDescription=Codex Workbench local phone entry\nWants=' + native + '\nAfter=' + native + '\n' + common
    web_text += 'ExecStart=:' + ' '.join(map(quote_unit, ['/usr/bin/python3', '-B', '-I',
        str(root / installed['controller'] / 'launcher.py'), '--state', str(root / 'state'), '--port', str(config['port']), 'web'])) + '\n'
    directory = unit_directory()
    hashes = {}
    for name, text in ((native, native_text), (web, web_text)):
        target = directory / name
        if target.exists():
            cfg.private_file(target)
            if not target.read_text().startswith(marker):
                raise cfg.InstanceError('同名用户服务不属于此实例，未覆盖。', 'service_name_collision')
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            output.write(text); output.flush(); os.fsync(output.fileno())
        hashes[name] = hashlib.sha256(text.encode()).hexdigest()
    # Scalar WorkingDirectory and ExecStart/EnvironmentFile have different
    # parsers. Validate the ACTUAL unit syntax, not just our rendered strings.
    command(['systemd-analyze', '--user', 'verify', *[str(directory / name) for name in (native, web)]],
            env=user_environment())
    command(['systemctl', '--user', 'daemon-reload'], env=user_environment())
    # Parsing success alone can still ignore a malformed EnvironmentFile.
    # Verify the manager's EFFECTIVE binding before reporting installation.
    verify_service_binding(config, {**installed, 'unit_sha256': hashes}, directory=directory)
    return hashes


def verify_service_binding(config: dict, installed: dict, *, directory: Path | None = None) -> None:
    directory = cfg.absolute_path(str(directory or Path.home() / '.config/systemd/user'))
    hashes = installed['unit_sha256']
    if set(hashes) != set(units(config)):
        raise cfg.InstanceError('服务尚未安装或记录不完整，未操作其他服务。', 'services_not_installed')
    for name in units(config):
        path = directory / name
        cfg.private_file(path)
        if digest(path) != hashes[name]:
            raise cfg.InstanceError('此实例的用户服务已被修改，未猜测停止对象。', 'service_changed')
        raw = command(['systemctl','--user','show',name,'--property=FragmentPath,DropInPaths,LoadState,EnvironmentFiles,WorkingDirectory'], env=user_environment())
        state = dict(line.split('=',1) for line in raw.splitlines() if '=' in line)
        if (state.get('FragmentPath') != str(path) or state.get('DropInPaths') or state.get('LoadState') != 'loaded'
                or state.get('EnvironmentFiles') != str(Path(config['root']) / 'service.env') + ' (ignore_errors=no)'
                or state.get('WorkingDirectory') != str(Path(config['root']) / 'home')):
            raise cfg.InstanceError('服务存在覆盖或实际加载位置不符，未操作。', 'service_changed')


def validate_installation(value: object) -> None:
    if (type(value) is not dict or set(value) != {'format_version', 'source_commit', 'runtime_commit',
            'release', 'controller', 'controller_sha256', 'native', 'unit_sha256'} or type(value['format_version']) is not int or value['format_version'] != 1
            or not re.fullmatch(r'controllers/[0-9a-f]{16}', str(value['controller']))):
        raise cfg.InstanceError('安装记录不完整，未猜测或接管其他软件。', 'invalid_installation')


def software(config: dict, *, verify=True, legacy_native=False) -> dict:
    cfg.validate_layout(config)
    path = Path(config['root']) / 'installed.json'
    cfg.private_file(path)
    value = json.loads(path.read_text())
    validate_installation(value)
    root = Path(config['root'])
    if _present(_receipt_path(root)) and not legacy_native:
        raise cfg.InstanceError('原生安装事务未决，未运行。', 'native_transaction_pending')
    if verify:
        manifest = release._verify_release(root / 'state/releases', value['release'], trusted_base_interpreter=HOST_PYTHON)
        if manifest['commit'] != value['runtime_commit']:
            raise cfg.InstanceError('安装记录与程序内容不一致。', 'invalid_installation')
        is_legacy = is_legacy_native_metadata(value['native'])
        if is_legacy and not legacy_native:
            raise cfg.InstanceError('旧版单文件原生程序不完整；只能在安装迁移备份中捕获资料并重建。', 'legacy_native_incomplete')
        verify_controller(root / value['controller'], value['controller_sha256'], legacy=is_legacy)
        if is_legacy:
            verify_legacy_native(root / 'native/codex', value['native'])
        else:
            verify_native(native_command(config), value['native'])
    return value


def installed_python(config: dict, installed: dict | None = None) -> tuple[Path, Path]:
    installed = installed or software(config)
    releases = Path(config['root']) / 'state/releases'
    selected = release._current_stamp(releases)
    stamp = selected or installed['release']
    runtime = releases / stamp / 'runtime'
    bundle.verify_bundle(runtime, trusted_base_interpreter=HOST_PYTHON)
    return runtime / '.venv/bin/python', runtime / 'app'


def runtime_json(config: dict, code: str, *, timeout=30, args=(), installed: dict | None = None) -> dict | list:
    # Reuse a lifecycle caller's already validated installation proof. Only
    # safe-stop supplies a legacy proof; normal callers retain strict defaults.
    python, app = installed_python(config, installed)
    raw = command([str(python), '-B', '-I', '-c', 'import sys;sys.path.insert(0,sys.argv[1]);' + code,
                   str(app), *args], env=instance_environment(config), cwd=app, timeout=timeout)
    return json.loads(raw)


def entry_matrix(config: dict) -> list:
    return runtime_json(config, "import json,qrcode;qr=qrcode.QRCode(border=0);qr.add_data(sys.argv[2]);qr.make(fit=True);print(json.dumps(qr.get_matrix()))",
                        args=[config['settings']['phone_origin'] + '/login'])


def bootstrap_account(config: dict) -> None:
    path = Path(config['root']) / 'state/accounts.json'
    expected = {config['account']: {'role': 'admin', 'enabled': True, 'totp': ''}}
    if path.exists():
        cfg.private_file(path)
        current = json.loads(path.read_text())
        if len(current) != 1 or not current.get(config['account'], {}).get('enabled') or current[config['account']].get('role') != 'admin':
            raise cfg.InstanceError('已有账号与所选实例不符，未覆盖。', 'account_conflict')
    else:
        cfg.write_json(path, expected, replace=False)
    secret = path.parent / 'session_secret'
    if not secret.exists():
        fd = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as output:
            output.write(secrets.token_urlsafe(48)); output.flush(); os.fsync(output.fileno())
    cfg.private_file(secret)
    # No TOTP enable marker, legacy secret or anonymous /setup is created.


def install(config: dict, source: Path) -> dict:
    if sys.version_info[:2] != (3, 12) or platform.machine() != 'x86_64':
        raise cfg.InstanceError('首版需要 Ubuntu 24.04 的 Python 3.12 / x86-64 环境。', 'unsupported_platform')
    user_environment()
    unit_directory()  # Fail environment checks before downloads/building.
    if any(unit_status(name) not in {'inactive', 'failed', 'not_installed'} for name in units(config)):
        raise cfg.InstanceError('此实例仍在运行或状态未知；先用“停止”等待在途任务结束，再安装。', 'instance_running')
    root = Path(config['root'])
    reconcile_native_transaction(config)
    releases = root / 'state/releases'
    releases.mkdir(mode=0o700, exist_ok=True)
    pending = release._pending(releases)
    if pending is not None:
        if pending[1] is not None:
            raise cfg.InstanceError('上次升级核验尚未结束，请先检查；未覆盖未决版本。', 'pending_runtime')
        # Both dedicated units were proved stopped above; no healthy fallback
        # exists for this first activation, so retain it as failed, not healthy.
        release.withdraw_initial_runtime(releases, pending[0])
    previous = release._current_stamp(releases)
    if previous:
        # A stopped coherent data + runtime backup precedes any upgrade.
        from shared.instance_recovery import backup
        backup(config, legacy_native=is_legacy_native_metadata(software(config, verify=False)['native']))
    build = root / 'build'
    build.mkdir(mode=0o700, exist_ok=True)
    stage = build / uuid.uuid4().hex
    stage.mkdir(mode=0o700)
    origin, commit = snapshot_source(source, stage / 'source')
    env = instance_environment(config)
    command(['/usr/bin/python3', '-I', '-m', 'venv', str(stage / '.venv')], env=env, timeout=60)
    python = stage / '.venv/bin/python'
    pip_env = network.pip_environment(env)
    network_command([str(python), '-I', '-m', 'pip', '--isolated', 'install', '--index-url', 'https://pypi.org/simple',
        '--disable-pip-version-check', '-r', str(stage / 'source/requirements-core.lock')],
        env=pip_env, timeout=900, mode=config['settings']['network_mode'],
        phase='依赖下载', code='dependency_download_failed')
    command([str(python), '-I', str(stage / 'source/scripts/check-core-deps.py')], env=env)
    command([str(python), '-I', '-m', 'pip', '--isolated', 'check'], env=pip_env)
    native_metadata, native_previous = install_native(config)
    try:
        controller, hashes = install_controller(stage / 'source', root)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
        snapshot = releases / stamp
        snapshot.mkdir(mode=0o700)
        release.prepare_runtime_release(stage / 'source', snapshot, previous, dependency_source=stage)
        value = {'format_version': 1, 'source_commit': origin, 'runtime_commit': commit, 'release': stamp,
                 'controller': controller, 'controller_sha256': hashes, 'native': native_metadata, 'unit_sha256': {}}
        bootstrap_account(config)
        value['unit_sha256'] = write_services(config, value)
        cfg.write_json(root / 'installed.json', value)
        reconcile_native_transaction(config)
    except BaseException:
        try:
            if _present(_receipt_path(root)):
                reconcile_native_transaction(config)
        except BaseException:
            # Keep the primary controller/runtime/bootstrap failure visible; an
            # unverifiable replacement is retained beside its predecessor rather
            # than deleting either tree during error handling.
            pass
        raise
    commit_native_transaction(native_previous)
    result = {'release': stamp, 'native_version': NATIVE_VERSION, 'note': '软件已安装，未自动启动或登录；还需配置手机 HTTPS 入口并由本人完成原生登录。'}
    if native_previous is not None:
        result['retained_native'] = str(native_previous)
        result['note'] += ' 此前原生目录已原样保留，未自动清理。'
    return result


def native_login_ready(config: dict) -> bool:
    result = subprocess.run([str(native_command(config)), 'login', 'status'],
        env=instance_environment(config), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    return result.returncode == 0


def start(config: dict) -> dict:
    if (Path(config['root']) / 'restore-incomplete.json').exists():
        raise cfg.InstanceError('此恢复副本未完成，不能启动；请保留证据并恢复到另一个新目录。', 'restore_incomplete')
    installed = software(config)
    if unit_status(units(config)[0]) == 'active':
        return {'note': '此实例已在运行；本次没有重启，也没有应用新保存的部署设置。'}
    if not config['settings']['phone_origin']:
        raise cfg.InstanceError('先在电脑面板填好自己的手机 HTTPS 入口；没有更改任何网络。', 'phone_origin_missing')
    if not native_login_ready(config):
        raise cfg.InstanceError('请先点“原生账号登录”，在弹出的原生窗口由本人完成登录。', 'native_login_required')
    root = Path(config['root'])
    releases = root / 'state/releases'
    selected = release._current_stamp(releases)
    if selected != installed['release']:
        if release._pending(releases) is not None:
            raise cfg.InstanceError('存在未决版本事务，未覆盖。', 'pending_runtime')
        release.write_runtime_pending(releases, installed['release'], selected)
        release.handoff_runtime(releases, installed['release'])
    # New config values become effective only on an explicit lifecycle action.
    installed['unit_sha256'] = write_services(config, installed)
    cfg.write_json(root / 'installed.json', installed)
    verify_service_binding(config, installed)
    command(['systemctl', '--user', 'start', *reversed(units(config))], env=user_environment(), timeout=30)
    deadline = time.monotonic() + 190
    while time.monotonic() < deadline:
        current = release._current_stamp(releases)
        if current and release._status(releases, current) == 'healthy' and unit_status(units(config)[0]) == 'active':
            try:
                proof = runtime_json(config, 'from shared.instance_lifecycle import ready;import json;print(json.dumps(ready(sys.argv[2])))',
                                     timeout=12, args=[str(config['port'])])
            except (cfg.InstanceError, OSError, ValueError, subprocess.SubprocessError):
                proof = {'ready': False}
            if proof.get('ready') is not True:
                time.sleep(.5)
                continue
            if current != installed['release']:
                installed.update(release=current, runtime_commit=release._read_meta(releases,current)['commit'])
                cfg.write_json(root / 'installed.json', installed)
                raise cfg.InstanceError('新版本未通过核验，已由运行控制器回到上一健康版本；数据未回滚。', 'runtime_rolled_back')
            return {'release': current, 'note': '实例已通过启动与核心链路核验；手机可达和本人绑定仍需分别确认。'}
        if unit_status(units(config)[0]) == 'failed':
            break
        time.sleep(.5)
    raise cfg.InstanceError('启动或核验未完成，未宣称可用；保留数据与版本，请检查状态。', 'startup_not_verified')


def stop(config: dict) -> dict:
    web, native = units(config)
    states = {name: unit_status(name) for name in (web, native)}
    if all(value in {'inactive', 'failed', 'not_installed'} for value in states.values()):
        return {'note': '此实例已经停止；未触及其他工作台。'}
    if any(value not in {'active', 'inactive', 'failed', 'not_installed'} for value in states.values()):
        raise cfg.InstanceError('实例状态未稳定，未强行停止。', 'service_state_unknown')
    installed = software(config, legacy_native=True)
    verify_service_binding(config, installed)
    # Use the actual selected app's protocol/fingerprint, never a newer manager
    # source's expectations. The gate stays closed from proof through stop.
    proof = runtime_json(config, "from shared.instance_lifecycle import drain;import json;print(json.dumps(drain(sys.argv[2],sys.argv[3],sys.argv[4])))",
                 timeout=45, args=[config['instance_id'], str(config['port']), states[web]], installed=installed)
    if proof.get('idle') is not True:
        raise cfg.InstanceError('仍有在途任务或无法证明空闲，未停止、未中断；稍后再试。', 'instance_busy')
    try:
        command(['systemctl', '--user', 'stop', web, native], env=user_environment(), timeout=120)
        if any(unit_status(name) not in {'inactive', 'failed'} for name in (web, native)):
            raise cfg.InstanceError('服务仍未确认停止，停写闸保持关闭。', 'stop_unconfirmed')
    except BaseException:
        # Do not reopen ingress unless stop or live idle is actually proven.
        raise
    runtime_json(config, "from shared.instance_lifecycle import reopen;import json;print(json.dumps(reopen(sys.argv[2])))", args=[config['instance_id']], installed=installed)
    return {'note': '仅此实例已停止；先停写并核验在途任务为空，没有中断任务。'}


def authorize(config: dict) -> dict:
    if not config['settings']['phone_origin']:
        raise cfg.InstanceError('先配置此实例自己的 HTTPS 手机入口。', 'phone_origin_missing')
    # Existing bound phones are never silently replaced or revoked.
    from shared.instance_manager import credential_exists
    if credential_exists(config):
        raise cfg.InstanceError('此实例已有绑定；日常请在手机登录。本次不会替换手机或撤销会话。', 'phone_already_bound')
    result = runtime_json(config, "from shared.passkey_auth import issue_registration_authorization;import json;code,_=issue_registration_authorization(sys.argv[2],replace=False);print(json.dumps({'one_time_code':code}))", args=[config['account']])
    return {**result, 'expires_seconds': 600,
            'login_url': config['settings']['phone_origin'] + '/auth/passkey/recover'}


def operate(path: Path, action: str, source: Path, payload=None) -> dict:
    with cfg.exclusive(path):
        config = cfg.read(path)
        if config is None:
            raise cfg.InstanceError('请先准备独立实例。', 'not_initialized')
        if action == 'install':
            return install(config, source)
        if action == 'start':
            return start(config)
        if action == 'stop':
            return stop(config)
        if action == 'restart':
            stop(config)
            return start(config)
        if action == 'login':
            software(config)
            # No stdout capture, codes in JSON, browser token copying, or shell.
            result = subprocess.run([str(native_command(config)), 'login', '--device-auth'],
                                    env=instance_environment(config))
            if result.returncode:
                raise cfg.InstanceError('原生本人登录尚未完成，请在原生窗口查看原因。', 'native_login_incomplete')
            return {}
        if action == 'authorize-phone':
            return authorize(config)
        from shared import instance_recovery
        if action == 'backup':
            return instance_recovery.backup(config)
        if action == 'restore':
            return instance_recovery.restore(config, payload)
        if action == 'rollback':
            return instance_recovery.rollback(config)
        if action == 'uninstall':
            return instance_recovery.uninstall(config)
        raise cfg.InstanceError('不支持的本机操作。', 'unsupported_action')
