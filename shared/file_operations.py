"""Browser-only safe writes: pinned directories, explicit conflicts, atomic install.

These guardrails do not restrict native Full Access conversations. Confirmation
fingerprints detect stale targets; they are not a lock against another OS writer.
"""
import ctypes
import errno
import os
import secrets
import shutil
import stat
from contextlib import contextmanager
from pathlib import Path

from shared.file_utils import allowed_destination, allowed_path


class MoveCleanupPending(ValueError):
    """The destination is committed; source cleanup did not finish."""


def _identity(info):
    return [info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns]


@contextmanager
def _directory(path):
    """Pin every directory component without following replacement links."""
    fd = os.open("/", os.O_PATH | os.O_DIRECTORY)
    try:
        for part in Path(path).parts[1:]:
            child = os.open(part, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _state(fd, name):
    try:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise ValueError("目标不是普通文件或目录，不能覆盖链接或特殊文件。")
    return _identity(info)


def destination_state(path):
    target = allowed_destination(path)
    with _directory(target.parent) as fd:
        return _state(fd, target.name)


def check_destination(path, expected=None):
    if destination_state(path) != expected:
        raise ValueError("目标已存在或已改变，请重新确认覆盖或选择新名称。")


def _assert_target(target, fd, expected):
    # Recheck visibility as well as identity immediately before committing.
    allowed_destination(target)
    pinned, current = os.fstat(fd), target.parent.stat()
    if (pinned.st_dev, pinned.st_ino) != (current.st_dev, current.st_ino):
        raise ValueError("目标目录已改变，请重新确认。")
    if _state(fd, target.name) != expected:
        raise ValueError("目标已存在或已改变，请重新确认覆盖或选择新名称。")


def _temporary(fd, *, directory=False):
    for _ in range(10):
        name = ".codex-web-" + secrets.token_hex(16)
        try:
            if directory:
                os.mkdir(name, 0o700, dir_fd=fd)
                return name, None
            opened = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=fd)
            return name, opened
        except FileExistsError:
            continue
    raise ValueError("无法创建安全暂存文件，请重试。")


def _rename(source_fd, source_name, target_fd, target_name, *, overwrite):
    if overwrite:
        os.replace(source_name, target_name, src_dir_fd=source_fd, dst_dir_fd=target_fd)
        return
    # Unlike rename(), RENAME_NOREPLACE cannot clobber a conflict appearing
    # after confirmation. DrvFS/9p may reject this flag; reserve a new empty
    # directory exclusively before replacing it, or let the caller use the
    # staged create-only file installation. Never fall back to bare rename.
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        code = errno.ENOSYS
    else:
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if not rename(source_fd, os.fsencode(source_name), target_fd, os.fsencode(target_name), 1):
            return
        code = ctypes.get_errno()
    if code not in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
        raise OSError(code, os.strerror(code))
    source = os.stat(source_name, dir_fd=source_fd, follow_symlinks=False)
    if not stat.S_ISDIR(source.st_mode):
        raise OSError(code, os.strerror(code))
    os.mkdir(target_name, 0o700, dir_fd=target_fd)
    reserved = _state(target_fd, target_name)
    try:
        if _state(target_fd, target_name) != reserved:
            raise ValueError("目标目录已改变，请重新确认。")
        os.replace(source_name, target_name, src_dir_fd=source_fd, dst_dir_fd=target_fd)
    except BaseException:
        if _state(target_fd, target_name) == reserved:
            os.rmdir(target_name, dir_fd=target_fd)
        raise


def _copy_bytes(source_fd, out_fd, *, mode=None, preserve_times=False):
    info = os.fstat(source_fd)
    before = _identity(info)
    if not stat.S_ISREG(before[2]):
        raise ValueError("只能复制普通文件。")
    with os.fdopen(os.dup(source_fd), "rb") as source, os.fdopen(os.dup(out_fd), "wb") as out:
        shutil.copyfileobj(source, out, 1024 * 1024)
        out.flush()
    if _identity(os.fstat(source_fd)) != before:
        raise ValueError("来源在复制时已改变，请重新确认。")
    if mode is not None:
        os.fchmod(out_fd, mode)
    if preserve_times:
        try:
            os.utime(out_fd, ns=(info.st_atime_ns, info.st_mtime_ns))
        except OSError:
            pass
    os.fsync(out_fd)


def _source(path):
    candidate = Path(os.path.abspath(Path(path).expanduser()))
    if allowed_path(candidate) != candidate:
        raise ValueError("来源已改变或包含符号链接，请重新确认。")
    return candidate


def _tree(source, fd, output_fd=None):
    """Validate every descendant while walking pinned directories."""
    before = _identity(os.fstat(fd))
    os.lseek(fd, 0, os.SEEK_SET)
    with os.scandir(fd) as entries:
        for entry in entries:
            child = _source(source / entry.name)
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                opened = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    if output_fd is None:
                        _tree(child, opened)
                    else:
                        os.mkdir(entry.name, 0o700, dir_fd=output_fd)
                        copied = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=output_fd)
                        try:
                            _tree(child, opened, copied)
                        finally:
                            os.close(copied)
                finally:
                    os.close(opened)
            elif stat.S_ISREG(info.st_mode):
                if output_fd is not None:
                    opened = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                    try:
                        copied = os.open(entry.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                         0o600, dir_fd=output_fd)
                        try:
                            _copy_bytes(opened, copied, mode=stat.S_IMODE(info.st_mode), preserve_times=True)
                        finally:
                            os.close(copied)
                    finally:
                        os.close(opened)
            else:
                raise ValueError("目录包含符号链接或特殊文件，暂不支持网页复制或移动。")
    if _identity(os.fstat(fd)) != before:
        raise ValueError("来源目录在操作时已改变，请重试。")
    if output_fd is not None:
        try:
            os.fchmod(output_fd, stat.S_IMODE(before[2]))
            info = os.fstat(fd)
            os.utime(output_fd, ns=(info.st_atime_ns, info.st_mtime_ns))
        except OSError:
            pass  # DrvFS may not support Unix metadata.


def _discard_directory(parent, name):
    """Remove only our private staging tree, including copied read-only dirs."""
    def writable(fd):
        os.fchmod(fd, 0o700)
        with os.scandir(fd) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    os.chmod(entry.name, 0o700, dir_fd=fd, follow_symlinks=False)
                    child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        writable(child)
                    finally:
                        os.close(child)
    os.chmod(name, 0o700, dir_fd=parent, follow_symlinks=False)
    opened = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        writable(opened)
    finally:
        os.close(opened)
    shutil.rmtree(name, dir_fd=parent)


def _install_file(source_fd, target, target_fd, expected, *, mode, preserve_times=False):
    name, out = _temporary(target_fd)
    try:
        _copy_bytes(source_fd, out, mode=mode, preserve_times=preserve_times)
        os.close(out)
        out = None
        _assert_target(target, target_fd, expected)
        if expected is None:
            # Atomic create-only installation. No final path is ever opened.
            os.link(name, target.name, src_dir_fd=target_fd, dst_dir_fd=target_fd,
                    follow_symlinks=False)
        else:
            os.replace(name, target.name, src_dir_fd=target_fd, dst_dir_fd=target_fd)
    finally:
        if out is not None:
            os.close(out)
        if _state(target_fd, name) is not None:
            os.unlink(name, dir_fd=target_fd)


def install_staged(staged, destination, *, expected=None, preserve_mode=False):
    target = allowed_destination(destination)
    if expected is not None and not stat.S_ISREG(expected[2]):
        raise ValueError("不能用文件覆盖目录。")
    staged = Path(staged)
    with _directory(staged.parent) as source_parent, _directory(target.parent) as parent:
        opened = os.open(staged.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source_parent)
        try:
            original = _identity(os.fstat(opened))
            mode = stat.S_IMODE(expected[2]) if preserve_mode and expected is not None else 0o600
            if any(root == target or root in target.parents for root in (Path("/mnt/c"), Path("/mnt/d"))):
                mode = None
            _assert_target(target, parent, expected)
            _install_file(opened, target, parent, expected, mode=mode)
            if _state(source_parent, staged.name) == original:
                os.unlink(staged.name, dir_fd=source_parent)
        finally:
            os.close(opened)


def validate_transfer(source, destination, *, move=False):
    source, target = _source(source), allowed_destination(destination)
    if source == target or source in target.parents:
        raise ValueError("目标与来源相同或在来源目录内部。")
    state = destination_state(target)
    if state is not None:
        if os.path.samefile(source, target):
            raise ValueError("目标与来源是同一个文件。")
        if source.is_dir() != stat.S_ISDIR(state[2]):
            raise ValueError("不能用文件覆盖目录或用目录覆盖文件。")
        if source.is_dir() and not move:
            raise ValueError("复制目录请使用新名称。")
        # Moving over a directory is allowed only when the OS can atomically
        # replace it (empty). Do not require listing the existing directory.
    return source, target, state


def transfer(source, destination, *, move=False, expected=None):
    source, target, _ = validate_transfer(source, destination, move=move)
    with _directory(source.parent) as source_parent, _directory(target.parent) as parent:
        _assert_target(target, parent, expected)
        initial = _state(source_parent, source.name)
        if initial is None:
            raise ValueError("来源已不存在，请重新确认。")
        if move and stat.S_ISREG(initial[2]):
            # Rename requires directory permissions, not readable file data.
            # Only fallback copying (EXDEV/DrvFS) needs an O_RDONLY descriptor.
            try:
                _rename(source_parent, source.name, parent, target.name, overwrite=expected is not None)
                return
            except OSError as error:
                if error.errno not in {errno.EXDEV, errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
                    raise
        opened = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source_parent)
        try:
            original = _identity(os.fstat(opened))
            if original != initial:
                raise ValueError("来源已改变，请重新确认。")
            is_directory = stat.S_ISDIR(original[2])
            if is_directory:
                _tree(source, opened)
            elif not stat.S_ISREG(original[2]):
                raise ValueError("来源不是普通文件或目录。")
            if move and is_directory:
                if _state(source_parent, source.name) != original:
                    raise ValueError("来源已改变，请重新确认。")
                _assert_target(target, parent, expected)
                try:
                    _rename(source_parent, source.name, parent, target.name, overwrite=expected is not None)
                    return
                except OSError as error:
                    if error.errno not in {errno.EXDEV, errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
                        raise
            if is_directory:
                temporary, _ = _temporary(parent, directory=True)
                try:
                    copied = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    try:
                        _tree(source, opened, copied)
                    finally:
                        os.close(copied)
                    _assert_target(target, parent, expected)
                    _rename(parent, temporary, parent, target.name, overwrite=expected is not None)
                finally:
                    if _state(parent, temporary) is not None:
                        _discard_directory(parent, temporary)
            else:
                mode = None if str(target).startswith(("/mnt/c/", "/mnt/d/")) else stat.S_IMODE(original[2])
                _install_file(opened, target, parent, expected, mode=mode, preserve_times=True)
            if move:
                try:
                    if _state(source_parent, source.name) != original:
                        raise ValueError("来源已改变")
                    if is_directory:
                        shutil.rmtree(source.name, dir_fd=source_parent)
                    else:
                        os.unlink(source.name, dir_fd=source_parent)
                except (OSError, ValueError) as error:
                    raise MoveCleanupPending("目标已完整保存，但来源清理未完成；请核对源与目标后手动清理。") from error
        finally:
            os.close(opened)
