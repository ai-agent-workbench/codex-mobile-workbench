"""Private, local deployment data for one optional Windows/WSL instance.

No browser control API, account credentials, service restart or network change
belongs here. Reads are side-effect free; initialization never adopts data.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import stat
import uuid
from urllib.parse import urlsplit


class InstanceError(ValueError):
    def __init__(self, message: str, code: str = 'instance_error'):
        super().__init__(message)
        self.code = code


LEGACY_SETTINGS = {'phone_origin', 'automations', 'completion_notifications',
                   'notification_command', 'notification_topic_file'}
SETTINGS = LEGACY_SETTINGS | {'network_mode', 'network_proxy'}


def absolute_path(value: str) -> Path:
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise InstanceError('请选择有效的 WSL 绝对路径。', 'invalid_path')
    path = Path(value).expanduser()
    if not path.is_absolute() or '..' in path.parts:
        raise InstanceError('需要不含上级跳转的 WSL 绝对路径。', 'invalid_path')
    for item in (path, *path.parents):
        if item.is_symlink():
            raise InstanceError('实例路径不能经过符号链接。', 'unsafe_path')
    return path


def private_directory(path: Path) -> None:
    absolute_path(str(path))
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise InstanceError('实例目录需属于当前 WSL 用户，且仅本人可访问。', 'unsafe_permissions')


def private_file(path: Path, *, max_size: int | None = 65536) -> None:
    absolute_path(str(path))
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1 or (max_size is not None and info.st_size > max_size)):
        raise InstanceError('实例配置文件权限或类型不安全。', 'unsafe_permissions')


def phone_origin(value: object) -> str:
    if value == '':
        return ''
    if not isinstance(value, str) or len(value) > 512 or any(c.isspace() or c == '\\' for c in value):
        raise InstanceError('手机入口应为不含账号密码的 HTTPS 地址。', 'invalid_origin')
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path not in {'', '/'}
                or parsed.query or parsed.fragment or (port is not None and not 1 <= port <= 65535)):
            raise ValueError()
        host = parsed.hostname.encode('idna').decode('ascii').lower()
        if ':' in host:
            host = '[' + host + ']'
        return 'https://' + host + (':' + str(port) if port not in {None, 443} else '')
    except (ValueError, UnicodeError):
        raise InstanceError('手机入口需要完整 HTTPS 来源地址，不含路径、口令或查询参数。', 'invalid_origin') from None


def settings(value: object) -> dict:
    # Version-1 profiles used the five deployment fields.  Read them as the
    # safe default without rewriting until a caller explicitly saves.
    if type(value) is not dict or set(value) != LEGACY_SETTINGS and set(value) != SETTINGS:
        raise InstanceError('部署配置字段不完整或包含未知内容。', 'invalid_config')
    output = dict(value)
    output.setdefault('network_mode', 'inherit')
    output.setdefault('network_proxy', '')
    if not isinstance(output['network_mode'], str) or output['network_mode'] not in {'inherit', 'direct', 'proxy'}:
        raise InstanceError('网络方式无效，未保存。', 'invalid_network_mode')
    if not isinstance(output['network_proxy'], str):
        raise InstanceError('代理地址无效，未保存。', 'invalid_network_proxy')
    if output['network_mode'] == 'proxy':
        from shared.instance_network import valid_proxy
        if not valid_proxy(output['network_proxy']):
            raise InstanceError('指定代理地址无效或缺失，未保存。', 'invalid_network_proxy')
    elif output['network_proxy']:
        # A proxy is meaningful only when explicitly selected.
        output['network_proxy'] = ''
    for key in ('automations', 'completion_notifications'):
        if type(output[key]) is not bool:
            raise InstanceError('可选功能必须明确选择启用或停用。', 'invalid_config')
    output['phone_origin'] = phone_origin(output['phone_origin'])
    for key in ('notification_command', 'notification_topic_file'):
        if not isinstance(output[key], str):
            raise InstanceError('通知配置只接受私有文件路径，不接受主题内容。', 'invalid_config')
        if output[key]:
            output[key] = str(absolute_path(output[key]))
    if output['completion_notifications'] and not all(output[key] for key in ('notification_command', 'notification_topic_file')):
        raise InstanceError('先配置自己的任务通知发送器与私有主题文件路径，再启用完成通知。', 'notification_not_configured')
    return output


def validate(value: object, path: Path) -> dict:
    fields = {'format_version', 'instance_id', 'root', 'account', 'port', 'settings'}
    if (type(value) is not dict or set(value) != fields
            or type(value['format_version']) is not int or value['format_version'] != 1):
        raise InstanceError('不支持的实例配置，未覆盖或自动重置。', 'invalid_config')
    if not isinstance(value['instance_id'], str) or not re.fullmatch('[0-9a-f]{12}', value['instance_id']):
        raise InstanceError('实例标识无效。', 'invalid_config')
    root = absolute_path(value['root'])
    if path != root / 'instance.json':
        raise InstanceError('配置文件与实例根不匹配，未接管其他目录。', 'invalid_config')
    account = value['account']
    if not isinstance(account, str) or not account or account != account.strip() or len(account) > 128 or any(ord(c) < 32 for c in account):
        raise InstanceError('管理员名称需要是非空短文本。', 'invalid_account')
    if type(value['port']) is not int or not 1024 <= value['port'] <= 65535:
        raise InstanceError('本机端口无效。', 'invalid_config')
    return {**value, 'settings': settings(value['settings'])}


def read(path: Path) -> dict | None:
    path = absolute_path(str(path))
    if not path.exists():
        return None
    private_directory(path.parent)
    private_file(path)
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, UnicodeError):
        raise InstanceError('实例配置不可读取，未自动重置。', 'invalid_config') from None
    return validate(value, path)


def write_json(path: Path, value: dict, *, replace: bool = True) -> None:
    private_directory(path.parent)
    if path.exists():
        if not replace:
            raise InstanceError('已有配置，未覆盖。', 'already_exists')
        private_file(path, max_size=None)
    temporary = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(value, output, ensure_ascii=False, sort_keys=True, indent=2)
            output.flush()
            os.fsync(output.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            # Do not race a new file into replacing an existing profile.
            os.link(temporary, path, follow_symlinks=False)
            temporary.unlink()
        directory = os.open(path.parent, os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def exclusive(path: Path):
    private_directory(path.parent)
    lock = path.parent / '.manager.lock'
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise InstanceError('管理锁不安全，未继续。', 'unsafe_permissions')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstanceError('此实例有另一项操作进行中；请检查状态，不要重复安装。', 'operation_in_progress') from None
        yield
    finally:
        os.close(descriptor)


LAYOUT = ('home', 'state', 'workspace', 'workspace/projects', 'workspace/inbox',
          'workspace/conversations', 'native-home', 'backups')


def validate_layout(config: dict) -> None:
    root = Path(config['root'])
    private_directory(root)
    for name in LAYOUT:
        private_directory(root / name)
    # Native login must never follow a prepared credential/config alias into
    # another HOME. Inspect metadata only; do not read credentials.
    for name in ('auth.json', 'config.toml'):
        path = root / 'native-home' / name
        if path.exists() or path.is_symlink():
            private_file(path, max_size=None)


def _ensure_directories(root: Path) -> None:
    for name in LAYOUT:
        directory = root / name
        directory.mkdir(mode=0o700, exist_ok=True)
        private_directory(directory)


def new_profile(path: Path, payload: dict) -> dict:
    if set(payload) != {'root', 'account'}:
        raise InstanceError('安装准备只接受实例目录和管理员名。', 'invalid_config')
    path = absolute_path(str(path))
    root = absolute_path(payload['root'])
    if path != root / 'instance.json':
        raise InstanceError('配置文件必须位于所选实例目录内。', 'invalid_config')
    home = Path.home().resolve()
    if home not in root.parents or any(str(root).startswith(prefix) for prefix in ('/mnt/', '/tmp/', '/run/')):
        raise InstanceError('首版请使用当前 WSL 用户主目录下的新目录，不能放在 Windows 挂载盘或临时目录。', 'invalid_root')
    # Validate every input before creating a directory or persistent lock.
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    value = validate({'format_version': 1, 'instance_id': uuid.uuid4().hex[:12],
        'root': str(root), 'account': payload['account'], 'port': port,
        'settings': {'phone_origin': '', 'automations': False, 'completion_notifications': False,
                     'notification_command': '', 'notification_topic_file': '',
                     'network_mode': 'inherit', 'network_proxy': ''}}, path)
    return value


def initialize(path: Path, payload: dict) -> dict:
    if set(payload) != {'root', 'account'}:
        raise InstanceError('安装准备只接受实例目录和管理员名。', 'invalid_config')
    path = absolute_path(str(path))
    root = absolute_path(payload['root'])
    if path != root / 'instance.json':
        raise InstanceError('配置文件必须位于所选实例目录内。', 'invalid_config')
    home = Path.home().resolve()
    if home not in root.parents or any(str(root).startswith(prefix) for prefix in ('/mnt/', '/tmp/', '/run/')):
        raise InstanceError('首版请使用当前 WSL 用户主目录下的新目录，不能放在 Windows 挂载盘或临时目录。', 'invalid_root')
    existing = read(path)
    if existing is not None:
        if existing['account'] != payload['account']:
            raise InstanceError('已有实例管理员不同，未覆盖。', 'already_exists')
        with exclusive(path):
            _ensure_directories(root)
        return existing
    if root.exists() and any(root.iterdir()):
        raise InstanceError('目录已有数据但不是本面板实例；请选择新的空目录，未接管或删除。', 'directory_not_empty')
    value = new_profile(path, payload)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_directory(root)
    with exclusive(path):
        if path.exists():
            raise InstanceError('另一操作已创建配置，请重新检查。', 'already_exists')
        write_json(path, value, replace=False)
        _ensure_directories(root)
        return value


def environment(config: dict) -> dict[str, str]:
    validate_layout(config)
    root = Path(config['root'])
    options = config['settings']
    runtime = Path('/run/user') / str(os.getuid()) / ('codex-web-' + config['instance_id'])
    origin = options['phone_origin']
    return {
        'HOME': str(root / 'home'), 'CODEX_HOME': str(root / 'native-home'),
        'XDG_RUNTIME_DIR': str(runtime.parent),
        'CODEX_WEB_STATE_DIR': str(root / 'state'), 'CODEX_WEB_ADMIN_ACCOUNT': config['account'],
        'CODEX_WEB_PROJECTS_DIR': str(root / 'workspace/projects'), 'CODEX_WEB_INBOX_DIR': str(root / 'workspace/inbox'),
        'CODEX_WEB_CONVERSATION_DIR': str(root / 'workspace/conversations'),
        'CODEX_WEB_FILE_ROOTS': os.pathsep.join(str(root / p) for p in ('workspace/projects', 'workspace/inbox', 'workspace/conversations')),
        'CODEX_WEB_FILE_GUARDED_PATHS': os.pathsep.join(str(root / p) for p in ('state', 'native-home')),
        # A user systemd manager can retain another deployment's environment.
        # Personal integrations are not silently inherited by a core instance.
        **{name: '0' for name in ('CODEX_WEB_DOT_ENABLED', 'CODEX_WEB_DOT_LOCAL_RELAY_ENABLED',
            'CODEX_WEB_DOT_MCP_ENABLED', 'CODEX_WEB_DOT_MCP_REPLY_ENABLED',
            'CODEX_WEB_DOT_EVENTS_OUTBOUND_ENABLED', 'CODEX_WEB_DOT_NTFY_ENABLED')},
        'CODEX_WEB_MANAGED_INSTALL': '1', 'CODEX_WEB_AUTOMATIONS_ENABLED': str(int(options['automations'])),
        'CODEX_WEB_COMPLETION_NOTIFICATIONS_ENABLED': str(int(options['completion_notifications'])),
        'CODEX_WEB_TASK_NOTIFICATION_COMMAND': options['notification_command'] or str(root / 'unconfigured-task-sender'),
        'CODEX_WEB_TASK_NTFY_TOPIC_FILE': options['notification_topic_file'] or str(root / 'unconfigured-task-topic'),
        'CODEX_WEB_SECURITY_NTFY_TOPIC_FILE': str(root / 'security-alert-topic'),
        'CODEX_WEB_PASSKEY_ENABLED': '1' if origin else '0',
        'CODEX_WEB_PASSKEY_ORIGIN': origin, 'CODEX_WEB_PASSKEY_RP_ID': urlsplit(origin).hostname or '',
        'CODEX_APP_SERVER_SOCKET': str(runtime / 'app-server.sock'),
        'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1',
    }
