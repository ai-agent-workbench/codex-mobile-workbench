#!/usr/bin/env python3
"""Developer wrapper; release snapshots use python -m engine.dot_mailbox_cli."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.dot_mailbox_cli import load_scope, main
if __name__ == '__main__':
    raise SystemExit(main())
