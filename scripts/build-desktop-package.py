#!/usr/bin/env python3
"""Build a local Windows/WSL source package; never publish automatically."""
import argparse
from pathlib import Path
import sys
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from shared.instance_package import export, content_files

parser=argparse.ArgumentParser()
parser.add_argument('--panel-exe',type=Path,required=True)
parser.add_argument('--destination',type=Path,required=True)
args=parser.parse_args()
source=Path(__file__).resolve().parents[1]
target=args.destination.absolute()
if target.is_relative_to(source) or source.is_relative_to(target):
    raise SystemExit('成品目录必须位于源码仓库之外。')
value=export(source,target,args.panel_exe)
archive=target.with_suffix('.zip')
with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_DEFLATED) as output:
    for name in content_files(target):
        output.write(target/name,arcname=target.name+'/'+name)
archive.chmod(0o600)
print('local_package='+str(archive))
print('source_commit='+value['source_commit'])
print('published=false')
