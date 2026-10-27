"""A reversible SQLite insertion barrier for the first legacy→gate load only.

No business rows, schedules, native goals or schema-version rows are rewritten.
Install is one short transaction; a legacy Runtime can finish existing work and
exit normally, with no SQLite write lock held during service shutdown. New HTTP
work fails closed on the legacy runtime; this brief bootstrap limitation must
be disclosed in the separately approved loading window.
"""
from __future__ import annotations
import hashlib

PREFIX = 'workbench_maintenance_bootstrap_'


def definitions(owner):
    if not isinstance(owner, str) or not owner or len(owner) > 128:
        raise ValueError('维护操作标识无效。')
    # A comment binds the guard to its owner without adding a table/data row.
    binding = hashlib.sha256(owner.encode()).hexdigest()
    guard = f"/* owner_sha256={binding} */"
    return {
        PREFIX + 'commands': f'''CREATE TRIGGER {PREFIX}commands BEFORE INSERT ON commands
        WHEN NEW.kind IN ('start','steer','compact') AND NEW.state!='rejected'
          AND NOT (NEW.origin='scheduled' AND EXISTS (
              SELECT 1 FROM conversation_automation_runs r WHERE r.command_id=NEW.id
              AND r.conversation_id=NEW.conversation_id AND r.status='dispatching'))
        BEGIN {guard} SELECT RAISE(ABORT,'workbench maintenance: new work disabled'); END''',
        PREFIX + 'goals': f'''CREATE TRIGGER {PREFIX}goals BEFORE INSERT ON native_goal_commands
        WHEN NEW.origin!='system' AND NEW.state!='rejected' AND
          (json_extract(NEW.payload_json,'$.action')='resume' OR
          (json_extract(NEW.payload_json,'$.action')='set' AND
           COALESCE(json_extract(NEW.payload_json,'$.status'),'active')='active'))
        BEGIN {guard} SELECT RAISE(ABORT,'workbench maintenance: goal activation disabled'); END''',
        PREFIX + 'automation': f'''CREATE TRIGGER {PREFIX}automation BEFORE INSERT ON conversation_automation_runs
        BEGIN {guard} SELECT RAISE(ABORT,'workbench maintenance: new scheduling disabled'); END''',
    }


def inspect(connection):
    return {row[0]: row[1] for row in connection.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND substr(name,1,?)=?", (len(PREFIX), PREFIX))}


def change(connection, owner, *, install):
    expected = definitions(owner)
    # Own short transaction, never the caller's shutdown transaction.
    required = {
        'commands': {'id','conversation_id','kind','origin','state'},
        'native_goal_commands': {'payload_json','origin','state'},
        'conversation_automation_runs': {'command_id','conversation_id','status'},
    }
    for table, columns in required.items():
        actual = {row[1] for row in connection.execute('PRAGMA table_info(' + table + ')')}
        if not columns <= actual:
            raise RuntimeError('旧版工作台结构不符，兼容闸未安装。')
    connection.execute('BEGIN IMMEDIATE')
    try:
        existing = inspect(connection)
        if existing and existing != expected:
            raise RuntimeError('兼容闸属于其他操作或定义变化；未覆盖。')
        if install and not existing:
            for statement in expected.values():
                connection.execute(statement)
        elif not install:
            for name in existing:
                connection.execute('DROP TRIGGER ' + name)
        connection.execute('COMMIT')
        return inspect(connection)
    except BaseException:
        connection.execute('ROLLBACK')
        raise


def legacy_terminal_counts(connection, boundary, web_started_at=None):
    """Cover old-runtime side effects before their channel claim.

    The bootstrap INSERT triggers seal new work. A fixed lower time boundary
    never ages a pending callback into an idle proof; live observation journals
    also cover older Push callbacks. Missing evidence conservatively times out,
    rather than replaying, marking sent or terminating delivery. Historical
    unjournalled terminals outside this boundary are not future callbacks.
    """
    return {
        'legacy_terminal_delivery': connection.execute("""
            SELECT COUNT(*) FROM turns t JOIN conversations c ON c.id=t.conversation_id
            WHERE t.status IN ('completed','failed','interrupted')
              AND t.notification_requested=1 AND c.completion_notification=1
              AND (t.delete_deferred_terminal<>0 OR
                   julianday(t.completed_at)>=julianday(?)-(600.0/86400.0))
              AND (
                (t.notification_state IS NULL AND
                 julianday(t.completed_at)>=julianday(?)-(600.0/86400.0))
                OR t.notification_state='sending'
                OR NOT EXISTS (SELECT 1 FROM web_push_dispatches d WHERE d.turn_id=t.id)
                OR EXISTS (SELECT 1 FROM web_push_deliveries d
                           WHERE d.turn_id=t.id AND d.state IN ('pending','sending'))
              )
        """, (boundary, boundary)).fetchone()[0],
        # The legacy ingress may archive media after terminal COMMIT even when
        # notifications are disabled. Only projections this Web process could
        # still be consuming participate; older unsupported history is not a
        # permanently busy callback. No paths or user contents are returned.
        'legacy_image_archival': connection.execute("""
            SELECT COUNT(*) FROM items i JOIN turns t ON t.id=i.turn_id
            WHERE i.kind='image_generation' AND i.source='notification_item'
              AND i.status IN ('completed','complete')
              AND t.status IN ('completed','failed','interrupted')
              AND julianday(i.updated_at)>=julianday(?)
              AND NOT EXISTS (SELECT 1 FROM artifacts a
                  WHERE a.conversation_id=i.conversation_id
                    AND a.native_item_id=i.native_item_id AND a.origin='image_generation_item')
        """, (web_started_at or boundary,)).fetchone()[0]
    }


def quiesce(state, owner, *, web_started_at, verify_web=None, timeout=900):
    """Legacy insert guard + new-runtime side file, then natural drain.

    Only the opted-in controlled publisher calls this. All locks/transactions
    finish before waiting; there is no self-turn exemption or forced stop.
    """
    import sqlite3
    import time
    from datetime import datetime, timezone
    from shared.maintenance_admission import MaintenanceAdmission, busy_counts
    started = datetime.fromisoformat(web_started_at)
    if started.tzinfo is None or started > datetime.now(timezone.utc):
        raise RuntimeError('旧工作台进程起点不可验证。')
    path = state / 'conversations.sqlite3'
    gate = MaintenanceAdmission(path)
    if gate.read()['mode'] != 'open':
        raise RuntimeError('已有工作台维护闸，拒接管。')
    with sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, isolation_level=None) as connection:
        if inspect(connection):
            raise RuntimeError('已有兼容准入闸，禁止盲重跑。')
        # Capture BEFORE sealing the INSERT boundary, never a rolling now().
        boundary = datetime.now(timezone.utc).isoformat()
        change(connection, owner, install=True)
    # If any following step fails, the transaction owner must retain or
    # explicitly restore this guard. Do not silently release unknown work.
    gate.change('quiescing', owner)
    until = time.monotonic() + timeout
    while True:
        if verify_web is not None and verify_web() is not True:
            raise RuntimeError('旧工作台进程改变；准入保护保留，不继续加载。')
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as connection:
            # Native state and unclaimed channel work share one read snapshot.
            connection.execute('BEGIN')
            try:
                counts = busy_counts(connection, web_started_at=web_started_at)
                counts.update(legacy_terminal_counts(connection, boundary, web_started_at))
            finally:
                connection.execute('ROLLBACK')
        if not any(counts.values()):
            # Native descendants and active native Goals need not have local
            # turns/commands. A read-only connection inspects loaded sessions;
            # missing protocol/runtime fails closed, never an empty inventory.
            from shared.maintenance_native import standalone_counts
            counts.update(standalone_counts())
            if not any(counts.values()):
                if verify_web is not None and verify_web() is not True:
                    raise RuntimeError('原工作台/原生进程在核验中变化，保护保留。')
                with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as connection:
                    connection.execute('BEGIN')
                    try:
                        latest = busy_counts(connection, web_started_at=web_started_at)
                        latest.update(legacy_terminal_counts(connection, boundary, web_started_at))
                    finally:
                        connection.execute('ROLLBACK')
                if not any(latest.values()):
                    return gate
                counts.update(latest)
        if time.monotonic() >= until:
            # Only fixed counter names and totals, never tool/user contents.
            remaining = ', '.join(f'{name}={count}' for name, count in sorted(counts.items()) if count)
            raise RuntimeError('在途工作未自然结束；不强杀、不加载候选。剩余类别：' + remaining)
        time.sleep(1)


def remove_after_attestation(state, owner, module_sha, *, timeout=120):
    import sqlite3
    from shared.maintenance_admission import MaintenanceAdmission, attest
    path = state / 'conversations.sqlite3'
    gate = MaintenanceAdmission(path)
    # Type=simple restart only proves process creation, not loaded HTTP readiness.
    import time
    import urllib.error
    deadline = time.monotonic() + timeout
    while True:
        try:
            proof = attest(gate, owner, module_sha)
            break
        except (OSError, urllib.error.URLError, RuntimeError):
            if time.monotonic() >= deadline:
                raise RuntimeError('准入实现未在有界等待内实际就绪；兼容闸保留。') from None
            time.sleep(.25)
    with sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, isolation_level=None) as connection:
        change(connection, owner, install=False)
    return proof


def reopen_after_acceptance(state, owner, module_sha):
    """The publisher calls this only after actual release/lifecycle acceptance.

    Loading the gate is a separate window, not the later capacity shutdown.
    Opening admission never stops work, so unrelated new native activity need
    not be idle again. Loaded implementation and this gate's owner still match.
    """
    from shared.maintenance_admission import MaintenanceAdmission, attest
    gate = MaintenanceAdmission(state / 'conversations.sqlite3')
    attest(gate, owner, module_sha, require_idle=False)
    return gate.change('open', owner)


def restore_legacy_admission(state, owner):
    """Caller MUST first prove the exact healthy pre-load runtime is selected.

    This only restores admission; it never rolls back rows, native goals,
    files, processes, or schedules. Not exposed as a general CLI rescue flag.
    """
    import sqlite3
    from shared.maintenance_admission import MaintenanceAdmission
    path = state / 'conversations.sqlite3'
    gate = MaintenanceAdmission(path)
    current = gate.read()
    if current['mode'] not in {'open', 'quiescing'} or (current['mode'] == 'quiescing' and current['owner'] != owner):
        raise RuntimeError('准入闸状态或归属变化，未恢复。')
    with sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, isolation_level=None) as connection:
        change(connection, owner, install=False)
    gate.change('open', owner)
