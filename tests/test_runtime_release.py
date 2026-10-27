from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from shared import runtime_release
from shared.runtime_release import (
    RuntimeReleaseError,
    accept_runtime,
    cancel_runtime_pending,
    handoff_runtime,
    prepare_runtime_release,
    rollback_runtime,
    select_healthy_runtime,
    selected_runtime,
    write_runtime_pending,
)

COMMIT = "a" * 40
OLD = "20261008-080000"
NEW = "20261008-090000"


class RuntimeReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.releases = self.root / "STATE" / "releases"
        self.releases.mkdir(parents=True)
        self._release(OLD, "healthy")
        os.symlink(f"{OLD}/runtime", self.releases / "runtime-current")
        self.verify = mock.patch.object(runtime_release, "verify_bundle", return_value={"commit": COMMIT})
        self.build = mock.patch.object(runtime_release, "build_bundle", side_effect=self._build)
        self.verify_mock = self.verify.start()
        self.build_mock = self.build.start()

    def tearDown(self) -> None:
        self.build.stop()
        self.verify.stop()
        self.temporary.cleanup()

    def _build(self, source: Path, destination: Path, **kwargs: Path) -> dict:
        destination.mkdir()
        return {"commit": COMMIT}

    def _release(self, stamp: str, status: str, previous: str | None = None) -> Path:
        release = self.releases / stamp
        release.mkdir()
        (release / "runtime").mkdir()
        (release / "version").write_text(stamp + "\n", encoding="utf-8")
        (release / "status").write_text(status + "\n", encoding="utf-8")
        (release / "runtime-release.json").write_text(
            json.dumps({"format_version": 1, "release": stamp, "previous": previous, "commit": COMMIT}), encoding="utf-8"
        )
        return release

    def _prepare(self, previous: str | None = OLD) -> None:
        snapshot = self.releases / NEW
        snapshot.mkdir()
        source = self.root / "development-source"
        source.mkdir()
        (source / "do-not-touch").write_text("development\n", encoding="utf-8")
        metadata = prepare_runtime_release(source, snapshot, previous)
        self.assertEqual(metadata["commit"], COMMIT)
        self.assertEqual((source / "do-not-touch").read_text(encoding="utf-8"), "development\n")

    def test_prepare_preserves_outer_contract_and_selected_real_path(self) -> None:
        self._prepare()
        release = self.releases / NEW
        self.assertEqual((release / "version").read_text(encoding="utf-8"), NEW + "\n")
        self.assertEqual((release / "status").read_text(encoding="utf-8"), "candidate\n")
        self.assertEqual(selected_runtime(self.releases), (self.releases / OLD / "runtime").resolve())
        self.assertEqual(os.readlink(self.releases / "runtime-current"), f"{OLD}/runtime")

    def test_prepare_passes_optional_dependency_source_to_builder(self) -> None:
        snapshot = self.releases / NEW
        snapshot.mkdir()
        dependency_source = self.root / "audited-dependencies"
        dependency_source.mkdir()
        prepare_runtime_release(self.root, snapshot, OLD, dependency_source=dependency_source)
        self.assertEqual(self.build_mock.call_args.kwargs, {"dependency_source": dependency_source})

    def test_pending_handoff_accept_transaction(self) -> None:
        self._prepare()
        write_runtime_pending(self.releases, NEW, OLD)
        self.assertEqual((self.releases / "pending-publish").read_text(encoding="utf-8"), f"{NEW}\n{OLD}\n")
        self.assertEqual((self.releases / "pending-publish").stat().st_mode & 0o777, 0o600)
        handoff_runtime(self.releases, NEW)
        self.assertEqual(os.readlink(self.releases / "runtime-current"), f"{NEW}/runtime")
        accept_runtime(self.releases, NEW)
        self.assertEqual((self.releases / NEW / "status").read_text(encoding="utf-8"), "healthy\n")
        self.assertFalse((self.releases / "pending-publish").exists())

    def test_rollback_requires_healthy_verified_fallback_and_failure_keeps_marker(self) -> None:
        self._prepare()
        write_runtime_pending(self.releases, NEW, OLD)
        handoff_runtime(self.releases, NEW)
        with mock.patch.object(runtime_release, "_swap_current", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                rollback_runtime(self.releases, NEW)
        self.assertTrue((self.releases / "pending-publish").exists())
        self.assertEqual((self.releases / NEW / "status").read_text(encoding="utf-8"), "failed\n")

    def test_interrupted_rollback_after_pointer_swap_can_finish_marker_cleanup(self):
        self._prepare()
        write_runtime_pending(self.releases, NEW, OLD)
        handoff_runtime(self.releases, NEW)
        original = Path.unlink
        def interrupt(path, *args, **kw):
            if path.name == 'pending-publish':
                raise OSError('interrupted marker removal')
            return original(path, *args, **kw)
        with mock.patch.object(Path, 'unlink', interrupt):
            with self.assertRaisesRegex(OSError, 'interrupted'):
                rollback_runtime(self.releases, NEW)
        self.assertEqual(os.readlink(self.releases / 'runtime-current'), f'{OLD}/runtime')
        self.assertTrue((self.releases / 'pending-publish').exists())
        rollback_runtime(self.releases, NEW)
        self.assertFalse((self.releases / 'pending-publish').exists())

    def test_cancel_only_before_handoff_and_manual_healthy_selection_rejects_pending(self) -> None:
        self._prepare()
        write_runtime_pending(self.releases, NEW, OLD)
        with self.assertRaises(RuntimeReleaseError):
            select_healthy_runtime(self.releases, OLD)
        cancel_runtime_pending(self.releases, NEW)
        self.assertFalse((self.releases / "pending-publish").exists())
        self.assertEqual((self.releases / NEW / "status").read_text(encoding="utf-8"), "candidate\n")
        selected = select_healthy_runtime(self.releases, OLD)
        self.assertEqual(selected, (self.releases / OLD / "runtime").resolve())

    def test_rejects_current_marker_and_release_link_mismatches(self) -> None:
        self._prepare()
        (self.releases / "runtime-current").unlink()
        os.symlink(f"{OLD}/not-runtime", self.releases / "runtime-current")
        with self.assertRaises(RuntimeReleaseError):
            selected_runtime(self.releases)
        (self.releases / "runtime-current").unlink()
        os.symlink(f"{OLD}/runtime", self.releases / "runtime-current")
        (self.releases / "pending-publish").write_text(f"{NEW}\nwrong\n", encoding="utf-8")
        with self.assertRaises(RuntimeReleaseError):
            handoff_runtime(self.releases, NEW)
        self.assertTrue((self.releases / "pending-publish").exists())
        (self.releases / NEW / "runtime").rmdir()
        os.symlink("/tmp", self.releases / NEW / "runtime")
        (self.releases / "runtime-current").unlink()
        os.symlink(f"{NEW}/runtime", self.releases / "runtime-current")
        with self.assertRaises(RuntimeReleaseError):
            selected_runtime(self.releases)

    def test_no_overwrite_prepare_and_pending(self) -> None:
        self._prepare()
        with self.assertRaises(RuntimeReleaseError):
            prepare_runtime_release(self.root, self.releases / NEW, OLD)
        write_runtime_pending(self.releases, NEW, OLD)
        with self.assertRaises(RuntimeReleaseError):
            write_runtime_pending(self.releases, NEW, OLD)


if __name__ == "__main__":
    unittest.main()
