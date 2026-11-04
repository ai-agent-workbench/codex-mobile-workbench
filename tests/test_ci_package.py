import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-ci-package.py"
COMMIT = "a" * 40


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CiPackageCheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ci-package-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.prefix = "codex-mobile-workbench-package"
        self.source = self.root / self.prefix
        self.source.mkdir()
        application = self.source / "app.py"
        application.write_text("# fixture\n", encoding="utf-8")
        panel = self.source / "CodexWorkbenchPanel.exe"
        panel.write_bytes(b"MZfixture")
        (self.source / "package-source.json").write_text(json.dumps({
            "format_version": 1,
            "source_commit": COMMIT,
            "files": {"app.py": {"sha256": sha256(application), "size": application.stat().st_size, "executable": False}},
            "panel_sha256": sha256(panel),
        }), encoding="utf-8")

    def archive(self, *, prefix=None) -> tuple[Path, Path]:
        archive = self.root / "package.zip"
        prefix = self.prefix if prefix is None else prefix
        with zipfile.ZipFile(archive, "w") as output:
            for source in self.source.iterdir():
                output.write(source, f"{prefix}/{source.name}")
        checksum = self.root / "package.zip.sha256"
        checksum.write_text(f"{sha256(archive)}  {archive.name}\n", encoding="utf-8")
        return archive, checksum

    def run_check(self, archive: Path, checksum: Path, *, source_commit=COMMIT):
        return subprocess.run([
            sys.executable, str(SCRIPT), "--archive", str(archive), "--sha256-file", str(checksum),
            "--prefix", self.prefix, "--source-commit", source_commit,
        ], cwd=ROOT, text=True, capture_output=True, check=False)

    def test_accepts_verified_archive_with_expected_commit(self):
        archive, checksum = self.archive()
        result = self.run_check(archive, checksum)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"source_commit={COMMIT}", result.stdout)

    def test_rejects_wrong_top_level_prefix(self):
        archive, checksum = self.archive(prefix="other-package")
        result = self.run_check(archive, checksum)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("顶层前缀", result.stderr)

    def test_rejects_source_commit_mismatch(self):
        archive, checksum = self.archive()
        result = self.run_check(archive, checksum, source_commit="b" * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("来源 commit", result.stderr)

    def test_rejects_blank_checksum_file(self):
        archive, checksum = self.archive()
        checksum.write_text("\n", encoding="utf-8")
        result = self.run_check(archive, checksum)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不能为空", result.stderr)
