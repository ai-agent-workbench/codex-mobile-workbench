"""Validation and compatibility policy for native structured user input.

This is intentionally isolated from prompt construction: requestUserInput is a
native App Server protocol exchange, not text injected into a model prompt.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from engine.codex_app_server import AppServerProtocolError


HOST_DEFAULT_OPTIONAL_WAIT_MS = 60_000
# Secrets must not leave an offline-verifiable password/PIN hash in SQLite.
# This key lives only in this Web process. A secret-command retry after a
# process restart cannot be authenticated and is conservatively rejected.
_SECRET_RECEIPT_KEY = secrets.token_bytes(32)


def rpc_identity(value: Any) -> tuple[str, str]:
    """Keep JSON-RPC numeric and string IDs distinct (and reject bool/null)."""
    if isinstance(value, str) and value:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")), "string"
    if isinstance(value, int) and not isinstance(value, bool):
        return json.dumps(value, separators=(",", ":")), "integer"
    raise AppServerProtocolError("requestUserInput JSON-RPC id is invalid")


def deadline_for_auto_resolution(*, is_blocking: bool, auto_resolution_ms: Any) -> tuple[str | None, str | None]:
    """Return the native interval or bounded host wait for nonblocking input.

    ``autoResolutionMs`` is deprecated by the current schema.  We retain its
    documented host-auto-resolution compatibility where an explicit,
    non-negative integer is supplied. Current native evidence also permits a
    bounded Web-owned wait for explicitly nonblocking null/missing intervals;
    blocking requests remain fail-closed.
    """
    if auto_resolution_ms is None:
        if not is_blocking:
            return (
                (datetime.now(timezone.utc) + timedelta(milliseconds=HOST_DEFAULT_OPTIONAL_WAIT_MS)).isoformat(),
                "host_optional_default",
            )
        return None, None
    if isinstance(auto_resolution_ms, bool) or not isinstance(auto_resolution_ms, int) or auto_resolution_ms < 0:
        raise AppServerProtocolError("requestUserInput autoResolutionMs is invalid")
    if is_blocking:
        return None, None
    now = datetime.now(timezone.utc)
    try:
        return (now + timedelta(milliseconds=auto_resolution_ms)).isoformat(), "native_interval"
    except OverflowError as error:
        raise AppServerProtocolError("requestUserInput autoResolutionMs is out of range") from error


def normalize_request(params: Any) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise AppServerProtocolError("requestUserInput params are invalid")
    thread_id, turn_id, item_id = (params.get(name) for name in ("threadId", "turnId", "itemId"))
    if not all(isinstance(value, str) and value for value in (thread_id, turn_id, item_id)):
        raise AppServerProtocolError("requestUserInput mapping is invalid")
    raw = params.get("questions")
    if not isinstance(raw, list) or not raw:
        raise AppServerProtocolError("requestUserInput questions are invalid")
    top_blocking = params.get("isBlocking")
    if not isinstance(top_blocking, bool):
        raise AppServerProtocolError("requestUserInput isBlocking is invalid")
    top_auto = params.get("autoResolutionMs")
    normalized: list[dict[str, Any]] = []
    ids: set[str] = set()
    for value in raw:
        if not isinstance(value, dict):
            raise AppServerProtocolError("requestUserInput question is invalid")
        question_id = value.get("id")
        header = value.get("header")
        question = value.get("question")
        if not isinstance(question_id, str) or not question_id or question_id in ids:
            raise AppServerProtocolError("requestUserInput question IDs are invalid")
        if not isinstance(header, str) or not isinstance(question, str) or not header.strip() or not question.strip():
            raise AppServerProtocolError("requestUserInput question text is invalid")
        options = value.get("options")
        if options is None:
            options = []
        if not isinstance(options, list):
            raise AppServerProtocolError("requestUserInput options are invalid")
        labels: set[str] = set()
        safe_options: list[dict[str, str]] = []
        for option in options:
            if not isinstance(option, dict) or not isinstance(option.get("label"), str):
                raise AppServerProtocolError("requestUserInput option is invalid")
            label = option["label"].strip()
            description = option.get("description", "")
            if not label or label in labels or not isinstance(description, str):
                raise AppServerProtocolError("requestUserInput option is invalid")
            labels.add(label)
            safe_options.append({"label": label, "description": description})
        is_other = value.get("isOther", False)
        is_secret = value.get("isSecret", False)
        if not isinstance(is_other, bool) or not isinstance(is_secret, bool):
            raise AppServerProtocolError("requestUserInput flags are invalid")
        ids.add(question_id)
        normalized.append({
            "id": question_id, "header": header, "question": question,
            "options": safe_options, "isOther": is_other, "isSecret": is_secret,
            "isBlocking": top_blocking,
        })
    is_blocking = top_blocking
    auto_resolution_ms = top_auto
    deadline_at, deadline_source = deadline_for_auto_resolution(
        is_blocking=is_blocking, auto_resolution_ms=auto_resolution_ms
    )
    canonical = json.dumps({
        "questions": normalized, "isBlocking": is_blocking,
        "autoResolutionMs": auto_resolution_ms,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "thread_id": thread_id, "turn_id": turn_id, "native_item_id": item_id,
        "questions": normalized, "is_blocking": is_blocking,
        "deadline_at": deadline_at,
        "deadline_source": deadline_source,
        "fingerprint": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def validate_answers(questions: list[dict[str, Any]], answers: Any, *, allow_empty: bool = False) -> dict[str, str]:
    if not isinstance(answers, dict):
        raise AppServerProtocolError("question answers are invalid")
    expected = {item["id"]: item for item in questions if isinstance(item, dict)}
    if set(answers) != set(expected):
        raise AppServerProtocolError("question answer IDs are invalid")
    result: dict[str, str] = {}
    for question_id, question in expected.items():
        value = answers[question_id]
        if not isinstance(value, str):
            raise AppServerProtocolError("question answer is invalid")
        selected = value.strip()
        labels = {item["label"] for item in question["options"]}
        # A native question without offered options is a text question. It is
        # not silently converted into a choice question by the Web layer.
        if labels and selected not in labels and not question.get("isOther"):
            raise AppServerProtocolError("question answer is not an offered option")
        if not selected and not allow_empty:
            raise AppServerProtocolError("question answer is empty")
        result[question_id] = value
    return result


def recommended_answers(questions: list[dict[str, Any]]) -> dict[str, str]:
    """Build the host recommendation used when a native optional wait expires.

    The App Server supplies the choices.  The host only uses their first,
    already marked-as-recommended position; a free-text-only question keeps an
    empty answer so it is not invented by the Web layer.
    """
    answers: dict[str, str] = {}
    for question in questions:
        question_id = question.get("id")
        options = question.get("options")
        if not isinstance(question_id, str):
            raise AppServerProtocolError("question recommendation is invalid")
        if isinstance(options, list) and options:
            first = options[0]
            if not isinstance(first, dict) or not isinstance(first.get("label"), str):
                raise AppServerProtocolError("question recommendation is invalid")
            answers[question_id] = first["label"]
        else:
            answers[question_id] = ""
    return validate_answers(questions, answers, allow_empty=True)


def answer_digest(answers: dict[str, str], *, contains_secret: bool = False) -> str:
    encoded = json.dumps(answers, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if contains_secret:
        return hmac.new(_SECRET_RECEIPT_KEY, encoded, hashlib.sha256).hexdigest()
    return hashlib.sha256(encoded).hexdigest()
