"""Independent disk-backed business fixtures, without repeated cold-schema DDL.

Cold-start, migration, damaged-schema and rollback tests must instantiate
ConversationStore directly. This helper never patches the production class,
shares a connection, changes WAL/FULL, or replaces a reopen of existing data.
"""
from functools import lru_cache
from pathlib import Path
import tempfile

from engine.conversation_store import ConversationStore


@lru_cache(maxsize=1)
def _empty_database() -> bytes:
    # Build from the current implementation, not a checked-in/stale snapshot.
    with tempfile.TemporaryDirectory(prefix="conversation-schema-fixture-") as root:
        path = Path(root) / "empty.sqlite3"
        store = ConversationStore(path)
        # Closing checkpoints the WAL before its independent bytes are read.
        store.close()
        return path.read_bytes()


def seeded_store(database_path: str | Path) -> ConversationStore:
    """Create an exclusive empty copy, then use the real store initializer."""
    path = Path(database_path)
    template = _empty_database()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Never overwrite an existing fixture (including a dangling symlink).
    with path.open("xb") as output:
        output.write(template)
    path.chmod(0o600)
    return ConversationStore(path)
