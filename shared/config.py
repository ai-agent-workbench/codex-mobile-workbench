"""Central configuration — all constants, paths, and env-var bindings.

Every module under shared/ and routes/ imports what it needs from here.
No route or helper should duplicate a path literal that already lives here.
"""
import os
from datetime import timedelta
from pathlib import Path


# ── State directory ─────────────────────────────────────────────────────────

HOME_DIR = Path(os.environ.get("HOME", str(Path.home()))).expanduser().resolve()
STATE = Path(os.environ.get("CODEX_WEB_STATE_DIR", HOME_DIR / ".local/share/codex-web")).expanduser()
PROJECTS_DIR = Path(os.environ.get("CODEX_WEB_PROJECTS_DIR", HOME_DIR / "Projects")).expanduser()
INBOX_DIR = Path(os.environ.get("CODEX_WEB_INBOX_DIR", HOME_DIR / "Inbox")).expanduser()
# A managed new instance is initialized locally, never through anonymous setup.
MANAGED_INSTALL = os.environ.get("CODEX_WEB_MANAGED_INSTALL", "0") == "1"
APP_ROOT = Path(__file__).resolve().parents[1]
CONVERSATION_DB = STATE / "conversations.sqlite3"
# Deployment options, not browser preferences. Unset keeps existing installs
# unchanged; new installations explicitly opt out in core.env.example.
AUTOMATIONS_ENABLED = os.environ.get("CODEX_WEB_AUTOMATIONS_ENABLED", "1").strip().lower() in {
    "1", "true", "yes", "on",
}
COMPLETION_NOTIFICATIONS_ENABLED = os.environ.get(
    "CODEX_WEB_COMPLETION_NOTIFICATIONS_ENABLED", "1"
).strip().lower() in {"1", "true", "yes", "on"}
DOT_MAILBOX_DB = STATE / "dot-mailbox.sqlite3"
# Deployment must explicitly select the existing admin and parent correlation.
# No token, OAuth client, or native model permission is created by this flag.
DOT_CONVERSATION_ENABLED = os.environ.get("CODEX_WEB_DOT_ENABLED", "0") == "1"
DOT_OWNER_ACCOUNT_ID = os.environ.get("CODEX_WEB_DOT_OWNER_ACCOUNT_ID", "").strip()
DOT_PARENT_REF = os.environ.get("CODEX_WEB_DOT_PARENT_REF", "").strip()
DOT_LOCAL_RELAY_ENABLED = os.environ.get("CODEX_WEB_DOT_LOCAL_RELAY_ENABLED", "0") == "1"
DOT_LOCAL_RELAY_SCOPE = STATE / "dot-relay" / "scope.json"
DOT_MCP_ENABLED = os.environ.get("CODEX_WEB_DOT_MCP_ENABLED", "0") == "1"
DOT_MCP_REPLY_ENABLED = os.environ.get("CODEX_WEB_DOT_MCP_REPLY_ENABLED", "0") == "1"
DOT_EVENTS_OUTBOUND_ENABLED = os.environ.get("CODEX_WEB_DOT_EVENTS_OUTBOUND_ENABLED", "0") == "1"
DOT_EVENTS_PROXY = os.environ.get("CODEX_WEB_DOT_EVENTS_PROXY", "")
DOT_EVENTS_PUBLIC_DNS = os.environ.get("CODEX_WEB_DOT_EVENTS_PUBLIC_DNS", "")
DOT_MCP_SCOPE = STATE / "dot-relay" / "mcp-scope.json"
DOT_EVENTS_DB = STATE / "dot-events.sqlite3"
DOT_DELIVERY_DB = STATE / "dot-delivery.sqlite3"
DOT_NTFY_ENABLED = os.environ.get("CODEX_WEB_DOT_NTFY_ENABLED", "0") == "1"
DOT_NTFY_LINK = os.environ.get("CODEX_WEB_DOT_NTFY_LINK", "").strip()
DOT_NTFY_TEST_ONCE_20261004 = os.environ.get("CODEX_WEB_DOT_NTFY_TEST_ONCE_20261004", "0") == "1"
WEB_PUSH_VAPID_PRIVATE_KEY = STATE / "web-push-vapid-private.pem"
CONVERSATION_ARTIFACTS = STATE / "conversation-artifacts"
CONVERSATION_WORKSPACES = Path(
    os.environ.get("CODEX_WEB_CONVERSATION_DIR", INBOX_DIR / "conversations")
)
CODEX_APP_SERVER_SOCKET = Path(
    os.environ.get(
        "CODEX_APP_SERVER_SOCKET",
        "/run/codex-web-app-server/app-server.sock",
    )
)
SECRET_FILE = STATE / "totp_secret"
ENABLED_FILE = STATE / "totp_enabled"
SESSION_FILE = STATE / "session_secret"
AUDIT_FILE = STATE / "audit.jsonl"
NTFY_TOPIC_FILE = Path(os.environ.get(
    "CODEX_WEB_SECURITY_NTFY_TOPIC_FILE", STATE / "notifications/security-topic.unconfigured"
))
# 任务结束提醒与安全告警使用不同主题；任务主题永不暴露给网页或 Agent。
TASK_NTFY_TOPIC_FILE = Path(os.environ.get(
    "CODEX_WEB_TASK_NTFY_TOPIC_FILE", STATE / "notifications/task-topic.unconfigured"
))
TASK_NOTIFICATION_COMMAND = Path(os.environ.get(
    "CODEX_WEB_TASK_NOTIFICATION_COMMAND", STATE / "notifications/task-notify.unconfigured"
))

# Administrator Passkey login. Turning it off closes the daily entry; the
# separately protected emergency entry retains configured-origin validation.
# RP/origin values are deployment secrets/configuration, never repository data.
PASSKEY_ENABLED = os.environ.get("CODEX_WEB_PASSKEY_ENABLED", "0").strip().lower() in {
    "1", "true", "yes", "on",
}
PASSKEY_RP_ID = os.environ.get("CODEX_WEB_PASSKEY_RP_ID", "").strip().lower()
PASSKEY_ORIGIN = os.environ.get("CODEX_WEB_PASSKEY_ORIGIN", "").strip()
PASSKEY_DB = STATE / "auth.sqlite3"
PASSKEY_SESSION_COOKIE = "__Host-codex_admin_session"
PASSKEY_FLOW_COOKIE = "__Host-codex_passkey_flow"
PASSKEY_CHALLENGE_MAX_AGE = int(timedelta(minutes=5).total_seconds())
PASSKEY_SESSION_IDLE_AGE = int(timedelta(days=30).total_seconds())
PASSKEY_SESSION_ABSOLUTE_AGE = int(timedelta(days=90).total_seconds())
PASSKEY_SESSION_TOUCH_INTERVAL = int(timedelta(minutes=15).total_seconds())
PASSKEY_RECOVERY_MAX_AGE = int(timedelta(minutes=10).total_seconds())

ACCOUNT = os.environ.get("CODEX_WEB_ADMIN_ACCOUNT", "owner")
MAX_AGE = int(timedelta(days=7).total_seconds())

# ── Multi-account storage ───────────────────────────────────────────────────

ACCOUNTS_FILE = STATE / "accounts.json"
ADMIN_MAX_AGE = MAX_AGE  # 7 days

UPLOADS = STATE / "uploads"
EDITS = STATE / "edits"

MAX_UPLOAD_BYTES = 1000 * 1024 * 1024
MAX_EDIT_BYTES = 1024 * 1024

# ── Browser file-manager guardrails ──────────────────────────────────────────────────

def _configured_paths(name, defaults, *, allow_empty=False):
    """Resolve an os.pathsep-separated browser file-manager path list."""
    configured = os.environ.get(name)
    values = defaults if configured is None else tuple(
        value.strip() for value in configured.split(os.pathsep) if value.strip()
    )
    if not values and not allow_empty:
        raise RuntimeError(name + " must contain at least one path")
    return tuple(Path(value).expanduser().resolve() for value in values)


# These lists only constrain direct browser file management. Native Full Access
# conversations retain the ordinary OS permissions of the service user.
FILE_ROOTS = _configured_paths("CODEX_WEB_FILE_ROOTS", (HOME_DIR,))

SENSITIVE_PATHS = _configured_paths("CODEX_WEB_FILE_GUARDED_PATHS", (
    HOME_DIR / ".ssh",
    HOME_DIR / ".codex",
    STATE,
    APP_ROOT,
), allow_empty=True)

# ── Model / reasoning / permission enums ────────────────────────────────────

# Direct and project conversations intentionally share one explicit default.
NEW_CONVERSATION_MODEL = "gpt-6-sol"
NEW_CONVERSATION_REASONING = "high"
NEW_CONVERSATION_PERMISSION = "danger-full-access"
