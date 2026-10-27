"""Short, durable admission barrier; never held across RPC or service shutdown.

A closed barrier rejects new work, not native notifications or an admitted turn.
There is deliberately no timer, schema change, task cancellation, or replay.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import secrets
import hmac
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

PROTOCOL = 'workbench-drain-v1'
MODULE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
# Import-time fingerprint covers consumers as well as the side-file helper.
# A previous worker with the same helper but old ingress logic must not attest
# a newly selected candidate. Immutable bundle verification remains separate.
CONTRACT_FILES = ('shared/maintenance_admission.py', 'engine/conversation_store.py',
    'engine/conversation_runtime.py', 'engine/conversation_goals.py',
    'engine/conversation_actions.py', 'engine/conversation_automation.py',
    'shared/message_attachments.py', 'routes/maintenance_admission.py', 'app.py',
    'engine/codex_app_server.py', 'engine/fixtures/codex_app_server_protocol_0_160_0.json',
    'shared/maintenance_native.py', 'shared/instance_primitives.py')
IMPLEMENTATION_SHA256 = hashlib.sha256(json.dumps({name: hashlib.sha256(
    (Path(__file__).resolve().parents[1] / name).read_bytes()).hexdigest()
    for name in CONTRACT_FILES}, sort_keys=True).encode()).hexdigest()
STEER_ONLY = '_maintenance_steer_only'


class MaintenanceAdmission:
    def __init__(self, database_path):
        self.database_path = Path(database_path)
        self.path = self.database_path.with_name('maintenance-admission.json')
        self.lock_path = self.path.with_suffix('.lock')
        self.token_path = self.path.with_suffix('.token')
        self._local = threading.local()

    def _lock(self, exclusive=False):
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode) or mode & 0o022:
            os.close(fd)
            raise RuntimeError('维护准入锁不安全。')
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        return fd

    def read(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {'protocol': PROTOCOL, 'mode': 'open', 'owner': None}
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_size > 4096:
                raise ValueError('unsafe admission state')
            with os.fdopen(fd, 'r', encoding='utf-8') as source:
                fd = -1
                state = json.load(source)
            if (state.get('protocol') != PROTOCOL or state.get('mode') not in {'open', 'quiescing'}
                    or not isinstance(state.get('owner'), str) or not state['owner']):
                raise ValueError('invalid admission state')
            return state
        except (OSError, ValueError, TypeError, AttributeError):
            # Never interpret damaged/unknown maintenance state as open.
            return {'protocol': PROTOCOL, 'mode': 'unavailable', 'owner': None}
        finally:
            if fd >= 0:
                os.close(fd)

    @contextmanager
    def admission(self):
        # Nested attachment/command reservations share the outer ticket until
        # their actual SQLite COMMIT. Maintenance EX never takes a DB lock.
        depth = getattr(self._local, 'depth', 0)
        fd = None if depth else self._lock()
        self._local.depth = depth + 1
        try:
            yield self.read()
        finally:
            self._local.depth = depth
            if fd is not None:
                os.close(fd)

    def _read_token(self):
        try:
            fd = os.open(self.token_path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size != 64:
                return None
            return os.read(fd, 65).decode('ascii')
        except (OSError, UnicodeError):
            return None
        finally:
            os.close(fd)

    def authorized(self, supplied):
        expected = self._read_token()
        return expected is not None and isinstance(supplied, str) and hmac.compare_digest(expected, supplied)

    def _ensure_token(self):
        try:
            fd = os.open(self.token_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            if self._read_token() is None:
                raise RuntimeError('维护控制凭据不可用；未替换。')
            return
        with os.fdopen(fd, 'w', encoding='ascii') as output:
            output.write(secrets.token_hex(32)); output.flush(); os.fsync(output.fileno())

    def change(self, mode, owner):
        if mode not in {'open', 'quiescing'} or not isinstance(owner, str) or not owner or len(owner) > 128:
            raise ValueError('维护模式或操作标识无效。')
        fd = self._lock(exclusive=True)
        temporary = self.path.with_name(self.path.name + '.new-' + str(os.getpid()))
        try:
            previous = self.read()
            if previous['mode'] == 'unavailable' or (previous['mode'] == 'quiescing' and previous['owner'] != owner):
                raise RuntimeError('维护闸状态未知或属于其他操作；未覆盖。')
            self._ensure_token()
            from datetime import datetime, timezone
            state = {'protocol': PROTOCOL, 'mode': mode, 'owner': owner,
                     'changed_at': datetime.now(timezone.utc).isoformat()}
            output = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(output, 'w', encoding='utf-8') as target:
                json.dump(state, target, ensure_ascii=False, sort_keys=True)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_DIRECTORY | os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return state
        finally:
            if temporary.exists():
                temporary.unlink()
            os.close(fd)

    def identity(self):
        return {'protocol': PROTOCOL,
                'module_sha256': MODULE_SHA256, 'implementation_sha256': IMPLEMENTATION_SHA256}


def busy_counts(connection, *, web_started_at=None):
    """One metadata snapshot; release read transaction before waiting/stopping."""
    started = None
    if web_started_at is not None:
        try:
            started = datetime.fromisoformat(web_started_at)
        except (TypeError, ValueError):
            raise RuntimeError('工作台进程起点不可验证。') from None
        if started.tzinfo is None or started > datetime.now(timezone.utc):
            raise RuntimeError('工作台进程起点不可验证。')
    own_transaction = not connection.in_transaction
    if own_transaction:
        connection.execute('BEGIN')
    try:
        return _busy_counts_snapshot(connection, started)
    finally:
        if own_transaction:
            connection.execute('ROLLBACK')


def _historical_legacy_reviews(connection, started):
    """Count only non-pending legacy projections that predate this Web process.

    This does not settle or rewrite a tool call. Native loaded-session checks
    and the current Runtime's in-memory review tasks remain separate barriers.
    Pending lifecycle/cleanup metadata, or uncertain dates, stay busy. The
    permanent _review_ownership reference intentionally survives completion;
    it is not a pending marker, and its native thread is still scanned.
    """
    rows = connection.execute("""
        SELECT tc.created_at, tc.updated_at, t.completed_at
        FROM tool_calls tc JOIN turns t ON t.id=tc.turn_id
          AND t.conversation_id=tc.conversation_id
        WHERE tc.tool='code_review' AND tc.state='inProgress'
          AND t.status IN ('completed','failed','interrupted')
          AND (tc.response_json IS NULL OR json_type(tc.response_json)='object')
          AND json_type(tc.response_json,'$.review_lifecycle') IS NULL
          AND json_type(tc.response_json,'$.review_cleanup') IS NULL
          AND (json_type(tc.response_json,'$._review_ownership') IS NULL OR (
              json_type(tc.response_json,'$._review_ownership')='object'
              AND json_type(tc.response_json,'$._review_ownership.review_thread_id')='text'
              AND length(json_extract(tc.response_json,'$._review_ownership.review_thread_id'))>0
              AND json_type(tc.response_json,'$._review_ownership.review_started') IN ('true','false')
              AND json_type(tc.response_json,'$._review_ownership.interrupt_attempted') IN ('true','false')))
          AND json_type(tc.response_json,'$.review_failure') IS NULL
          AND json_type(tc.response_json,'$.review_thread_id') IS NULL
          AND json_type(tc.response_json,'$.review_turn_id') IS NULL
    """)
    historical = 0
    for row in rows:
        try:
            created, updated, completed = (datetime.fromisoformat(value) for value in row)
        except (TypeError, ValueError):
            continue
        if (all(value.tzinfo is not None for value in (created, updated, completed))
                and created <= updated < started and created <= completed < started):
            historical += 1
    return historical


def _busy_counts_snapshot(connection, started=None):
    """Includes native continuation, settlement, and uncertain delivery."""
    checks = {
        'conversations': "state IN ('starting_thread','starting_turn','active','interrupting','reconciling') OR active_turn_id IS NOT NULL",
        'turns': "status IN ('inProgress','active','queued','stopping') OR notification_state='sending'",
        'web_push_deliveries': "state IN ('pending','sending')",
        'commands': "state IN ('submitted','queued','delivery_unknown') OR (state='accepted' AND kind='interrupt' AND json_extract(payload_json,'$.scope')='conversation_tree')",
        'conversation_automation_runs': "status IN ('dispatching','queued','accepted','delivery_unknown')",
        'native_goal_commands': "state IN ('submitted','delivery_unknown')",
        'native_goal_projection': "continuing=1 OR settlement_pending=1 OR json_extract(goal_json,'$.status')='active' OR (ever_native=1 AND sync_state NOT IN ('synced','draft')) OR (json_extract(draft_json,'$.status')='active' AND COALESCE(json_extract(draft_json,'$.activation_unconfirmed'),0)!=1 AND EXISTS (SELECT 1 FROM commands c WHERE c.conversation_id=native_goal_projection.conversation_id AND c.kind='start' AND c.state IN ('submitted','queued','accepted','completed','delivery_unknown')))",
        'native_interaction_requests': "state IN ('awaiting','claimed','response_sent','awaiting_reissue','delivery_unknown')",
        'tool_calls': "state != 'completed' OR (tool='code_review' AND (json_extract(response_json,'$.review_lifecycle.phase') IN ('reserved','thread_created','start_requested','turn_started') OR json_extract(response_json,'$.review_cleanup.status')='unconfirmed'))",
        'question_requests': "state IN ('awaiting_answer','answer_claimed','resolved_before_send','response_sent','awaiting_activity','awaiting_reissue','delivery_unknown')",
    }
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    counts = {name: connection.execute('SELECT COUNT(*) FROM ' + name + ' WHERE ' + condition).fetchone()[0]
            for name, condition in checks.items() if name in tables}
    if started is not None and {'tool_calls', 'turns'} <= tables:
        counts['tool_calls'] -= _historical_legacy_reviews(connection, started)
    if 'async_question_requests' in tables:
        counts['async_question_requests'] = connection.execute("SELECT COUNT(*) FROM async_question_requests q LEFT JOIN turns t ON t.id=q.turn_id WHERE q.state IN ('awaiting_answer','answer_claimed','delivery_unknown') AND (t.status IS NULL OR t.status IN ('inProgress','active','queued','stopping') OR q.deadline_at IS NOT NULL OR q.state!='awaiting_answer')").fetchone()[0]
    return counts



def attest(gate, owner, expected_sha, *, base_url='http://127.0.0.1:18080', expected_mode='quiescing', require_idle=True):
    """Actual loaded implementation proof, not a side-file existence check."""
    import urllib.request
    import urllib.parse
    if expected_mode not in {'open', 'quiescing'} or type(require_idle) is not bool:
        raise ValueError('维护核验模式无效。')
    parsed = urllib.parse.urlsplit(base_url)
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port
            or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password):
        raise ValueError('维护核验只允许显式本机 HTTP 端口。')
    token = gate._read_token()
    if token is None:
        raise RuntimeError('维护控制凭据不可用。')
    request = urllib.request.Request(base_url + '/internal/maintenance-admission',
                                    headers={'X-Workbench-Maintenance-Token': token})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=5) as response:
        data = json.loads(response.read(65537))
    if (data.get('protocol') != PROTOCOL or data.get('module_sha256') != expected_sha
            or data.get('implementation_sha256') != IMPLEMENTATION_SHA256
            or data.get('database') != str(gate.database_path.resolve())
            or data.get('state', {}).get('mode') != expected_mode
            or data['state'].get('owner') != owner
            or not isinstance(data.get('busy'), dict)
            or any(type(count) is not int or count < 0 for count in data['busy'].values())
            or data.get('idle') is not (not any(data['busy'].values()))
            or (require_idle and data.get('idle') is not True)):
        raise RuntimeError('实际工作台准入协议未生效或仍有在途工作。')
    return data
