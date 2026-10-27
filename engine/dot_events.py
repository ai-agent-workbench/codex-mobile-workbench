"""Single-principal, single-conversation MCP Events adapter.

Only committed mailbox messages are scanned. Replies never emit new events.
An explicitly enabled reply tool reuses the mailbox transaction.
No model invocation, native RPC, file tools, or public network listener.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import threading
import time
import uuid

from engine.dot_event_store import DotEventStore
from engine.dot_mailbox import (DotConversationService, DotScope, DotMailboxError,
                                DotMailboxDenied, DotMailboxConflict, MCP_REPLY_SOURCE, _identifier)
from engine.dot_webhooks import (RelayError, SecureWebhookPoster, callback_url, json_bytes,
                                 signing_key, webhook_headers)


EVENT = "dot.message.created"
DEFAULT_TTL_MS = 86_400_000


def _object(value, required, optional=()):
    if not isinstance(value,dict) or not set(required) <= set(value) or set(value)-set(required)-set(optional):
        raise RelayError(-32602,"request does not match the supported schema")
    return value


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp,timezone.utc).isoformat().replace("+00:00","Z")


@dataclass(frozen=True)
class RelayBinding:
    principal_id: str
    owner_account_id: str
    conversation_id: str
    parent_dot_ref: str
    callback_hosts: frozenset[str]

    def __post_init__(self):
        try:
            _identifier(self.principal_id,"principal_id")
            self.scope()
        except DotMailboxError as error:
            raise RelayError(-32602,"invalid relay binding") from error
        if (not isinstance(self.callback_hosts,frozenset)
                or len(self.callback_hosts) > 4 or any(not isinstance(h,str) or not h
                or h != h.lower() or h.endswith(".") or "*" in h for h in self.callback_hosts)):
            raise RelayError(-32602,"callback hosts must be a finite exact allowlist")
        for host in self.callback_hosts:
            callback_url("https://"+host+"/",self.callback_hosts)

    def scope(self):
        return DotScope(self.owner_account_id,self.conversation_id,self.parent_dot_ref)

    def identity(self):
        return {key:getattr(self,key) for key in
                ("principal_id","owner_account_id","conversation_id","parent_dot_ref")}


def event_definition():
    return {"name":EVENT,"description":"A new message was committed to the selected dot conversation.",
            "delivery":["webhook"],
            "inputSchema":{"type":"object","properties":{"conversation_id":{"type":"string"}},
                           "required":["conversation_id"],"additionalProperties":False},
            "payloadSchema":{"type":"object","properties":{
                "conversation_id":{"type":"string"},"message_id":{"type":"string"},
                "ordinal":{"type":"integer","minimum":1},"revision":{"type":"integer","minimum":1}},
                "required":["conversation_id","message_id","ordinal","revision"],"additionalProperties":False}}


class DotEventRelay:
    def __init__(self, mailbox_path, events_path, binding: RelayBinding, *, enabled=False,
                 authorizer=None, poster=None, clock=None, reply_conversation_id=None):
        self.binding = binding
        self.enabled = enabled is True
        self.authorizer = authorizer or (lambda principal: False)
        self.poster = poster or SecureWebhookPoster(binding.callback_hosts)
        self.clock = clock or time.time
        self.mailbox = DotConversationService(Path(mailbox_path),enabled=self.enabled,read_only=True)
        self.reply_conversation_id = reply_conversation_id
        self._reply_mailbox = None
        self._reply_lock = threading.RLock()
        self.events_path = Path(events_path)
        self._store = None
        self._store_lock = threading.RLock()

    @property
    def store(self):
        if not self.enabled:
            raise RelayError(-32012,"dot MCP relay is disabled")
        with self._store_lock:
            if self._store is None:
                self._store = DotEventStore(self.events_path,self.binding.identity())
            return self._store

    @property
    def reply_enabled(self):
        return self.enabled and self.reply_conversation_id == self.binding.conversation_id

    def close(self):
        self.mailbox.close()
        with self._reply_lock:
            if self._reply_mailbox is not None:
                self._reply_mailbox.close()
                self._reply_mailbox = None
        if self._store is not None:
            self._store.close()
            self._store = None

    def authorize(self, principal):
        if not self.enabled or principal != self.binding.principal_id or not self.authorizer(principal):
            raise RelayError(-32012,"relay access is disabled, revoked or outside the selected principal")
        try:
            conversation = self.mailbox.existing(self.binding.owner_account_id,self.binding.parent_dot_ref)
            if conversation is None or conversation["id"] != self.binding.conversation_id:
                raise RelayError(-32012,"conversation is outside the relay binding")
        except DotMailboxError as error:
            raise RelayError(-32012,"conversation is outside the relay binding") from error

    def read_message(self, principal, arguments):
        self.authorize(principal)
        _object(arguments,{"conversation_id"},{"message_id"})
        self._conversation(arguments["conversation_id"])
        try:
            return self.mailbox.read(self.binding.scope(),message_id=arguments.get("message_id"))
        except DotMailboxError as error:
            raise RelayError(-32012,"message is outside the relay binding") from error

    def snapshot(self, principal, arguments):
        self.authorize(principal)
        _object(arguments,{"conversation_id"})
        self._conversation(arguments["conversation_id"])
        return self.mailbox.public_snapshot(self.binding.conversation_id,self.binding.owner_account_id,
                                            parent_dot_ref=self.binding.parent_dot_ref)

    def reply_message(self, principal, arguments):
        self.authorize(principal)
        if not self.reply_enabled:
            raise RelayError(-32012,"direct reply is not granted for this conversation")
        _object(arguments,{"conversation_id","message_id","reply_id","text","expected_revision"})
        self._conversation(arguments["conversation_id"])
        values = {key:arguments[key] for key in ("message_id","reply_id","text","expected_revision")}
        scope = DotScope(self.binding.owner_account_id,self.binding.conversation_id,
                         self.binding.parent_dot_ref,frozenset({"reply"}))
        try:
            with self._reply_lock:
                # Keep all read tools on their original read-only connection.
                # Revalidate the existing grant before opening/using the writer.
                self.authorize(principal)
                if self._reply_mailbox is None:
                    self._reply_mailbox = DotConversationService(self.mailbox.path,enabled=True)
                reply = self._reply_mailbox.reply(scope,source=MCP_REPLY_SOURCE,**values)
            return {"backend":"dot_mailbox","conversation_id":self.binding.conversation_id,
                    "reply":reply}
        except DotMailboxConflict as error:
            raise RelayError(-32009,str(error),{"kind":"mailbox_conflict"}) from error
        except DotMailboxDenied as error:
            raise RelayError(-32012,"message is outside the authorized reply scope") from error
        except DotMailboxError as error:
            raise RelayError(-32602,str(error)) from error

    def _conversation(self, conversation_id):
        if conversation_id != self.binding.conversation_id:
            raise RelayError(-32012,"conversation is outside the relay binding")

    def _event_arguments(self, params):
        if not isinstance(params["name"],str):
            raise RelayError(-32602,"event name must be a string")
        if params["name"] != EVENT:
            raise RelayError(-32011,"unknown event",{"kind":"event"})
        _object(params["arguments"],{"conversation_id"})
        self._conversation(params["arguments"]["conversation_id"])

    def _subscription_id(self, params):
        key = [self.binding.principal_id,params["delivery"]["url"],params["name"],params["arguments"]]
        return "sub_dot_"+hashlib.sha256(json_bytes(key)).hexdigest()

    def subscribe(self, principal, params):
        self.authorize(principal)
        _object(params,{"name","arguments","delivery"},{"cursor","ttlMs","maxAgeMs"})
        self._event_arguments(params)
        delivery = _object(params["delivery"],{"mode","url","secret"})
        if delivery["mode"] != "webhook":
            raise RelayError(-32014,"only webhook delivery is supported",{"feature":"deliveryMode"})
        callback_url(delivery["url"],self.binding.callback_hosts)
        signing_key(delivery["secret"])
        if params.get("cursor") is not None or params.get("maxAgeMs") is not None:
            raise RelayError(-32014,"this relay does not advertise cursor replay",{"feature":"cursorReplay"})
        requested_ttl = params.get("ttlMs",DEFAULT_TTL_MS)
        if requested_ttl is not None and (type(requested_ttl) is not int or requested_ttl < 1):
            raise RelayError(-32602,"ttlMs must be a positive integer or null")
        ttl = min(requested_ttl or DEFAULT_TTL_MS,DEFAULT_TTL_MS)
        sid = self._subscription_id(params)
        challenge = secrets.token_urlsafe(32)
        body = json_bytes({"type":"verification","challenge":challenge})
        started = self.clock()
        headers = webhook_headers(sid,"msg_verification_"+uuid.uuid4().hex,body,delivery["secret"],started)
        response = self.poster.post(delivery["url"],body,headers)
        verified = None
        response_is_json = False
        try:
            verified = json.loads(response.body) if len(response.body) <= 4096 else None
            response_is_json = len(response.body)<=4096
            echoed = verified.get("challenge") if isinstance(verified,dict) else None
        except (ValueError,UnicodeError):
            echoed = None
        matches = False
        if isinstance(echoed,str):
            try:
                matches = hmac.compare_digest(echoed.encode("utf-8"),challenge.encode("ascii"))
            except UnicodeError:
                pass
        status = response.status if type(response.status) is int and 100<=response.status<=599 else None
        within_deadline = self.clock()-started<=10
        present = isinstance(verified,dict) and "challenge" in verified
        oversized = response.body_oversized or len(response.body)>4096
        result = ("http_status_rejected" if status is None or not 200<=status<300 else
                  "response_oversized" if oversized else
                  "response_not_json" if not response_is_json else
                  "response_not_object" if not isinstance(verified,dict) else
                  "challenge_missing" if not present else
                  "challenge_not_string" if not isinstance(echoed,str) else
                  "challenge_mismatch" if not matches else
                  "verification_deadline_exceeded" if not within_deadline else "verified")
        if result!="verified":
            # These are fixed classifications and booleans, never endpoint
            # content, nonce, signing material, URL or arbitrary error text.
            raise RelayError(-32015,"callback verification failed",{"reason":"challenge_failed",
                "verification":{"http_status":status,"content_type":response.content_type,
                                "response_is_json":response_is_json,
                                "response_is_object":isinstance(verified,dict),
                                "challenge_field_present":present,
                                "challenge_field_is_string":isinstance(echoed,str),
                                "challenge_matches":matches,"within_deadline":within_deadline,
                                "response_oversized":oversized,"result":result}})
        # Recheck a potentially revoked grant after the external round trip.
        self.authorize(principal)
        value = self.mailbox.mailbox.snapshot(self.binding.conversation_id,self.binding.owner_account_id,
                                             parent_dot_ref=self.binding.parent_dot_ref)
        head = max((m["ordinal"] for m in value["messages"]),default=0)
        now = self.clock()
        expires_at = now+ttl/1000
        self.store.subscribe(sid=sid,principal=principal,name=EVENT,
            arguments=json_bytes(params["arguments"]).decode("utf-8"),url=delivery["url"],
            secret=delivery["secret"],expires_at=expires_at,head=head,now=now)
        return {"id":sid,"refreshBefore":_iso(expires_at),"cursor":None,"truncated":False}

    def unsubscribe(self, principal, params):
        self.authorize(principal)
        _object(params,{"name","arguments","delivery"})
        self._event_arguments(params)
        delivery = _object(params["delivery"],{"url"},{"mode"})
        if delivery.get("mode","webhook") != "webhook":
            raise RelayError(-32014,"only webhook delivery is supported",{"feature":"deliveryMode"})
        callback_url(delivery["url"],self.binding.callback_hosts)
        # Idempotent even before any subscription; do not create a DB to cancel nothing.
        if self._store is not None or self.events_path.exists():
            self.store.unsubscribe(self._subscription_id(params),self.clock())
        return {}

    def scan(self):
        self.authorize(self.binding.principal_id)
        value = self.mailbox.mailbox.snapshot(self.binding.conversation_id,self.binding.owner_account_id,
                                             parent_dot_ref=self.binding.parent_dot_ref)
        return self.store.enqueue(value["messages"],self.binding.conversation_id,self.clock())

    def pump(self, *, limit=1):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid pump limit")
        self.scan()
        outcome = {"accepted":0,"retried":0,"abandoned":0,"skipped":0}
        for _ in range(limit):
            self.authorize(self.binding.principal_id)
            now = self.clock()
            job = self.store.claim(now)
            if job is None:
                break
            row = self.store.delivery(job,self.clock())
            if row is None:
                outcome["skipped"] += 1
                continue
            self.authorize(row["principal"])
            # Check once more immediately before the outbound call. A request
            # already in flight cannot be recalled when the user revokes access.
            row = self.store.delivery(job,self.clock())
            if row is None:
                outcome["skipped"] += 1
                continue
            previous = row["previous_secret"] if row["rotate_until"] > now else None
            headers = webhook_headers(row["subscription_id"],row["event_id"],row["body"],
                                      row["secret"],self.clock(),previous)
            try:
                response = self.poster.post(row["url"],row["body"],headers)
                status = response.status
                accepted = 200 <= status < 300
                retry = status == 429 or 500 <= status < 600
                reason = "accepted" if accepted else "http_transient" if retry else "http_rejected"
            except RelayError as error:
                accepted = False
                retry = error.code == -32015
                reason = "transport_unavailable" if retry else "callback_rejected"
            now = self.clock()
            retry = retry and row["attempts"] < 5 and now-row["first_at"] < 900
            result = "delivered" if accepted else "queued" if retry else "abandoned"
            self.store.finish(job,status=result,reason=reason,
                              next_at=now+min(2**row["attempts"],120) if retry else now)
            outcome["accepted" if accepted else "retried" if retry else "abandoned"] += 1
        return outcome
