"""Conversation-owned files published by App Server items and tools.

Browsers receive only opaque artifact IDs.  A source file is copied once into
private managed storage, so later previews never expose or depend on an
arbitrary machine path.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import stat
import unicodedata
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote

from engine.codex_app_server import AppServerProtocolError
from engine.conversation_store import ConversationStore, utc_now
from shared.config import STATE


ARTIFACT_MAX_BYTES = 100 * 1024 * 1024
ARTIFACT_MAX_COUNT_PER_CONVERSATION = 200
ARTIFACT_MAX_TOTAL_BYTES_PER_CONVERSATION = 1024 * 1024 * 1024
TEXT_PREVIEW_MAX_BYTES = 256 * 1024
_TEXT_SUFFIXES = {
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".log",
}
_HTML_SUFFIXES = {".html", ".htm"}


def publish_artifact_dynamic_tool() -> dict:
    """The one small Web-owned tool exposed to App Server threads."""
    return {
        "type": "function",
        "name": "publish_artifact",
        "description": (
            "Publish a user-facing file in the current conversation after creating it. "
            "Use this for finished images, documents, spreadsheets, PDFs, Markdown, "
            "text, archives, source files, or other local files the user should preview "
            "or download. The source may be any regular file readable by the service "
            "user. Only report it as published when this tool returns success=true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path of the finished local file.",
                },
                "title": {
                    "type": "string",
                    "description": "Optional short display title.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    }


class ConversationArtifactService:
    def __init__(
        self,
        store: ConversationStore,
        storage_root: str | os.PathLike[str] | None = None,
        *,
        max_count_per_conversation: int = ARTIFACT_MAX_COUNT_PER_CONVERSATION,
        max_total_bytes_per_conversation: int = (
            ARTIFACT_MAX_TOTAL_BYTES_PER_CONVERSATION
        ),
    ):
        self.store = store
        self.root = Path(storage_root or (STATE / "conversation-artifacts"))
        self.max_count_per_conversation = int(max_count_per_conversation)
        self.max_total_bytes_per_conversation = int(
            max_total_bytes_per_conversation
        )
        if (
            self.max_count_per_conversation < 1
            or self.max_total_bytes_per_conversation < 1
        ):
            raise ValueError("artifact quotas must be positive")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)

    def publish(
        self,
        conversation_id: str,
        *,
        turn_id: str | None,
        source_path: str,
        idempotency_key: str,
        origin: str,
        title: str | None = None,
        native_item_id: str | None = None,
    ) -> dict:
        existing = self.store.artifact_for_key(conversation_id, idempotency_key)
        if existing is not None:
            return existing
        self.store.get_conversation(conversation_id)
        source, source_identity = self._validated_source(source_path)
        name = self._safe_name(title or source.name, source.suffix)
        # Store IDs deliberately start with an alphanumeric character.  Hex
        # also avoids the rare leading '-'/'_' values produced by token_urlsafe.
        artifact_id = secrets.token_hex(18)
        directory = self.root / conversation_id / artifact_id
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        directory.chmod(0o700)
        destination = directory / name
        try:
            digest, size, prefix = self._copy(
                source, destination, source_identity
            )
            mime_type, preview_kind = self._classify(name, prefix, destination)
            record = self.store.insert_artifact(
                {
                    "id": artifact_id,
                    "conversation_id": conversation_id,
                    "turn_id": turn_id,
                    "native_item_id": native_item_id,
                    "idempotency_key": idempotency_key,
                    "name": name,
                    "mime_type": mime_type,
                    "size": size,
                    "sha256": digest,
                    "storage_relpath": destination.relative_to(self.root).as_posix(),
                    "preview_kind": preview_kind,
                    "origin": origin,
                    "created_at": utc_now(),
                },
                max_count=self.max_count_per_conversation,
                max_total_bytes=self.max_total_bytes_per_conversation,
            )
            if record["id"] != artifact_id:
                # A concurrent replay won the unique idempotency key.  Keep
                # the durable winner and discard this private staging copy.
                destination.unlink(missing_ok=True)
                directory.rmdir()
        except BaseException:
            try:
                destination.unlink(missing_ok=True)
                directory.rmdir()
            except OSError:
                pass
            raise
        return record

    def path_for(self, record: dict) -> Path:
        relative = record.get("storage_relpath")
        if not isinstance(relative, str):
            raise AppServerProtocolError("artifact storage record is invalid")
        root = self.root.resolve()
        path = (root / relative).resolve()
        if path == root or root not in path.parents:
            raise AppServerProtocolError("artifact storage path escaped its root")
        try:
            info = path.lstat()
        except OSError as error:
            raise AppServerProtocolError("artifact file is unavailable") from error
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise AppServerProtocolError("artifact file is not regular")
        if info.st_size != int(record.get("size") or -1):
            raise AppServerProtocolError("artifact file size changed")
        return path

    def text_preview(self, record: dict) -> str:
        if record.get("preview_kind") != "text":
            raise AppServerProtocolError("artifact is not text-previewable")
        path = self.path_for(record)
        with path.open("rb") as handle:
            data = handle.read(TEXT_PREVIEW_MAX_BYTES + 1)
        truncated = len(data) > TEXT_PREVIEW_MAX_BYTES
        text = data[:TEXT_PREVIEW_MAX_BYTES].decode("utf-8", errors="replace")
        if truncated:
            text += "\n\n……预览已截断，请下载查看完整文件。"
        return text

    @staticmethod
    def html_previewable(record: dict) -> bool:
        """Accept new HTML records and pre-upgrade managed .html artifacts."""
        kind = record.get("preview_kind")
        suffix = Path(str(record.get("name") or "")).suffix.lower()
        return kind == "html" or (kind == "download" and suffix in _HTML_SUFFIXES)

    def delete_conversation(self, conversation_id: str) -> None:
        if (
            not isinstance(conversation_id, str)
            or not conversation_id
            or Path(conversation_id).name != conversation_id
        ):
            raise AppServerProtocolError("conversation artifact directory is invalid")
        root = self.root.resolve()
        directory = (root / conversation_id).resolve()
        if directory.parent != root:
            raise AppServerProtocolError("conversation artifact directory escaped its root")
        if directory.exists():
            shutil.rmtree(directory)

    def _validated_source(
        self, path_value: str
    ) -> tuple[Path, tuple[int, int]]:
        if not isinstance(path_value, str) or not path_value:
            raise AppServerProtocolError("artifact path is required")
        candidate = Path(path_value)
        if not candidate.is_absolute():
            raise AppServerProtocolError("artifact path must be absolute")
        try:
            raw_info = candidate.lstat()
            if stat.S_ISLNK(raw_info.st_mode):
                raise AppServerProtocolError("artifact source cannot be a symlink")
            candidate = candidate.resolve(strict=True)
            info = candidate.lstat()
        except AppServerProtocolError:
            raise
        except OSError as error:
            raise AppServerProtocolError("artifact source is unavailable") from error
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise AppServerProtocolError("artifact source must be a regular file")
        if info.st_size > ARTIFACT_MAX_BYTES:
            raise AppServerProtocolError("artifact exceeds the size limit")
        return candidate, (info.st_dev, info.st_ino)

    @staticmethod
    def _safe_name(value: str, source_suffix: str) -> str:
        raw = unicodedata.normalize("NFKC", str(value or "")).strip()
        raw = Path(raw).name
        raw = "".join(character for character in raw if ord(character) >= 32)
        raw = re.sub(r"[\\/:*?\"<>|]+", "-", raw).strip(" .")
        if not raw:
            raw = "artifact" + source_suffix.lower()
        # ``title`` is a display label, while the source suffix is the
        # authoritative file format.  Path.suffix cannot decide whether a
        # title already has an extension: version labels such as ``v0.8 草图``
        # are otherwise mistaken for one and lose their real ``.html`` suffix.
        if source_suffix and not raw.lower().endswith(source_suffix.lower()):
            raw += source_suffix.lower()
        suffix = Path(raw).suffix[:20]
        stem_limit = max(1, 160 - len(suffix))
        return Path(raw).stem[:stem_limit].rstrip(" .") + suffix

    @staticmethod
    def _copy(
        source: Path,
        destination: Path,
        expected_identity: tuple[int, int],
    ) -> tuple[str, int, bytes]:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(source, flags)
        digest = hashlib.sha256()
        size = 0
        prefix = b""
        try:
            source_info = os.fstat(descriptor)
            if not stat.S_ISREG(source_info.st_mode):
                raise AppServerProtocolError("artifact source must be regular")
            if (source_info.st_dev, source_info.st_ino) != expected_identity:
                raise AppServerProtocolError(
                    "artifact source changed before it could be copied"
                )
            out = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "rb", closefd=False) as source_handle:
                    with os.fdopen(out, "wb") as destination_handle:
                        while True:
                            chunk = source_handle.read(1024 * 1024)
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > ARTIFACT_MAX_BYTES:
                                raise AppServerProtocolError("artifact exceeds the size limit")
                            if len(prefix) < 512:
                                prefix += chunk[: 512 - len(prefix)]
                            digest.update(chunk)
                            destination_handle.write(chunk)
                        destination_handle.flush()
                        os.fsync(destination_handle.fileno())
            except BaseException:
                try:
                    os.close(out)
                except OSError:
                    pass
                raise
        finally:
            os.close(descriptor)
        return digest.hexdigest(), size, prefix

    @staticmethod
    def _classify(name: str, prefix: bytes, path: Path) -> tuple[str, str]:
        lower = name.lower()
        if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png", "image"
        if prefix.startswith(b"\xff\xd8\xff"):
            return "image/jpeg", "image"
        if prefix.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif", "image"
        if prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
            return "image/webp", "image"
        if prefix.startswith(b"%PDF-"):
            return "application/pdf", "pdf"
        if Path(lower).suffix in _HTML_SUFFIXES:
            return "text/html", "html"
        if Path(lower).suffix in _TEXT_SUFFIXES:
            try:
                with path.open("rb") as handle:
                    sample = handle.read(TEXT_PREVIEW_MAX_BYTES)
                if b"\x00" not in sample:
                    sample.decode("utf-8")
                    mime = mimetypes.guess_type(name)[0] or "text/plain"
                    return mime, "text"
            except (OSError, UnicodeDecodeError):
                pass
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        # SVG and other active text formats remain downloads. HTML is only
        # rendered by the dedicated sandboxed preview endpoint.
        if mime.startswith("text/"):
            mime = "text/plain"
        return mime, "download"


def tool_result(success: bool, payload: dict) -> dict:
    """App Server consumes envelope status; models see only contentItems."""
    return {
        "contentItems": [{
            "type": "inputText",
            "text": json.dumps({**payload, "success": success}, ensure_ascii=False),
        }],
        "success": success,
    }


def tool_success(artifact: dict) -> dict:
    base = ("/conversation/" + quote(str(artifact["conversation_id"]), safe="")
            + "/artifact/" + quote(str(artifact["id"]), safe=""))
    return tool_result(True, {
        "operation": "publish_artifact", "artifact_id": artifact["id"],
        "conversation_id": artifact["conversation_id"], "name": artifact["name"],
        "size": artifact["size"], "sha256": artifact["sha256"],
        "content_url": base, "download_url": base + "?download=1",
    })


def tool_failure(message: str) -> dict:
    # Shared by non-artifact tools too; avoid mislabelling title/task failures.
    return tool_result(False, {"error": str(message)[:300]})
