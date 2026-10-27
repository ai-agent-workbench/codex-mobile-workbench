"""Keep the explicit offline check inventory complete and failure-transparent."""
import contextlib
import io
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts import run_ui_checks


class UiValidationRunnerTests(unittest.TestCase):
    def test_all_collects_every_local_js_check_and_never_live_python_probes(self):
        planned = [run_ui_checks.script_for(name) for name in run_ui_checks.plan(["all"])]
        actual = {path.relative_to(run_ui_checks.ROOT)
                  for path in (run_ui_checks.ROOT / "tests").glob("check_*.js")}
        self.assertEqual(set(planned), actual)
        self.assertEqual(len(planned), len(actual))
        self.assertTrue(all(path.suffix == ".js" for path in planned))
        self.assertEqual(run_ui_checks.plan(["core", "all", "native"]), run_ui_checks.plan(["all"]))

    def test_dry_run_never_checks_dependencies_or_executes_children(self):
        with mock.patch("sys.argv", ["ui", "--dry-run", "core"]), \
             mock.patch.object(run_ui_checks, "preflight") as preflight, \
             mock.patch.object(run_ui_checks.subprocess, "run") as run, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(run_ui_checks.main(), 0)
        self.assertIn("check_conversation_mobile_playwright.js", output.getvalue())
        preflight.assert_not_called()
        run.assert_not_called()

    def test_first_child_failure_is_returned_without_running_later_checks(self):
        with mock.patch("sys.argv", ["ui", "core"]), \
             mock.patch.object(run_ui_checks, "preflight", return_value=True), \
             mock.patch.object(run_ui_checks.subprocess, "run", return_value=SimpleNamespace(returncode=7)) as run, \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run_ui_checks.main(), 7)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs["cwd"], run_ui_checks.ROOT)
