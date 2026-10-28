"""One local manager for a separate owner-owned WSL instance.

The desktop window is only a client of this CLI. Ordinary conversation
execution/permissions remain in the existing native runtime, not this module.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from urllib.parse import urlsplit

from shared import instance_config as cfg
from shared import instance_network as network


SOURCE = Path(__file__).resolve().parents[1]
ACTIONS = ('inspect', 'initialize', 'configure', 'install', 'start', 'stop', 'restart',
           'login', 'authorize-phone', 'backup', 'restore', 'rollback', 'uninstall')


def revision(config: dict) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def user_environment() -> dict[str, str]:
    environment = os.environ.copy()
    runtime = Path('/run/user') / str(os.getuid())
    if not (runtime / 'bus').is_socket():
        raise cfg.InstanceError('WSL 用户服务尚不可用；请先完成 Ubuntu/systemd 用户环境准备。', 'user_services_unavailable')
    environment.update(XDG_RUNTIME_DIR=str(runtime), DBUS_SESSION_BUS_ADDRESS='unix:path=' + str(runtime / 'bus'))
    return environment


def units(config: dict) -> tuple[str, str]:
    prefix = 'codex-web-local-' + config['instance_id']
    return prefix + '.service', prefix + '-native.service'


def unit_status(name: str) -> str:
    try:
        result = subprocess.run(['systemctl', '--user', 'show', name, '--property=LoadState,ActiveState'],
            env=user_environment(), capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return 'not_checked'
        fields = dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
        if fields.get('LoadState') == 'not-found' and fields.get('ActiveState') == 'inactive':
            return 'not_installed'
        value = fields.get('ActiveState')
        return value if fields.get('LoadState') == 'loaded' and value in {'active','activating','deactivating','inactive','failed'} else 'not_checked'
    except (cfg.InstanceError, OSError, subprocess.TimeoutExpired):
        return 'not_checked'


def credential_exists(config: dict) -> bool:
    path = Path(config['root']) / 'state' / 'auth.sqlite3'
    if not path.exists():
        return False
    cfg.private_file(path, max_size=None)
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as connection:
        table = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='passkey_credentials'").fetchone()
        return bool(table and connection.execute('SELECT 1 FROM passkey_credentials WHERE revoked_at IS NULL LIMIT 1').fetchone())


def check(name: str, title: str, status: str, detail: str = '') -> dict:
    return {'name': name, 'title': title, 'status': status, 'detail': detail}


def inspect(path: Path) -> dict:
    config = cfg.read(path)
    checks = [check('python', 'Python 3.12', 'ok' if sys.version_info[:2] == (3, 12) else 'missing'),
              check('git', '源码快照工具', 'ok' if shutil.which('git') else 'missing')]
    try:
        release = Path('/etc/os-release').read_text()
        supported = 'ID=ubuntu' in release and 'VERSION_ID="24.04"' in release
    except OSError:
        supported = False
    checks.append(check('curl', 'HTTPS 下载工具', 'ok' if shutil.which('curl') else 'missing'))
    checks.append(check('venv', 'Python 独立安装环境', 'ok' if importlib.util.find_spec('ensurepip') else 'missing'))
    checks.append(check('platform', 'Ubuntu 24.04 WSL2 目标环境', 'ok' if supported else 'not_checked',
                        '首版只核验此组合；其他环境不自动修改或安装。'))
    try:
        user_environment()
        checks.append(check('user_services', '本人 WSL 用户服务', 'ok'))
    except cfg.InstanceError:
        checks.append(check('user_services', '本人 WSL 用户服务', 'missing', '需要可用的 systemd 用户服务。'))
    options = config['settings'] if config else {'network_mode': 'inherit', 'network_proxy': ''}
    network_state = {**network.summary(options, os.environ),
                     'note': '下载与下次原生登录使用已保存配置；后台服务在显式启动或重启后生效。'
                             '不自动读取 Windows 系统代理，也未测试模型或手机连通性。'}
    checks.append(check('network_policy', '联网方式', 'not_checked', network_state['description']))
    if config is None:
        return {'instance_found': False, 'root': str(path.parent), 'account': '', 'revision': 'absent',
                'local_address': None,
                'settings': {'network_mode': 'inherit', 'network_proxy': ''},
                'network': network_state,
                'checks': checks + [check('instance', '实例安装准备', 'missing')],
                'services': {'web': 'not_installed', 'app_server': 'not_installed'},
                'phone_entry': None, 'native_login': 'not_checked', 'warnings': []}
    root = Path(config['root'])
    web, native = units(config)
    checks.append(check('instance', '本人独立实例', 'ok', '只管理此目录，不接管原有工作台。'))
    installed = root / 'installed.json'
    software_status = 'missing'
    if installed.exists():
        try:
            from shared.instance_runtime import software, instance_environment
            software(config)
            software_status = 'ok'
        except (cfg.InstanceError, OSError, ValueError):
            software_status = 'error'
    checks.append(check('software', '核心程序与依赖', software_status,
                        '按本地固定清单核验；安装不等于已启动或已登录。'))
    origin = config['settings']['phone_origin']
    checks.append(check('phone_origin', '手机 HTTPS 入口配置', 'ok' if origin else 'missing',
                        '保存地址不等于改变网络或证明手机可达。'))
    checks.append(check('phone_identity', '本人手机绑定', 'ok' if credential_exists(config) else 'missing'))
    login_state = 'not_checked'
    from shared.instance_native import command as native_command
    command = native_command(root)
    if software_status == 'ok' and command.is_file():
        try:
            result = subprocess.run([str(command), 'login', 'status'],
                env=instance_environment(config), stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=15)
            login_state = 'ready' if result.returncode == 0 else 'missing'
        except (OSError, subprocess.TimeoutExpired):
            login_state = 'missing'
    checks.append(check('native_login', '原生账号登录', 'ok' if login_state == 'ready' else login_state,
                        '仅核对独立实例的本地登录状态，不调用模型、不复制其他登录。'))
    checks.append(check('model_connection', '模型实际连接', 'not_checked',
                        '账号已登录不等于模型可用；以首次任务实际返回为准，本检查不调用模型。'))
    checks.append(check('phone_connection', '手机当前可达', 'not_checked',
                        '需手机在实际使用的网络打开入口；保存地址或已有绑定不代替这项验证。'))
    entry = {'url': origin + '/login', 'qr_matrix': None} if origin else None
    if entry is not None and installed.is_file():
        # QR is rendered by the installed core runtime, not a Windows dependency.
        try:
            from shared.instance_runtime import entry_matrix
            if software_status == 'ok':
                entry['qr_matrix'] = entry_matrix(config)
        except (ImportError, cfg.InstanceError):
            pass
    warnings = ['安装、本人原生登录、手机 HTTPS 连接及手机本人验证是不同检查，不能互相替代。']
    public_settings = {**config['settings'], 'network_proxy': ''}
    return {'instance_found': True, 'root': config['root'], 'account': config['account'],
            'settings': public_settings, 'network': network_state, 'revision': revision(config), 'checks': checks,
            'local_address': 'http://127.0.0.1:' + str(config['port']),
            'services': {'web': unit_status(web), 'app_server': unit_status(native)},
            'phone_entry': entry, 'native_login': login_state, 'warnings': warnings}


def configure(path: Path, value: dict) -> dict:
    legacy = cfg.LEGACY_SETTINGS | {'expected_revision'}
    allowed = legacy | {'network_mode', 'network_proxy'}
    if type(value) is not dict or not legacy <= set(value) or not set(value) <= allowed:
        raise cfg.InstanceError('请先重新检查实例，再保存完整部署设置。', 'invalid_config')
    with cfg.exclusive(path):
        config = cfg.read(path)
        if config is None:
            raise cfg.InstanceError('尚未准备实例。', 'not_initialized')
        if value['expected_revision'] != revision(config):
            raise cfg.InstanceError('部署设置已被其他操作更改；请重新检查后比较，未覆盖。', 'revision_conflict')
        # Old five-field panel payloads deliberately preserve the private
        # network choice.  A mode-only/new null proxy retains a saved proxy;
        # moving away from proxy clears it so it cannot revive later.
        options_input = {key: value[key] for key in cfg.LEGACY_SETTINGS}
        mode = value.get('network_mode', config['settings']['network_mode'])
        supplied_proxy = value.get('network_proxy', None)
        if mode == 'proxy':
            selected = config['settings']['network_proxy'] if supplied_proxy is None else supplied_proxy
            if not selected:
                raise cfg.InstanceError('请选择有效的指定代理地址后再保存。', 'invalid_network_proxy')
        else:
            selected = ''
        options = cfg.settings({**options_input, 'network_mode': mode, 'network_proxy': selected})
        if options['phone_origin'] != config['settings']['phone_origin'] and credential_exists(config):
            raise cfg.InstanceError('已有手机绑定；更换 HTTPS 来源需要单独迁移认证，未自动替换或撤销。', 'bound_origin_change')
        if options['completion_notifications']:
            for key in ('notification_command', 'notification_topic_file'):
                item = cfg.absolute_path(options[key])
                if not item.is_file() or not os.access(item, os.R_OK):
                    raise cfg.InstanceError('通知发送器或私有主题文件尚不可读；未启用，也未发送测试通知。', 'notification_not_configured')
        config = {**config, 'settings': options}
        cfg.write_json(path, config)
        return {'revision': revision(config), 'restart_required': True,
                'note': '仅保存此实例的部署设置；下载和下次原生登录立即使用，后台服务需显式启动或重启。'
                        '未更改网关、组网或防火墙。'}


def input_object() -> dict:
    raw = sys.stdin.buffer.read(65537)
    if len(raw) > 65536:
        raise cfg.InstanceError('配置输入过大。', 'invalid_input')
    try:
        value = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError):
        raise cfg.InstanceError('需要 UTF-8 JSON 配置输入。', 'invalid_input') from None
    if type(value) is not dict:
        raise cfg.InstanceError('需要 JSON 对象。', 'invalid_input')
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='本人本机工作台实例管理（不是远程命令接口）')
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('action', choices=ACTIONS)
    args = parser.parse_args(argv)
    try:
        path = cfg.absolute_path(str(args.config))
        if args.action == 'inspect':
            result = inspect(path)
        elif args.action == 'initialize':
            config = cfg.initialize(path, input_object())
            result = {'instance_found': True, 'root': config['root'], 'revision': revision(config),
                      'note': '只完成独立实例准备，尚不等于软件安装、账号登录或手机可用。'}
        elif args.action == 'configure':
            result = configure(path, input_object())
        elif args.action == 'restore':
            # A replacement computer has no installed instance or profile yet.
            # Restore owns its explicitly chosen NEW target atomically and
            # never reads/modifies the selected source instance's live state.
            from shared.instance_recovery import restore
            result = restore(None, input_object())
        else:
            from shared.instance_runtime import operate
            result = operate(path, args.action, SOURCE)
        if args.action != 'login':
            print(json.dumps({'ok': True, 'action': args.action, **result}, ensure_ascii=False))
        return 0
    except cfg.InstanceError as error:
        print(json.dumps({'ok': False, 'action': args.action, 'code': error.code, 'error': str(error)}, ensure_ascii=False))
        return 2
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, subprocess.SubprocessError):
        print(json.dumps({'ok': False, 'action': args.action, 'code': 'operation_failed',
                          'error': '本机操作未完成；未报告成功，请保留实例数据并重新检查。'}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
