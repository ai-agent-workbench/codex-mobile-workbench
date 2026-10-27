"""Focused safety coverage for the offline runtime candidate builder."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import shutil
import tempfile
import unittest
from unittest import mock

from shared import runtime_bundle
from shared.runtime_bundle import BundleError, build_bundle, verify_bundle


class RuntimeBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "scripts").mkdir()
        (self.source / "fixtures").mkdir()
        (self.source / ".gitignore").write_text(".venv/\n__pycache__/\n", encoding="utf-8")
        (self.source / "app.py").write_text("VALUE = 'committed'\n", encoding="utf-8")
        rollback = self.source / "scripts" / "example.sh"
        rollback.write_text("#!/bin/sh\necho example\n", encoding="utf-8")
        rollback.chmod(0o755)
        (self.source / "fixtures" / "example.json").write_text('{"fixture": true}\n', encoding="utf-8")
        (self.source / "fixtures" / "中文.json").write_text('{"fixture": "中文"}\n', encoding="utf-8")
        (self.source / "line\nbreak.txt").write_text("newline filename\n", encoding="utf-8")
        (self.source / "requirements.lock").write_text("demo==1.0\n", encoding="utf-8")
        self._command("git", "init", "-q")
        self._command("git", "config", "user.email", "test@example.invalid")
        self._command("git", "config", "user.name", "Runtime Bundle Test")
        self._command("git", "add", ".")
        self._command("git", "commit", "-qm", "fixture")
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", ".venv"], cwd=self.source, check=True)
        source_python = self.source / ".venv" / "bin" / "python"
        purelib = Path(subprocess.check_output(
            [source_python, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"], text=True
        ).strip())
        (purelib / "demo.py").write_text("ANSWER = 42\n", encoding="utf-8")
        dist_info = purelib / "demo-1.0.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text("Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n", encoding="utf-8")
        (dist_info / "RECORD").write_text("", encoding="utf-8")
        private = purelib / "private"
        private.mkdir(mode=0o700)
        (private / "data.txt").write_text("private dependency data\n", encoding="utf-8")
        purelib.chmod(0o700)
        self.bundle = self.root / "candidate"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _command(self, *args: str) -> None:
        subprocess.run(args, cwd=self.source, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _build(self) -> dict:
        return build_bundle(self.source, self.bundle)

    def _unlock(self, *paths: Path) -> None:
        for path in paths:
            os.chmod(path, 0o700)

    def _remove_bundle(self) -> None:
        for dirname, directories, files in os.walk(self.bundle, topdown=False):
            for name in [*directories, *files]:
                path = Path(dirname) / name
                if not path.is_symlink():
                    os.chmod(path, 0o700)
            os.chmod(dirname, 0o700)
        shutil.rmtree(self.bundle)

    def test_build_archives_committed_app_and_copies_independent_dependencies(self) -> None:
        manifest = self._build()
        self.assertEqual(manifest, verify_bundle(self.bundle))
        self.assertIn("app/scripts/example.sh", manifest["files"])
        self.assertIn("app/fixtures/example.json", manifest["files"])
        self.assertIn("app/fixtures/中文.json", manifest["files"])
        self.assertIn("app/line\nbreak.txt", manifest["files"])
        self.assertIn("app/requirements.lock", manifest["files"])
        copied_demo = next((self.bundle / ".venv").glob("lib/python*/site-packages/demo.py"))
        self.assertIn(copied_demo.relative_to(self.bundle).as_posix(), manifest["files"])
        self.assertEqual(manifest["runtime"]["command"], [".venv/bin/python", "-m", "uvicorn"])
        self.assertEqual(manifest["runtime"]["environment"], {"PYTHONDONTWRITEBYTECODE": "1"})
        self.assertFalse((self.bundle / ".venv" / "bin" / "uvicorn").exists())
        self.assertFalse(any(child.name.lower().startswith("activate") for child in (self.bundle / ".venv" / "bin").iterdir()))
        config = (self.bundle / ".venv" / "pyvenv.cfg").read_text(encoding="utf-8")
        self.assertIn(os.fspath(self.bundle), config)
        self.assertNotIn(".candidate.building-", config)

        # Neither later development edits nor later dependency edits alter the candidate.
        (self.source / "app.py").write_text("VALUE = 'development'\n", encoding="utf-8")
        source_demo = next((self.source / ".venv").glob("lib/python*/site-packages/demo.py"))
        source_demo.write_text("ANSWER = 7\n", encoding="utf-8")
        self.assertEqual((self.bundle / "app" / "app.py").read_text(encoding="utf-8"), "VALUE = 'committed'\n")
        copied_demo = next((self.bundle / ".venv").glob("lib/python*/site-packages/demo.py"))
        self.assertEqual(copied_demo.read_text(encoding="utf-8"), "ANSWER = 42\n")
        self.assertEqual(manifest, verify_bundle(self.bundle))

        # This is the only runtime execution check: the new venv imports copied bytes,
        # not a source checkout or the removed staging path.
        result = subprocess.run(
            [self.bundle / ".venv" / "bin" / "python", "-B", "-I", "-c",
             "import demo,json,sys; print(json.dumps({'answer': demo.ANSWER, 'path': sys.path}))"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        imported = json.loads(result.stdout)
        self.assertEqual(imported["answer"], 42)
        self.assertNotIn(os.fspath(self.source), imported["path"])
        self.assertFalse(any(".candidate.building-" in entry for entry in imported["path"]))

    def test_dirty_source_is_refused_without_destination(self) -> None:
        (self.source / "app.py").write_text("dirty\n", encoding="utf-8")
        with self.assertRaises(BundleError):
            self._build()
        self.assertFalse(self.bundle.exists())

    def test_dependency_symlink_and_unknown_pth_are_refused(self) -> None:
        purelib = next((self.source / ".venv").glob("lib/python*/site-packages"))
        (purelib / "escape.py").symlink_to("/etc/passwd")
        with self.assertRaises(BundleError):
            self._build()
        self.assertFalse(self.bundle.exists())
        (purelib / "escape.py").unlink()
        marker = self.root / "pth-was-executed"
        (purelib / "untrusted.pth").write_text(
            f"import pathlib; pathlib.Path({str(marker)!r}).write_text('executed')\n", encoding="utf-8"
        )
        with self.assertRaises(BundleError):
            self._build()
        self.assertFalse(marker.exists(), "unknown .pth executed before the builder rejected it")
        self.assertFalse(self.bundle.exists())

    def test_verify_rejects_tampering_extra_files_and_bad_manifest(self) -> None:
        self._build()
        app = self.bundle / "app"
        app_file = app / "app.py"
        self._unlock(self.bundle, app, app_file)
        app_file.write_text("tampered\n", encoding="utf-8")
        app_file.chmod(0o400)
        app.chmod(0o500)
        self.bundle.chmod(0o500)
        with self.assertRaises(BundleError):
            verify_bundle(self.bundle)

        # Rebuild a fresh candidate for additional, independent verifier failures.
        self._remove_bundle()
        self._build()
        self._unlock(self.bundle)
        extra = self.bundle / "unexpected.txt"
        extra.write_text("extra\n", encoding="utf-8")
        extra.chmod(0o400)
        self.bundle.chmod(0o500)
        with self.assertRaises(BundleError):
            verify_bundle(self.bundle)

        self._unlock(self.bundle)
        extra.unlink()
        manifest = self.bundle / "runtime-bundle.json"
        os.chmod(manifest, 0o600)
        manifest.write_text(json.dumps({"bad": True}), encoding="utf-8")
        manifest.chmod(0o400)
        self.bundle.chmod(0o500)
        with self.assertRaises(BundleError):
            verify_bundle(self.bundle)

    def test_destination_and_cli_contract(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "shared.runtime_bundle", "build", "--source", os.fspath(self.source), "--destination", os.fspath(self.bundle)],
            cwd=Path(__file__).parents[1], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), verify_bundle(self.bundle))
        result = subprocess.run(
            [sys.executable, "-m", "shared.runtime_bundle", "verify", "--bundle", os.fspath(self.bundle)],
            cwd=Path(__file__).parents[1], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        with self.assertRaises(BundleError):
            build_bundle(self.source, self.source / "nested-candidate")
        linked_parent = self.root / "linked-parent"
        linked_parent.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(BundleError):
            build_bundle(self.source, linked_parent / "through-link")

    def test_git_archive_rejects_tracked_link_and_export_ignored_file(self) -> None:
        (self.source / "tracked-link").symlink_to("/etc/passwd")
        self._command("git", "add", "tracked-link")
        self._command("git", "commit", "-qm", "tracked link")
        with self.assertRaises(BundleError):
            self._build()
        self.assertFalse(self.bundle.exists())

        (self.source / "tracked-link").unlink()
        (self.source / "hidden.txt").write_text("must archive\n", encoding="utf-8")
        (self.source / ".gitattributes").write_text("hidden.txt export-ignore\n", encoding="utf-8")
        self._command("git", "add", "-A")
        self._command("git", "commit", "-qm", "export ignore")
        with self.assertRaises(BundleError):
            self._build()
        self.assertFalse(self.bundle.exists())

    def test_copy_drift_and_publication_race_are_refused(self) -> None:
        original_copy = runtime_bundle._copy_dependencies
        source_demo = next((self.source / ".venv").glob("lib/python*/site-packages/demo.py"))

        def drift(source: Path, destination: Path) -> None:
            original_copy(source, destination)
            source_demo.write_text("ANSWER = 7\n", encoding="utf-8")

        with mock.patch.object(runtime_bundle, "_copy_dependencies", drift):
            with self.assertRaises(BundleError):
                self._build()
        self.assertFalse(self.bundle.exists())

        # Recreate a clean fixture after the deliberate source mutation.
        source_demo.write_text("ANSWER = 42\n", encoding="utf-8")
        original_rename = runtime_bundle._rename_noreplace

        def race(stage: Path, destination: Path) -> None:
            destination.mkdir()
            original_rename(stage, destination)

        with mock.patch.object(runtime_bundle, "_rename_noreplace", race):
            with self.assertRaises(BundleError):
                self._build()
        self.assertTrue(self.bundle.is_dir())
        self.assertEqual(list(self.bundle.iterdir()), [])

    def test_verifier_rejects_empty_directory_and_duplicate_json_key(self) -> None:
        self._build()
        self._unlock(self.bundle)
        empty = self.bundle / "unlisted-empty"
        empty.mkdir(mode=0o500)
        self.bundle.chmod(0o500)
        with self.assertRaises(BundleError):
            verify_bundle(self.bundle)
        self._unlock(self.bundle)
        empty.rmdir()
        manifest = self.bundle / "runtime-bundle.json"
        os.chmod(manifest, 0o600)
        manifest.write_text('{"format_version": 1, "format_version": 1}', encoding="utf-8")
        manifest.chmod(0o400)
        self.bundle.chmod(0o500)
        with self.assertRaises(BundleError):
            verify_bundle(self.bundle)

    def test_exact_standard_setuptools_pth_is_allowed(self) -> None:
        purelib = next((self.source / ".venv").glob("lib/python*/site-packages"))
        (purelib / "_distutils_hack.py").write_text("def add_shim(): pass\n", encoding="utf-8")
        (purelib / "distutils-precedence.pth").write_text(runtime_bundle._DISTUTILS_PTH, encoding="utf-8")
        self.assertEqual(self._build(), verify_bundle(self.bundle))

    def test_sitecustomize_is_rejected_before_normal_probe(self) -> None:
        purelib = next((self.source / ".venv").glob("lib/python*/site-packages"))
        marker = self.root / "sitecustomize-was-executed"
        (purelib / "sitecustomize.py").write_text(
            f"from pathlib import Path; Path({str(marker)!r}).write_text('executed')\n", encoding="utf-8"
        )
        with self.assertRaises(BundleError):
            self._build()
        self.assertFalse(marker.exists(), "sitecustomize executed before rejection")
        self.assertFalse(self.bundle.exists())
        (purelib / "sitecustomize.py").unlink()
        package = purelib / "usercustomize"
        package.mkdir()
        (package / "__init__.py").write_text(
            f"from pathlib import Path; Path({str(marker)!r}).write_text('executed')\n", encoding="utf-8"
        )
        with self.assertRaises(BundleError):
            self._build()
        self.assertFalse(marker.exists(), "usercustomize package executed before rejection")

    def test_interruptions_clean_only_owned_staging(self) -> None:
        original_copy = runtime_bundle._copy_dependencies

        def interrupt_copy(source: Path, destination: Path) -> None:
            original_copy(source, destination)
            raise KeyboardInterrupt()

        with mock.patch.object(runtime_bundle, "_copy_dependencies", interrupt_copy):
            with self.assertRaises(KeyboardInterrupt):
                self._build()
        self.assertFalse(self.bundle.exists())
        self.assertFalse(list(self.root.glob(".candidate.building-*")))

        destination = self.root / "candidate-lock"
        original_lock = runtime_bundle._lock_down

        def interrupt_lock(bundle: Path, manifest: dict) -> None:
            original_lock(bundle, manifest)
            raise SystemExit(2)

        with mock.patch.object(runtime_bundle, "_lock_down", interrupt_lock):
            with self.assertRaises(SystemExit):
                build_bundle(self.source, destination)
        self.assertFalse(destination.exists())
        self.assertFalse(list(self.root.glob(".candidate-lock.building-*")))

    def test_unreadable_trees_are_rejected_instead_of_treated_as_empty(self) -> None:
        manifest = self._build()
        purelib = next((self.source / ".venv").glob("lib/python*/site-packages"))
        real_scandir = os.scandir

        def fail_scan(path):
            actual = Path(os.readlink(f"/proc/self/fd/{path}")) if isinstance(path, int) else Path(path)
            if actual in {purelib, self.bundle / "app"}:
                raise PermissionError("synthetic unreadable directory")
            return real_scandir(path)

        with mock.patch.object(runtime_bundle.os, "scandir", side_effect=fail_scan):
            with self.assertRaisesRegex(BundleError, "cannot enumerate"):
                runtime_bundle._dependency_snapshot(purelib)
            with self.assertRaisesRegex(BundleError, "cannot enumerate"):
                build_bundle(self.source, self.root / "unreadable-candidate")
            with self.assertRaisesRegex(BundleError, "cannot enumerate"):
                verify_bundle(self.bundle)
        self.assertFalse((self.root / "unreadable-candidate").exists())
        self.assertFalse(list(self.root.glob(".unreadable-candidate.building-*")))
        self.assertEqual(verify_bundle(self.bundle), manifest)

    def test_entry_classification_is_explicit_and_never_silently_skips_descendants(self) -> None:
        purelib = next((self.source / ".venv").glob("lib/python*/site-packages"))
        nested = purelib / "nested"
        nested.mkdir()
        (nested / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
        expected = runtime_bundle._dependency_snapshot(purelib)
        real_scandir = os.scandir

        class EntryWithBrokenClassification:
            def __init__(self, entry):
                self.name = entry.name

            def is_dir(self, *args, **kwargs):
                raise PermissionError("cached classification failed")

        class ScanWithBrokenClassification:
            def __init__(self, path):
                self.scan = real_scandir(path)

            def __enter__(self):
                return iter(EntryWithBrokenClassification(entry) for entry in self.scan)

            def __exit__(self, *args):
                self.scan.close()

        with mock.patch.object(runtime_bundle.os, "scandir", side_effect=ScanWithBrokenClassification):
            actual = runtime_bundle._dependency_snapshot(purelib)
        self.assertEqual(actual, expected)
        self.assertIn("nested/module.py", actual)

        real_stat = os.stat

        def fail_stat(path, *args, **kwargs):
            if path == "nested" and kwargs.get("dir_fd") is not None:
                raise PermissionError("authoritative classification failed")
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(runtime_bundle.os, "stat", side_effect=fail_stat):
            with self.assertRaisesRegex(BundleError, "cannot enumerate"):
                runtime_bundle._dependency_snapshot(purelib)


if __name__ == "__main__":
    unittest.main()
