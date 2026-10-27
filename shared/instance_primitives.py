"""Small local process and tree primitives shared by independent instances."""
from __future__ import annotations
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import stat
from shared.runtime_bundle import _entries
from shared.runtime_release import RuntimeReleaseError, _no_links

def process_identity(pid: int) -> str:
    if pid <= 0: raise RuntimeReleaseError("service does not have a live MainPID")
    value = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
    if len(value) < 20 or os.stat(f"/proc/{pid}").st_uid != os.getuid(): raise RuntimeReleaseError("service process identity is not owned by caller")
    return value[19]

def process_started_at(identity: dict) -> str:
    """Absolute process-birth floor, pinned around boot metadata reads."""
    pid = identity["pid"]
    if process_identity(pid) != identity["starttime"]: raise RuntimeReleaseError("Web process changed before drain")
    boot_seconds = next(int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines() if line.startswith("btime "))
    seconds = boot_seconds + int(identity["starttime"]) / os.sysconf("SC_CLK_TCK")
    if process_identity(pid) != identity["starttime"]: raise RuntimeReleaseError("Web process changed while deriving drain floor")
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()

def copy_tree(source: Path, target: Path, budget=None) -> None:
    """Copy a verified regular-file tree without links or implicit activation."""
    _no_links(source); target.mkdir(mode=0o700); modes = [(target, stat.S_IMODE(source.stat().st_mode))]
    for relative, path, item in _entries(source):
        output = target / relative
        if stat.S_ISDIR(item.st_mode): output.mkdir(mode=0o700); modes.append((output, stat.S_IMODE(item.st_mode)))
        elif stat.S_ISREG(item.st_mode) and item.st_nlink == 1:
            budget.copy(path, output, item.st_size) if budget is not None else shutil.copyfile(path, output)
            output.chmod(0o500 if item.st_mode & 0o111 else 0o400)
        else: raise ValueError("unsafe runtime recovery input")
    for path, mode in reversed(modes): path.chmod(mode)
