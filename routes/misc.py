"""Minor routes: notifications, settings, inbox, and PWA assets."""
import json

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from engine.codex_app_server import AppServerError, CodexAppServerClient
from shared.config import (
    CODEX_APP_SERVER_SOCKET,
    CONVERSATION_WORKSPACES,
    NTFY_TOPIC_FILE,
)
import shared.html_lib as hlib
from shared.html_lib import html, nav
from shared.security import load_accounts, logged_in, require_auth

router = APIRouter()
_PRIVATE_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
}


async def _read_codex_weekly_quota():
    client = CodexAppServerClient(CODEX_APP_SERVER_SOCKET)
    try:
        await client.connect()
        return await client.read_weekly_rate_limit()
    finally:
        await client.close()


@router.get("/notifications")
def notifications(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    account = logged_in(request)
    try:
        topic = NTFY_TOPIC_FILE.read_text(encoding="ascii").strip()
    except OSError:
        return html("通知",
            hlib.templates.get_template("notifications.html").render(
                nav_html=nav("settings"),
                web_push_admin=bool(account and load_accounts().get(account, {}).get("role") == "admin"),
            )
            + "<p class='error'>通知主题暂不可读。</p><a href='/home'>返回工作台</a>",
        )
    return html("通知",
        hlib.templates.get_template("notifications.html").render(
            topic=topic, nav_html=nav("settings"),
            web_push_admin=bool(account and load_accounts().get(account, {}).get("role") == "admin"),
        ),
    )


@router.get("/settings")
def settings(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import projects
    response = html("设置", hlib.templates.get_template("settings_center.html").render(
        nav_html=nav("settings"), projects_path=str(projects.ROOT)))
    response.headers.update(_PRIVATE_HEADERS)
    return response


@router.get("/settings/codex-quota")
async def codex_quota(request: Request):
    redirect = require_auth(request)
    if redirect:
        if redirect.headers.get("x-codex-auth-required") == "1":
            return redirect
        status_code = 403 if redirect.status_code == 403 else 401
        return JSONResponse(
            {"error": "登录状态无效，无法读取订阅额度。"},
            status_code=status_code,
            headers=_PRIVATE_HEADERS,
        )
    try:
        value = await _read_codex_weekly_quota()
    except (AppServerError, OSError, ValueError):
        return JSONResponse(
            {"error": "暂时无法读取 Codex 周额度，请稍后刷新。"},
            status_code=503,
            headers=_PRIVATE_HEADERS,
        )
    return JSONResponse(value, headers=_PRIVATE_HEADERS)


@router.get("/settings/completion-sound")
def completion_sound_settings(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    body = hlib.templates.get_template(
        "completion_sound_settings.html"
    ).render(nav_html=nav())
    return html("前台完成提示音", body)


@router.get("/settings/tts")
def tts_settings(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    body = hlib.templates.get_template("tts_settings.html").render(nav_html=nav())
    return html("回复朗读（TTS）", body)


@router.get("/inbox")
def inbox_overview(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    from pathlib import Path
    from urllib.parse import quote
    import html as html_escape
    from engine.conversation_service import conversation_service

    rows = []
    root = CONVERSATION_WORKSPACES
    if root.exists():
        for directory in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not directory.is_dir():
                continue
            files = [item for item in directory.iterdir() if item.is_file()]
            size = sum(item.stat().st_size for item in files)
            try:
                summary = conversation_service().workspace_summary(directory.name)
                title = summary["title"]
                link = summary["url"]
            except Exception:
                title = "未知对话"
                link = "/files?path=" + quote(str(directory))
            rows.append(
                "<li><a class='task-title' href='" + link + "'>"
                + html_escape.escape(title) + "</a><small>"
                + str(len(files)) + " 个附件，" + str(size) + " bytes</small></li>"
            )
    inbox_root = CONVERSATION_WORKSPACES.parent
    loose = (
        [item for item in inbox_root.iterdir() if item.is_file()]
        if inbox_root.is_dir() else []
    )
    body = (
        nav() + "<h1>收件箱整理</h1>"
        + "<p>这里只列出临时对话附件，不会自动删除。</p>"
        + "<h2>按对话整理</h2><ul class='task-list'>"
        + ("".join(rows) or "<li>暂无对话附件。</li>")
        + "</ul><h2>未整理文件</h2><p>" + str(len(loose)) + " 个文件</p>"
        + "<a class='button secondary' href='/files?path="
        + quote(str(inbox_root)) + "'>打开收件箱</a>"
    )
    return html("收件箱整理", body)


@router.get("/manifest.webmanifest")
def web_manifest():
    content = {
        "name": "Codex 工作台",
        "short_name": "Codex",
        "start_url": "/home",
        "display": "standalone",
        "background_color": "#151925",
        "theme_color": "#151925",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/static/icon-maskable-192.png", "sizes": "192x192", "type": "image/png", "purpose": "maskable"},
            {"src": "/static/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }
    return Response(json.dumps(content, indent=2), media_type="application/manifest+json")


@router.get("/sw.js")
def service_worker():
    from pathlib import Path
    return Response(
        (Path(__file__).resolve().parent.parent / "static" / "service-worker.js").read_text(encoding="utf-8"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/static/{filename:path}")
def static_asset(filename: str, request: Request):
    from pathlib import Path
    STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
    path = (STATIC_DIR / filename).resolve()
    if not str(path).startswith(str(STATIC_DIR.resolve())):
        return Response("forbidden", status_code=403)
    if not path.is_file():
        return Response("not found", status_code=404)
    # 模板生成的 URL 带文件 mtime 版本号。只有请求版本与当前文件精确匹配时
    # 才永久缓存；文件变化后 URL 随之变化。无版本或旧版本请求继续回源，
    # 避免直接访问静态资源时长期滞留旧内容。
    current_version = str(int(path.stat().st_mtime))
    cache_control = (
        "public, max-age=31536000, immutable"
        if request.query_params.get("v") == current_version
        else "no-cache"
    )
    return FileResponse(path, headers={"Cache-Control": cache_control})
