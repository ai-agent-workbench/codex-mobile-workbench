import os
from pathlib import Path
import subprocess
import tempfile
import shutil
import sys
import unittest

from shared import instance_package as package
from shared import instance_runtime as runtime
from shared.instance_config import InstanceError


class SourcePackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='source-package-',dir=Path.home())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir(mode=0o700)
        (self.source / 'script.sh').write_text('#!/bin/sh\ntrue\n')
        (self.source / 'script.sh').chmod(0o700)
        (self.source / 'app.py').write_text('# fixture\n')
        env = {**os.environ,'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null'}
        for args in (['init','-q'],['add','.'],['-c','user.name=Fixture','-c','user.email=fixture@example.invalid',
                   '-c','commit.gpgsign=false','commit','-qm','fixture']):
            subprocess.run(['git',*args],cwd=self.source,env=env,check=True,capture_output=True)
        self.exe = self.root / 'panel.exe'
        self.exe.write_bytes(b'MZinert fixture, never executed')
        self.target = self.root / 'package'

    def test_package_rebuild_has_no_upstream_git_history_and_restores_exec_bit(self):
        value = package.export(self.source,self.target,self.exe)
        self.assertFalse((self.target / '.git').exists())
        self.assertEqual(package.verify(self.target), value)
        # A Windows archive extraction can lose POSIX modes without losing content.
        (self.target / 'script.sh').chmod(0o600)
        destination = self.root / 'materialized'
        self.assertEqual(package.materialize(self.target,destination),value['source_commit'])
        self.assertTrue((destination / 'script.sh').stat().st_mode & 0o100)
        self.assertFalse((destination / package.PANEL).exists())
        snapshot = self.root / 'snapshot'
        origin,commit = runtime.snapshot_source(self.target,snapshot)
        self.assertEqual(origin,value['source_commit'])
        self.assertEqual(len(commit),40)
        self.assertEqual(subprocess.check_output(['git','remote'],cwd=snapshot), b'')
        self.assertEqual(int(subprocess.check_output(['git','rev-list','--count','HEAD'],cwd=snapshot)),1)

    def test_plain_python_manager_inspection_does_not_pollute_verified_package(self):
        actual = Path(__file__).resolve().parents[1]
        for name in ('scripts/manage-instance.py','shared/__init__.py',
                     'shared/instance_config.py','shared/instance_manager.py'):
            path = self.source / name
            path.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(actual/name,path)
        subprocess.run(['git','add','.'],cwd=self.source,check=True,capture_output=True)
        subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture@example.invalid',
            '-c','commit.gpgsign=false','commit','-qm','manager fixture'],cwd=self.source,check=True,capture_output=True)
        package.export(self.source,self.target,self.exe)
        env=os.environ.copy()
        for key in ('PYTHONDONTWRITEBYTECODE','PYTHONPATH'):
            env.pop(key,None)
        result=subprocess.run([sys.executable,str(self.target/'scripts/manage-instance.py'),
            '--config',str(self.root/'missing/instance.json'),'inspect'],env=env,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        package.verify(self.target)
        self.assertFalse(list(self.target.rglob('__pycache__')))

    def test_corrupt_or_extra_file_rejects_before_materialization(self):
        package.export(self.source,self.target,self.exe)
        (self.target / 'app.py').write_text('# corrupted\n')
        destination=self.root / 'must-not-create'
        with self.assertRaises(InstanceError):
            package.materialize(self.target,destination)
        self.assertFalse(destination.exists())

    def test_symlink_and_existing_package_are_not_adopted(self):
        package.export(self.source,self.target,self.exe)
        with self.assertRaises(InstanceError):
            package.export(self.source,self.target,self.exe)
        (self.target / 'extra').symlink_to(self.exe)
        with self.assertRaises(InstanceError):
            package.verify(self.target)
