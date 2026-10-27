"""File-browse, upload, edit, delete, move, and transfer routes."""
import html as html_escape
import hashlib
import json
import logging
import os
import secrets
import stat
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from engine.codex_app_server import AppServerError
from itsdangerous import BadData, SignatureExpired

from shared.config import (
    EDITS, FILE_ROOTS, INBOX_DIR, PROJECTS_DIR, MAX_EDIT_BYTES, MAX_UPLOAD_BYTES, UPLOADS,
)
from shared.file_utils import (
    allowed_destination, allowed_path,
    editable_text, install_upload, safe_upload_filename, sensitive_path, sensitive_direct_child_names,
    staged_edit, staged_upload,
)
from shared.file_operations import (
    MoveCleanupPending, check_destination, destination_state, install_staged,
    transfer, validate_transfer,
)
from shared.html_lib import (
    file_actions as _file_actions, html, human_size, nav,
    return_with_upload_notice, safe_return_path, suggested_copy_name,
    upload_notice,
)
from shared.security import audit, confirmation_signer, file_page_signer, notify, require_auth

from shared.upload_confirmation import load_upload_confirmation
from shared.upload_receipts import create_upload_receipt, discard_upload_receipt
from shared.message_attachment_uploads import validated_scope

logger = logging.getLogger(__name__)
router = APIRouter()

_FILE_PAGE_SIZE = 100
_FILE_PAGE_SORT = "directories-casefold-name-original-v1"
_FILE_PAGE_PURPOSE = "file-page-cursor-v1"


class _FileListingUnavailable(Exception):
    """The directory cannot be read consistently enough to show safely."""


class _FileListingChanged(_FileListingUnavailable):
    """The directory changed during this read; do not mix page generations."""


def _stat_fingerprint(info):
    """Include more than mtime: DrvFS can have coarse timestamp precision."""
    return (
        info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode),
        info.st_nlink, info.st_size,
        getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000)),
        getattr(info, "st_ctime_ns", int(info.st_ctime * 1_000_000_000)),
    )


def _readable_file_entry(child, sensitive_names=None):
    """Read one scandir entry below an already validated directory.

    A regular direct child inherits its parent's allowed root. Only links need
    another resolve/root check; download and mutation routes validate again at
    use time. Listing is not a write transaction or a snapshot of file content.
    """
    try:
        link_info = child.stat(follow_symlinks=False)
        path = Path(child.path)
        if stat.S_ISLNK(link_info.st_mode):
            resolved = allowed_path(path)
            target_info = resolved.stat()
        else:
            resolved = path
            if (child.name in sensitive_names if sensitive_names is not None else sensitive_path(resolved)):
                return None
            target_info = link_info
        is_directory = stat.S_ISDIR(target_info.st_mode)
        if not is_directory and not stat.S_ISREG(target_info.st_mode):
            return None
        # Do not expose entries that the service account could not subsequently use.
        required_access = os.R_OK | (os.X_OK if is_directory else 0)
        if not os.access(resolved, required_access):
            return None
    except (OSError, ValueError):
        return None
    return {
        "path": resolved,
        "name": child.name,
        "directory": is_directory,
        "size": target_info.st_size,
        # Paging depends on visibility/order/link destinations, not file bytes.
        "fingerprint": (child.name, is_directory, str(resolved)),
    }


def _visible_file_entries(directory, search):
    """Filter before paging and produce a cursor snapshot without leaking skips."""
    try:
        before = directory.stat()
        with os.scandir(directory) as scan:
            children = list(scan)
    except OSError as error:
        raise _FileListingUnavailable from error
    # Compute only path-policy invariants once per request. Every entry still
    # gets a fresh stat/access check and links still resolve through allowed_path.
    sensitive_names = sensitive_direct_child_names(directory)
    entries = []
    for child in children:
        if search.casefold() not in child.name.casefold():
            continue
        entry = _readable_file_entry(child, sensitive_names)
        if entry is not None:
            entries.append(entry)
    try:
        after = directory.stat()
        # One parent check also covers a directory/ancestor replaced by a link
        # after the route validated it but before scandir's first stat.
        if allowed_path(directory, require_directory=True) != directory:
            raise _FileListingChanged
    except ValueError as error:
        raise _FileListingChanged from error
    except OSError as error:
        raise _FileListingUnavailable from error
    entries.sort(key=lambda entry: (
        not entry["directory"], entry["name"].casefold(), entry["name"],
    ))
    digest = hashlib.sha256()
    digest.update(repr((_stat_fingerprint(before), _stat_fingerprint(after))).encode())
    for entry in entries:
        digest.update(repr(entry["fingerprint"]).encode())
        digest.update(b"\0")
    before_identity = _stat_fingerprint(before)
    after_identity = _stat_fingerprint(after)
    if before_identity != after_identity:
        raise _FileListingChanged
    return entries, after_identity, digest.hexdigest()


def _files_url(directory, search="", cursor=""):
    query = {"path": str(directory)}
    if search:
        query["search"] = search
    if cursor:
        query["cursor"] = cursor
    return "/files?" + urlencode(query)


def _file_list_conflict(directory, search):
    refresh = _files_url(directory, search)
    response = html("文件列表已变化", nav() + "<h1>文件列表已变化</h1>"
                    "<p class='error'>目录、筛选条件或可见文件已变化，请刷新后继续浏览。</p>"
                    "<p><a class='button' href='" + html_escape.escape(refresh, quote=True)
                    + "'>刷新文件列表</a></p>")
    response.status_code = 409
    return response


def _file_snapshot_token(identity, fingerprint):
    # Signing the cursor does not encrypt its payload. Keep parent nlink/size
    # (which include hidden descendants) behind a keyed opaque fingerprint.
    return file_page_signer().make_signer().get_signature(
        repr((identity, fingerprint)).encode("utf-8")
    ).decode("ascii")


def _file_page_token(directory, search, offset, identity, fingerprint):
    return file_page_signer().dumps({
        "purpose": _FILE_PAGE_PURPOSE,
        "directory": str(directory),
        "search": search,
        "sort": _FILE_PAGE_SORT,
        "offset": offset,
        "snapshot": _file_snapshot_token(identity, fingerprint),
    })


def _file_shortcut_paths():
    configured = []
    known = (
        (INBOX_DIR, "收件箱"),
        (PROJECTS_DIR, "项目"),
    )
    for path, label in (*known, *((root, str(root)) for root in FILE_ROOTS)):
        resolved = path.resolve()
        if not any(resolved == root or root in resolved.parents for root in FILE_ROOTS):
            continue
        if resolved in {item[0] for item in configured}:
            continue
        configured.append((resolved, label))
    return configured


def _file_shortcuts():
    configured = _file_shortcut_paths()
    return (
        "<div class='toolbar'>"
        + "".join(
            "<a class='button' href='/files?path=" + quote(str(path)) + "'>"
            + html_escape.escape(label) + "</a>"
            for path, label in configured
        )
        + "</div>"
    )


@router.get("/files")
def files(
    request: Request, path: str | None = None, uploaded: str = "", uploaded_batch: str = "",
    search: str = "", cursor: str = "",
):
    redirect = require_auth(request)
    if redirect:
        return redirect
    path = path or str(FILE_ROOTS[0])
    try:
        directory = allowed_path(path, require_directory=True)
    except ValueError as error:
        return html("文件浏览",
            "<h1>文件浏览</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='./'>返回工作台</a>",
        )
    search = (search or "").strip()
    offset = 0
    cursor_payload = None
    if cursor:
        try:
            candidate = file_page_signer().loads(cursor, max_age=3600)
            if (
                not isinstance(candidate, dict)
                or candidate.get("purpose") != _FILE_PAGE_PURPOSE
                or candidate.get("directory") != str(directory)
                or candidate.get("search") != search
                or candidate.get("sort") != _FILE_PAGE_SORT
                or not isinstance(candidate.get("offset"), int)
                or candidate["offset"] < 0
                or not isinstance(candidate.get("snapshot"), str)
            ):
                raise BadData("invalid file page cursor")
            cursor_payload = candidate
            offset = candidate["offset"]
        except (BadData, SignatureExpired):
            return _file_list_conflict(directory, search)
    try:
        entries, identity, fingerprint = _visible_file_entries(directory, search)
    except _FileListingChanged:
        return _file_list_conflict(directory, search)
    except _FileListingUnavailable:
        if cursor:
            return _file_list_conflict(directory, search)
        return html("文件浏览", nav() + "<h1>文件浏览</h1>"
                    "<p class='error'>当前目录无法安全读取，请检查后刷新。</p>")
    if cursor_payload and (
        not secrets.compare_digest(cursor_payload["snapshot"], _file_snapshot_token(identity, fingerprint))
        or offset >= len(entries)
    ):
        return _file_list_conflict(directory, search)

    items = []
    if directory.parent != directory and any(
        directory.parent == root or root in directory.parent.parents for root in FILE_ROOTS
    ):
        items.append(
            "<li><a href='" + _files_url(directory.parent)
            + "'>..（上级目录）</a></li>"
        )
    page_entries = entries[offset:offset + _FILE_PAGE_SIZE]
    for entry in page_entries:
        resolved = entry["path"]
        name = html_escape.escape(entry["name"])
        if entry["directory"]:
            items.append(
                "<li class='file-entry'><div class='file-entry-main'>"
                "<span class='file-name'>📁 <a href='/files?path="
                + quote(str(resolved)) + "'>" + name + "</a></span></div>"
                + _file_actions(resolved, True) + "</li>"
            )
        else:
            items.append(
                "<li class='file-entry'><div class='file-entry-main'>"
                "<span class='file-name'>📄 " + name + "</span>"
                "<small> " + str(entry["size"]) + " bytes</small></div>"
                + _file_actions(resolved, False) + "</li>"
            )
    page_note = "<p class='file-page-status'>本页显示 " + str(len(page_entries)) + " 项。"
    if offset + len(page_entries) < len(entries):
        page_note += "还有更多可加载。"
    page_note += "</p>"
    pagination = []
    if offset:
        previous = _file_page_token(
            directory, search, max(0, offset - _FILE_PAGE_SIZE), identity, fingerprint,
        )
        pagination.append("<a class='button secondary' rel='prev' href='"
                          + html_escape.escape(_files_url(directory, search, previous), quote=True)
                          + "'>上一页</a>")
    if offset + len(page_entries) < len(entries):
        following = _file_page_token(
            directory, search, offset + _FILE_PAGE_SIZE, identity, fingerprint,
        )
        pagination.append("<a class='button' rel='next' href='"
                          + html_escape.escape(_files_url(directory, search, following), quote=True)
                          + "'>下一页</a>")
    pagination_html = ("<nav class='file-pagination' aria-label='文件分页'>"
                       + "".join(pagination) + "</nav>") if pagination else ""
    search_form = (
        "<form class='file-search' action='/files' method='get'>"
        "<input type='hidden' name='path' value='"
        + html_escape.escape(str(directory), quote=True) + "'>"
        "<label for='file-search-query'>按名称搜索当前目录</label>"
        "<input id='file-search-query' name='search' type='search' value='"
        + html_escape.escape(search, quote=True) + "' autocomplete='off'>"
        "<button>搜索</button></form>"
    )
    upload_form = (
        "<h2>上传文件</h2>"
        "<form action='/files/upload' method='post' enctype='multipart/form-data'>"
        "<input type='hidden' name='directory' value='"
        + html_escape.escape(str(directory), quote=True) + "'>"
        "<input type='hidden' name='return_to' value='"
        + html_escape.escape("/files?path=" + quote(str(directory)), quote=True) + "'>"
        "<input name='files' type='file' multiple required>"
        "<button>上传</button></form>"
        "<p><small>可一次选择多个文件；本批文件总大小上限 1000 MB。</small></p>"
    )
    shortcuts = _file_shortcuts()
    file_actions_script = (
        "<script>(function(){document.addEventListener('click',function(e){"
        "document.querySelectorAll('.file-actions[open]').forEach(function(d){"
        "if(!d.contains(e.target))d.removeAttribute('open');});});"
        "document.querySelectorAll('.file-actions').forEach(function(d){"
        "d.addEventListener('toggle',function(){if(!d.open)return;"
        "var p=d.querySelector('.file-actions-panel');if(!p)return;"
        "var r=d.getBoundingClientRect();"
        "var s=window.innerHeight-r.bottom;"
        "var h=Math.min(p.offsetHeight||240,320);"
        "p.classList.toggle('panel-up',s<h);});});})();</script>"
    )
    import shared.html_lib as hlib
    body = (
        nav()
        + upload_notice(uploaded, uploaded_batch)
        + hlib.templates.get_template("generic_page.html").render(
            title="",
            content=(
                "<h1>文件浏览</h1>" + shortcuts
                + "<p class='path'>当前目录：<code>"
                + html_escape.escape(str(directory)) + "</code></p>"
                + search_form + page_note + upload_form
                + "<ul class='file-list'>" + "".join(items) + "</ul>" + pagination_html
            ),
        )
    )
    return html("文件浏览", body + file_actions_script)


@router.get("/download")
def download(request: Request, path: str):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        file_path = allowed_path(path)
        if not file_path.is_file():
            raise ValueError("只能下载普通文件。")
    except ValueError as error:
        return html("下载失败",
            "<h1>下载失败</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='files'>返回文件浏览</a>",
        )
    try:
        with file_path.open("rb"):
            pass
    except OSError:
        return html("下载失败",
            "<h1>下载失败</h1>"
            "<p class='error'>该文件受 Windows 系统保护，当前账户无法读取。</p>"
            "<a href='/files'>返回文件浏览</a>",
        )
    audit(request, "file_downloaded", str(file_path))
    return FileResponse(file_path, filename=file_path.name)


@router.get("/files/properties")
def file_properties(request: Request, path: str):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        target = allowed_path(path)
        stat = target.stat()
        kind = "目录" if target.is_dir() else "文件"
        size = "—" if target.is_dir() else (
            human_size(stat.st_size) + "（" + str(stat.st_size) + " bytes）"
        )
        modified = datetime.fromtimestamp(stat.st_mtime).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError) as error:
        return html("无法读取属性",
            nav() + "<h1>无法读取属性</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='/files'>返回文件</a>",
        )
    values = (
        ("名称", target.name), ("类型", kind), ("大小", size),
        ("修改时间", modified), ("完整路径", str(target)),
    )
    details = "".join(
        "<dt>" + label + "</dt><dd>" + html_escape.escape(value) + "</dd>"
        for label, value in values
    )
    return html("属性",
        nav() + "<h1>属性</h1><dl class='property-grid'>" + details + "</dl>"
        + "<p><a href='/files?path=" + quote(str(target if target.is_dir() else target.parent))
        + "'>返回所在目录</a></p>",
    )


@router.get("/files/choose")
def choose_directory(request: Request, action: str, path: str, directory: str = ""):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        if action not in {"copy", "move"}:
            raise ValueError("操作无效。")
        source = allowed_path(path)
        current = allowed_path(directory, require_directory=True) if directory else source.parent
    except ValueError as error:
        return html("无法选择目录",
            nav() + "<h1>无法选择目录</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='/files'>返回文件</a>",
        )
    source_arg = quote(str(source))
    items = []
    parent_allowed = (
        current.parent != current
        and any(current.parent == root or root in current.parent.parents for root in FILE_ROOTS)
        and not sensitive_path(current.parent)
    )
    if parent_allowed:
        items.append(
            "<li><a href='/files/choose?action=" + action + "&path=" + source_arg
            + "&directory=" + quote(str(current.parent)) + "'>⬆ 上一级</a></li>"
        )
    try:
        children = sorted(
            (c for c in current.iterdir() if c.is_dir() and not sensitive_path(c.resolve())),
            key=lambda c: c.name.lower(),
        )
    except OSError:
        children = []
    for child in children[:300]:
        items.append(
            "<li><a href='/files/choose?action=" + action + "&path=" + source_arg
            + "&directory=" + quote(str(child.resolve())) + "'>📁 "
            + html_escape.escape(child.name) + "</a></li>"
        )
    shortcuts = "<div class='toolbar'>" + "".join(
        "<a class='button secondary' href='/files/choose?" + html_escape.escape(urlencode({
            'action': action, 'path': str(source), 'directory': str(path)}), quote=True)
        + "'>" + html_escape.escape(label) + "</a>"
        for path, label in _file_shortcut_paths()
    ) + "</div>"
    label = "复制到这里" if action == "copy" else "移动到这里"
    return html("选择目标文件夹",
        nav() + "<h1>选择目标文件夹</h1>"
        + "<p><small>来源：" + html_escape.escape(source.name) + "</small></p>"
        + shortcuts
        + "<p class='picker-current'>当前文件夹：<code>"
        + html_escape.escape(str(current)) + "</code></p>"
        + "<form action='/files/transfer/prepare' method='post'>"
        + "<input type='hidden' name='action' value='" + action + "'>"
        + "<input type='hidden' name='path' value='" + html_escape.escape(str(source), quote=True) + "'>"
        + "<input type='hidden' name='directory' value='" + html_escape.escape(str(current), quote=True) + "'>"
        + "<button>" + label + "</button></form>"
        + "<ul class='folder-picker'>" + "".join(items) + "</ul>",
    )


@router.post("/files/transfer/prepare")
def transfer_prepare(request: Request, action: str = Form(...),
                     path: str = Form(...), directory: str = Form(...)):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        if action not in {"copy", "move"}:
            raise ValueError("操作无效。")
        source = allowed_path(path)
        destination_dir = allowed_path(directory, require_directory=True)
        default_name = (
            suggested_copy_name(source) if action == "copy" and destination_dir == source.parent
            else source.name
        )
    except ValueError as error:
        return html("操作无效",
            nav() + "<h1>操作无效</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='/files'>返回文件</a>",
        )
    verb = "复制" if action == "copy" else "移动"
    return html(verb,
        nav() + "<h1>" + verb + "</h1>"
        + "<p>目标文件夹：<code>" + html_escape.escape(str(destination_dir)) + "</code></p>"
        + "<form action='/files/transfer/confirm' method='post'>"
        + "<input type='hidden' name='action' value='" + action + "'>"
        + "<input type='hidden' name='path' value='" + html_escape.escape(str(source), quote=True) + "'>"
        + "<input type='hidden' name='directory' value='" + html_escape.escape(str(destination_dir), quote=True) + "'>"
        + "<label>名称</label><input name='name' value='"
        + html_escape.escape(default_name, quote=True) + "' required>"
        + "<button>确认" + verb + "</button></form>",
    )


@router.post("/files/transfer/confirm")
def transfer_confirm(request: Request, action: str = Form(...),
                     path: str = Form(...), directory: str = Form(...), name: str = Form(...)):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        if action not in {"copy", "move"}:
            raise ValueError("操作无效。")
        source = allowed_path(path)
        destination_dir = allowed_path(directory, require_directory=True)
        filename = safe_upload_filename(name)
        destination = destination_dir / filename
        source, destination, target_state = validate_transfer(
            source, destination, move=action == "move"
        )
        token = confirmation_signer().dumps({
            "action": action, "source": str(source), "destination": str(destination),
            "target_state": target_state,
        })
        verb = "复制" if action == "copy" else "移动"
        return html("确认" + verb,
            nav() + "<h1>确认" + verb + "</h1>"
            + "<p>来源：<code>" + html_escape.escape(str(source)) + "</code></p>"
            + "<p>目标：<code>" + html_escape.escape(str(destination)) + "</code></p>"
            + ("<p class='error'>目标已存在，确认执行将覆盖原内容，无法从工作台恢复。</p>"
               if target_state is not None else "")
            + "<form action='/files/transfer/execute' method='post'>"
            + "<input type='hidden' name='token' value='"
            + html_escape.escape(token, quote=True) + "'>"
            + ("<button>确认覆盖并执行</button>" if target_state is not None else "<button>确认执行</button>")
            + "</form>",
        )
    except ValueError as error:
        return html("操作无效",
            nav() + "<h1>操作无效</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='/files'>返回</a>",
        )


@router.post("/files/transfer/execute")
def transfer_execute(request: Request, token: str = Form(...)):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        payload = confirmation_signer().loads(token, max_age=3600)
        source = Path(payload["source"])
        destination = Path(payload["destination"])
        if payload["action"] not in {"copy", "move"}:
            raise ValueError("操作无效。")
        transfer(source, destination, move=payload["action"] == "move",
                 expected=payload.get("target_state"))
        event = "file_moved" if payload["action"] == "move" else "file_copied"
        audit(request, event, str(source) + " -> " + str(destination))
        notify(event, request.client.host if request.client else "unknown", str(destination))
        return html("操作成功",
            nav() + "<h1>操作成功</h1>"
            + "<p><code>" + html_escape.escape(str(destination)) + "</code></p>"
            + "<a href='/files?path=" + quote(str(destination.parent)) + "'>打开目标目录</a>",
        )
    except MoveCleanupPending as error:
        return html("移动已保存，需清理来源",
            nav() + "<h1>移动已保存，需清理来源</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='/files'>返回文件</a>",
        )
    except Exception as error:
        return html("操作失败",
            nav() + "<h1>操作失败</h1>"
            + "<p class='error'>" + html_escape.escape(str(error) if isinstance(error, ValueError)
                else "权限不足、文件被占用或确认已失效；请重新确认。") + "</p>"
            + "<a href='/files'>返回文件</a>",
        )


@router.get("/edit")
def edit(request: Request, path: str):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        file_path = allowed_path(path)
        content = editable_text(file_path)
    except ValueError as error:
        return html("无法编辑",
            "<h1>无法编辑</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='/files'>返回</a>",
        )
    return html("编辑文本",
        "<h1>编辑文本文件</h1><p><code>" + html_escape.escape(str(file_path)) + "</code></p>"
        + "<form action='/edit/confirm' method='post'>"
        + "<input type='hidden' name='path' value='" + html_escape.escape(str(file_path), quote=True) + "'>"
        + "<textarea name='content' rows='18' required>"
        + html_escape.escape(content) + "</textarea><button>进入写入确认</button></form>",
    )


@router.post("/edit/confirm")
def edit_confirm(request: Request, path: str = Form(...), content: str = Form(...)):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        file_path = allowed_path(path)
        editable_text(file_path)
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_EDIT_BYTES:
            raise ValueError("内容超过 1 MB 上限。")
        EDITS.mkdir(mode=0o700, parents=True, exist_ok=True)
        edit_id = secrets.token_hex(16)
        staged = EDITS / edit_id
        with staged.open("x", encoding="utf-8") as output:
            output.write(content)
        staged.chmod(0o600)
        payload = {"path": str(file_path), "edit_id": edit_id,
                   "target_state": destination_state(file_path)}
        token = confirmation_signer().dumps(payload)
    except ValueError as error:
        return html("无法编辑",
            "<h1>无法编辑</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='/files'>返回</a>",
        )
    return html("确认覆盖文本",
        "<h1>确认覆盖文本文件</h1>"
        + "<p class='error'>此操作将覆盖现有文件内容。</p>"
        + "<p><code>" + html_escape.escape(str(file_path)) + "</code></p>"
        + "<form action='/edit/write' method='post'>"
        + "<input type='hidden' name='token' value='"
        + html_escape.escape(token, quote=True) + "'>"
        + "<button>确认覆盖写入</button></form>",
    )


@router.post("/edit/write")
def edit_write(request: Request, token: str = Form(...)):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        payload = confirmation_signer().loads(token, max_age=3600)
        file_path = allowed_destination(payload["path"])
        if payload.get("target_state") is None:
            raise ValueError("编辑确认已失效，请重新打开文件确认。")
        editable_text(file_path)
        staged = staged_edit(payload["edit_id"])
        content = staged.read_text(encoding="utf-8")
        if len(content.encode("utf-8")) > MAX_EDIT_BYTES:
            raise ValueError("内容超过上限。")
        install_staged(staged, file_path, expected=payload["target_state"], preserve_mode=True)
        audit(request, "file_edited", str(file_path))
        notify("file_edited", request.client.host if request.client else "unknown", str(file_path))
        return html("写入成功",
            "<h1>写入成功</h1>"
            + "<p><code>" + html_escape.escape(str(file_path)) + "</code></p>"
            + "<a href='/files?path=" + quote(str(file_path.parent)) + "'>返回目录</a>",
        )
    except Exception:
        return html("写入失败",
            "<h1>写入失败</h1>"
            + "<p class='error'>文件可能已被更改、删除或权限不足。</p>"
            + "<a href='/files'>返回</a>",
        )


@router.get("/delete")
def delete(request: Request, path: str):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        target = allowed_path(path)
    except ValueError as error:
        return html("无法删除",
            "<h1>无法删除</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='/files'>返回</a>",
        )
    kind = "空目录" if target.is_dir() else "文件"
    token = confirmation_signer().dumps({"path": str(target)})
    return html("确认删除",
        "<h1>确认删除 " + kind + "</h1>"
        + "<p class='error'>删除后无法从工作台恢复。</p>"
        + "<p><code>" + html_escape.escape(str(target)) + "</code></p>"
        + "<form action='/delete/confirm' method='post'>"
        + "<input type='hidden' name='token' value='"
        + html_escape.escape(token, quote=True) + "'><button>继续</button></form>",
    )


@router.post("/delete/confirm")
def delete_confirm(request: Request, token: str = Form(...)):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        payload = confirmation_signer().loads(token, max_age=3600)
        target = allowed_path(payload["path"])
    except Exception:
        return html("删除确认失效", "<h1>删除确认失效</h1><a href='/files'>返回</a>")
    return html("再次确认删除",
        "<h1>再次确认删除</h1>"
        + "<p>请输入完整名称以确认：</p>"
        + "<p><code>" + html_escape.escape(target.name) + "</code></p>"
        + "<form action='/delete/execute' method='post'>"
        + "<input type='hidden' name='token' value='"
        + html_escape.escape(token, quote=True) + "'>"
        + "<input name='name' required placeholder='完整文件或目录名称'>"
        + "<button>永久删除</button></form>",
    )


@router.post("/delete/execute")
def delete_execute(request: Request, token: str = Form(...), name: str = Form(...)):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        payload = confirmation_signer().loads(token, max_age=3600)
        target = allowed_path(payload["path"])
        if name != target.name:
            raise ValueError("名称不匹配。")
        parent = target.parent
        if target.is_dir():
            target.rmdir()
        else:
            target.unlink()
        audit(request, "file_deleted", str(target))
        notify("file_deleted", request.client.host if request.client else "unknown", str(target))
        return html("删除成功",
            "<h1>删除成功</h1>"
            + "<a href='/files?path=" + quote(str(parent)) + "'>返回目录</a>",
        )
    except OSError:
        return html("删除失败",
            "<h1>删除失败</h1>"
            + "<p class='error'>目录非空、文件受保护或权限不足。递归删除未开放。</p>"
            + "<a href='/files'>返回</a>",
        )
    except Exception:
        return html("删除失败",
            "<h1>删除失败</h1>"
            + "<p class='error'>确认无效或名称不匹配。</p>"
            + "<a href='/files'>返回</a>",
        )


@router.get("/move")
def move(request: Request, path: str):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        source = allowed_path(path)
    except ValueError as error:
        return html("无法移动",
            "<h1>无法移动</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='/files'>返回</a>",
        )
    return html("移动或重命名",
        "<h1>移动或重命名</h1>"
        + "<p>来源：<code>" + html_escape.escape(str(source)) + "</code></p>"
        + "<form action='/move/confirm' method='post'>"
        + "<input type='hidden' name='path' value='" + html_escape.escape(str(source), quote=True) + "'>"
        + "<label>目标目录</label><input name='directory' value='"
        + html_escape.escape(str(source.parent), quote=True) + "' required>"
        + "<label>新名称</label><input name='name' value='"
        + html_escape.escape(source.name, quote=True) + "' required>"
        + "<button>进入确认</button></form>",
    )


@router.post("/move/confirm")
def move_confirm(request: Request, path: str = Form(...),
                 directory: str = Form(...), name: str = Form(...)):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        source = allowed_path(path)
        destination_dir = allowed_path(directory, require_directory=True)
        filename = safe_upload_filename(name)
        destination = destination_dir / filename
        source, destination, _ = validate_transfer(source, destination, move=True)
        payload = {"source": str(source), "destination": str(destination), "overwrite": False}
        token = confirmation_signer().dumps(payload)
    except ValueError as error:
        return html("无法移动",
            "<h1>无法移动</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='/files'>返回</a>",
        )
    warning = (
        "<p class='error'>目标已存在，下一步会要求再次确认覆盖。</p>"
        if destination.exists() else ""
    )
    return html("确认移动",
        "<h1>确认移动或重命名</h1>"
        + "<p>来源：<code>" + html_escape.escape(str(source)) + "</code></p>"
        + "<p>目标：<code>" + html_escape.escape(str(destination)) + "</code></p>"
        + warning
        + "<form action='/move/execute' method='post'>"
        + "<input type='hidden' name='token' value='"
        + html_escape.escape(token, quote=True) + "'>"
        + "<button>确认执行</button></form>",
    )


@router.post("/move/execute")
def move_execute(request: Request, token: str = Form(...)):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        payload = confirmation_signer().loads(token, max_age=3600)
        source, destination, current_state = validate_transfer(
            payload["source"], payload["destination"], move=True
        )
        if current_state is not None and not payload.get("overwrite"):
            payload["overwrite"] = True
            payload["target_state"] = current_state
            return html("再次确认覆盖",
                "<h1>再次确认覆盖</h1>"
                + "<p class='error'>目标已存在，将被覆盖，无法从工作台恢复。</p>"
                + "<p><code>" + html_escape.escape(str(destination)) + "</code></p>"
                + "<form action='/move/execute' method='post'>"
                + "<input type='hidden' name='token' value='"
                + html_escape.escape(confirmation_signer().dumps(payload), quote=True) + "'>"
                + "<button>确认覆盖并移动</button></form>",
            )
        transfer(source, destination, move=True, expected=payload.get("target_state"))
        audit(request, "file_moved", str(source) + " -> " + str(destination))
        notify("file_moved", request.client.host if request.client else "unknown", str(destination))
        return html("移动成功",
            "<h1>移动成功</h1>"
            + "<p><code>" + html_escape.escape(str(destination)) + "</code></p>"
            + "<a href='/files?path=" + quote(str(destination.parent)) + "'>打开目标目录</a>",
        )
    except MoveCleanupPending as error:
        return html("移动已保存，需清理来源",
            "<h1>移动已保存，需清理来源</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='/files'>返回</a>",
        )
    except Exception as error:
        return html("移动失败",
            "<h1>移动失败</h1>"
            + "<p class='error'>" + html_escape.escape(str(error) if isinstance(error, ValueError)
                else "权限不足、目标目录非空或文件被占用；请重新确认。") + "</p>"
            + "<a href='/files'>返回</a>",
        )


def _unique_upload_names(names, directory):
    windows = any(root == directory or root in directory.parents
                  for root in (Path("/mnt/c"), Path("/mnt/d")))
    keys = [name.casefold() if windows else name for name in names]
    if len(keys) != len(set(keys)):
        raise ValueError("本批上传包含重名文件，请先改名或取消重复文件后重新上传。")


def _close_upload_files(files):
    for upload_file in files:
        try:
            upload_file.file.close()
        except (AttributeError, OSError):
            pass


def _discard_upload_staging(paths):
    """Discard only staging paths allocated by this request."""
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("upload_staging_cleanup_failed error_type=OSError")


class _PartialUploadFailure(Exception):
    def __init__(self, cause, saved_count):
        self.cause = cause
        self.saved_count = saved_count


def _save_staged_uploads(directory, staged_files):
    """Validate a complete batch, then atomically install its staged files."""
    target_dir = allowed_path(directory, require_directory=True)
    if target_dir != Path(directory):
        raise ValueError("目标目录已改变，请重新确认上传。")
    names = [safe_upload_filename(info["name"]) for info in staged_files]
    _unique_upload_names(names, target_dir)

    # Every target and staged file is checked before any install.  This is also
    # used by the no-confirmation path, so a late conflict never permits a
    # partial silent overwrite.
    for file_info, name in zip(staged_files, names):
        check_destination(target_dir / name, file_info.get("target_state"))
        staged_upload(file_info["upload_id"])

    destinations = []
    try:
        for file_info, name in zip(staged_files, names):
            destination = target_dir / name
            expected = file_info.get("target_state")
            if expected is None:
                install_upload(staged_upload(file_info["upload_id"]), destination)
            else:
                install_upload(staged_upload(file_info["upload_id"]), destination,
                               expected=expected)
            destinations.append(destination)
    except Exception as error:
        if destinations:
            raise _PartialUploadFailure(error, len(destinations)) from error
        raise
    return destinations


def _upload_failure_response(error, saved_count=0, batch_count=0):
    if isinstance(error, _PartialUploadFailure):
        saved_count += error.saved_count
        error = error.cause
    # Log categories only: signed tokens, paths and filenames stay private.
    if isinstance(error, SignatureExpired):
        reason = "clock_skew" if str(error) == "clock_skew" else "expired"
        message = (
            "服务器时间存在偏差，请稍等几秒后返回上一页，再次确认上传。"
            if reason == "clock_skew" else
            "上传确认已超过 1 小时，请重新选择文件上传。"
        )
    elif isinstance(error, BadData):
        reason, message = "invalid_token", "上传确认凭证无效，请重新选择文件上传。"
    elif isinstance(error, ValueError):
        reason, message = "validation", str(error)
    else:
        reason, message = "storage", "保存上传文件失败，请稍后重试；若仍失败，请联系维护人员。"
    all_saved = batch_count > 0 and saved_count == batch_count
    if all_saved:
        message = ("本批 " + str(saved_count)
                   + " 个文件均已保存，但上传结果登记失败。请到项目文件查看，不要重复上传。")
    elif saved_count:
        message = ("本批已保存 " + str(saved_count)
                   + " 个文件，其余未完成。请核对后仅重新上传未完成文件。" + message)
    logger.warning("upload_save_failed reason=%s error_type=%s", reason, type(error).__name__)
    title = "文件已保存" if all_saved else "上传失败"
    response = html(title,
        "<h1>" + title + "</h1><p class='error'>"
        + html_escape.escape(message) + "</p><a href='/files'>返回</a>",
    )
    response.status_code = 500 if reason == "storage" else 400
    return response


def _prepare_upload_notice(names):
    if len(names) == 1:
        return names[0], ""
    try:
        return "", create_upload_receipt(names, directory=UPLOADS)
    except (OSError, ValueError) as error:
        # A display receipt must not prevent an otherwise valid file install.
        logger.warning("upload_receipt_failed error_type=%s", type(error).__name__)
        return "共 " + str(len(names)) + " 个文件（清单暂不可用，请到项目文件查看）", ""


def _save_message_upload(request, directory, staged_files, attachment_context):
    """Share original upload semantics; add local version references only."""
    if not attachment_context:
        return _save_staged_uploads(directory, staged_files), ""
    from engine.conversation_service import conversation_service
    scope = validated_scope(request, attachment_context, directory)
    attachments = conversation_service().attachments
    records = attachments.prepare(scope, [
        (staged_upload(info["upload_id"]), info["name"]) for info in staged_files
    ])
    try:
        destinations = _save_staged_uploads(directory, staged_files)
    except _PartialUploadFailure as error:
        # Do not pretend partially saved originals were rolled back. Keep only
        # the versions of the files this request actually installed.
        try:
            attachments.commit(records[:error.saved_count])
        except BaseException as registration_error:
            attachments.discard(records)
            raise _PartialUploadFailure(registration_error, error.saved_count) from registration_error
        attachments.discard(records[error.saved_count:])
        raise
    except BaseException:
        attachments.discard(records)
        raise
    try:
        batch_id = attachments.commit(records)
    except BaseException as error:
        attachments.discard(records)
        # Originals are already saved: retain the established no-reupload
        # failure receipt rather than claiming upload itself never happened.
        raise _PartialUploadFailure(error, len(destinations)) from error
    return destinations, batch_id


@router.post("/files/upload")
def upload(request: Request, directory: str = Form(...),
           files: list[UploadFile] = File(...), return_to: str = Form(""),
           attachment_context: str = Form("")):
    redirect = require_auth(request)
    if redirect:
        return redirect
    attachment_context = attachment_context if isinstance(attachment_context, str) else ""
    try:
        target_dir = allowed_path(directory, require_directory=True)
        if attachment_context:
            validated_scope(request, attachment_context, target_dir)
    except (ValueError, BadData, AppServerError) as error:
        _close_upload_files(files)
        return html("上传失败",
            "<h1>上传失败</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='/files'>返回</a>",
        )
    try:
        named_files = [
            (upload_file, safe_upload_filename(upload_file.filename or "untitled"))
            for upload_file in files
        ]
        _unique_upload_names([name for _, name in named_files], target_dir)
        states = {name: destination_state(target_dir / name) for _, name in named_files}
        for state in states.values():
            if state is not None and not stat.S_ISREG(state[2]):
                raise ValueError("不能用上传文件覆盖目录。")
    except ValueError as error:
        _close_upload_files(files)
        return html("上传失败",
            "<h1>上传失败</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='/files'>返回</a>",
        )

    staged_files = []
    staged_paths = []
    total_size = 0
    try:
        UPLOADS.mkdir(mode=0o700, parents=True, exist_ok=True)
        for upload_file, name in named_files:
            upload_id = secrets.token_hex(12)
            staged_path = UPLOADS / upload_id
            with staged_path.open("xb") as out:
                staged_paths.append(staged_path)
                while True:
                    chunk = upload_file.file.read(65536)
                    if not chunk:
                        break
                    total_size += len(chunk)
                    if total_size > MAX_UPLOAD_BYTES:
                        raise ValueError("本批文件总大小超过 1000 MB 上限。")
                    out.write(chunk)
            staged_path.chmod(0o600)
            staged_files.append({
                "name": name, "upload_id": upload_id,
                "target_state": states[name],
            })
    except Exception as error:
        _discard_upload_staging(staged_paths)
        return _upload_failure_response(error)
    finally:
        _close_upload_files(files)

    if not staged_files:
        return html("上传失败",
            "<h1>上传失败</h1>"
            + "<p class='error'>未选择文件或文件名为空。</p>"
            + "<a href='/files'>返回</a>",
        )
    return_to = safe_return_path(return_to)
    if not any(file_info["target_state"] is not None for file_info in staged_files):
        saved_count = 0
        receipt_id = ""
        try:
            names = [file_info["name"] for file_info in staged_files]
            notice_name, receipt_id = _prepare_upload_notice(names)
            destinations, attachment_batch = _save_message_upload(
                request, str(target_dir), staged_files, attachment_context
            )
            saved_count = len(destinations)
            audit(request, "files_uploaded", str(target_dir))
            return RedirectResponse(
                return_with_upload_notice(
                    return_to, notice_name, receipt_id, attachment_batch
                ), 303
            )
        except Exception as error:
            if receipt_id and saved_count != len(staged_files):
                discard_upload_receipt(receipt_id, directory=UPLOADS)
            return _upload_failure_response(error, saved_count, len(staged_files))

    save_args = {
        "directory": str(target_dir),
        "files": staged_files,
        "return_to": return_to,
        **({"attachment_context": attachment_context} if attachment_context else {}),
    }
    token = confirmation_signer().dumps(save_args)
    return html("确认上传",
        nav() + "<h1>确认上传文件</h1>"
        + "<p>目标目录：<code>" + html_escape.escape(str(target_dir)) + "</code></p>"
        + "<ul>" + "".join(
            "<li>" + html_escape.escape(f["name"])
            + " <small>(" + str(Path(f["name"]).suffix) + ")</small></li>"
            + ("<li class='error'>同名文件已存在：确认上传将覆盖原内容，无法从工作台恢复。</li>"
               if f["target_state"] is not None else "")
            for f in staged_files
        ) + "</ul>"
        + "<form action='/files/upload/confirm' method='post'>"
        + "<input type='hidden' name='token' value='"
        + html_escape.escape(token, quote=True) + "'>"
        + "<button>确认覆盖并上传</button></form>",
    )


@router.post("/files/upload/confirm")
def upload_confirm(request: Request, token: str = Form(...)):
    redirect = require_auth(request)
    if redirect:
        return redirect
    saved_count = 0
    batch_count = 0
    receipt_id = ""
    try:
        payload = load_upload_confirmation(confirmation_signer(), token)
        names = [safe_upload_filename(file_info["name"]) for file_info in payload["files"]]
        batch_count = len(names)
        notice_name, receipt_id = _prepare_upload_notice(names)
        destinations, attachment_batch = _save_message_upload(
            request, payload["directory"], payload["files"],
            payload.get("attachment_context", ""),
        )
        saved_count = len(destinations)
        destination = destinations[-1] if destinations else None
        audit(request, "files_uploaded", str(destination.parent if destination else ""))
        return_to = safe_return_path(payload.get("return_to", "/files"))
        return RedirectResponse(
            return_with_upload_notice(
                return_to, notice_name, receipt_id, attachment_batch
            ), 303
        )
    except Exception as error:
        if receipt_id and saved_count != batch_count:
            discard_upload_receipt(receipt_id, directory=UPLOADS)
        return _upload_failure_response(error, saved_count, batch_count)
