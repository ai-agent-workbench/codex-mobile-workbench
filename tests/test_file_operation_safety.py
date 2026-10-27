"""Real temporary files protect browser write/confirmation safety, not production."""
import errno
import ctypes
import html
import json
import io
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from itsdangerous import URLSafeTimedSerializer
from starlette.requests import Request

from routes import files as routes
from shared import file_operations as ops, file_utils


class Upload:
    def __init__(self, name, body=b"uploaded"):
        self.filename, self.file = name, io.BytesIO(body)


class FileOperationSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="browser-file-safety-", dir=Path.home())
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.guard = self.root / "protected"
        self.guard.mkdir()
        self.secret = self.guard / "secret.txt"
        self.secret.write_bytes(b"protected")
        self.signer = URLSafeTimedSerializer("test-only-key", salt="test-only-confirm")
        self.request = Request({"type": "http", "method": "POST", "path": "/files",
                                "headers": [], "client": ("127.0.0.1", 1)})
        for context in (
            patch.object(file_utils, "FILE_ROOTS", (self.root,)),
            patch.object(file_utils, "SENSITIVE_PATHS", (self.guard,)),
            patch.object(routes, "require_auth", return_value=None),
            patch.object(routes, "confirmation_signer", return_value=self.signer),
            patch.object(routes, "file_page_signer", return_value=self.signer),
            patch.object(routes, "audit"), patch.object(routes, "notify"),
            patch.object(routes, "UPLOADS", self.root / "uploads"),
            patch.object(file_utils, "UPLOADS", self.root / "uploads"),
            patch.object(routes, "EDITS", self.root / "edits"),
            patch.object(file_utils, "EDITS", self.root / "edits"),
        ):
            context.start()
            self.addCleanup(context.stop)

    def file(self, name, data=b"source"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def token(self, response):
        return html.unescape(re.search(r"name='token' value='([^']+)'", response.body.decode())[1])

    def prepared_transfer(self, source, target, action="copy"):
        return routes.transfer_confirm(self.request, action, str(source), str(target.parent), target.name)

    def assert_no_temporary(self, directory=None):
        self.assertEqual(list((directory or self.root).glob(".codex-web-*")), [])

    def list_files(self, directory=None, **query):
        with patch.object(routes, "FILE_ROOTS", (self.root,)):
            return routes.files(self.request, str(directory or self.root), **query)

    def next_cursor(self, response):
        match = re.search(r"rel='next' href='([^']+)'", response.body.decode())
        if not match:
            return ""
        return parse_qs(urlsplit(html.unescape(match[1])).query)["cursor"][0]

    def listed_names(self, response):
        return re.findall(r"📄 ([^<]+)</span>|📁 <a [^>]+>([^<]+)</a>", response.body.decode())

    def test_file_browser_does_not_silently_lose_the_505th_safe_file(self):
        """Regression: the old fixed 500-entry list made later safe files unreachable."""
        for number in range(505):
            self.file(f"item-{number:04d}.txt")
        response = self.list_files()
        for _ in range(5):
            response = self.list_files(cursor=self.next_cursor(response))
        self.assertIn("item-0504.txt", response.body.decode())

    def test_file_browser_pages_every_safe_item_without_duplicates(self):
        for count in (0, 1, 99, 100, 101, 500, 505, 5000):
            with self.subTest(count=count):
                directory = self.root / ("list-" + str(count))
                directory.mkdir()
                expected = [f"entry-{number:04d}.txt" for number in range(count)]
                for name in expected:
                    self.file(str(directory.relative_to(self.root) / name))
                response = self.list_files(directory)
                found = []
                while True:
                    body = response.body.decode()
                    self.assertLessEqual(body.count("data-file-actions"), 100)
                    found.extend(match[0] or match[1] for match in self.listed_names(response))
                    cursor = self.next_cursor(response)
                    if not cursor:
                        break
                    response = self.list_files(directory, cursor=cursor)
                self.assertEqual(found, expected)
                self.assertEqual(len(found), len(set(found)))

    def test_listing_fast_path_rechecks_permissions_types_and_sensitive_links(self):
        allowed = self.file("visible.txt")
        secret = self.file("hidden-root/private.txt")
        link = self.root / "link.txt"
        link.symlink_to(allowed)
        with patch.object(file_utils, "SENSITIVE_PATHS", (secret.parent,)):
            first = self.list_files().body.decode()
            self.assertIn("visible.txt", first)
            self.assertIn("link.txt", first)
            self.assertNotIn("hidden-root", first)
            allowed.chmod(0)
            unreadable = self.list_files().body.decode()
            self.assertNotIn("visible.txt", unreadable)
            self.assertNotIn("link.txt", unreadable)
            allowed.chmod(0o600)
            link.unlink()
            link.symlink_to(secret)
            replaced = self.list_files().body.decode()
            self.assertIn("visible.txt", replaced)
            self.assertNotIn("link.txt", replaced)
            self.assertNotIn("hidden-root", replaced)
            # The helper is also safe if directly used below a sensitive root.
            self.assertIsNone(file_utils.sensitive_direct_child_names(secret.parent))
            with os.scandir(secret.parent) as scan:
                self.assertIsNone(routes._readable_file_entry(next(scan)))

    def test_file_browser_sorts_directories_then_casefold_name_and_original_name(self):
        for name in ("z-folder", "A-folder", "中文目录"):
            (self.root / name).mkdir()
        for name in ("beta.txt", "alpha.txt", "Alpha.txt", "资料&<.txt"):
            self.file(name)
        body = self.list_files().body.decode()
        positions = [body.index(name) for name in ("A-folder", "z-folder", "Alpha.txt", "alpha.txt", "beta.txt")]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("中文目录", body)
        self.assertIn("资料&amp;&lt;.txt", body)

    def test_file_browser_cursor_binds_filter_snapshot_and_rejects_tampering(self):
        for number in range(101):
            self.file(f"entry-{number:04d}.txt")
        first = self.list_files()
        cursor = self.next_cursor(first)
        self.assertTrue(cursor)
        self.assertEqual(self.list_files(cursor=cursor, search="entry-0100").status_code, 409)
        self.assertEqual(self.list_files(cursor=cursor + "tampered").status_code, 409)
        self.file("later.txt")
        self.assertEqual(self.list_files(cursor=cursor).status_code, 409)
        after_add = self.list_files()
        after_add_cursor = self.next_cursor(after_add)
        (self.root / "later.txt").unlink()
        self.assertEqual(self.list_files(cursor=after_add_cursor).status_code, 409)
        after_delete = self.list_files()
        after_delete_cursor = self.next_cursor(after_delete)
        (self.root / "entry-0100.txt").rename(self.root / "renamed.txt")
        self.assertEqual(self.list_files(cursor=after_delete_cursor).status_code, 409)
        link_target = self.file("link-target-a.txt")
        other_target = self.file("link-target-b.txt")
        link = self.root / "safe-link"
        link.symlink_to(link_target)
        with_link = self.list_files()
        with_link_cursor = self.next_cursor(with_link)
        link.unlink()
        link.symlink_to(other_target)
        self.assertEqual(self.list_files(cursor=with_link_cursor).status_code, 409)

    def test_file_browser_hides_guarded_links_and_unreadable_entries_before_paging(self):
        self.file("safe.txt")
        (self.root / "safe-link").symlink_to(self.root / "safe.txt")
        (self.root / "broken-link").symlink_to(self.root / "missing.txt")
        (self.root / "guard-link").symlink_to(self.secret)
        (self.root / "outside-link").symlink_to(Path(__file__).resolve())
        unreadable = self.file("unreadable.txt")
        unreadable.chmod(0o000)
        self.addCleanup(unreadable.chmod, 0o600)
        unreadable_directory = self.root / "unreadable-directory"
        unreadable_directory.mkdir()
        unreadable_directory.chmod(0o000)
        self.addCleanup(unreadable_directory.chmod, 0o700)
        actual_access = os.access
        def deny_fixture_entries(path, mode):
            return Path(path).name not in {"unreadable.txt", "unreadable-directory"} and actual_access(path, mode)
        with patch.object(routes.os, "access", side_effect=deny_fixture_entries):
            body = self.list_files().body.decode()
        for hidden in ("secret.txt", "broken-link", "guard-link", "outside-link", "unreadable.txt",
                       "unreadable-directory", "protected"):
            self.assertNotIn(hidden, body)
        self.assertNotIn("已跳过", body)
        self.assertIn("safe.txt", body)
        self.assertIn("safe-link", body)

    def test_file_browser_page_cursor_does_not_reject_file_content_edits(self):
        for number in range(101):
            self.file(f"entry-{number:04d}.txt")
        cursor = self.next_cursor(self.list_files())
        (self.root / "entry-0000.txt").write_text("edited without changing the listing")
        response = self.list_files(cursor=cursor)
        self.assertEqual(response.status_code, 200)
        self.assertIn("entry-0100.txt", response.body.decode())

    def test_file_browser_cursor_contains_no_raw_directory_metadata(self):
        for number in range(101):
            self.file(f"entry-{number:04d}.txt")
        for number in range(5):
            (self.guard / f"hidden-{number}").mkdir()
        payload = self.signer.loads(self.next_cursor(self.list_files()))
        self.assertEqual(set(payload), {"purpose", "directory", "search", "sort", "offset", "snapshot"})
        self.assertIsInstance(payload["snapshot"], str)
        self.assertNotIn("hidden-", json.dumps(payload))
        # Different inaccessible targets must not be serialized into page tokens.
        self.assertNotIn(str(self.guard), json.dumps(payload))

    def test_file_browser_unsafe_prefix_cannot_displace_safe_later_entries(self):
        for number in range(120):
            (self.root / f"000-hidden-{number:04d}").symlink_to(self.secret)
        for number in range(101):
            self.file(f"zzz-safe-{number:04d}.txt")
        first = self.list_files()
        second = self.list_files(cursor=self.next_cursor(first))
        self.assertEqual(len(self.listed_names(first)), 100)
        self.assertIn("zzz-safe-0100.txt", second.body.decode())
        for response in (first, second, self.list_files(search="hidden")):
            self.assertNotIn("000-hidden-", response.body.decode())
            self.assertNotIn(str(self.secret), response.body.decode())

    def test_file_browser_replaced_directory_and_mid_scan_change_require_refresh(self):
        directory = self.root / "listing"
        directory.mkdir()
        for number in range(101):
            self.file(f"listing/file-{number:04d}.txt")
        cursor = self.next_cursor(self.list_files(directory))
        directory.rename(self.root / "old-listing")
        directory.mkdir()
        self.file("listing/replacement.txt")
        self.assertEqual(self.list_files(directory, cursor=cursor).status_code, 409)
        original = routes._readable_file_entry
        changed = False
        def change_during_scan(child, *args):
            nonlocal changed
            entry = original(child, *args)
            if not changed:
                changed = True
                self.file("listing/new-during-scan.txt")
            return entry
        with patch.object(routes, "_readable_file_entry", side_effect=change_during_scan):
            result = self.list_files(directory)
        self.assertEqual(result.status_code, 409)
        self.assertIn("刷新文件列表", result.body.decode())

    def test_file_browser_directory_swap_before_scan_does_not_expose_outside_names(self):
        directory = self.root / "listing"
        directory.mkdir()
        with tempfile.TemporaryDirectory(prefix="file-list-outside-", dir=Path.home()) as temp:
            outside = Path(temp)
            (outside / "must-not-expose.txt").write_text("outside allowed roots")
            original = routes._visible_file_entries
            def swap_before_scan(path, search):
                directory.rename(self.root / "old-listing")
                directory.symlink_to(outside, target_is_directory=True)
                return original(path, search)
            with patch.object(routes, "_visible_file_entries", side_effect=swap_before_scan):
                response = self.list_files(directory)
            self.assertEqual(response.status_code, 409)
            self.assertNotIn("must-not-expose.txt", response.body.decode())

    def test_file_browser_uses_plain_search_and_pagination_links(self):
        for number in range(101):
            self.file(f"entry-{number:04d}.txt")
        body = self.list_files().body.decode()
        self.assertIn("method='get'", body)
        self.assertIn("name='search'", body)
        self.assertIn("本页显示 100 项。还有更多可加载。", body)
        self.assertIn("rel='next'", body)
        self.assertEqual(body.count("data-file-actions"), 100)
        second = self.list_files(cursor=self.next_cursor(self.list_files()))
        self.assertIn("rel='prev'", second.body.decode())
        searched = self.list_files(search="ENTRY-0100")
        self.assertIn("entry-0100.txt", searched.body.decode())
        self.assertNotIn("entry-0099.txt", searched.body.decode())

    def test_copy_requires_explicit_overwrite_and_preserves_source(self):
        source, target = self.file("source"), self.file("target", b"old")
        with self.assertRaises(ValueError):
            file_utils.copy_file_compatible(source, target)
        self.assertEqual(target.read_bytes(), b"old")
        state = ops.destination_state(target)
        file_utils.copy_file_compatible(source, target, expected=state)
        self.assertEqual(target.read_bytes(), b"source")
        self.assertEqual(source.read_bytes(), b"source")
        self.assert_no_temporary()

    def test_transfer_confirmation_names_overwrite_and_executes_it(self):
        source, target = self.file("source"), self.file("target", b"old")
        response = self.prepared_transfer(source, target)
        self.assertIn("确认覆盖并执行", response.body.decode())
        self.assertIn("无法从工作台恢复", response.body.decode())
        result = routes.transfer_execute(self.request, self.token(response))
        self.assertIn("操作成功", result.body.decode())
        self.assertEqual(target.read_bytes(), b"source")

    def test_new_conflict_after_confirmation_is_not_overwritten(self):
        source, target = self.file("source"), self.root / "target"
        response = self.prepared_transfer(source, target)
        target.write_bytes(b"late file")
        result = routes.transfer_execute(self.request, self.token(response))
        self.assertIn("重新确认", result.body.decode())
        self.assertEqual(target.read_bytes(), b"late file")

    def test_changed_existing_conflict_needs_fresh_confirmation(self):
        source, target = self.file("source"), self.file("target", b"old")
        response = self.prepared_transfer(source, target)
        target.write_bytes(b"edited since confirmation")
        result = routes.transfer_execute(self.request, self.token(response))
        self.assertIn("操作失败", result.body.decode())
        self.assertEqual(target.read_bytes(), b"edited since confirmation")

    def test_old_transfer_token_cannot_authorize_overwrite(self):
        source, target = self.file("source"), self.file("target", b"old")
        token = self.signer.dumps({"action": "copy", "source": str(source), "destination": str(target)})
        result = routes.transfer_execute(self.request, token)
        self.assertIn("操作失败", result.body.decode())
        self.assertEqual(target.read_bytes(), b"old")

    def test_complete_target_guard_rejects_sensitive_missing_target(self):
        source = self.file("source")
        for action in ("copy", "move"):
            response = self.prepared_transfer(source, self.guard / "new", action)
            self.assertIn("误操作护栏", response.body.decode())
        self.assertFalse((self.guard / "new").exists())

    def test_target_link_and_broken_link_are_rejected(self):
        source = self.file("source")
        for referent in (self.secret, self.guard / "missing"):
            target = self.root / "link"
            target.symlink_to(referent)
            with self.assertRaises(ValueError):
                ops.transfer(source, target)
            target.unlink()
        self.assertEqual(self.secret.read_bytes(), b"protected")

    def test_source_same_file_hardlink_rejected(self):
        source, target = self.file("source"), self.root / "hardlink"
        os.link(source, target)
        with self.assertRaises(ValueError):
            ops.transfer(source, target, expected=ops.destination_state(target))
        self.assertEqual(source.read_bytes(), b"source")

    def test_source_link_changed_after_confirmation_is_not_followed(self):
        source, target = self.file("source"), self.root / "target"
        response = self.prepared_transfer(source, target)
        source.unlink()
        source.symlink_to(self.secret)
        result = routes.transfer_execute(self.request, self.token(response))
        self.assertIn("操作失败", result.body.decode())
        self.assertFalse(target.exists())

    def test_new_target_conflict_in_commit_window_is_create_only(self):
        source, target = self.file("source"), self.root / "target"
        original = ops._assert_target
        calls = 0
        def race(*args):
            nonlocal calls
            original(*args)
            calls += 1
            if calls == 2:
                target.symlink_to(self.secret)
        with patch.object(ops, "_assert_target", side_effect=race):
            with self.assertRaises(FileExistsError):
                ops.transfer(source, target)
        self.assertEqual(self.secret.read_bytes(), b"protected")
        self.assertTrue(target.is_symlink())
        self.assert_no_temporary()

    def test_upload_does_not_touch_old_predictable_staging_link(self):
        staged, target = self.file("staged"), self.root / "target"
        old = self.root / ".target.codex-web-upload"
        old.symlink_to(self.secret)
        file_utils.install_upload(staged, target)
        self.assertEqual(target.read_bytes(), b"source")
        self.assertFalse(target.is_symlink())
        self.assertEqual(self.secret.read_bytes(), b"protected")
        self.assertTrue(old.is_symlink())

    def test_edit_does_not_touch_predictable_staging_link_and_preserves_mode(self):
        target = self.file("target", b"old")
        target.chmod(0o640)
        old = self.root / ".target.codex-web-tmp"
        old.symlink_to(self.secret)
        confirmed = routes.edit_confirm(self.request, str(target), "edited")
        result = routes.edit_write(self.request, self.token(confirmed))
        self.assertIn("写入成功", result.body.decode())
        self.assertEqual(target.read_bytes(), b"edited")
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.secret.read_bytes(), b"protected")
        self.assertFalse(target.is_symlink())

    def test_edit_rejects_modified_file_and_retains_staging(self):
        target = self.file("target", b"old")
        confirmed = routes.edit_confirm(self.request, str(target), "edited")
        target.write_bytes(b"other editor")
        result = routes.edit_write(self.request, self.token(confirmed))
        self.assertIn("写入失败", result.body.decode())
        self.assertEqual(target.read_bytes(), b"other editor")
        self.assertEqual(len(list((self.root / "edits").iterdir())), 1)

    def test_upload_rejects_staged_link(self):
        uploads = self.root / "uploads"
        uploads.mkdir()
        (uploads / "token").symlink_to(self.secret)
        with self.assertRaises(ValueError):
            file_utils.staged_upload("token")
        with self.assertRaises(OSError):
            ops.install_staged(uploads / "token", self.root / "target")

    def test_temporary_name_collision_is_retried_not_followed(self):
        staged, target = self.file("staged"), self.root / "target"
        malicious = self.root / (".codex-web-" + "0" * 32)
        malicious.symlink_to(self.secret)
        with patch.object(ops.secrets, "token_hex", side_effect=["0" * 32, "1" * 32]):
            file_utils.install_upload(staged, target)
        self.assertEqual(self.secret.read_bytes(), b"protected")
        self.assertEqual(target.read_bytes(), b"source")

    def test_copy_failure_leaves_old_target_and_no_temporary(self):
        source, target = self.file("source"), self.file("target", b"old")
        with patch.object(ops, "_copy_bytes", side_effect=OSError(errno.ENOSPC, "fixture")):
            with self.assertRaises(OSError):
                ops.transfer(source, target, expected=ops.destination_state(target))
        self.assertEqual(target.read_bytes(), b"old")
        self.assertEqual(source.read_bytes(), b"source")
        self.assert_no_temporary()

    def test_cross_device_move_copy_failure_keeps_both_originals(self):
        source, target = self.file("source"), self.file("target", b"old")
        with patch.object(ops, "_rename", side_effect=OSError(errno.EXDEV, "fixture")), \
                patch.object(ops, "_copy_bytes", side_effect=OSError(errno.EACCES, "fixture")):
            with self.assertRaises(OSError):
                ops.transfer(source, target, move=True, expected=ops.destination_state(target))
        self.assertEqual(target.read_bytes(), b"old")
        self.assertEqual(source.read_bytes(), b"source")
        self.assert_no_temporary()

    def test_move_same_filesystem_replaces_atomically_without_predelete(self):
        source, target = self.file("source"), self.file("target", b"old")
        with patch.object(ops.os, "unlink", side_effect=AssertionError("predelete")):
            ops.transfer(source, target, move=True, expected=ops.destination_state(target))
        self.assertFalse(source.exists())
        self.assertEqual(target.read_bytes(), b"source")

    def test_move_same_filesystem_unreadable_regular_file_needs_no_read_access(self):
        for existing in (False, True):
            source = self.file("source", b"unreadable fixture")
            target = self.root / "target"
            if existing:
                target.write_bytes(b"old")
            expected = ops.destination_state(target)
            source.chmod(0)
            ops.transfer(source, target, move=True, expected=expected)
            self.assertFalse(source.exists())
            self.assertEqual(target.stat().st_mode & 0o777, 0)
            target.chmod(0o600)
            self.assertEqual(target.read_bytes(), b"unreadable fixture")
            target.unlink()

    def test_real_cross_filesystem_move_and_failed_unreadable_source(self):
        if not Path("/dev/shm").is_dir():
            self.skipTest("no second filesystem")
        with tempfile.TemporaryDirectory(prefix="browser-safety-", dir="/dev/shm") as other:
            other = Path(other)
            if other.stat().st_dev == self.root.stat().st_dev:
                self.skipTest("same filesystem")
            with patch.object(file_utils, "FILE_ROOTS", (self.root, other)):
                source, target = self.file("source"), other / "target"
                target.write_bytes(b"old")
                ops.transfer(source, target, move=True, expected=ops.destination_state(target))
                self.assertFalse(source.exists())
                self.assertEqual(target.read_bytes(), b"source")
                source = self.file("unreadable")
                source.chmod(0)
                try:
                    with self.assertRaises(PermissionError):
                        ops.transfer(source, target, move=True, expected=ops.destination_state(target))
                    self.assertEqual(target.read_bytes(), b"source")
                    self.assertTrue(source.exists())
                finally:
                    source.chmod(0o600)

    def test_cross_device_move_cleanup_error_reports_committed_destination(self):
        source, target = self.file("source"), self.file("target", b"old")
        unlink = ops.os.unlink
        def fail_source(name, **kwargs):
            if name == "source":
                raise PermissionError("fixture")
            return unlink(name, **kwargs)
        with patch.object(ops, "_rename", side_effect=OSError(errno.EXDEV, "fixture")), \
                patch.object(ops.os, "unlink", side_effect=fail_source):
            with self.assertRaises(ops.MoveCleanupPending):
                ops.transfer(source, target, move=True, expected=ops.destination_state(target))
        self.assertEqual(target.read_bytes(), b"source")
        self.assertEqual(source.read_bytes(), b"source")

    def test_legacy_move_requires_second_confirmation_and_no_predelete_on_failure(self):
        source, target = self.file("source"), self.file("target", b"old")
        first = routes.move_confirm(self.request, str(source), str(self.root), target.name)
        second = routes.move_execute(self.request, self.token(first))
        self.assertIn("再次确认覆盖", second.body.decode())
        with patch.object(ops, "_rename", side_effect=OSError(errno.EACCES, "fixture")):
            result = routes.move_execute(self.request, self.token(second))
        self.assertIn("移动失败", result.body.decode())
        self.assertEqual(target.read_bytes(), b"old")
        self.assertEqual(source.read_bytes(), b"source")

    def test_directory_copy_checks_guarded_descendants_before_committing(self):
        source = self.root / "tree"
        source.mkdir()
        guarded = self.file("tree/private/key", b"fixture")
        target = self.root / "copied"
        with patch.object(file_utils, "SENSITIVE_PATHS", (guarded.parent,)):
            with self.assertRaises(ValueError):
                file_utils.copy_directory_compatible(source, target)
        self.assertFalse(target.exists())
        self.assert_no_temporary()

    def test_directory_copy_rejects_link_or_special_file_without_partial_target(self):
        source = self.root / "tree"
        source.mkdir()
        self.file("tree/ordinary", b"copied first")
        for kind in ("link", "fifo"):
            special = source / "special"
            if kind == "link":
                special.symlink_to(self.secret)
            else:
                os.mkfifo(special)
            with self.assertRaises(ValueError):
                ops.transfer(source, self.root / "copied")
            self.assertFalse((self.root / "copied").exists())
            self.assert_no_temporary()
            special.unlink()

    def test_directory_copy_rejects_self_descendant_even_in_direct_helper(self):
        source = self.root / "tree"
        source.mkdir()
        for move in (False, True):
            with self.assertRaises(ValueError):
                ops.transfer(source, source / "copied", move=move)
        self.assertEqual(list(source.iterdir()), [])

    def test_directory_copy_preserves_nested_content(self):
        source = self.root / "tree"
        self.file("tree/nested/leaf", b"nested")
        target = self.root / "copied"
        file_utils.copy_directory_compatible(source, target)
        self.assertEqual((target / "nested/leaf").read_bytes(), b"nested")
        self.assert_no_temporary()

    def test_unsupported_noreplace_keeps_windows_file_and_directory_compatibility(self):
        def unsupported(*args):
            ctypes.set_errno(errno.EINVAL)
            return -1
        with patch.object(ops.ctypes, "CDLL", return_value=SimpleNamespace(renameat2=unsupported)):
            source, target = self.file("source"), self.root / "target"
            ops.transfer(source, target, move=True)
            self.assertEqual(target.read_bytes(), b"source")
            self.assertFalse(source.exists())
            tree = self.root / "tree"
            self.file("tree/leaf", b"nested")
            ops.transfer(tree, self.root / "copied")
            ops.transfer(tree, self.root / "moved", move=True)
            self.assertFalse(tree.exists())
            self.assertEqual((self.root / "copied/leaf").read_bytes(), b"nested")
            self.assertEqual((self.root / "moved/leaf").read_bytes(), b"nested")

    def test_copy_preserves_file_modification_time(self):
        source = self.file("source")
        os.utime(source, ns=(1_000_000_000, 2_000_000_000))
        ops.transfer(source, self.root / "copied")
        self.assertEqual((self.root / "copied").stat().st_mtime_ns, source.stat().st_mtime_ns)

    def test_directory_commit_failure_cleans_readonly_staging_tree(self):
        source = self.root / "tree"
        self.file("tree/nested/leaf", b"nested")
        (source / "nested").chmod(0o500)
        self.addCleanup((source / "nested").chmod, 0o700)
        with patch.object(ops, "_rename", side_effect=OSError(errno.EACCES, "fixture")):
            with self.assertRaises(OSError):
                ops.transfer(source, self.root / "copied")
        self.assertFalse((self.root / "copied").exists())
        self.assert_no_temporary()

    def test_directory_copy_failure_leaves_no_partial_copy(self):
        source = self.root / "tree"
        self.file("tree/leaf", b"leaf")
        with patch.object(ops, "_copy_bytes", side_effect=OSError(errno.ENOSPC, "fixture")):
            with self.assertRaises(OSError):
                ops.transfer(source, self.root / "copied")
        self.assertFalse((self.root / "copied").exists())
        self.assert_no_temporary()

    def test_directory_move_empty_target_keeps_original_on_failure(self):
        source, target = self.root / "tree", self.root / "target"
        self.file("tree/leaf", b"leaf")
        target.mkdir()
        with patch.object(ops, "_rename", side_effect=OSError(errno.EACCES, "fixture")):
            with self.assertRaises(OSError):
                ops.transfer(source, target, move=True, expected=ops.destination_state(target))
        self.assertTrue(target.is_dir())
        self.assertEqual((source / "leaf").read_bytes(), b"leaf")
        ops.transfer(source, target, move=True, expected=ops.destination_state(target))
        self.assertEqual((target / "leaf").read_bytes(), b"leaf")
        self.assertFalse(source.exists())

    def test_upload_existing_name_requires_warning_then_installs(self):
        target = self.file("report.txt", b"old")
        response = routes.upload(self.request, str(self.root), [Upload("report.txt")], "/files")
        self.assertIn("同名文件已存在", response.body.decode())
        self.assertIn("确认覆盖并上传", response.body.decode())
        result = routes.upload_confirm(self.request, self.token(response))
        self.assertEqual(result.status_code, 303)
        self.assertEqual(target.read_bytes(), b"uploaded")

    def test_upload_duplicate_names_rejected_without_staging(self):
        response = routes.upload(self.request, str(self.root), [Upload("same"), Upload("same")], "/files")
        self.assertIn("本批上传包含重名", response.body.decode())
        self.assertFalse((self.root / "uploads").exists())
        self.assertFalse((self.root / "same").exists())

    def test_upload_mixed_batch_waits_for_explicit_overwrite_without_installing_new_file(self):
        existing = self.file("existing", b"old")
        response = routes.upload(
            self.request, str(self.root), [Upload("existing"), Upload("new")], "/files"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("确认覆盖并上传", response.body.decode())
        self.assertEqual(existing.read_bytes(), b"old")
        self.assertFalse((self.root / "new").exists())
        result = routes.upload_confirm(self.request, self.token(response))
        self.assertEqual(result.status_code, 303)
        self.assertEqual(existing.read_bytes(), b"uploaded")
        self.assertEqual((self.root / "new").read_bytes(), b"uploaded")

    def test_upload_batch_all_conflicts_checked_before_first_install(self):
        self.file("existing", b"old")
        response = routes.upload(
            self.request, str(self.root), [Upload("existing"), Upload("late")], "/files"
        )
        self.file("late", b"external new file")
        result = routes.upload_confirm(self.request, self.token(response))
        self.assertEqual(result.status_code, 400)
        self.assertEqual((self.root / "existing").read_bytes(), b"old")
        self.assertEqual((self.root / "late").read_bytes(), b"external new file")
        self.assertEqual(len(list((self.root / "uploads").iterdir())), 2)

    def test_upload_new_batch_saves_directly_and_reports_every_file(self):
        response = routes.upload(
            self.request, str(self.root), [Upload("first"), Upload("second")], "/files?path=x"
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual((self.root / "first").read_bytes(), b"uploaded")
        self.assertEqual((self.root / "second").read_bytes(), b"uploaded")
        receipt = re.search(r"uploaded_batch=([0-9a-f]{24})", response.headers["location"])
        self.assertIsNotNone(receipt)
        self.assertLess(len(response.headers["location"]), 100)
        self.assertEqual(
            (self.root / "uploads" / ("receipt-" + receipt[1] + ".json")).read_text(),
            '["first", "second"]',
        )

    def test_upload_late_conflict_after_staging_is_rejected_without_overwrite(self):
        original = routes.staged_upload
        def add_late_conflict(upload_id):
            self.file("late", b"external new file")
            return original(upload_id)
        with patch.object(routes, "staged_upload", side_effect=add_late_conflict):
            response = routes.upload(self.request, str(self.root), [Upload("late")], "/files")
        self.assertEqual(response.status_code, 400)
        self.assertEqual((self.root / "late").read_bytes(), b"external new file")

    def test_upload_signed_directory_replaced_by_link_is_not_redirected(self):
        directory, other = self.root / "destination", self.root / "other"
        directory.mkdir(); other.mkdir()
        (directory / "new").write_bytes(b"old")
        response = routes.upload(self.request, str(directory), [Upload("new")], "/files")
        directory.rename(self.root / "previous-directory")
        directory.symlink_to(other, target_is_directory=True)
        result = routes.upload_confirm(self.request, self.token(response))
        self.assertEqual(result.status_code, 400)
        self.assertIn("目标目录已改变", result.body.decode())
        self.assertFalse((other / "new").exists())

    def test_traversal_only_ancestor_does_not_require_directory_read_permission(self):
        ancestor, directory = self.root / "traverse", self.root / "traverse/child"
        directory.mkdir(parents=True)
        source = directory / "source"
        source.write_bytes(b"fixture")
        target = directory / "target"
        ancestor.chmod(0o111)
        self.addCleanup(ancestor.chmod, 0o700)
        ops.transfer(source, target)
        self.assertEqual(target.read_bytes(), b"fixture")
        ops.transfer(source, directory / "moved", move=True)
        self.assertFalse(source.exists())
        staged = directory / "stage"
        staged.write_bytes(b"new")
        ops.install_staged(staged, target, expected=ops.destination_state(target))
        self.assertEqual(target.read_bytes(), b"new")
        (directory / "upload").write_bytes(b"old")
        response = routes.upload(self.request, str(directory), [Upload("upload")], "/files")
        self.assertEqual(routes.upload_confirm(self.request, self.token(response)).status_code, 303)
        response = routes.edit_confirm(self.request, str(target), "edited")
        self.assertIn("写入成功", routes.edit_write(self.request, self.token(response)).body.decode())

    def test_write_search_only_target_directory_supports_file_install(self):
        source, directory = self.file("source"), self.root / "write-only"
        directory.mkdir()
        directory.chmod(0o300)
        self.addCleanup(directory.chmod, 0o700)
        ops.transfer(source, directory / "copied")
        ops.transfer(source, directory / "moved", move=True)
        self.assertEqual((directory / "copied").read_bytes(), b"source")
        self.assertEqual((directory / "moved").read_bytes(), b"source")

    def test_unreadable_empty_directory_can_be_atomically_replaced_by_move(self):
        source, target = self.root / "tree", self.root / "target"
        self.file("tree/leaf", b"nested")
        target.mkdir(); target.chmod(0)
        self.addCleanup(target.chmod, 0o700)
        ops.transfer(source, target, move=True, expected=ops.destination_state(target))
        self.assertFalse(source.exists())
        self.assertEqual((target / "leaf").read_bytes(), b"nested")

    def test_nonempty_target_directory_move_fails_without_predelete(self):
        source, target = self.root / "tree", self.root / "target"
        self.file("tree/leaf", b"source")
        self.file("target/existing", b"old")
        with self.assertRaises(OSError):
            ops.transfer(source, target, move=True, expected=ops.destination_state(target))
        self.assertEqual((source / "leaf").read_bytes(), b"source")
        self.assertEqual((target / "existing").read_bytes(), b"old")

    def test_upload_repeated_token_cannot_overwrite_new_target(self):
        self.file("first", b"old")
        response = routes.upload(self.request, str(self.root), [Upload("first")], "/files")
        token = self.token(response)
        self.assertEqual(routes.upload_confirm(self.request, token).status_code, 303)
        result = routes.upload_confirm(self.request, token)
        self.assertEqual(result.status_code, 400)
        self.assertEqual((self.root / "first").read_bytes(), b"uploaded")

    def test_upload_duplicate_names_in_old_signed_payload_rejected(self):
        token = self.signer.dumps({"directory": str(self.root), "files": [
            {"name": "same", "upload_id": "one"}, {"name": "same", "upload_id": "two"}]})
        result = routes.upload_confirm(self.request, token)
        self.assertEqual(result.status_code, 400)
        self.assertFalse((self.root / "same").exists())

    def test_upload_storage_failure_reports_actual_partial_batch(self):
        original = routes.install_upload
        def fail_second(staged, target, **kwargs):
            if target.name == "second":
                raise OSError(errno.ENOSPC, "fixture")
            return original(staged, target, **kwargs)
        with patch.object(routes, "install_upload", side_effect=fail_second):
            result = routes.upload(
                self.request, str(self.root), [Upload("first"), Upload("second")], "/files"
            )
        self.assertEqual(result.status_code, 500)
        self.assertIn("本批已保存 1 个文件", result.body.decode())
        self.assertTrue((self.root / "first").exists())
        self.assertFalse((self.root / "second").exists())

    def test_upload_total_size_limit_is_readable_and_cleans_only_this_batch(self):
        first, second = Upload("first", b"123"), Upload("second", b"456")
        with patch.object(routes, "MAX_UPLOAD_BYTES", 5):
            response = routes.upload(self.request, str(self.root), [first, second], "/files")
        self.assertEqual(response.status_code, 400)
        self.assertIn("本批文件总大小超过 1000 MB 上限", response.body.decode())
        self.assertTrue(first.file.closed)
        self.assertTrue(second.file.closed)
        self.assertEqual(list((self.root / "uploads").iterdir()), [])

    def test_upload_staging_collision_preserves_unowned_file(self):
        staging = self.root / "uploads"
        staging.mkdir()
        occupied = staging / ("a" * 24)
        occupied.write_bytes(b"another request")
        uploaded = Upload("new")
        with patch.object(routes.secrets, "token_hex", return_value=occupied.name):
            response = routes.upload(self.request, str(self.root), [uploaded], "/files")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(occupied.read_bytes(), b"another request")
        self.assertFalse((self.root / "new").exists())
        self.assertTrue(uploaded.file.closed)

    def test_upload_audit_failure_reports_all_saved_without_inviting_retry(self):
        for conflict in (False, True):
            with self.subTest(conflict=conflict):
                name = "audit-confirmed" if conflict else "audit-direct"
                if conflict:
                    self.file(name, b"old")
                    pending = routes.upload(self.request, str(self.root), [Upload(name)], "/files")
                with patch.object(routes, "audit", side_effect=OSError(errno.ENOSPC, "fixture")):
                    result = (routes.upload_confirm(self.request, self.token(pending)) if conflict else
                              routes.upload(self.request, str(self.root), [Upload(name)], "/files"))
                self.assertEqual(result.status_code, 500)
                self.assertEqual((self.root / name).read_bytes(), b"uploaded")
                self.assertIn("均已保存", result.body.decode())
                self.assertIn("不要重复上传", result.body.decode())
                self.assertNotIn("其余未完成", result.body.decode())

    def test_upload_receipt_failure_does_not_block_direct_or_confirmed_save(self):
        for conflict in (False, True):
            with self.subTest(conflict=conflict):
                names = ["receipt-first-" + str(conflict), "receipt-second-" + str(conflict)]
                if conflict:
                    self.file(names[0], b"old")
                    pending = routes.upload(self.request, str(self.root), [Upload(n) for n in names], "/files")
                with patch.object(routes, "create_upload_receipt", side_effect=OSError(errno.ENOSPC, "fixture")):
                    result = (routes.upload_confirm(self.request, self.token(pending)) if conflict else
                              routes.upload(self.request, str(self.root), [Upload(n) for n in names], "/files"))
                self.assertEqual(result.status_code, 303)
                self.assertTrue(all((self.root / n).read_bytes() == b"uploaded" for n in names))
                from urllib.parse import parse_qs, urlsplit
                notice = parse_qs(urlsplit(result.headers["location"]).query)["uploaded"][0]
                self.assertIn("共 2 个文件", notice)
                self.assertIn("清单暂不可用", notice)

    def test_file_manager_renders_complete_batch_notice(self):
        result = routes.upload(self.request, str(self.root), [Upload("one"), Upload("two")], "/files")
        receipt = re.search(r"uploaded_batch=([0-9a-f]{24})", result.headers["location"])[1]
        with patch("shared.upload_receipts.UPLOADS", self.root / "uploads"):
            page = routes.files(self.request, str(self.root), uploaded_batch=receipt).body.decode()
        self.assertIn("附件已上传（共 2 个）", page)
        self.assertIn("<li>one</li>", page)
        self.assertIn("<li>two</li>", page)

    def test_upload_destination_directory_rejected_without_staging(self):
        (self.root / "folder").mkdir()
        response = routes.upload(self.request, str(self.root), [Upload("folder")], "/files")
        self.assertIn("不能用上传文件覆盖目录", response.body.decode())
        self.assertFalse((self.root / "uploads").exists())

    def test_changed_target_parent_is_not_written_through_pinned_old_directory(self):
        target = self.root / "parent/target"
        target.parent.mkdir()
        source = self.file("source")
        original = ops._copy_bytes
        def race(*args, **kwargs):
            original(*args, **kwargs)
            target.parent.rename(self.root / "previous-parent")
            target.parent.mkdir()
        with patch.object(ops, "_copy_bytes", side_effect=race):
            with self.assertRaises(ValueError):
                ops.transfer(source, target)
        self.assertFalse(target.exists())
        self.assertFalse((self.root / "previous-parent/target").exists())
        self.assert_no_temporary(self.root / "previous-parent")


if __name__ == "__main__":
    unittest.main()
