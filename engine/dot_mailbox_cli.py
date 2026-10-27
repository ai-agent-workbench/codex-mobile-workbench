#!/usr/bin/env python3
"""Restricted local pull/reply transport for one explicitly selected dot mailbox.

Production access defaults off and additionally requires an owner-only scope
file. This code never provisions that persistent grant or any credential.
It is not an MCP endpoint or a ChatGPT conversation API.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from engine.dot_mailbox import DotConversationService, DotMailboxDenied, DotMailboxError, DotScope
from shared.config import (STATE, DOT_CONVERSATION_ENABLED, DOT_LOCAL_RELAY_ENABLED,
    DOT_LOCAL_RELAY_SCOPE, DOT_MAILBOX_DB, DOT_OWNER_ACCOUNT_ID, DOT_PARENT_REF)


def load_scope(path, *, isolated_test=False):
    requested = Path(path).absolute()
    if isolated_test:
        if requested.name != "isolated-scope.json":
            raise DotMailboxDenied("only isolated-scope.json is accepted in test mode")
    else:
        if not DOT_CONVERSATION_ENABLED or not DOT_LOCAL_RELAY_ENABLED:
            raise DotMailboxDenied("production local relay is disabled")
        if os.name != "posix" or requested != DOT_LOCAL_RELAY_SCOPE.absolute() or requested.is_symlink():
            raise DotMailboxDenied("production requires the configured Linux scope file")
    path = Path(path).resolve(strict=True)
    if not isolated_test:
        if path.parent != DOT_LOCAL_RELAY_SCOPE.parent.resolve():
            raise DotMailboxDenied("scope directory does not match")
        for item in (path,path.parent):
            metadata=item.stat()
            if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode)&0o077:
                raise DotMailboxDenied("scope file and directory must be private to the current OS user")
    if not path.is_file() or path.stat().st_size > 8192:
        raise DotMailboxDenied("invalid isolated scope file")
    value = json.loads(path.read_text(encoding="utf-8"))
    keys = {"schema_version","environment","database","owner_account_id",
            "conversation_id","parent_dot_ref","operations"}
    if not isolated_test:
        keys.add("enabled")
    if not isinstance(value,dict) or set(value) != keys:
        raise DotMailboxDenied("invalid isolated scope shape")
    environment = "isolated-prototype" if isolated_test else "production-dot"
    if type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["environment"] != environment:
        raise DotMailboxDenied("scope version or environment mismatch")
    production_root = STATE.resolve()
    if isolated_test and (path.parent == production_root or production_root in path.parents):
        raise DotMailboxDenied("the prototype CLI refuses the production state directory")
    if value["database"] != "dot-mailbox.sqlite3":
        raise DotMailboxDenied("database must be next to the isolated scope file")
    operations = value["operations"]
    if not isinstance(operations,list) or not all(isinstance(op,str) for op in operations):
        raise DotMailboxDenied("invalid scope operations")
    if not isolated_test:
        if value["enabled"] is not True or value["owner_account_id"] != DOT_OWNER_ACCOUNT_ID or value["parent_dot_ref"] != DOT_PARENT_REF:
            raise DotMailboxDenied("scope is revoked or owner/parent binding changed")
        if DOT_MAILBOX_DB.is_symlink():
            raise DotMailboxDenied("production mailbox symlinks are denied")
    database = ((path.parent / value["database"]) if isolated_test else DOT_MAILBOX_DB).resolve(strict=True)
    if isolated_test and database.parent != path.parent:
        raise DotMailboxDenied("database symlink escapes the isolated scope directory")
    # A test grant is a scope selector under existing OS authority, not a new
    # bearer credential or authenticated machine principal.
    return database, DotScope(value["owner_account_id"],value["conversation_id"],
                              value["parent_dot_ref"],frozenset(operations))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--isolated-test",action="store_true")
    parser.add_argument("--scope")
    commands = parser.add_subparsers(dest="command",required=True)
    read = commands.add_parser("read")
    read.add_argument("--conversation-id",required=True)
    read.add_argument("--message-id")
    reply = commands.add_parser("reply")
    reply.add_argument("--conversation-id",required=True)
    reply.add_argument("--message-id",required=True)
    reply.add_argument("--reply-id",required=True)
    reply.add_argument("--expected-revision",type=int,required=True)
    args = parser.parse_args(argv)
    service = None
    try:
        if args.isolated_test and not args.scope:
            raise DotMailboxDenied("isolated mode requires an explicit scope selector")
        database, scope = load_scope(args.scope or DOT_LOCAL_RELAY_SCOPE,isolated_test=args.isolated_test)
        if args.conversation_id != scope.conversation_id:
            raise DotMailboxDenied("conversation is outside the selected scope")
        scope.require(args.command)
        service = DotConversationService(database,enabled=True,read_only=args.command == "read")
        if args.command == "read":
            result = service.read(scope,message_id=args.message_id)
        else:
            # No shell evaluation, prompt execution or generated model reply.
            text = sys.stdin.read(60_001)
            result = service.reply(scope,message_id=args.message_id,reply_id=args.reply_id,
                                   text=text,expected_revision=args.expected_revision)
        print(json.dumps(result,ensure_ascii=False))
        return 0
    except (DotMailboxError,OSError,ValueError,TypeError,sqlite3.Error) as error:
        print(json.dumps({"error":{"code":getattr(error,"code","invalid_request"),
                                 "message":str(error) if isinstance(error,DotMailboxError) else "dot scope or mailbox unavailable"}},ensure_ascii=False))
        return 2
    finally:
        if service is not None:
            service.close()


if __name__ == "__main__":
    raise SystemExit(main())
