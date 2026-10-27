from __future__ import annotations

import contextlib
import importlib.util
import io
from importlib import metadata
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check-core-deps.py"
SPEC = importlib.util.spec_from_file_location("check_core_deps", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
check_core_deps = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_core_deps)


class CoreDependencyCheckerTests(unittest.TestCase):
    def run_checker(self, contents: str, versions: dict[str, str]) -> tuple[int, str, str, Path]:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        lock_path = Path(temporary_directory.name) / "core.lock"
        lock_path.write_text(contents, encoding="utf-8")

        def installed_version(name: str) -> str:
            try:
                return versions[check_core_deps._canonical_name(name)]
            except KeyError as exc:
                raise metadata.PackageNotFoundError(name) from exc

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(check_core_deps.metadata, "version", side_effect=installed_version),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            result = check_core_deps.main(["--lock", str(lock_path)])
        return result, stdout.getvalue(), stderr.getvalue(), lock_path

    def test_accepts_matching_distributions_without_writing_lock(self) -> None:
        contents = "FastAPI==0.139.0\njsonschema==4.25.1\n"
        result, stdout, stderr, lock_path = self.run_checker(
            contents,
            {"fastapi": "0.139.0", "jsonschema": "4.25.1"},
        )

        self.assertEqual(result, 0)
        self.assertEqual(stdout, "核心依赖检查通过。\n")
        self.assertEqual(stderr, "")
        self.assertEqual(lock_path.read_text(encoding="utf-8"), contents)

    def test_reports_missing_distribution(self) -> None:
        result, stdout, stderr, _ = self.run_checker("fastapi==0.139.0\n", {})

        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertIn("缺少：fastapi==0.139.0", stderr)

    def test_reports_version_mismatch(self) -> None:
        result, stdout, stderr, _ = self.run_checker(
            "fastapi==0.139.0\n", {"fastapi": "0.138.0"}
        )

        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertIn("需要 0.139.0，当前 0.138.0", stderr)

    def test_rejects_non_exact_lock_entry(self) -> None:
        result, stdout, stderr, _ = self.run_checker("fastapi>=0.139.0\n", {})

        self.assertEqual(result, 2)
        self.assertEqual(stdout, "")
        self.assertIn("不是精确的 name==version", stderr)

    def test_rejects_duplicate_canonical_distribution_name(self) -> None:
        result, stdout, stderr, _ = self.run_checker(
            "typing_extensions==4.16.0\ntyping-extensions==4.16.0\n", {}
        )

        self.assertEqual(result, 2)
        self.assertEqual(stdout, "")
        self.assertIn("分发名重复", stderr)

    def test_reports_missing_or_undecodable_lock_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corrupt = root / "corrupt.lock"
            corrupt.write_bytes(b"\xff\xfe")
            for path in (root / "absent.lock", corrupt):
                with self.subTest(path=path.name):
                    stderr = io.StringIO()
                    with contextlib.redirect_stderr(stderr):
                        result = check_core_deps.main(["--lock", str(path)])
                    self.assertEqual(result, 2)
                    self.assertIn("无法读取锁文件", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
