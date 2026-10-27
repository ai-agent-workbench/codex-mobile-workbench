"""Authentication, session signing, audit logging, and push notifications."""
import json
import os
import subprocess
import secrets
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import pyotp
from itsdangerous import BadSignature, URLSafeTimedSerializer

from shared.config import (
    ACCOUNT, ACCOUNTS_FILE, ADMIN_MAX_AGE, AUDIT_FILE, ENABLED_FILE, MAX_AGE,
    NTFY_TOPIC_FILE, SECRET_FILE, SESSION_FILE, STATE,
    TASK_NTFY_TOPIC_FILE, TASK_NOTIFICATION_COMMAND,
)


# ── Account storage helpers ─────────────────────────────────────────────────

def load_accounts():
    """Read accounts.json, return dict keyed by account name."""
    if not ACCOUNTS_FILE.exists():
        return {}
    try:
        data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_accounts(accounts):
    """Atomically write accounts dict to accounts.json (0600)."""
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    new_path = ACCOUNTS_FILE.with_suffix(".json.new")
    new_path.write_text(json.dumps(accounts, ensure_ascii=False, indent=2), encoding="utf-8")
    new_path.chmod(0o600)
    new_path.replace(ACCOUNTS_FILE)


# ── Login failure lockout (per account+IP, in-memory — resets on restart) ────
#
# 2026-08-14 安全修复：原为账号全局锁定（存 accounts.json），任何外部人员
# 连打 5 次错误密码即可让门店永远登不上（锁定 DoS）。改为按 (账号, 来源IP)
# 维度滑动窗口计数：攻击者只能锁住自己这个 IP，门店从其他网络（如手机 4G）
# 仍可正常登录。暴力破解防护由每 IP 5 次/分钟的登录限流兜底。

_LOGIN_FAIL_WINDOW = 600   # 秒；窗口内失败计数
_LOGIN_FAIL_MAX = 5        # 窗口内达到此次数即锁定该 (账号, IP)
_login_fails: dict[str, list[float]] = {}


def _fail_key(account_name, ip):
    return str(account_name) + "|" + str(ip or "unknown")


def _fail_bucket(account_name, ip):
    now = time.monotonic()
    key = _fail_key(account_name, ip)
    bucket = [t for t in _login_fails.get(key, []) if t >= now - _LOGIN_FAIL_WINDOW]
    if bucket:
        _login_fails[key] = bucket
    else:
        _login_fails.pop(key, None)
    return bucket


def record_failed_login(account_name, ip=""):
    """Record one failed login for (account, ip); notify when lock triggers."""
    key = _fail_key(account_name, ip)
    bucket = _fail_bucket(account_name, ip)
    bucket.append(time.monotonic())
    _login_fails[key] = bucket
    if len(bucket) == _LOGIN_FAIL_MAX:
        _notify_account_event("account_locked", account_name,
                              "账号 " + str(account_name) + " 来自 " + str(ip or "unknown")
                              + " 的登录连续失败 " + str(_LOGIN_FAIL_MAX) + " 次，该来源已锁定 10 分钟。")


def check_account_locked(account_name, ip=""):
    """Return (is_locked, reason) for this (account, ip) pair."""
    bucket = _fail_bucket(account_name, ip)
    if len(bucket) < _LOGIN_FAIL_MAX:
        return (False, "")
    remain = int(_LOGIN_FAIL_WINDOW - (time.monotonic() - bucket[0])) + 1
    return (True, "该来源登录失败次数过多，请约 " + str(max(1, remain // 60 + 1)) + " 分钟后再试。")


def clear_failed_logins(account_name, ip=""):
    """Clear failure counter for (account, ip) after a successful login."""
    _login_fails.pop(_fail_key(account_name, ip), None)


def _notify_account_event(event, account_name, detail):
    """Shortcut for security-alert ntfy push from account operations."""
    message = _SECURITY_ALERTS.get(event)
    if not message:
        return
    title, body, priority, tags = message
    parts = [body, detail]
    notify_message(title, "\n".join(parts), priority, tags)


# ── Crypto / TOTP / signer (unchanged logic) ────────────────────────────────

def _saved_secret(path, factory):
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(factory(), encoding="ascii")
        path.chmod(0o600)
    return path.read_text(encoding="ascii").strip()


def totp():
    return pyotp.TOTP(_saved_secret(SECRET_FILE, pyotp.random_base32))


def signer():
    return URLSafeTimedSerializer(
        _saved_secret(SESSION_FILE, lambda: secrets.token_urlsafe(48)),
        salt="codex-web",
    )


def confirmation_signer():
    return URLSafeTimedSerializer(
        _saved_secret(SESSION_FILE, lambda: secrets.token_urlsafe(48)),
        salt="codex-web-task-confirm",
    )


def file_page_signer():
    """Signer namespace reserved for read-only file-list cursors."""
    return URLSafeTimedSerializer(
        _saved_secret(SESSION_FILE, lambda: secrets.token_urlsafe(48)),
        salt="codex-web-file-page",
    )


# ── Audit / notify ──────────────────────────────────────────────────────────

def audit(request, event, detail="", account=None):
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    record = {
        "time": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "ip": request.client.host if request.client else "unknown",
        "detail": str(detail),
    }
    if account:
        record["account"] = account
    with AUDIT_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=True) + "\n")
    AUDIT_FILE.chmod(0o600)
    notify(event, record["ip"], str(detail))


def notify_message(title, body, priority="default", tags="lock"):
    if not NTFY_TOPIC_FILE.exists():
        return False
    topic = NTFY_TOPIC_FILE.read_text(encoding="ascii").strip()
    if not topic:
        return False
    if not title.isascii():
        import base64
        title = "=?UTF-8?B?" + base64.b64encode(title.encode("utf-8")).decode("ascii") + "?="
    req = urllib.request.Request(
        "https://ntfy.sh/" + topic,
        data=body.encode("utf-8"),
        headers={"Title": title, "Priority": priority, "Tags": tags},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            response.read()
            return 200 <= int(response.status) < 300
    except Exception:
        return False


_SECURITY_ALERTS = {
    "totp_login_failed": ("登录失败", "有人尝试登录工作台，如非本人请检查安全", "high", "lock"),
    "totp_enabled": ("验证器已启用", "双因素认证已开启", "default", "lock"),
    "file_deleted": ("文件已删除", "有文件或目录被删除", "high", "warning"),
    "account_locked": ("账号已锁定", "门店账号因多次密码错误被锁定", "high", "lock"),
    "account_unlocked": ("账号已解锁", "门店账号已手动解锁", "default", "lock"),
}


def notify(event, ip, detail=""):
    message = _SECURITY_ALERTS.get(event)
    if message is None:
        return
    title, body, priority, tags = message
    parts = [body]
    if detail:
        parts.append(str(detail)[:200])
    if ip and ip not in {"background", "unknown"}:
        parts.append("来源：" + ip)
    notify_message(title, "\n".join(parts), priority, tags)


def task_notification_available():
    """Check configured files, without opening a topic or sending a probe."""
    try:
        return all(path.is_absolute() and path.is_file() and os.access(path, os.R_OK)
                   for path in (TASK_NOTIFICATION_COMMAND, TASK_NTFY_TOPIC_FILE))
    except OSError:
        return False


def notify_task_completion(title, body, *, tags="white_check_mark"):
    """Use the approved task sender without reading its dedicated topic."""
    if not task_notification_available():
        return False, "task_sender_not_configured"
    environment = os.environ.copy()
    environment["NTFY_TOPIC_FILE"] = str(TASK_NTFY_TOPIC_FILE)
    environment["NTFY_MARKDOWN"] = "1"
    environment["NTFY_TAGS"] = str(tags or "white_check_mark")
    try:
        result = subprocess.run(
            [
                "bash", str(TASK_NOTIFICATION_COMMAND),
                str(title), str(body), "default",
            ],
            check=False,
            timeout=20,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as error:
        return False, type(error).__name__
    if result.returncode == 0:
        return True, "sent"
    return False, "exit_" + str(result.returncode)


# ── Auth guards ─────────────────────────────────────────────────────────────

def current_legacy_account(request):
    """Accept only a signed, unexpired administrator cookie for explicit fallback.

    Archived employee accounts/cookies no longer identify a workbench user.
    Role is checked against both the token and the authoritative account store.
    """
    token = request.cookies.get("codex_web_session")
    if not token:
        return None
    try:
        data = signer().loads(token, max_age=ADMIN_MAX_AGE)
    except BadSignature:
        return None
    if isinstance(data, str):
        name = data
    elif isinstance(data, dict) and data.get("r") == "admin":
        name = data.get("n")
        timestamp = data.get("ts")
        if not isinstance(timestamp, (int, float)) or not 0 <= time.time() - timestamp <= ADMIN_MAX_AGE:
            return None
    else:
        return None
    if not isinstance(name, str) or load_accounts().get(name, {}).get("role") != "admin":
        return None
    return name


def current_account(request):
    """Return the account from a revocable Passkey session or legacy cookie."""
    from shared.passkey_auth import session_from_request
    passkey_session = session_from_request(request)
    if passkey_session:
        return passkey_session.account
    return current_legacy_account(request)


def logged_in(request):
    """Check session cookie; return account-name string or None.

    Accepts two token formats transparently:
      - Legacy: signer().dumps(ACCOUNT)  → string "workbench-owner"
      - New:    signer().dumps({"n": name, "r": role, "ts": unix_time})

    Additionally verifies the account exists and is enabled in accounts.json.
    """
    account = current_account(request)
    if not account:
        return None
    accounts = load_accounts()
    entry = accounts.get(account)
    if not entry or not entry.get("enabled") or entry.get("role") != "admin":
        return None
    return account


def require_auth(request):
    """Return None if the request is authorised, else a Response to return.

    - No accounts file + no ENABLED_FILE → redirect /setup (first-time)
    - Not logged in → HTML navigation to /login; API/mutation gets JSON 403
    - Logged-in admin → pass
    - Archived non-administrator accounts/cookies are not accepted
    - Locked source (account+IP) or disabled account → 403
    """
    from fastapi.responses import PlainTextResponse, RedirectResponse
    from shared.login_recovery import authentication_required

    # First-time setup check: no accounts store AND totp wasn't enabled yet
    if not ACCOUNTS_FILE.exists() and not ENABLED_FILE.exists():
        return RedirectResponse("/setup", 303)

    account = current_account(request)
    if not account:
        return authentication_required(request)

    accounts = load_accounts()
    entry = accounts.get(account, {})

    # Disabled account check
    if not entry.get("enabled"):
        audit(request, "admin_forbidden", f"disabled account={account}", account=account)
        return authentication_required(request)

    # Locked-source check (per account+IP; blocks even valid cookies from that IP)
    client_ip = request.client.host if request.client else ""
    locked, reason = check_account_locked(account, client_ip)
    if locked:
        audit(request, "admin_forbidden", f"locked account={account} ip={client_ip}", account=account)
        return PlainTextResponse(reason, status_code=403)

    role = entry.get("role", "uploader")

    if role == "admin":
        return None  # full access

    return PlainTextResponse("无权访问", status_code=403)
