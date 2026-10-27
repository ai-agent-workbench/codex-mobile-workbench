#!/usr/bin/env python3
"""Read-only-source S0 baseline for ConversationStore.snapshot().

Creates only a synthetic SQLite database under this script's output directory.
It never starts the runtime, contacts an App Server, or sends notifications.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BODY = ("合成基准正文；固定种子，不含任何生产会话内容。" * 32)[:256]
COUNTS = {20: 127, 200: 1270, 1000: 6351}


def percentile95(values: list[float]) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * 0.95
    lower, upper = int(position), min(int(position) + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def populate(store, turns: int, wanted_items: int) -> None:
    store.create_conversation(
        "synthetic-conversation", title="S0 synthetic", cwd="/tmp",
        settings={"model": "fixture", "effort": "medium", "permission": "default", "service_tier": None},
    )
    store.bind_thread("synthetic-conversation", {"id": "synthetic-thread"}, server_version="fixture")
    base, remainder = divmod(wanted_items, turns)
    for index in range(turns):
        turn_id, command_id = f"turn-{index:04d}", f"command-{index:04d}"
        store.begin_command("synthetic-conversation", command_id, kind="start", text="synthetic prompt")
        store.bind_turn("synthetic-conversation", {"id": turn_id, "status": "inProgress"}, command_id=command_id)
        item_count = base + (1 if index < remainder else 0)
        for item_index in range(item_count):
            store.apply_notification({
                "method": "item/completed",
                "params": {"threadId": "synthetic-thread", "turnId": turn_id,
                           "item": {"id": f"item-{index:04d}-{item_index:02d}", "type": "agentMessage",
                                    "phase": "final_answer" if item_index == item_count - 1 else "commentary",
                                    "text": BODY}},
            })
        store.apply_notification({"method": "turn/completed", "params": {
            "threadId": "synthetic-thread", "turn": {"id": turn_id, "status": "completed", "items": []}}})


def measure(store, samples: int, reader: str) -> dict:
    read = {"snapshot": store.snapshot, "summary": store.get_conversation,
            "terminal-checkpoints": store.turn_terminal_checkpoints}[reader]
    # One unreported warmup settles SQLite page/cache allocation.
    read("synthetic-conversation")
    milliseconds, reads = [], []
    for _ in range(samples):
        statements: list[str] = []
        store._db.set_trace_callback(statements.append)
        started = time.perf_counter_ns()
        snapshot = read("synthetic-conversation")
        milliseconds.append((time.perf_counter_ns() - started) / 1_000_000)
        store._db.set_trace_callback(None)
        reads.append({
            "turns_sql": sum("FROM turns WHERE conversation_id" in value for value in statements),
            "items_sql": sum("FROM items i LEFT JOIN conversation_search_item_sources" in value for value in statements),
        })
    return {
        "samples": samples, "warmup": 1,
        "milliseconds": [round(value, 3) for value in milliseconds],
        "median_ms": round(statistics.median(milliseconds), 3),
        "p95_ms": round(percentile95(milliseconds), 3),
        "returned_turns": len(snapshot.get("turns", [])),
        "returned_checkpoint_turns": len(snapshot) if reader == "terminal-checkpoints" else 0,
        "returned_items": sum(len(turn["items"]) for turn in snapshot.get("turns", [])),
        "full_read_sql_per_sample": reads,
        "future_status_summary_fields": ["id", "title", "revision", "state", "active_turn_id"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=7)
    parser.add_argument("--source-dir", type=Path, default=ROOT)
    parser.add_argument("--reader", choices=("snapshot", "summary", "terminal-checkpoints"), default="snapshot")
    args = parser.parse_args()
    if args.samples < 7:
        raise SystemExit("S0 requires at least seven reported samples")
    root = args.source_dir.resolve()
    sys.path.insert(0, str(root))
    from engine.conversation_store import ConversationStore
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    report = {"source_root": str(root), "source_commit": commit, "reader": args.reader, "fixture": {
        "seed": "S0-fixed-synthetic-v1", "body_chars": len(BODY), "journal_mode": "WAL", "synchronous": "FULL"}, "sizes": {}}
    temporary = tempfile.TemporaryDirectory(prefix="conversation-read-bench-")
    runs = Path(temporary.name)
    for turns, item_count in COUNTS.items():
        run = runs / f"snapshot-{turns}-turns.sqlite3"
        for path in (run, Path(str(run) + "-wal"), Path(str(run) + "-shm")):
            path.unlink(missing_ok=True)
        store = ConversationStore(run)
        try:
            populate(store, turns, item_count)
            pragma = {key: store._db.execute(f"PRAGMA {key}").fetchone()[0] for key in ("journal_mode", "synchronous")}
            result = measure(store, args.samples, args.reader)
            result["pragmas"] = pragma
            report["sizes"][str(turns)] = result
        finally:
            store.close()
    temporary.cleanup()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
