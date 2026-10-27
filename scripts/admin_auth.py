#!/usr/bin/env python3
"""Local-only maintenance for the single administrator Passkey/session."""
import argparse
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.passkey_auth import (  # noqa: E402
    PasskeyError,
    issue_registration_authorization,
    maintenance_status,
    revoke_all_sessions,
    revoke_passkey,
)
from shared.security import audit, load_accounts  # noqa: E402


def _admin_account() -> str:
    admins = [
        name for name, entry in load_accounts().items()
        if entry.get("role") == "admin" and entry.get("enabled")
    ]
    if len(admins) != 1:
        raise PasskeyError("single_admin_required")
    return admins[0]


def _audit(event: str, account: str, detail: str = "") -> None:
    request = SimpleNamespace(client=SimpleNamespace(host="local-maintenance"))
    audit(request, event, detail, account=account)


def main() -> int:
    parser = argparse.ArgumentParser(description="管理员 Passkey 本机维护")
    parser.add_argument(
        "action",
        choices=("status", "revoke-session", "revoke-passkey", "prepare-replacement",
                 "authorize-registration"),
    )
    args = parser.parse_args()
    try:
        account = _admin_account()
        if args.action == "status":
            status = maintenance_status(account)
            print("Passkey：" + ("已绑定" if status["credential"] else "未绑定"))
            if status["credential_fingerprint"]:
                print("凭据指纹：" + status["credential_fingerprint"])
            print("有效管理员会话：" + str(status["active_sessions"]))
            return 0
        if args.action == "revoke-session":
            session_ids = revoke_all_sessions(account, "local_maintenance")
            _audit("passkey_sessions_revoked", account, "count=" + str(len(session_ids)))
            print("已撤销管理员会话：" + str(len(session_ids)))
            return 0
        if args.action == "revoke-passkey":
            count, session_ids = revoke_passkey(account, "local_maintenance")
            _audit(
                "passkey_credential_revoked", account,
                "credential_count=" + str(count) + " session_count=" + str(len(session_ids)),
            )
            print("已撤销 Passkey：" + str(count))
            print("已撤销管理员会话：" + str(len(session_ids)))
            return 0
        replace = args.action == "prepare-replacement"
        code, session_ids = issue_registration_authorization(account, replace=replace)
        _audit(
            "passkey_registration_authorized", account,
            "replacement=" + ("yes" if replace else "no")
            + " session_count=" + str(len(session_ids)),
        )
        print("一次性本机恢复码（仅显示本次，有效 10 分钟）：")
        print(code)
        return 0
    except PasskeyError as exc:
        print("操作未完成：" + exc.code, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
