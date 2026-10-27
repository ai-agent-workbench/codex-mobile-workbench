#!/usr/bin/env python3
"""维护准入闸本地入口。默认只读；关闭/开启不等于停服务或验收。"""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared.maintenance_admission import MaintenanceAdmission, attest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--action', choices=('status', 'close', 'open', 'bootstrap-inspect', 'bootstrap-install', 'bootstrap-remove'), default='status')
    parser.add_argument('--owner')
    parser.add_argument('--expected-module-sha')
    args = parser.parse_args()
    if not args.db.is_absolute() or not args.db.is_file():
        parser.error('必须指定已有数据库的绝对路径。')
    gate = MaintenanceAdmission(args.db)
    if args.action.startswith('bootstrap-'):
        if args.action == 'bootstrap-remove':
            if not args.expected_module_sha:
                parser.error('兼容闸移除须核验已加载准入实现的 SHA。')
            attest(gate, args.owner, args.expected_module_sha)
        import sqlite3
        from shared.maintenance_bootstrap import inspect, change
        with sqlite3.connect(args.db.as_uri() + ('?mode=ro' if args.action == 'bootstrap-inspect' else '?mode=rw'), uri=True, isolation_level=None) as connection:
            guards = (inspect(connection) if args.action == 'bootstrap-inspect' else
                      change(connection, args.owner, install=args.action == 'bootstrap-install'))
        # Trigger definitions contain only the owner's digest, not user data.
        print(json.dumps({'bootstrap_triggers': guards}, ensure_ascii=False))
        return
    state = gate.read() if args.action == 'status' else gate.change(
        'quiescing' if args.action == 'close' else 'open', args.owner)
    print(json.dumps({**gate.identity(), 'state': state}, ensure_ascii=False))


if __name__ == '__main__':
    main()
