"""Build and verify a self-contained, offline runtime candidate bundle.

This module deliberately operates only on a clean Git checkout and a separately
created destination.  It never imports the web application or starts its
lifespan; source code is obtained with ``git archive HEAD``.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import uuid
from importlib import metadata
from typing import Any, Iterator

_MANIFEST = "runtime-bundle.json"
_FORMAT_VERSION = 1
_DISTUTILS_PTH = (
    "import os; var = 'SETUPTOOLS_USE_DISTUTILS'; enabled = "
    "os.environ.get(var, 'local') == 'local'; enabled and "
    "__import__('_distutils_hack').add_shim(); \n"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")


class BundleError(ValueError):
    """The requested candidate cannot safely be built or verified."""


class HostPythonMismatch(BundleError):
    """The canonical trusted host interpreter changed; old code cannot run."""


def _subprocess_env() -> dict[str, str]:
    """Do not allow caller Python configuration to influence probes or venv creation."""
    env = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONSAFEPATH"):
        env.pop(name, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run(args: list[str], *, cwd: Path | None = None, text: bool = True) -> str:
    try:
        completed = subprocess.run(
            args, cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=text, env=_subprocess_env(),
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "")
        raise BundleError(f"command failed: {' '.join(args)}: {detail}".strip()) from exc
    return completed.stdout if text else completed.stdout.decode("utf-8")


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _assert_no_link_components(path: Path, *, must_exist: bool) -> None:
    """Reject a supplied path whose existing components traverse symlinks."""
    absolute = _absolute(path)
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            item = os.lstat(current)
        except FileNotFoundError:
            if must_exist:
                raise BundleError(f"path does not exist: {absolute}")
            break
        if stat.S_ISLNK(item.st_mode):
            raise BundleError(f"symbolic-link path is not accepted: {current}")
    if must_exist:
        item = os.lstat(absolute)
        if not stat.S_ISDIR(item.st_mode):
            raise BundleError(f"not a directory: {absolute}")


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _prepare_paths(source: Path, destination: Path) -> tuple[Path, Path]:
    _assert_no_link_components(source, must_exist=True)
    source = _absolute(source)
    _assert_no_link_components(destination, must_exist=False)
    destination = _absolute(destination)
    if destination.exists() or destination.is_symlink():
        raise BundleError(f"destination already exists: {destination}")
    if not destination.parent.is_dir():
        raise BundleError(f"destination parent does not exist: {destination.parent}")
    if _inside(destination, source) or _inside(source, destination):
        raise BundleError("destination must be outside the source checkout")
    return source, destination


def _git_source(source: Path) -> str:
    top = Path(_run(["git", "rev-parse", "--show-toplevel"], cwd=source).strip())
    if _absolute(top) != source:
        raise BundleError("source must be the Git worktree root")
    commit = _run(["git", "rev-parse", "--verify", "HEAD"], cwd=source).strip()
    if not _COMMIT.fullmatch(commit):
        raise BundleError("source has no valid HEAD commit")
    if _run(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=source).strip():
        raise BundleError("source Git worktree is not completely clean")
    tree = _run(["git", "ls-tree", "-r", "-z", commit], cwd=source)
    if any(record.partition("\t")[0].startswith("160000 ") for record in tree.split("\0") if record):
        raise BundleError("source contains a submodule that git archive cannot include")
    return commit


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _entries(root: Path) -> Iterator[tuple[str, Path, os.stat_result]]:
    """Enumerate explicitly: os.walk can silently swallow is_dir() errors.

    Type classification has one authoritative source (non-following stat).
    Every directory open, scandir iteration and stat failure is fatal.  Opening
    directories with O_NOFOLLOW also prevents a queued directory becoming a
    symlink before it is visited.
    """
    pending = [root]
    while pending:
        directory = pending.pop()
        fd = None
        try:
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            with os.scandir(fd) as scan:
                children = sorted(scan, key=lambda entry: entry.name)
            descend = []
            for child in children:
                path = directory / child.name
                item = os.stat(child.name, dir_fd=fd, follow_symlinks=False)
                yield path.relative_to(root).as_posix(), path, item
                if stat.S_ISDIR(item.st_mode):
                    descend.append(path)
            pending.extend(reversed(descend))
        except OSError as error:
            raise BundleError("cannot enumerate the complete bundle or dependency tree") from error
        finally:
            if fd is not None:
                os.close(fd)


def _validate_dependency_tree(site_packages: Path) -> None:
    if not site_packages.is_dir() or site_packages.is_symlink():
        raise BundleError("source virtualenv has no direct site-packages directory")
    for relative, path, item in _entries(site_packages):
        if stat.S_ISLNK(item.st_mode):
            raise BundleError(f"dependency tree contains a symbolic link: {relative}")
        # site.py imports these names automatically from the site-packages root.
        # Reject modules, bytecode, extension modules, and same-named packages before
        # the normal isolated probe can execute any dependency-controlled code.
        top_level = relative.split("/", 1)[0]
        if top_level in {"sitecustomize", "usercustomize"} or any(
            top_level.startswith(f"{hook}.") for hook in ("sitecustomize", "usercustomize")
        ):
            raise BundleError(f"automatic site hook is not allowed: {relative}")
        if stat.S_ISDIR(item.st_mode):
            continue
        if not stat.S_ISREG(item.st_mode):
            raise BundleError(f"dependency tree contains a special entry: {relative}")
        name = path.name
        if name.endswith(".egg-link"):
            raise BundleError(f"editable egg-link is not allowed: {relative}")
        if name.endswith(".pth"):
            if name != "distutils-precedence.pth" or path.read_text(encoding="utf-8") != _DISTUTILS_PTH:
                raise BundleError(f"unrecognised .pth file is not allowed: {relative}")
        if name == "direct_url.json" and path.parent.name.endswith(".dist-info"):
            try:
                direct_url = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise BundleError(f"invalid direct_url metadata: {relative}") from exc
            if direct_url.get("dir_info", {}).get("editable") is True:
                raise BundleError(f"editable direct_url is not allowed: {relative}")


def _entry_snapshot(item: os.stat_result, path: Path | None = None) -> tuple[Any, ...]:
    common = (item.st_dev, item.st_ino, item.st_mode, item.st_mtime_ns, item.st_ctime_ns)
    if path is None:
        return ("d", *common)
    return ("f", *common, item.st_size, _sha256(path))


def _dependency_snapshot(site_packages: Path) -> dict[str, tuple[Any, ...]]:
    _validate_dependency_tree(site_packages)
    snapshot: dict[str, tuple[Any, ...]] = {".": _entry_snapshot(os.lstat(site_packages))}
    for relative, path, item in _entries(site_packages):
        snapshot[relative] = _entry_snapshot(item, None if stat.S_ISDIR(item.st_mode) else path)
    return snapshot


def _copy_matches_source_snapshot(snapshot: dict[str, tuple[Any, ...]], copied: Path) -> None:
    """Check copied bytes/modes against the pre-copy source, not merely post-copy source."""
    copied_snapshot = _dependency_snapshot(copied)
    if set(snapshot) != set(copied_snapshot):
        raise BundleError("copied dependency tree has missing or extra entries")
    for relative, source_entry in snapshot.items():
        target_entry = copied_snapshot[relative]
        # Device/inode/timestamps necessarily differ.  Kind, mode, size, and hash must not.
        if source_entry[0] != target_entry[0] or source_entry[3] != target_entry[3]:
            raise BundleError(f"copied dependency metadata differs: {relative}")
        if source_entry[0] == "f" and source_entry[-2:] != target_entry[-2:]:
            raise BundleError(f"copied dependency bytes differ: {relative}")


def _venv_details(source: Path) -> dict[str, Any]:
    venv = source / ".venv"
    _assert_no_link_components(venv, must_exist=True)
    python = venv / "bin" / "python"
    if not python.exists():
        raise BundleError("source virtualenv has no .venv/bin/python")
    config = venv / "pyvenv.cfg"
    try:
        config_text = config.read_text(encoding="utf-8")
    except OSError as exc:
        raise BundleError("source virtualenv has no readable pyvenv.cfg") from exc
    config_snapshot = _entry_snapshot(os.stat(config), config)
    settings: dict[str, str] = {}
    for line in config_text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            settings[key.strip().lower()] = value.strip().lower()
    if settings.get("include-system-site-packages") != "false":
        raise BundleError("source virtualenv must not include system site-packages")

    # -S avoids site.py, so no .pth or editable hook can run before inspection.
    safe_probe = (
        "import json,platform,sys; print(json.dumps({'base_executable':"
        "getattr(sys,'_base_executable',sys.executable),'version':platform.python_version(),"
        "'version_info':list(sys.version_info[:3]),'architecture':platform.machine()}))"
    )
    try:
        details = json.loads(_run([os.fspath(python), "-B", "-I", "-S", "-c", safe_probe]).strip())
    except (json.JSONDecodeError, BundleError) as exc:
        raise BundleError("cannot safely inspect source virtualenv") from exc
    base = Path(details["base_executable"])
    if not base.is_absolute() or not base.exists() or not base.is_file():
        raise BundleError("source virtualenv has no usable base interpreter")
    base_snapshot = _entry_snapshot(os.stat(base), base)
    version = details["version"]
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise BundleError("source virtualenv Python version is invalid")
    purelib = venv / "lib" / f"python{details['version_info'][0]}.{details['version_info'][1]}" / "site-packages"
    _assert_no_link_components(purelib, must_exist=True)
    if not _inside(purelib.resolve(), venv.resolve()):
        raise BundleError("source virtualenv site-packages escapes .venv")
    # Validate all files before any normal startup can process an approved .pth.
    _validate_dependency_tree(purelib)

    normal_probe = (
        "import json,platform,sys,sysconfig; print(json.dumps({'prefix':sys.prefix,"
        "'base_prefix':sys.base_prefix,'version':platform.python_version(),"
        "'version_info':list(sys.version_info[:3]),'architecture':platform.machine(),"
        "'purelib':sysconfig.get_paths()['purelib']}))"
    )
    try:
        running = json.loads(_run([os.fspath(python), "-B", "-I", "-c", normal_probe]).strip())
        base_info = json.loads(_run([os.fspath(base), "-B", "-I", "-S", "-c", normal_probe]).strip())
    except (json.JSONDecodeError, BundleError) as exc:
        raise BundleError("cannot inspect validated source virtualenv") from exc
    if (Path(running["prefix"]).resolve() != venv.resolve() or running["prefix"] == running["base_prefix"]
            or Path(running["purelib"]) != purelib or running["version_info"] != details["version_info"]
            or base_info["version_info"][:2] != details["version_info"][:2]
            or base_info["prefix"] != base_info["base_prefix"]):
        raise BundleError("source .venv is not an isolated compatible virtualenv")
    return {
        "venv": venv, "python": python, "base": base, "version": version,
        "version_info": details["version_info"], "architecture": details["architecture"],
        "purelib": purelib, "config_snapshot": config_snapshot,
        "base_snapshot": base_snapshot,
    }


def _distributions(site_packages: Path) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    try:
        found = metadata.distributions(path=[os.fspath(site_packages)])
        for distribution in found:
            name = distribution.metadata.get("Name")
            version = distribution.version
            if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
                raise BundleError("installed distribution is missing name or exact version")
            result.append({"name": name, "version": version})
    except Exception as exc:
        if isinstance(exc, BundleError):
            raise
        raise BundleError("cannot enumerate installed distributions") from exc
    result.sort(key=lambda entry: (entry["name"].lower(), entry["version"]))
    if len({(entry["name"].lower(), entry["version"]) for entry in result}) != len(result):
        raise BundleError("duplicate installed distribution metadata")
    return result


def _safe_tar_name(name: str) -> str:
    if (not isinstance(name, str) or not name or "\\" in name or name.startswith("/")
            or any(part in ("", ".", "..") for part in name.split("/"))):
        raise BundleError(f"unsafe archive member: {name!r}")
    candidate = PurePosixPath(name)
    if candidate.as_posix() != name:
        raise BundleError(f"non-canonical archive member: {name!r}")
    return name


def _assert_archive_complete(source: Path, app: Path, commit: str) -> None:
    """git archive may honour export-ignore; a runtime must contain every tracked file."""
    listing = _run(["git", "ls-tree", "-r", "-z", "--name-only", commit], cwd=source)
    expected = {name for name in listing.split("\0") if name}
    actual = {relative for relative, _path, item in _entries(app) if stat.S_ISREG(item.st_mode)}
    if expected != actual:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise BundleError(f"git archive did not reproduce all tracked source files (missing={missing[:3]}, extra={extra[:3]})")


def _archive_source(source: Path, app: Path, commit: str, budget=None) -> None:
    process = subprocess.Popen(
        ["git", "archive", "--format=tar", commit], cwd=source, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, env=_subprocess_env(),
    )
    assert process.stdout is not None
    seen: set[str] = set()
    failure: BaseException | None = None
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            for member in archive:
                name = _safe_tar_name(member.name)
                if name in seen:
                    raise BundleError(f"duplicate archive member: {name}")
                seen.add(name)
                target = app / name
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=False)
                    continue
                if not member.isfile() or member.issym() or member.islnk() or member.isdev() or member.isfifo():
                    raise BundleError(f"unsafe archive member type: {name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                source_file = archive.extractfile(member)
                if source_file is None:
                    raise BundleError(f"unreadable archive member: {name}")
                if budget is not None:budget.take(member.size)
                with source_file, target.open("xb") as output:
                    if budget is None:shutil.copyfileobj(source_file, output)
                    else:
                        remaining=member.size
                        while remaining:
                            budget.guard(min(1024*1024,remaining));part=source_file.read(min(1024*1024,remaining))
                            if not part:raise BundleError('budgeted archive truncated')
                            output.write(part);remaining-=len(part)
                os.chmod(target, stat.S_IMODE(member.mode))
    except BaseException as exc:
        failure = exc
        raise
    finally:
        process.stdout.close()
        if failure is not None and process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    if process.returncode != 0:
        raise BundleError("git archive failed")


def _copy_dependencies(source: Path, destination: Path, *, budget=None, snapshot=None) -> None:
    """Copy without links, preserving source directory and regular-file modes."""
    directory_modes: list[tuple[Path, int]] = [(destination, stat.S_IMODE(os.lstat(source).st_mode))]
    os.chmod(destination, 0o700)  # keep writable until descendants are copied
    for relative, path, item in _entries(source):
        target = destination / relative
        if stat.S_ISDIR(item.st_mode):
            target.mkdir(parents=True, exist_ok=False, mode=0o700)
            directory_modes.append((target, stat.S_IMODE(item.st_mode)))
        elif stat.S_ISREG(item.st_mode):
            target.parent.mkdir(parents=True, exist_ok=True)
            if budget is None:
                with path.open("rb") as input_file, target.open("xb") as output_file:shutil.copyfileobj(input_file, output_file)
            else:
                size=snapshot[relative][-2]
                if item.st_size!=size:raise BundleError('budgeted dependency changed size')
                budget.copy(path,target,size)
            os.chmod(target, stat.S_IMODE(item.st_mode))
        else:  # _validate_dependency_tree made this unreachable, retain fail-closed behavior.
            raise BundleError(f"unsafe dependency entry: {relative}")
    for directory, mode in reversed(directory_modes):
        os.chmod(directory, mode)


def _new_venv(base: Path, staging: Path, destination: Path, version_info: list[int]) -> Path:
    bundle_venv = staging / ".venv"
    _run([os.fspath(base), "-B", "-I", "-S", "-m", "venv", "--without-pip", "--copies", os.fspath(bundle_venv)])
    # lib64 is only a convenience alias; remove it so the entire runtime
    # is a regular-file tree accepted by the existing lifecycle recorder.
    lib64 = bundle_venv / "lib64"
    if lib64.is_symlink() and os.readlink(lib64) == "lib":
        lib64.unlink()
    python = bundle_venv / "bin" / "python"
    script = "import json,sys; print(json.dumps(list(sys.version_info[:2])))"
    if json.loads(_run([os.fspath(python), "-B", "-I", "-S", "-c", script]).strip()) != version_info[:2]:
        raise BundleError("new virtualenv Python version is incompatible")
    script = "import sysconfig; print(sysconfig.get_paths()['purelib'])"
    purelib = Path(_run([os.fspath(python), "-B", "-I", "-c", script]).strip())
    if not _inside(purelib.resolve(), bundle_venv.resolve()):
        raise BundleError("new virtualenv site-packages escapes bundle")
    # Activation scripts embed the temporary staging location and are not runtime inputs.
    for child in (bundle_venv / "bin").iterdir():
        if child.name not in {"python", "python3", f"python{version_info[0]}.{version_info[1]}"}:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    config = bundle_venv / "pyvenv.cfg"
    config.write_text(config.read_text(encoding="utf-8").replace(os.fspath(staging), os.fspath(destination)), encoding="utf-8")
    return purelib


def _allowed_venv_link(relative: str, target: str, base: Path, version_info: list[int]) -> bool:
    path = PurePosixPath(relative)
    if relative == ".venv/lib64":
        return target == "lib"
    if path.parent == PurePosixPath(".venv/bin") and path.name in {"python", "python3", f"python{version_info[0]}.{version_info[1]}"}:
        # venv may use an absolute target or a relative link; both must resolve to this base binary.
        return True
    return False


def _collect_manifest(staging: Path, *, commit: str, venv: dict[str, Any], distributions: list[dict[str, str]]) -> dict[str, Any]:
    files: dict[str, dict[str, Any]] = {}
    links: dict[str, str] = {}
    directories: list[str] = []
    base_resolved = venv["base"].resolve()
    for relative, path, item in _entries(staging):
        if relative == _MANIFEST:
            continue
        if stat.S_ISDIR(item.st_mode):
            directories.append(relative)
            continue
        if stat.S_ISLNK(item.st_mode):
            target = os.readlink(path)
            if not _allowed_venv_link(relative, target, venv["base"], venv["version_info"]):
                raise BundleError(f"unexpected bundle symbolic link: {relative}")
            if path.resolve() != base_resolved and not (relative == ".venv/lib64" and path.resolve() == (staging / ".venv/lib").resolve()):
                raise BundleError(f"bundle symbolic link escapes allowed target: {relative}")
            links[relative] = target
            continue
        if not stat.S_ISREG(item.st_mode):
            raise BundleError(f"bundle contains a special entry: {relative}")
        files[relative] = {
            "sha256": _sha256(path),
            "size": item.st_size,
            "executable": bool(item.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)),
        }
    return {
        "format_version": _FORMAT_VERSION,
        "commit": commit,
        "python": {
            "implementation": "CPython",
            "version": venv["version"],
            "architecture": venv["architecture"],
            "base_interpreter": {"path": os.fspath(venv["base"]), "sha256": _sha256(venv["base"])},
            "depends_on_host_base_python": True,
        },
        "distributions": distributions,
        "runtime": {
            "command": [".venv/bin/python", "-m", "uvicorn"],
            "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
        },
        "directories": sorted(directories),
        "files": dict(sorted(files.items())),
        "symlinks": dict(sorted(links.items())),
    }


def _lock_down(bundle: Path, manifest: dict[str, Any]) -> None:
    for relative, path, item in list(_entries(bundle)):
        if stat.S_ISDIR(item.st_mode):
            os.chmod(path, 0o500)
        elif stat.S_ISREG(item.st_mode):
            executable = manifest["files"].get(relative, {}).get("executable", False)
            os.chmod(path, 0o500 if executable else 0o400)
    os.chmod(bundle, 0o500)


def _remove_owned_tree(root: Path) -> None:
    """Remove only a staging tree created by this invocation, despite final modes."""
    if not root.exists() and not root.is_symlink():
        return
    for dirname, directory_names, file_names in os.walk(root, topdown=False, followlinks=False):
        for name in [*directory_names, *file_names]:
            path = Path(dirname) / name
            try:
                if not path.is_symlink():
                    os.chmod(path, 0o700)
            except OSError:
                pass
        try:
            os.chmod(dirname, 0o700)
        except OSError:
            pass
    shutil.rmtree(root, ignore_errors=True)


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically publish only if the caller's absent destination remains absent."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
    except AttributeError as exc:
        raise BundleError("renameat2(RENAME_NOREPLACE) is required for safe publication") from exc
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    at_fdcwd = -100
    rename_noreplace = 1
    result = renameat2(at_fdcwd, os.fsencode(source), at_fdcwd, os.fsencode(destination), rename_noreplace)
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise BundleError("destination appeared during build; refusing to overwrite it")
    raise BundleError(f"atomic candidate publication failed: {os.strerror(error)}")


def build_bundle(source: Path, destination: Path, *, dependency_source: Path | None = None, budget=None) -> dict[str, Any]:
    """Build a verified candidate at an absent destination and return its manifest."""
    source, destination = _prepare_paths(Path(source), Path(destination))
    commit = _git_source(source)
    dependency_source = source if dependency_source is None else _absolute(Path(dependency_source))
    _assert_no_link_components(dependency_source, must_exist=True)
    venv = _venv_details(dependency_source)
    before = _dependency_snapshot(venv["purelib"])
    distributions = _distributions(venv["purelib"])
    if budget is not None:budget.reserve_bundle(source,commit,venv,before)
    staging = destination.parent / f".{destination.name}.building-{uuid.uuid4().hex}"
    if staging.exists() or staging.is_symlink():
        raise BundleError("unexpected staging path already exists")
    try:
        staging.mkdir(mode=0o700)
        app = staging / "app"
        app.mkdir(mode=0o700)
        _archive_source(source, app, commit, **({"budget":budget} if budget is not None else {}))
        _assert_archive_complete(source, app, commit)
        if budget is not None:budget.take(3*venv["base"].stat().st_size+1024*1024)
        destination_purelib = _new_venv(venv["base"], staging, destination, venv["version_info"])
        if budget is not None:budget.refresh()
        _copy_dependencies(venv["purelib"], destination_purelib, **({"budget":budget,"snapshot":before} if budget is not None else {}))
        _copy_matches_source_snapshot(before, destination_purelib)
        if before != _dependency_snapshot(venv["purelib"]):
            raise BundleError("source dependency tree changed during build")
        if (venv["config_snapshot"] != _entry_snapshot(os.stat(venv["venv"] / "pyvenv.cfg"), venv["venv"] / "pyvenv.cfg")
                or venv["base_snapshot"] != _entry_snapshot(os.stat(venv["base"]), venv["base"])):
            raise BundleError("source virtualenv configuration or base Python changed during build")
        if _git_source(source) != commit:
            raise BundleError("source Git identity changed during build")
        manifest = _collect_manifest(staging, commit=commit, venv=venv, distributions=distributions)
        manifest_path = staging / _MANIFEST
        raw=(json.dumps(manifest,sort_keys=True,indent=2)+"\n").encode()
        if budget is None:manifest_path.write_bytes(raw)
        else:budget.write(manifest_path,raw)
        if budget is not None:budget.refresh()
        _lock_down(staging, manifest)
        # Verify before publishing the staging tree.  The manifest itself remains writable only
        # long enough for this call; a successful destination is always read-only afterwards.
        verify_bundle(staging)
        _rename_noreplace(staging, destination)
        return manifest
    except BaseException:
        if staging.exists() or staging.is_symlink():
            _remove_owned_tree(staging)
        raise


def _relative_key(value: Any) -> str:
    if not isinstance(value, str):
        raise BundleError("manifest path is not a string")
    return _safe_tar_name(value)


def _strict_manifest(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {"format_version", "commit", "python", "distributions", "runtime", "directories", "files", "symlinks"}:
        raise BundleError("manifest has unknown, missing, or invalid top-level fields")
    if type(raw["format_version"]) is not int or raw["format_version"] != _FORMAT_VERSION or not isinstance(raw["commit"], str) or not _COMMIT.fullmatch(raw["commit"]):
        raise BundleError("manifest format or commit is invalid")
    python = raw["python"]
    if not isinstance(python, dict) or set(python) != {"implementation", "version", "architecture", "base_interpreter", "depends_on_host_base_python"}:
        raise BundleError("manifest Python section is invalid")
    base = python["base_interpreter"]
    if (python["implementation"] != "CPython" or not isinstance(python["version"], str)
            or not re.fullmatch(r"\d+\.\d+\.\d+", python["version"]) or not isinstance(python["architecture"], str)
            or not python["architecture"] or python["depends_on_host_base_python"] is not True
            or not isinstance(base, dict) or set(base) != {"path", "sha256"}
            or not isinstance(base["path"], str) or not os.path.isabs(base["path"])
            or not isinstance(base["sha256"], str) or not _SHA256.fullmatch(base["sha256"])):
        raise BundleError("manifest base Python declaration is invalid")
    distributions = raw["distributions"]
    if not isinstance(distributions, list) or any(not isinstance(item, dict) or set(item) != {"name", "version"} or not all(isinstance(item[field], str) and item[field] for field in ("name", "version")) for item in distributions):
        raise BundleError("manifest distributions are invalid")
    if (distributions != sorted(distributions, key=lambda item: (item["name"].lower(), item["version"]))
            or len({(item["name"].lower(), item["version"]) for item in distributions}) != len(distributions)):
        raise BundleError("manifest distributions are not ordered or unique")
    runtime = raw["runtime"]
    if runtime != {"command": [".venv/bin/python", "-m", "uvicorn"], "environment": {"PYTHONDONTWRITEBYTECODE": "1"}}:
        raise BundleError("manifest runtime command is invalid")
    directories = raw["directories"]
    files = raw["files"]
    links = raw["symlinks"]
    if not isinstance(directories, list) or not isinstance(files, dict) or not isinstance(links, dict):
        raise BundleError("manifest entry lists are invalid")
    clean_directories = [_relative_key(path) for path in directories]
    if directories != sorted(directories) or len(clean_directories) != len(set(clean_directories)):
        raise BundleError("manifest directories are invalid or unordered")
    clean_files: dict[str, Any] = {}
    for path, entry in files.items():
        path = _relative_key(path)
        if path == _MANIFEST or not isinstance(entry, dict) or set(entry) != {"sha256", "size", "executable"} or not isinstance(entry["sha256"], str) or not _SHA256.fullmatch(entry["sha256"]) or type(entry["size"]) is not int or entry["size"] < 0 or type(entry["executable"]) is not bool:
            raise BundleError("manifest file entry is invalid")
        if path in clean_files or path in clean_directories:
            raise BundleError("manifest file path duplicates another entry")
        clean_files[path] = entry
    clean_links: dict[str, str] = {}
    for path, target in links.items():
        path = _relative_key(path)
        if path in clean_files or path in clean_directories or path in clean_links or not isinstance(target, str) or not target:
            raise BundleError("manifest symbolic-link entry is invalid")
        clean_links[path] = target
    if list(files) != sorted(files) or list(links) != sorted(links):
        raise BundleError("manifest entries are not ordered")
    raw["directories"] = clean_directories
    raw["files"] = clean_files
    raw["symlinks"] = clean_links
    return raw


def verify_bundle(bundle: Path, *, trusted_base_interpreter: Path | None = None) -> dict[str, Any]:
    """Reject missing, additional, tampered, or unsafe candidate bundle entries."""
    _assert_no_link_components(Path(bundle), must_exist=True)
    bundle = _absolute(Path(bundle))
    manifest_path = bundle / _MANIFEST
    try:
        manifest_item = os.lstat(manifest_path)
        if not stat.S_ISREG(manifest_item.st_mode):
            raise BundleError("manifest is not a regular file")
        def reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise BundleError(f"manifest has duplicate JSON key: {key}")
                result[key] = value
            return result
        raw = json.loads(manifest_path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_pairs)
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleError("manifest cannot be read") from exc
    manifest = _strict_manifest(raw)
    if stat.S_IMODE(os.lstat(bundle).st_mode) != 0o500 or stat.S_IMODE(manifest_item.st_mode) != 0o400:
        raise BundleError("bundle permissions are not locked down")
    base = Path(manifest["python"]["base_interpreter"]["path"])
    if trusted_base_interpreter is not None:
        # A selected backup is data, not an authority to choose the command
        # used for its own verification. Check at the SAME parse/use boundary.
        trusted = Path(trusted_base_interpreter).resolve(strict=True)
        if base != trusted:
            raise BundleError('backup does not use the trusted host base Python')
        if manifest['python']['base_interpreter']['sha256'] != _sha256(trusted):
            raise HostPythonMismatch('trusted host Python changed; rebuild software before use')
        base = trusted
    if not base.exists() or not base.is_file() or _sha256(base) != manifest["python"]["base_interpreter"]["sha256"]:
        raise BundleError("host base Python is unavailable or changed")
    inspect = "import json,platform,sys; print(json.dumps({'prefix':sys.prefix,'base_prefix':sys.base_prefix,'version':platform.python_version(),'architecture':platform.machine()}))"
    try:
        base_details = json.loads(_run([os.fspath(base), "-B", "-I", "-S", "-c", inspect]).strip())
    except (BundleError, json.JSONDecodeError) as exc:
        raise BundleError("host base Python cannot be inspected") from exc
    if base_details["prefix"] != base_details["base_prefix"] or base_details["version"] != manifest["python"]["version"]:
        raise BundleError("host base Python differs from bundle")
    if platform.machine() != manifest["python"]["architecture"] or base_details["architecture"] != manifest["python"]["architecture"]:
        raise BundleError("host architecture differs from bundle")
    version_info = [int(part) for part in manifest["python"]["version"].split(".")]
    observed_directories: set[str] = set()
    observed_files: set[str] = set()
    observed_links: set[str] = set()
    for relative, path, item in _entries(bundle):
        if relative == _MANIFEST:
            continue
        if stat.S_ISDIR(item.st_mode):
            if stat.S_IMODE(item.st_mode) != 0o500:
                raise BundleError(f"directory permissions are not locked down: {relative}")
            observed_directories.add(relative)
            continue
        if stat.S_ISLNK(item.st_mode):
            target = os.readlink(path)
            if manifest["symlinks"].get(relative) != target or not _allowed_venv_link(relative, target, base, version_info):
                raise BundleError(f"unexpected symbolic link: {relative}")
            resolved = path.resolve()
            if not ((relative == ".venv/lib64" and resolved == (bundle / ".venv/lib").resolve()) or resolved == base.resolve()):
                raise BundleError(f"symbolic link reaches outside permitted runtime: {relative}")
            observed_links.add(relative)
            continue
        if not stat.S_ISREG(item.st_mode):
            raise BundleError(f"special bundle entry: {relative}")
        entry = manifest["files"].get(relative)
        if entry is None or item.st_size != entry["size"] or _sha256(path) != entry["sha256"]:
            raise BundleError(f"bundle file integrity failure: {relative}")
        if stat.S_IMODE(item.st_mode) != (0o500 if entry["executable"] else 0o400):
            raise BundleError(f"file permissions are not locked down: {relative}")
        observed_files.add(relative)
    if (observed_directories != set(manifest["directories"]) or observed_files != set(manifest["files"])
            or observed_links != set(manifest["symlinks"])):
        raise BundleError("bundle has missing or extra entries")
    return manifest


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    build = actions.add_parser("build")
    build.add_argument("--source", required=True, type=Path)
    build.add_argument("--destination", required=True, type=Path)
    verify = actions.add_parser("verify")
    verify.add_argument("--bundle", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        manifest = build_bundle(arguments.source, arguments.destination) if arguments.action == "build" else verify_bundle(arguments.bundle)
    except BundleError as exc:
        parser.exit(2, f"runtime bundle error: {exc}\n")
    print(json.dumps(manifest, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
