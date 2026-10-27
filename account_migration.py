"""
Account migration: called once at startup.
Converts legacy totp_secret + totp_enabled → multi-account accounts.json.
Safe to call every startup — returns immediately if accounts.json already exists.
"""
import json
from pathlib import Path

from shared.config import ACCOUNT, ACCOUNTS_FILE, ENABLED_FILE, SECRET_FILE, STATE, MANAGED_INSTALL


def migrate_accounts():
    if ACCOUNTS_FILE.exists():
        return
    if MANAGED_INSTALL or (not SECRET_FILE.exists() and not ENABLED_FILE.exists()):
        # No legacy identity exists to migrate. Creating a disabled fixed
        # account here would make genuine first-time setup unreachable.
        return
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        totp_secret = SECRET_FILE.read_text(encoding="ascii").strip()
    except OSError:
        totp_secret = ""
    enabled = ENABLED_FILE.exists()
    accounts = {
        ACCOUNT: {
            "role": "admin",
            "totp": totp_secret,
            "enabled": enabled,
        }
    }
    ACCOUNTS_FILE.write_text(json.dumps(accounts, ensure_ascii=False, indent=2), encoding="utf-8")
    ACCOUNTS_FILE.chmod(0o600)
