"""Standalone process/copy boundaries, independent of private deployment tools."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from shared import instance_primitives as primitives
from shared.runtime_release import RuntimeReleaseError


class InstancePrimitiveTests(unittest.TestCase):
    def test_current_process_identity_and_birth_are_real_and_stable(self):
        pid = os.getpid()
        identity = primitives.process_identity(pid)
        self.assertTrue(identity.isdigit())
        born = datetime.fromisoformat(primitives.process_started_at({
            'pid': pid, 'starttime': identity,
        }))
        self.assertLessEqual(born, datetime.now(timezone.utc))
        self.assertEqual(identity, primitives.process_identity(pid))

    def test_foreign_process_owner_is_rejected(self):
        with mock.patch.object(primitives.os, 'stat', return_value=SimpleNamespace(
                st_uid=os.getuid() + 1)):
            with self.assertRaises(RuntimeReleaseError):
                primitives.process_identity(os.getpid())

    def test_process_change_during_birth_read_is_rejected(self):
        with mock.patch.object(primitives, 'process_identity', side_effect=['10', '11']), \
                mock.patch.object(Path, 'read_text', return_value='btime 1\n'):
            with self.assertRaisesRegex(RuntimeReleaseError, 'changed while'):
                primitives.process_started_at({'pid': 123, 'starttime': '10'})

    def test_copy_preserves_closed_modes_and_rejects_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir(mode=0o700)
            (source / 'plain').write_text('synthetic data')
            (source / 'executable').write_text('synthetic executable bytes')
            (source / 'executable').chmod(0o700)
            target = root / 'copy'
            primitives.copy_tree(source, target)
            self.assertEqual((target / 'plain').read_text(), 'synthetic data')
            self.assertEqual((target / 'plain').stat().st_mode & 0o777, 0o400)
            self.assertEqual((target / 'executable').stat().st_mode & 0o777, 0o500)
            for kind in ('symlink', 'hardlink'):
                link = source / 'untrusted-link'
                if kind == 'symlink':
                    link.symlink_to(source / 'plain')
                else:
                    os.link(source / 'plain', link)
                try:
                    with self.subTest(kind=kind), self.assertRaises(ValueError):
                        primitives.copy_tree(source, root / ('bad-' + kind))
                finally:
                    link.unlink()


class PortableDefaultsTests(unittest.TestCase):
    def test_unconfigured_defaults_stay_with_owner_and_never_find_author_tools(self):
        with tempfile.TemporaryDirectory() as home:
            source = Path(__file__).resolve().parents[1]
            code = """import json
from shared import config as c
print(json.dumps({'home': str(c.HOME_DIR), 'state': str(c.STATE),
    'roots': [str(x) for x in c.FILE_ROOTS], 'account': c.ACCOUNT,
    'notification': [str(c.NTFY_TOPIC_FILE), str(c.TASK_NTFY_TOPIC_FILE),
                     str(c.TASK_NOTIFICATION_COMMAND)]}))
"""
            output = subprocess.check_output([sys.executable, '-B', '-c', code],
                cwd=source, env={'HOME': home, 'PATH': os.environ.get('PATH', ''),
                                 'PYTHONDONTWRITEBYTECODE': '1'}, text=True)
            value = json.loads(output)
            state = Path(home) / '.local/share/codex-web'
            self.assertEqual(value['home'], home)
            self.assertEqual(value['state'], str(state))
            self.assertEqual(value['roots'], [home])
            self.assertEqual(value['account'], 'owner')
            self.assertTrue(all(Path(p).is_relative_to(state) and
                                p.endswith('.unconfigured')
                                for p in value['notification']))
            self.assertFalse(state.exists())
