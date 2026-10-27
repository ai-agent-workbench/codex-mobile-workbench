"""Passkey credentials, one-time ceremonies, and revocable admin sessions.

The feature is dormant unless ``CODEX_WEB_PASSKEY_ENABLED`` is explicitly
enabled.  WebAuthn imports are lazy so the existing TOTP/uploader service keeps
working before the separately authorised dependency/bootstrap phase.
"""
from __future__ import annotations

import base64
from collections import defaultdict, deque
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from types import SimpleNamespace
from urllib.parse import urlsplit

from shared.config import (
    PASSKEY_CHALLENGE_MAX_AGE,
    PASSKEY_DB,
    PASSKEY_ENABLED,
    PASSKEY_FLOW_COOKIE,
    PASSKEY_ORIGIN,
    PASSKEY_RECOVERY_MAX_AGE,
    PASSKEY_RP_ID,
    PASSKEY_SESSION_ABSOLUTE_AGE,
    PASSKEY_SESSION_COOKIE,
    PASSKEY_SESSION_IDLE_AGE,
    PASSKEY_SESSION_TOUCH_INTERVAL,
)


class PasskeyError(Exception):
    """A deliberately non-sensitive Passkey failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PasskeySession:
    account: str
    session_id: str
    credential_id: bytes
    created_at: int
    last_used_at: int
    absolute_expires_at: int


@dataclass(frozen=True)
class CeremonyStart:
    selector: str
    options: dict


@dataclass(frozen=True)
class AuthenticationSuccess:
    account: str
    token: str
    session_id: str
    revoked_session_ids: tuple[str, ...]


_SCHEMA_LOCK = threading.RLock()
_FINISH_RATE_LOCK = threading.Lock()
_FINISH_RATE_BUCKETS: dict[str, deque[float]] = defaultdict(deque)
_FINISH_RATE_MAX = 5
_FINISH_RATE_WINDOW = 60


def _now() -> int:
    return int(time.time())


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def cookie_binding(value: str) -> str:
    """Hash a legacy-session cookie for ceremony binding."""
    return _token_hash("legacy:" + value)


def source_fingerprint(request) -> str:
    """Return a non-reversible, short source label for local audit/status."""
    client = getattr(request, "client", None)
    ip = getattr(client, "host", "") or "unknown"
    headers = getattr(request, "headers", {}) or {}
    agent = str(headers.get("user-agent", ""))[:256]
    return hashlib.sha256((ip + "\n" + agent).encode("utf-8")).hexdigest()[:16]


def request_is_same_origin(request) -> bool:
    """Require a strict configured Origin (Referer only for legacy form POSTs)."""
    if configuration_error():
        return False
    from shared.request_origin import privacy_form_is_same_origin
    if privacy_form_is_same_origin(request, PASSKEY_ORIGIN):
        return True
    headers = getattr(request, "headers", {}) or {}
    origin = str(headers.get("origin", "")).rstrip("/")
    if origin:
        return secrets.compare_digest(origin, PASSKEY_ORIGIN.rstrip("/"))
    referer = str(headers.get("referer", ""))
    if not referer:
        return False
    parsed = urlsplit(referer)
    referer_origin = parsed.scheme + "://" + parsed.netloc
    return secrets.compare_digest(referer_origin, PASSKEY_ORIGIN.rstrip("/"))


def _b64url_decode(value: str, *, maximum: int = 8192) -> bytes:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise PasskeyError("invalid_credential")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise PasskeyError("invalid_credential") from exc


def configuration_error() -> str:
    if not PASSKEY_ENABLED:
        return "disabled"
    if not PASSKEY_RP_ID or not PASSKEY_ORIGIN:
        return "missing_configuration"
    if any(ch in PASSKEY_RP_ID for ch in "/:@"):
        return "invalid_rp_id"
    parsed = urlsplit(PASSKEY_ORIGIN)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.hostname.lower() != PASSKEY_RP_ID
    ):
        return "invalid_origin"
    return ""


def require_ready() -> None:
    error = configuration_error()
    if error:
        raise PasskeyError(error)


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS passkey_users (
            account TEXT PRIMARY KEY,
            user_handle BLOB NOT NULL UNIQUE,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS passkey_credentials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account TEXT NOT NULL,
            user_handle BLOB NOT NULL,
            credential_id BLOB NOT NULL UNIQUE,
            public_key BLOB NOT NULL,
            sign_count INTEGER NOT NULL DEFAULT 0,
            transports TEXT NOT NULL DEFAULT '[]',
            device_type TEXT NOT NULL DEFAULT '',
            backed_up INTEGER NOT NULL DEFAULT 0,
            aaguid TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            last_used_at INTEGER,
            revoked_at INTEGER,
            revoked_reason TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_passkey_per_account
            ON passkey_credentials(account) WHERE revoked_at IS NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_passkey_globally
            ON passkey_credentials((1)) WHERE revoked_at IS NULL;
        CREATE INDEX IF NOT EXISTS passkey_credential_account
            ON passkey_credentials(account, revoked_at);
        CREATE TABLE IF NOT EXISTS webauthn_flows (
            selector_hash TEXT PRIMARY KEY,
            flow_type TEXT NOT NULL CHECK(flow_type IN ('registration', 'authentication')),
            challenge BLOB NOT NULL,
            account TEXT NOT NULL,
            user_handle BLOB NOT NULL,
            binding_hash TEXT,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            consumed_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS webauthn_flow_expiry
            ON webauthn_flows(expires_at, consumed_at);
        CREATE TABLE IF NOT EXISTS admin_sessions (
            token_hash TEXT PRIMARY KEY,
            account TEXT NOT NULL,
            credential_id BLOB NOT NULL,
            created_at INTEGER NOT NULL,
            last_used_at INTEGER NOT NULL,
            absolute_expires_at INTEGER NOT NULL,
            revoked_at INTEGER,
            revoked_reason TEXT,
            source_hash TEXT NOT NULL DEFAULT '',
            reauthenticated_at INTEGER NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_admin_session
            ON admin_sessions(account) WHERE revoked_at IS NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_admin_session_globally
            ON admin_sessions((1)) WHERE revoked_at IS NULL;
        CREATE INDEX IF NOT EXISTS admin_session_expiry
            ON admin_sessions(absolute_expires_at, revoked_at);
        CREATE TABLE IF NOT EXISTS registration_authorizations (
            token_hash TEXT PRIMARY KEY,
            account TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            consumed_at INTEGER
        );
        """
    )


def _connect() -> sqlite3.Connection:
    with _SCHEMA_LOCK:
        PASSKEY_DB.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            PASSKEY_DB.parent.chmod(0o700)
        except OSError:
            pass
        try:
            fd = os.open(PASSKEY_DB, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        try:
            PASSKEY_DB.chmod(0o600)
        except OSError:
            pass
        conn = sqlite3.connect(str(PASSKEY_DB), timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        _init_schema(conn)
        try:
            PASSKEY_DB.chmod(0o600)
        except OSError:
            pass
        return conn


@contextmanager
def _transaction():
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _enabled_admin_account() -> str | None:
    from shared.security import load_accounts
    admins = [
        name for name, entry in load_accounts().items()
        if entry.get("enabled") and entry.get("role") == "admin"
    ]
    return admins[0] if len(admins) == 1 else None


def _admin_is_enabled(account: str) -> bool:
    admin = _enabled_admin_account()
    return bool(admin and secrets.compare_digest(admin, account))


def _check_finish_rate(identifier: bytes | str) -> None:
    """Apply the account/credential half of finish rate limiting.

    The HTTP middleware independently limits each source IP. This global,
    non-reversible key prevents distributed attempts against the single active
    account or credential without retaining the credential ID in logs.
    """
    if isinstance(identifier, bytes):
        material = b"credential:\x00" + identifier
    else:
        material = ("account:\x00" + identifier).encode("utf-8")
    key = hashlib.sha256(material).hexdigest()
    now = time.monotonic()
    cutoff = now - _FINISH_RATE_WINDOW
    with _FINISH_RATE_LOCK:
        bucket = _FINISH_RATE_BUCKETS[key]
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= _FINISH_RATE_MAX:
            raise PasskeyError("finish_rate_limited")
        bucket.append(now)


def _user_handle(account: str) -> bytes:
    now = _now()
    with _transaction() as conn:
        row = conn.execute(
            "SELECT user_handle FROM passkey_users WHERE account=?", (account,),
        ).fetchone()
        if row:
            return bytes(row["user_handle"])
        handle = secrets.token_bytes(32)
        conn.execute(
            "INSERT INTO passkey_users(account,user_handle,created_at) VALUES(?,?,?)",
            (account, handle, now),
        )
        return handle


def active_credential_exists(account: str | None = None) -> bool:
    conn = _connect()
    try:
        if account:
            row = conn.execute(
                "SELECT 1 FROM passkey_credentials WHERE account=? AND revoked_at IS NULL",
                (account,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM passkey_credentials WHERE revoked_at IS NULL LIMIT 1"
            ).fetchone()
        return row is not None
    finally:
        conn.close()


def _active_credential() -> sqlite3.Row:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM passkey_credentials WHERE revoked_at IS NULL ORDER BY id"
        ).fetchall()
        if len(rows) != 1:
            raise PasskeyError("credential_unavailable")
        return rows[0]
    finally:
        conn.close()


def _insert_flow(
    flow_type: str,
    account: str,
    user_handle: bytes,
    challenge: bytes,
    binding_hash: str | None,
) -> str:
    selector = secrets.token_urlsafe(32)
    now = _now()
    with _transaction() as conn:
        conn.execute(
            "DELETE FROM webauthn_flows WHERE expires_at<=? OR consumed_at IS NOT NULL",
            (now - PASSKEY_CHALLENGE_MAX_AGE,),
        )
        conn.execute(
            """INSERT INTO webauthn_flows(
                   selector_hash,flow_type,challenge,account,user_handle,binding_hash,
                   created_at,expires_at,consumed_at
               ) VALUES(?,?,?,?,?,?,?,?,NULL)""",
            (
                _token_hash(selector), flow_type, challenge, account, user_handle,
                binding_hash, now, now + PASSKEY_CHALLENGE_MAX_AGE,
            ),
        )
    return selector


def _claim_flow(
    selector: str,
    flow_type: str,
    binding_hash: str | None = None,
) -> sqlite3.Row:
    if not selector:
        raise PasskeyError("flow_missing")
    now = _now()
    with _transaction() as conn:
        row = conn.execute(
            "SELECT * FROM webauthn_flows WHERE selector_hash=?",
            (_token_hash(selector),),
        ).fetchone()
        if not row or row["flow_type"] != flow_type:
            raise PasskeyError("flow_invalid")
        if row["consumed_at"] is not None or int(row["expires_at"]) <= now:
            raise PasskeyError("flow_expired")
        expected_binding = row["binding_hash"]
        if expected_binding and not secrets.compare_digest(expected_binding, binding_hash or ""):
            raise PasskeyError("flow_binding_mismatch")
        updated = conn.execute(
            """UPDATE webauthn_flows SET consumed_at=?
               WHERE selector_hash=? AND consumed_at IS NULL AND expires_at>?""",
            (now, row["selector_hash"], now),
        )
        if updated.rowcount != 1:
            raise PasskeyError("flow_replayed")
        return row


@lru_cache(maxsize=1)
def _webauthn_modules():
    try:
        from webauthn import (
            generate_authentication_options,
            generate_registration_options,
            options_to_json,
            verify_authentication_response,
            verify_registration_response,
        )
        from webauthn.helpers.structs import (
            AttestationConveyancePreference,
            AuthenticatorSelectionCriteria,
            ResidentKeyRequirement,
            UserVerificationRequirement,
        )
    except ImportError as exc:
        raise PasskeyError("dependency_unavailable") from exc
    return SimpleNamespace(
        generate_authentication_options=generate_authentication_options,
        generate_registration_options=generate_registration_options,
        options_to_json=options_to_json,
        verify_authentication_response=verify_authentication_response,
        verify_registration_response=verify_registration_response,
        AttestationConveyancePreference=AttestationConveyancePreference,
        AuthenticatorSelectionCriteria=AuthenticatorSelectionCriteria,
        ResidentKeyRequirement=ResidentKeyRequirement,
        UserVerificationRequirement=UserVerificationRequirement,
    )


def _registration_options(account: str, user_handle: bytes, challenge: bytes) -> dict:
    api = _webauthn_modules()
    selection = api.AuthenticatorSelectionCriteria(
        resident_key=api.ResidentKeyRequirement.REQUIRED,
        require_resident_key=True,
        user_verification=api.UserVerificationRequirement.REQUIRED,
    )
    options = api.generate_registration_options(
        rp_id=PASSKEY_RP_ID,
        rp_name="Codex 工作台",
        user_name=account,
        user_id=user_handle,
        user_display_name="管理员",
        challenge=challenge,
        timeout=PASSKEY_CHALLENGE_MAX_AGE * 1000,
        attestation=api.AttestationConveyancePreference.NONE,
        authenticator_selection=selection,
    )
    return json.loads(api.options_to_json(options))


def _authentication_options(challenge: bytes) -> dict:
    api = _webauthn_modules()
    options = api.generate_authentication_options(
        rp_id=PASSKEY_RP_ID,
        challenge=challenge,
        timeout=PASSKEY_CHALLENGE_MAX_AGE * 1000,
        allow_credentials=None,
        user_verification=api.UserVerificationRequirement.REQUIRED,
    )
    return json.loads(api.options_to_json(options))


def begin_registration(account: str, binding_hash: str | None) -> CeremonyStart:
    require_ready()
    if not _admin_is_enabled(account):
        raise PasskeyError("account_unavailable")
    if active_credential_exists():
        raise PasskeyError("credential_exists")
    handle = _user_handle(account)
    challenge = secrets.token_bytes(32)
    options = _registration_options(account, handle, challenge)
    selector = _insert_flow("registration", account, handle, challenge, binding_hash)
    return CeremonyStart(selector=selector, options=options)


def begin_authentication() -> CeremonyStart:
    require_ready()
    credential = _active_credential()
    account = str(credential["account"])
    if not _admin_is_enabled(account):
        raise PasskeyError("account_unavailable")
    challenge = secrets.token_bytes(32)
    options = _authentication_options(challenge)
    selector = _insert_flow(
        "authentication", account, bytes(credential["user_handle"]), challenge, None,
    )
    return CeremonyStart(selector=selector, options=options)


def _verified_registration(payload: dict, challenge: bytes):
    api = _webauthn_modules()
    try:
        return api.verify_registration_response(
            credential=payload,
            expected_challenge=bytes(challenge),
            expected_rp_id=PASSKEY_RP_ID,
            expected_origin=PASSKEY_ORIGIN,
            require_user_verification=True,
        )
    except Exception as exc:
        raise PasskeyError("registration_rejected") from exc


def _verified_authentication(payload: dict, challenge: bytes, credential: sqlite3.Row):
    api = _webauthn_modules()
    try:
        return api.verify_authentication_response(
            credential=payload,
            expected_challenge=bytes(challenge),
            expected_rp_id=PASSKEY_RP_ID,
            expected_origin=PASSKEY_ORIGIN,
            credential_public_key=bytes(credential["public_key"]),
            credential_current_sign_count=int(credential["sign_count"]),
            require_user_verification=True,
        )
    except Exception as exc:
        raise PasskeyError("authentication_rejected") from exc


def _replace_session_tx(
    conn: sqlite3.Connection,
    account: str,
    credential_id: bytes,
    source_hash: str,
) -> tuple[str, str, tuple[str, ...]]:
    now = _now()
    revoked = tuple(
        str(row["token_hash"])
        for row in conn.execute(
            "SELECT token_hash FROM admin_sessions WHERE revoked_at IS NULL",
        ).fetchall()
    )
    conn.execute(
        """UPDATE admin_sessions SET revoked_at=?,revoked_reason='replaced'
           WHERE revoked_at IS NULL""",
        (now,),
    )
    token = secrets.token_urlsafe(48)
    session_id = _token_hash(token)
    conn.execute(
        """INSERT INTO admin_sessions(
               token_hash,account,credential_id,created_at,last_used_at,
               absolute_expires_at,revoked_at,revoked_reason,source_hash,reauthenticated_at
           ) VALUES(?,?,?,?,?,?,NULL,NULL,?,?)""",
        (
            session_id, account, credential_id, now, now,
            now + PASSKEY_SESSION_ABSOLUTE_AGE, source_hash, now,
        ),
    )
    return token, session_id, revoked


def finish_registration(
    selector: str,
    payload: dict,
    binding_hash: str | None,
    source_hash: str,
) -> AuthenticationSuccess:
    require_ready()
    flow = _claim_flow(selector, "registration", binding_hash)
    _check_finish_rate(str(flow["account"]))
    verified = _verified_registration(payload, bytes(flow["challenge"]))
    account = str(flow["account"])
    if not _admin_is_enabled(account):
        raise PasskeyError("account_unavailable")
    credential_id = bytes(verified.credential_id)
    public_key = bytes(verified.credential_public_key)
    transports = payload.get("response", {}).get("transports", [])
    if not isinstance(transports, list):
        transports = []
    transports = [str(item) for item in transports if isinstance(item, str)][:8]
    now = _now()
    try:
        with _transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM passkey_credentials WHERE revoked_at IS NULL",
            ).fetchone():
                raise PasskeyError("credential_exists")
            conn.execute(
                """INSERT INTO passkey_credentials(
                       account,user_handle,credential_id,public_key,sign_count,transports,
                       device_type,backed_up,aaguid,created_at,last_used_at,revoked_at,revoked_reason
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)""",
                (
                    account, bytes(flow["user_handle"]), credential_id, public_key,
                    int(verified.sign_count), json.dumps(transports),
                    str(getattr(verified.credential_device_type, "value", "")),
                    int(bool(verified.credential_backed_up)), str(verified.aaguid), now, now,
                ),
            )
            token, session_id, revoked = _replace_session_tx(
                conn, account, credential_id, source_hash,
            )
    except sqlite3.IntegrityError as exc:
        raise PasskeyError("credential_conflict") from exc
    return AuthenticationSuccess(account, token, session_id, revoked)


def _credential_for_assertion(payload: dict, flow: sqlite3.Row) -> sqlite3.Row:
    response = payload.get("response")
    if not isinstance(response, dict):
        raise PasskeyError("invalid_credential")
    user_handle = _b64url_decode(response.get("userHandle", ""))
    raw_id = _b64url_decode(payload.get("rawId", ""))
    if user_handle != bytes(flow["user_handle"]):
        raise PasskeyError("identity_mismatch")
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT * FROM passkey_credentials
               WHERE credential_id=? AND user_handle=? AND account=? AND revoked_at IS NULL""",
            (raw_id, user_handle, str(flow["account"])),
        ).fetchone()
        if not row:
            raise PasskeyError("credential_unavailable")
        return row
    finally:
        conn.close()


def finish_authentication(
    selector: str,
    payload: dict,
    source_hash: str,
) -> AuthenticationSuccess:
    require_ready()
    flow = _claim_flow(selector, "authentication")
    credential = _credential_for_assertion(payload, flow)
    _check_finish_rate(bytes(credential["credential_id"]))
    verified = _verified_authentication(payload, bytes(flow["challenge"]), credential)
    if bytes(verified.credential_id) != bytes(credential["credential_id"]):
        raise PasskeyError("identity_mismatch")
    account = str(credential["account"])
    if not _admin_is_enabled(account):
        raise PasskeyError("account_unavailable")
    old_count = int(credential["sign_count"])
    new_count = int(verified.new_sign_count)
    if new_count < 0 or (old_count > 0 and new_count <= old_count):
        raise PasskeyError("counter_rollback")
    now = _now()
    with _transaction() as conn:
        current = conn.execute(
            """SELECT sign_count FROM passkey_credentials
               WHERE credential_id=? AND revoked_at IS NULL""",
            (bytes(credential["credential_id"]),),
        ).fetchone()
        if not current:
            raise PasskeyError("credential_unavailable")
        current_count = int(current["sign_count"])
        if current_count != old_count and (current_count > 0 or new_count > 0):
            raise PasskeyError("counter_changed")
        conn.execute(
            """UPDATE passkey_credentials
               SET sign_count=?,last_used_at=?,device_type=?,backed_up=?
               WHERE credential_id=? AND revoked_at IS NULL""",
            (
                new_count, now,
                str(getattr(verified.credential_device_type, "value", "")),
                int(bool(verified.credential_backed_up)),
                bytes(credential["credential_id"]),
            ),
        )
        token, session_id, revoked = _replace_session_tx(
            conn, account, bytes(credential["credential_id"]), source_hash,
        )
    return AuthenticationSuccess(account, token, session_id, revoked)


def session_from_token(token: str, *, touch: bool = True) -> PasskeySession | None:
    if not PASSKEY_ENABLED or not token:
        return None
    session_id = _token_hash(token)
    now = _now()
    with _transaction() as conn:
        row = conn.execute(
            "SELECT * FROM admin_sessions WHERE token_hash=?", (session_id,),
        ).fetchone()
        if not row or row["revoked_at"] is not None:
            return None
        if not _admin_is_enabled(str(row["account"])):
            conn.execute(
                """UPDATE admin_sessions SET revoked_at=?,revoked_reason='account_unavailable'
                   WHERE token_hash=? AND revoked_at IS NULL""",
                (now, session_id),
            )
            return None
        expired = (
            int(row["absolute_expires_at"]) <= now
            or int(row["last_used_at"]) + PASSKEY_SESSION_IDLE_AGE <= now
        )
        if expired:
            conn.execute(
                """UPDATE admin_sessions SET revoked_at=?,revoked_reason='expired'
                   WHERE token_hash=? AND revoked_at IS NULL""",
                (now, session_id),
            )
            return None
        last_used = int(row["last_used_at"])
        if touch and now - last_used >= PASSKEY_SESSION_TOUCH_INTERVAL:
            conn.execute(
                "UPDATE admin_sessions SET last_used_at=? WHERE token_hash=? AND revoked_at IS NULL",
                (now, session_id),
            )
            last_used = now
        return PasskeySession(
            account=str(row["account"]),
            session_id=session_id,
            credential_id=bytes(row["credential_id"]),
            created_at=int(row["created_at"]),
            last_used_at=last_used,
            absolute_expires_at=int(row["absolute_expires_at"]),
        )


def session_from_request(request, *, touch: bool = True) -> PasskeySession | None:
    cookies = getattr(request, "cookies", {}) or {}
    return session_from_token(str(cookies.get(PASSKEY_SESSION_COOKIE, "")), touch=touch)


def session_id_from_request(request) -> str | None:
    session = session_from_request(request, touch=False)
    return session.session_id if session else None


def revoke_token(token: str, reason: str = "logout") -> str | None:
    if not token:
        return None
    session_id = _token_hash(token)
    now = _now()
    with _transaction() as conn:
        updated = conn.execute(
            """UPDATE admin_sessions SET revoked_at=?,revoked_reason=?
               WHERE token_hash=? AND revoked_at IS NULL""",
            (now, reason[:64], session_id),
        )
        return session_id if updated.rowcount == 1 else None


def revoke_request_session(request, reason: str = "logout") -> str | None:
    cookies = getattr(request, "cookies", {}) or {}
    return revoke_token(str(cookies.get(PASSKEY_SESSION_COOKIE, "")), reason)


def revoke_all_sessions(account: str, reason: str = "maintenance") -> tuple[str, ...]:
    now = _now()
    with _transaction() as conn:
        rows = conn.execute(
            "SELECT token_hash FROM admin_sessions WHERE account=? AND revoked_at IS NULL",
            (account,),
        ).fetchall()
        ids = tuple(str(row["token_hash"]) for row in rows)
        conn.execute(
            """UPDATE admin_sessions SET revoked_at=?,revoked_reason=?
               WHERE account=? AND revoked_at IS NULL""",
            (now, reason[:64], account),
        )
        return ids


def revoke_passkey(account: str, reason: str = "maintenance") -> tuple[int, tuple[str, ...]]:
    now = _now()
    with _transaction() as conn:
        sessions = tuple(
            str(row["token_hash"])
            for row in conn.execute(
                "SELECT token_hash FROM admin_sessions WHERE account=? AND revoked_at IS NULL",
                (account,),
            ).fetchall()
        )
        conn.execute(
            """UPDATE admin_sessions SET revoked_at=?,revoked_reason=?
               WHERE account=? AND revoked_at IS NULL""",
            (now, reason[:64], account),
        )
        updated = conn.execute(
            """UPDATE passkey_credentials SET revoked_at=?,revoked_reason=?
               WHERE account=? AND revoked_at IS NULL""",
            (now, reason[:64], account),
        )
        return updated.rowcount, sessions


def issue_registration_authorization(
    account: str,
    *,
    replace: bool = False,
) -> tuple[str, tuple[str, ...]]:
    if not _admin_is_enabled(account):
        raise PasskeyError("account_unavailable")
    now = _now()
    code = secrets.token_urlsafe(32)
    with _transaction() as conn:
        active = conn.execute(
            "SELECT 1 FROM passkey_credentials WHERE revoked_at IS NULL"
        ).fetchone()
        if active and not replace:
            raise PasskeyError("credential_exists")
        sessions: tuple[str, ...] = ()
        if replace:
            sessions = tuple(
                str(row["token_hash"])
                for row in conn.execute(
                    "SELECT token_hash FROM admin_sessions WHERE account=? AND revoked_at IS NULL",
                    (account,),
                ).fetchall()
            )
            conn.execute(
                """UPDATE admin_sessions SET revoked_at=?,revoked_reason='replacement_prepared'
                   WHERE account=? AND revoked_at IS NULL""",
                (now, account),
            )
            conn.execute(
                """UPDATE passkey_credentials SET revoked_at=?,revoked_reason='replacement_prepared'
                   WHERE account=? AND revoked_at IS NULL""",
                (now, account),
            )
        conn.execute(
            "DELETE FROM registration_authorizations WHERE expires_at<=? OR consumed_at IS NOT NULL",
            (now - PASSKEY_RECOVERY_MAX_AGE,),
        )
        conn.execute(
            """INSERT INTO registration_authorizations(
                   token_hash,account,created_at,expires_at,consumed_at
               ) VALUES(?,?,?,?,NULL)""",
            (_token_hash(code), account, now, now + PASSKEY_RECOVERY_MAX_AGE),
        )
        return code, sessions


def consume_registration_authorization(code: str) -> str:
    if not code:
        raise PasskeyError("authorization_missing")
    now = _now()
    with _transaction() as conn:
        row = conn.execute(
            "SELECT * FROM registration_authorizations WHERE token_hash=?",
            (_token_hash(code),),
        ).fetchone()
        if not row or row["consumed_at"] is not None or int(row["expires_at"]) <= now:
            raise PasskeyError("authorization_invalid")
        updated = conn.execute(
            """UPDATE registration_authorizations SET consumed_at=?
               WHERE token_hash=? AND consumed_at IS NULL AND expires_at>?""",
            (now, row["token_hash"], now),
        )
        if updated.rowcount != 1:
            raise PasskeyError("authorization_replayed")
        return str(row["account"])


def maintenance_status(account: str) -> dict:
    conn = _connect()
    try:
        credential = conn.execute(
            """SELECT credential_id,created_at,last_used_at FROM passkey_credentials
               WHERE account=? AND revoked_at IS NULL""",
            (account,),
        ).fetchone()
        sessions = conn.execute(
            """SELECT COUNT(*) AS count FROM admin_sessions
               WHERE account=? AND revoked_at IS NULL""",
            (account,),
        ).fetchone()
        return {
            "credential": bool(credential),
            "credential_fingerprint": (
                hashlib.sha256(bytes(credential["credential_id"])).hexdigest()[:12]
                if credential else ""
            ),
            "credential_created_at": int(credential["created_at"]) if credential else None,
            "credential_last_used_at": (
                int(credential["last_used_at"]) if credential and credential["last_used_at"] else None
            ),
            "active_sessions": int(sessions["count"]),
        }
    finally:
        conn.close()


__all__ = [
    "AuthenticationSuccess", "CeremonyStart", "PasskeyError", "PasskeySession",
    "PASSKEY_FLOW_COOKIE", "PASSKEY_SESSION_COOKIE", "active_credential_exists",
    "begin_authentication", "begin_registration", "configuration_error",
    "consume_registration_authorization", "cookie_binding", "finish_authentication",
    "finish_registration", "issue_registration_authorization", "maintenance_status",
    "revoke_all_sessions", "revoke_passkey", "revoke_request_session",
    "request_is_same_origin",
    "session_from_request", "session_from_token", "session_id_from_request",
    "source_fingerprint",
]
