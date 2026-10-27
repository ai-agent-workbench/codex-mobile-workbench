"""Pinned SQLite-only attachment fallback barrier, independent of app imports."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3


def quarantine(previous: Path, database: Path) -> int:
    target = previous / 'app/shared/message_attachments.py'
    if target.is_file() and re.search(r'^ATTACHMENT_DELIVERY_VERSION = [1-9][0-9]*$',
                                      target.read_text(encoding='utf-8'), re.MULTILINE):
        return 0
    if not database.exists():
        return 0
    connection = sqlite3.connect(database.absolute().as_uri() + '?mode=rw', uri=True, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_attachments'").fetchone():
            return 0
        connection.execute('BEGIN IMMEDIATE')
        try:
            connection.execute("INSERT INTO schema_meta(key,value) VALUES('message_attachments_rollback_barrier','1') ON CONFLICT(key) DO UPDATE SET value='1'")
            count = 0
            now = datetime.now(timezone.utc).isoformat()
            for row in connection.execute("SELECT id,conversation_id,state,reason,payload_json FROM commands WHERE state IN ('submitted','queued')").fetchall():
                if not json.loads(row['payload_json']).get('attachments'):
                    continue
                unknown = row['state'] == 'submitted' and row['reason'] == 'native_rpc_sending'
                connection.execute('UPDATE commands SET state=?,reason=?,updated_at=? WHERE id=?', (
                    'delivery_unknown' if unknown else 'rejected',
                    'attachment_rollback_requires_reconcile' if unknown else 'attachment_rollback_not_delivered', now, row['id']))
                if unknown:
                    connection.execute("UPDATE conversations SET state='needs_user',recovery_state='attachment_rollback_requires_reconcile',updated_at=?,revision=revision+1 WHERE id=?", (now, row['conversation_id']))
                count += 1
            connection.execute('COMMIT')
            return count
        except BaseException:
            connection.execute('ROLLBACK')
            raise
    finally:
        connection.close()
