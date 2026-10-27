import asyncio
from datetime import datetime
import errno
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from shared.web_stall_monitor import WebStallMonitor, _StallWriter


class Clock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


class Code:
    def __init__(self, filename="/safe/module.py", name="work"):
        self.co_filename = filename
        self.co_name = name


class Frame:
    def __init__(self, next_frame=None, filename="/safe/module.py", name="work"):
        self.f_code = Code(filename, name)
        self.f_lineno = 17
        self.f_back = next_frame

    @property
    def f_locals(self):  # The implementation must never touch this.
        raise AssertionError("locals must not be collected")


def read_records(directory):
    path = Path(directory) / "web-stalls" / "stalls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


class WebStallMonitorTests(unittest.TestCase):
    def make_monitor(self, directory, clock, **kwargs):
        monitor = WebStallMonitor(
            directory,
            clock=clock,
            frames_provider=lambda: {threading.get_ident(): Frame()},
            **kwargs,
        )
        monitor._main_thread_id = threading.get_ident()
        return monitor

    def test_healthy_observations_do_not_create_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = Clock()
            monitor = self.make_monitor(directory, clock, stall_seconds=5)
            clock.value = 4.9
            monitor.check_once()
            self.assertFalse((Path(directory) / "web-stalls").exists())

    def test_default_threshold_captures_at_five_seconds_without_faster_polling(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = Clock()
            monitor = self.make_monitor(directory, clock)
            for clock.value in (1.0, 2.0, 3.0, 4.0, 4.999):
                monitor.check_once()
            self.assertFalse((Path(directory) / "web-stalls").exists())
            clock.value = 5.0
            monitor.check_once()
            records = read_records(directory)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["kind"], "web_stall_snapshot")
            self.assertEqual(records[0]["stale_for_seconds"], 5.0)
            self.assertNotIn("capture_delayed", records[0])
            status = monitor.status()
            self.assertEqual(status["threshold_seconds"], 5.0)
            self.assertEqual(status["heartbeat_seconds"], 1.0)
            self.assertEqual(status["capture_interval_seconds"], 10.0)
            self.assertEqual(status["cooldown_seconds"], 300.0)

    def test_threshold_second_capture_recovery_and_cross_episode_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = Clock()
            monitor = self.make_monitor(directory, clock, stall_seconds=5, capture_interval_seconds=5, cooldown_seconds=100)
            clock.value = 5
            monitor.check_once()
            clock.value = 9
            monitor.check_once()
            clock.value = 10
            monitor.check_once()
            clock.value = 20
            monitor.check_once()  # Same episode is capped at two captures.
            monitor._heartbeat_state = (1, 20)
            clock.value = 21
            monitor.check_once()
            records = read_records(directory)
            self.assertEqual([record["event"] for record in records], ["stall", "stall", "recovery"])
            self.assertEqual(records[-1]["captures"], 2)
            self.assertEqual(records[0]["kind"], "web_stall_snapshot")
            self.assertEqual(records[0]["process_id"], os.getpid())
            self.assertIsNotNone(datetime.fromisoformat(records[0]["ts"]).tzinfo)
            self.assertEqual(records[-1]["observed_stale_gap_seconds"], 20)

            monitor._heartbeat_state = (2, 0)
            clock.value = 30
            monitor.check_once()
            monitor._heartbeat_state = (3, 30)
            clock.value = 31
            monitor.check_once()
            records = read_records(directory)
            self.assertEqual(records[-1]["event"], "recovery")
            self.assertTrue(records[-1]["capture_suppressed"])
            self.assertEqual(records[-1]["captures"], 0)

    def test_stack_bounds_priority_privacy_and_delayed_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = Clock()
            deep = None
            for _ in range(30):
                deep = Frame(deep, filename="/source/" + "x" * 500, name="function_" + "y" * 200)
            main = threading.get_ident()
            frames = {main: deep}
            # IDs beyond the main thread force thread truncation without recording names.
            for ident in range(1000, 1040):
                frames[ident] = deep
            monitor = WebStallMonitor(directory, clock=clock, frames_provider=lambda: frames, stall_seconds=5)
            monitor._main_thread_id = main
            clock.value = 12
            monitor.check_once()
            raw = (Path(directory) / "web-stalls" / "stalls.jsonl").read_bytes()
            self.assertLessEqual(len(raw), 64 * 1024)
            record = json.loads(raw)
            self.assertTrue(record["capture_delayed"])
            self.assertEqual(record["threads"][0]["thread_id"], main)
            self.assertTrue(record["threads"][0]["frames_truncated"])
            self.assertLessEqual(len(record["threads"]), 32)
            self.assertNotIn("f_locals", raw.decode())
            self.assertNotIn("secret-value", raw.decode())
            for frame in record["threads"][0]["frames"]:
                self.assertLessEqual(len(frame["file"].encode()), 160)
                self.assertLessEqual(len(frame["function"].encode()), 80)

    def test_stale_episode_does_not_recover_without_a_new_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = Clock()
            monitor = self.make_monitor(directory, clock, stall_seconds=5)
            clock.value = 5
            monitor.check_once()
            # A forged fresh timestamp with the same sequence is not a heartbeat.
            monitor._heartbeat_state = (0, 10)
            clock.value = 10
            monitor.check_once()
            self.assertEqual([r["event"] for r in read_records(directory)], ["stall"])

    def test_heartbeat_advancing_before_capture_prevents_a_post_recovery_stack(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = Clock()
            monitor = self.make_monitor(directory, clock)
            monitor._last_check = 19.5
            frames_provider = mock.Mock(side_effect=AssertionError("must not capture after recovery"))
            monitor._frames_provider = frames_provider
            reads = []
            def clock_with_recovery():
                reads.append(True)
                if len(reads) == 2:
                    monitor._heartbeat_state = (1, 20.0)
                return 20.0
            monitor._clock = clock_with_recovery
            monitor.check_once()
            frames_provider.assert_not_called()
            records = read_records(directory)
            self.assertEqual([r["event"] for r in records], ["recovery"])
            self.assertEqual(records[0]["captures"], 0)
            self.assertEqual(records[0]["recovery_observation"], "heartbeat_advanced_before_capture")

    def test_heartbeat_advancing_during_capture_marks_stack_as_uncertain(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = Clock()
            monitor = self.make_monitor(directory, clock)
            monitor._last_check = 19.5
            def frames_after_recovery():
                clock.value = 21.0
                monitor._heartbeat_state = (1, 21.0)
                return {threading.get_ident(): Frame()}
            monitor._frames_provider = frames_after_recovery
            clock.value = 20.0
            monitor.check_once()
            snapshot, recovery = read_records(directory)
            self.assertTrue(snapshot["capture_delayed"])
            self.assertFalse(snapshot["heartbeat_unchanged_during_capture"])
            self.assertEqual(snapshot["stack_observation"], "heartbeat_advanced_during_capture")
            self.assertEqual(snapshot["capture_finished_monotonic"], 21.0)
            self.assertEqual(recovery["captures"], 1)
            self.assertFalse(monitor.status()["active_episode"])

    def test_observer_delay_before_or_during_capture_is_explicit(self):
        for delayed_at in ("before", "during"):
            with self.subTest(delayed_at=delayed_at), tempfile.TemporaryDirectory() as directory:
                clock = Clock()
                monitor = self.make_monitor(directory, clock)
                monitor._last_check = 19.5
                if delayed_at == "before":
                    reads = []
                    def delayed_clock():
                        reads.append(True)
                        return 20.0 if len(reads) == 1 else 100.0
                    monitor._clock = delayed_clock
                else:
                    def delayed_frames():
                        clock.value = 100.0
                        return {threading.get_ident(): Frame()}
                    monitor._frames_provider = delayed_frames
                    clock.value = 20.0
                monitor.check_once()
                snapshot = read_records(directory)[0]
                self.assertTrue(snapshot["capture_delayed"])
                self.assertEqual(snapshot["stack_observation"], "delayed_stale_heartbeat")
                self.assertEqual(snapshot["observation_gap_seconds"], 80.0)
                self.assertTrue(snapshot["heartbeat_unchanged_during_capture"])

    def test_missed_healthy_window_separates_consecutive_stall_episodes(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = Clock()
            monitor = self.make_monitor(directory, clock)
            for clock.value in (20.0, 30.0):
                monitor.check_once()
            # The observer missed recovery, and the new heartbeat is stale again.
            monitor._heartbeat_state = (1, 340.0)
            clock.value = 365.0
            monitor.check_once()
            records = read_records(directory)
            self.assertEqual([r["event"] for r in records], ["stall", "stall", "recovery", "stall"])
            self.assertEqual(records[2]["captures"], 2)
            self.assertTrue(records[2]["recovery_observation_delayed"])
            self.assertEqual(records[3]["heartbeat_sequence_before"], 1)
            self.assertEqual(monitor._episode_captures, 1)

    def test_delayed_recovery_includes_current_gap_and_only_claims_a_time_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = Clock()
            monitor = self.make_monitor(directory, clock)
            monitor._last_check = 19.5
            clock.value = 20.0
            monitor.check_once()
            monitor._heartbeat_state = (1, 99.0)
            clock.value = 100.0
            monitor.check_once()
            records = read_records(directory)
            self.assertEqual([r["event"] for r in records], ["stall", "recovery"])
            recovery = records[-1]
            self.assertEqual(recovery["observation_gap_seconds"], 80.0)
            self.assertEqual(recovery["observation_span_seconds"], 100.0)
            self.assertTrue(recovery["span_is_upper_bound"])
            self.assertTrue(recovery["recovery_observation_delayed"])
            self.assertNotIn("episode_duration_seconds", recovery)

    def test_observer_gap_after_loop_resumes_is_not_a_fake_past_stack(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = Clock()
            monitor = WebStallMonitor(directory, clock=clock, stall_seconds=5, cooldown_seconds=100,
                                      frames_provider=mock.Mock(side_effect=AssertionError("must not capture")))
            clock.value = 12
            monitor._heartbeat_state = (1, 12)
            monitor.check_once()
            record = read_records(directory)[0]
            self.assertEqual(record["kind"], "web_stall_observation_gap")
            self.assertFalse(record["stack_captured"])
            self.assertNotIn("threads", record)
            clock.value = 24
            monitor._heartbeat_state = (2, 24)
            monitor.check_once()
            self.assertEqual(len(read_records(directory)), 1)

    def test_monotonic_injected_clock_is_the_only_time_source(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch("shared.web_stall_monitor.time.time", side_effect=AssertionError):
            clock = Clock()
            monitor = self.make_monitor(directory, clock, stall_seconds=2)
            clock.value = 2
            monitor.check_once()
            self.assertEqual(read_records(directory)[0]["observed_at_monotonic"], 2)

    def test_monitor_contains_writer_failures(self):
        class FailingWriter:
            last_error = "write_error"
            def write(self, record):
                return False

        with tempfile.TemporaryDirectory() as directory:
            clock = Clock()
            monitor = WebStallMonitor(directory, clock=clock, stall_seconds=1, writer=FailingWriter())
            monitor._main_thread_id = threading.get_ident()
            clock.value = 1
            monitor.check_once()
            self.assertEqual(monitor.status()["last_error"], "write_error")

    def test_start_reentry_and_stop_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            loop = asyncio.new_event_loop()
            try:
                monitor = WebStallMonitor(directory, heartbeat_seconds=.01, stall_seconds=1)
                self.assertTrue(monitor.start(loop))
                self.assertFalse(monitor.start(loop))
                began = time.monotonic()
                monitor.stop()
                self.assertLess(time.monotonic() - began, .4)
                self.assertFalse(monitor.status()["enabled"])
                self.assertFalse(monitor.start(loop))
            finally:
                loop.close()

    def test_thread_start_failure_leaves_no_orphan_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory:
            loop = asyncio.new_event_loop()
            thread = mock.Mock()
            thread.start.side_effect = OSError("cannot start")
            try:
                monitor = WebStallMonitor(directory, thread_factory=lambda **kwargs: thread)
                self.assertFalse(monitor.start(loop))
                loop.run_until_complete(asyncio.sleep(0))
                self.assertEqual(monitor._heartbeat_state[0], 0)
                self.assertIsNone(monitor._heartbeat_handle)
                self.assertEqual(monitor.status()["last_error"], "start_error")
            finally:
                loop.close()

    def test_writer_rotation_permissions_links_lock_and_partial_write(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = _StallWriter(directory, max_file_bytes=140, max_record_bytes=120)
            record = {"event": "stall", "padding": "x" * 70}
            for _ in range(4):
                self.assertTrue(writer.write(record))
            stalls = Path(directory) / "web-stalls"
            logs = [stalls / "stalls.jsonl", stalls / "stalls.jsonl.1", stalls / "stalls.jsonl.2"]
            self.assertTrue(all(path.stat().st_size <= 140 for path in logs if path.exists()))
            self.assertLessEqual(sum(path.stat().st_size for path in logs if path.exists()), 420)

            (stalls / "stalls.jsonl").chmod(0o644)
            self.assertFalse(writer.write(record))
            self.assertEqual(writer.last_error, "unsafe_path")
            (stalls / "stalls.jsonl").chmod(0o600)
            (stalls / "stalls.jsonl").unlink()
            os.symlink("/tmp/not-a-log", stalls / "stalls.jsonl")
            self.assertFalse(writer.write(record))
            self.assertEqual(writer.last_error, "unsafe_path")
            (stalls / "stalls.jsonl").unlink()

            lock_fd = os.open(stalls / "stalls.lock", os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                self.assertFalse(writer.write(record))
                self.assertEqual(writer.last_error, "lock_busy")
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)

            # Actually write a prefix before failing, including the PermissionError
            # subclass: neither failure may leave a malformed tail behind.
            real_write = os.write
            for error in (OSError(errno.EIO, "disk failure"), PermissionError(errno.EACCES, "denied")):
                attempts = []
                def partial_then_fail(fd, data):
                    if not attempts:
                        attempts.append(True)
                        return real_write(fd, data[:5])
                    raise error
                with mock.patch("shared.web_stall_monitor.os.write", side_effect=partial_then_fail):
                    self.assertFalse(writer.write(record))
                self.assertEqual((stalls / "stalls.jsonl").read_bytes(), b"")

    def test_writer_refuses_base_links_hardlinks_and_oversize_generations(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as elsewhere:
            base = Path(directory)
            target = Path(elsewhere)
            (base / "linked-base").symlink_to(target, target_is_directory=True)
            self.assertFalse(_StallWriter(base / "linked-base").write({"kind": "test"}))
            self.assertEqual(list(target.iterdir()), [])
            writer = _StallWriter(base, max_file_bytes=140)
            self.assertTrue(writer.write({"kind": "test"}))
            log = base / "web-stalls/stalls.jsonl"
            log.unlink()
            outside = target / "outside"
            outside.write_bytes(b"do-not-modify")
            outside.chmod(0o600)
            os.link(outside, log)
            self.assertFalse(writer.write({"kind": "test"}))
            self.assertEqual(outside.read_bytes(), b"do-not-modify")
            log.unlink()
            log.write_bytes(b"x" * 141)
            log.chmod(0o600)
            self.assertFalse(writer.write({"kind": "test"}))
            self.assertFalse(log.with_name(log.name + ".1").exists())
            self.assertEqual(log.stat().st_size, 141)

    def test_unsafe_directory_fails_open(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as elsewhere:
            os.symlink(elsewhere, Path(directory) / "web-stalls")
            writer = _StallWriter(directory)
            self.assertFalse(writer.write({"event": "stall"}))
            self.assertEqual(writer.last_error, "unsafe_path")

    def test_real_asyncio_block_is_captured_in_isolated_process(self):
        with tempfile.TemporaryDirectory() as directory:
            script = r'''
import asyncio, sys, time
from pathlib import Path
from shared.web_stall_monitor import WebStallMonitor
async def block_main():
    time.sleep(.35)
async def run(path):
    monitor = WebStallMonitor(path, heartbeat_seconds=.01, stall_seconds=.1, capture_interval_seconds=.05, cooldown_seconds=.2)
    monitor.start(asyncio.get_running_loop())
    # Slow asynchronous work does not block Web and must not be called a stall.
    await asyncio.sleep(.2)
    assert not (Path(path)/'web-stalls/stalls.jsonl').exists()
    await block_main()
    await asyncio.sleep(.08)
    monitor.stop()
asyncio.run(run(sys.argv[1]))
'''
            result = subprocess.run(
                [sys.executable, "-c", script, directory], cwd=Path(__file__).resolve().parents[1],
                text=True, capture_output=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            records = read_records(directory)
            self.assertTrue(any(record["event"] == "recovery" for record in records))
            stacks = [record for record in records if record["event"] == "stall"]
            self.assertTrue(stacks)
            self.assertTrue(any(frame["function"] == "block_main" for record in stacks for thread in record["threads"] for frame in thread["frames"]))


if __name__ == "__main__":
    unittest.main()
