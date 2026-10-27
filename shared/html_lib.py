"""HTML builder helpers used across route modules.

Every `def` here that was previously a top-level function in app.py
keeps the identical signature and behaviour.  The Jinja2 `templates`
object lives here so that `html()` can render base.html without every
route needing its own instance.
"""
import html as html_escape
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

# ── Jinja2 instance (single shared instance) ────────────────────────────────

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# Activate the new UI only once this backend version is actually loaded.
templates.env.globals["compact_controls_ready"] = True


def static_url(path):
    """给 /static 资源 URL 追加 ?v=<mtime> 缓存穿透版本号。

    页面 HTML 每次实时渲染不缓存，文件一变版本号即变，浏览器必然重新下载。
    根治手机端启发式缓存致已发布 JS 长期不生效（2026-07-28 实证：无缓存头
    时期入缓存的旧条目按文件年龄 10% 视为新鲜，数小时不回源，no-cache 管不到）。
    """
    try:
        mtime = int((STATIC_DIR / path[len("/static/"):]).stat().st_mtime)
    except OSError:
        mtime = 0
    return path + "?v=" + str(mtime)


templates.env.globals["static_url"] = static_url


# ── Page-level builders ─────────────────────────────────────────────────────

def html(title, body):
    """Render a page using the Jinja2 base template (previously in app.py)."""
    return HTMLResponse(templates.get_template("base.html").render(
        title=title, content=body,
    ))


def nav(active: str | None = None):
    """Render the shared mobile navigation without changing its route targets."""
    items = (
        ("home", "/home", "首页", "<path d='m3 11 9-8 9 8'/><path d='M5 10v10h14V10'/>") ,
        ("projects", "/projects", "项目", "<path d='M5 4h14v16H5z'/><path d='M8 8h8M8 12h8M8 16h5'/>") ,
        ("history", "/projects/history", "历史", "<path d='M3 12a9 9 0 1 0 3-6.7L3 8'/><path d='M3 3v5h5'/><path d='M12 7v5l3 2'/>") ,
        ("files", "/files", "文件", "<path d='M4 4h6l2 2h8v14H4z'/><path d='M8 10h8M8 14h5'/>") ,
        ("settings", "/settings", "设置", "<circle cx='12' cy='12' r='3'/><path d='M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-2.12 2.12-.06-.06a1.7 1.7 0 0 0-1.88-.34 1.7 1.7 0 0 0-1.04 1.56V20h-3v-.08A1.7 1.7 0 0 0 9.66 18.4a1.7 1.7 0 0 0-1.88.34l-.06.06L5.6 16.68l.06-.06A1.7 1.7 0 0 0 6 14.74 1.7 1.7 0 0 0 4.44 13.7H4v-3h.44A1.7 1.7 0 0 0 6 9.66a1.7 1.7 0 0 0-.34-1.88L5.6 7.72 7.72 5.6l.06.06A1.7 1.7 0 0 0 9.66 6 1.7 1.7 0 0 0 10.7 4.44V4h3v.44A1.7 1.7 0 0 0 15.74 6a1.7 1.7 0 0 0 1.88-.34l.06-.06 2.12 2.12-.06.06a1.7 1.7 0 0 0-.34 1.88 1.7 1.7 0 0 0 1.56 1.04H20v3h-.04A1.7 1.7 0 0 0 19.4 15Z'/>") ,
    )
    links = []
    for key, href, label, paths in items:
        current = " aria-current='page'" if key == active else ""
        links.append(
            "<a href='" + href + "'" + current + ">"
            "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' "
            "stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'>" + paths + "</svg>"
            "<span>" + label + "</span></a>"
        )
    return "<nav class='nav' aria-label='主导航'>" + "".join(links) + "</nav>"


def management_script(form_id):
    safe_form_id = html_escape.escape(form_id, quote=True)
    return (
        "<script>(function(){const form=document.getElementById('" + safe_form_id + "');if(!form)return;"
        "let timer,startX=0,startY=0,managing=false;"
        "function reserveBar(){document.body.style.setProperty('--manage-bar-height',form.getBoundingClientRect().height+'px')}"
        "if(window.ResizeObserver)new ResizeObserver(reserveBar).observe(form);window.addEventListener('resize',reserveBar);"
        "function cards(){return [...document.querySelectorAll('.manage-check[form=\"" + safe_form_id + "\"]')].map(b=>b.closest('.manage-card')).filter(Boolean)}"
        "function update(){const all=cards();all.forEach(c=>c.classList.toggle('selected',c.querySelector('input').checked));"
        "const count=form.querySelector('[data-count]');if(count)count.textContent=all.filter(c=>c.querySelector('input').checked).length+' 项';}"
        "function enter(card){managing=true;document.body.classList.add('managing');reserveBar();card.querySelector('input').checked=true;card.classList.add('selected');update();if(navigator.vibrate)navigator.vibrate(20)}"
        "function cancel(){clearTimeout(timer)}"
        "document.addEventListener('pointerdown',e=>{const card=e.target.closest('.manage-card');if(!card||!card.querySelector('.manage-check[form=\"" + safe_form_id + "\"]')||e.target.closest('input,button'))return;cancel();startX=e.clientX;startY=e.clientY;timer=setTimeout(()=>enter(card),500)});"
        "document.addEventListener('pointerup',cancel);document.addEventListener('pointercancel',cancel);"
        "document.addEventListener('pointermove',e=>{if(Math.abs(e.clientX-startX)>10||Math.abs(e.clientY-startY)>10)cancel()});"
        "document.addEventListener('click',e=>{const card=e.target.closest('.manage-card');if(!card||!card.querySelector('.manage-check[form=\"" + safe_form_id + "\"]'))return;if(e.target.closest('input'))return;if(!managing)return;e.preventDefault();const box=card.querySelector('input');box.checked=!box.checked;update()});"
        "document.addEventListener('change',e=>{if(e.target.matches('.manage-check')&&e.target.form===form)update()});"
        "document.addEventListener('contextmenu',e=>{const card=e.target.closest('.manage-card');if(card&&card.querySelector('.manage-check[form=\"" + safe_form_id + "\"]'))e.preventDefault()});"
        "form.querySelector('[data-cancel]').onclick=()=>{managing=false;document.body.classList.remove('managing');cards().forEach(c=>c.querySelector('input').checked=false);update()};"
        "form.querySelector('[data-all]').onclick=()=>{const all=cards(),checked=all.length&&all.every(c=>c.querySelector('input').checked);all.forEach(c=>c.querySelector('input').checked=!checked);update()};"
        "form.addEventListener('submit',e=>{const action=e.submitter&&e.submitter.value,count=cards().filter(c=>c.querySelector('input').checked).length;if(!count){e.preventDefault();return}if(action==='delete'&&!confirm('确定删除选中的 '+count+' 项？此操作不可恢复。'))e.preventDefault()})})();</script>"
    )


def upload_button(dialog_id="upload-dialog"):
    safe_dialog_id = html_escape.escape(dialog_id, quote=True)
    return (
        "<button class='attach-button' type='button' title='添加附件' aria-label='添加附件' "
        "onclick=\"document.getElementById('" + safe_dialog_id + "').showModal()\">＋</button>"
    )


def upload_panel(directory, return_to, dialog_id="upload-dialog", *, attachment_context=""):
    safe_directory = html_escape.escape(directory, quote=True)
    safe_return = html_escape.escape(return_to, quote=True)
    safe_dialog_id = html_escape.escape(dialog_id, quote=True)
    return (
        "<dialog id='" + safe_dialog_id + "' data-light-dismiss='empty-upload'>"
        "<h2>添加附件</h2>"
        "<form action='/files/upload' method='post' enctype='multipart/form-data'>"
        "<input type='hidden' name='directory' value='" + safe_directory + "'>"
        "<input type='hidden' name='return_to' value='" + safe_return + "'>"
        + ("<input type='hidden' name='attachment_context' value='"
           + html_escape.escape(attachment_context, quote=True) + "'>" if attachment_context else "")
        +
        "<input name='files' type='file' multiple required>"
        "<p><small>上传目标：<code>" + safe_directory + "</code>。"
        "网页文件范围是防误操作护栏，不是 Full Access 对话的权限边界。</small></p>"
        "<p><small>可一次选择多个文件；本批文件总大小上限 1000 MB。</small></p>"
        "<div class='dialog-actions'>"
        "<button type='button' class='secondary' onclick=\"this.closest('dialog').close()\">取消</button>"
        "<button>上传</button></div></form></dialog>"
    )


def upload_dialog(directory, return_to, dialog_id="upload-dialog"):
    return upload_button(dialog_id) + upload_panel(directory, return_to, dialog_id)


def safe_return_path(path, fallback="/home"):
    if path.startswith("/") and not path.startswith("//") and "\r" not in path and "\n" not in path:
        return path
    return fallback


def return_with_upload_notice(path, uploaded, uploaded_batch="", attachment_batch=""):
    parts = urlsplit(path)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key not in {"uploaded", "uploaded_batch", "attachment_batch"}]
    if uploaded_batch:
        query.append(("uploaded_batch", uploaded_batch))
    elif uploaded:
        query.append(("uploaded", uploaded))
    if attachment_batch:
        query.append(("attachment_batch", attachment_batch))
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(query), parts.fragment))


def upload_notice(filename, batch_id=""):
    if batch_id:
        from shared.upload_receipts import load_upload_receipt
        names = load_upload_receipt(batch_id)
        if names is None:
            content = "附件已上传，清单已过期，请到项目文件查看"
        else:
            content = ("附件已上传（共 " + str(len(names)) + " 个）：<ul>"
                       + "".join("<li>" + html_escape.escape(name) + "</li>" for name in names)
                       + "</ul>")
    elif filename:
        content = "附件已上传：<b>" + html_escape.escape(filename) + "</b>"
    else:
        return ""
    return (
        "<div class='notice' id='upload-notice'>" + content + "</div>"
        "<script>document.getElementById('upload-notice').scrollIntoView({block:'nearest'});</script>"
    )


def file_actions(path, is_directory):
    encoded = quote(str(path))
    codex = "/quick?workdir=" + quote(str(path if is_directory else path.parent))
    if not is_directory:
        codex += "&prompt=" + quote("处理这个文件：" + path.name)
    actions = [
        ("交给 Codex", codex, ""),
        ("复制到", "/files/choose?action=copy&path=" + encoded, ""),
        ("移动到", "/files/choose?action=move&path=" + encoded, ""),
        ("重命名", "/move?path=" + encoded, ""),
        ("属性", "/files/properties?path=" + encoded, ""),
    ]
    if is_directory:
        actions.insert(0, ("打开", "/files?path=" + encoded, ""))
    if not is_directory:
        actions[1:1] = [
            ("下载", "/download?path=" + encoded, ""),
            ("编辑", "/edit?path=" + encoded, ""),
        ]
    actions.append(("删除", "/delete?path=" + encoded, "danger"))
    links = "".join(
        "<a class='" + style + "' href='" + href
        + "' onclick=\"this.closest('details').removeAttribute('open')\">"
        + label + "</a>"
        for label, href, style in actions
    )
    return (
        "<details class='file-actions' data-file-actions>"
        "<summary title='更多操作' aria-label='更多操作'>⋯</summary>"
        "<div class='file-actions-panel'>" + links + "</div></details>"
    )


def suggested_copy_name(source):
    if source.is_dir() or not source.suffix:
        return source.name + " - 副本"
    return source.stem + " - 副本" + source.suffix


def human_size(size):
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return (str(int(value)) if unit == "B" else f"{value:.1f}") + " " + unit
        value /= 1024
