"""Signed upload-to-composer context, always rechecked against current scope."""
from pathlib import Path

from engine.codex_app_server import AppServerProtocolError
from shared.message_attachments import MessageAttachmentService, public_attachment
from shared.security import confirmation_signer, logged_in


def upload_scope(request, *, conversation_id=None, project_id=None):
    from engine.conversation_service import conversation_service
    owner = logged_in(request) if hasattr(request, "cookies") else None
    if conversation_id:
        service = conversation_service()
        conversation = service.store.get_conversation(conversation_id)
        if conversation.get("owner_account_id") not in {None, owner}:
            raise AppServerProtocolError("无权向此对话添加附件。")
        project_id = conversation.get("project_id")
        if project_id:
            import projects
            directory = projects.upload_directory(project_id)
        else:
            directory = conversation["cwd"]
        kind, scope_id = "conversation", conversation_id
    else:
        import projects
        directory = projects.upload_directory(project_id)
        kind, scope_id = "project", project_id
    return MessageAttachmentService.scope(owner=owner, kind=kind,
                                         scope_id=scope_id, directory=directory)


def upload_context(request, *, conversation_id=None, project_id=None):
    # Older rendering/unit doubles have no authenticated account; do not grant
    # them a scope capability or instantiate a production registry.
    owner = logged_in(request) if hasattr(request, "cookies") else None
    if not owner:
        return "", None
    scope = upload_scope(request, conversation_id=conversation_id, project_id=project_id)
    return confirmation_signer().dumps({"purpose": "message_upload", **scope}), scope


def validated_scope(request, token, directory):
    # This is a signed scope label, not a bearer authorization or overwrite
    # approval. It cannot outlive/change the current login, owner or directory.
    # Do not expire ordinary uploads merely because a composer sat open: the
    # separate destructive confirmation still uses its existing 1-hour window.
    payload = confirmation_signer().loads(token)
    if not isinstance(payload, dict) or payload.get("purpose") != "message_upload":
        raise AppServerProtocolError("附件上传凭证无效。")
    kind = payload.get("kind")
    if kind not in {"conversation", "project"}:
        raise AppServerProtocolError("附件上传范围无效。")
    scope = upload_scope(request, **{
        "conversation_id" if kind == "conversation" else "project_id": payload.get("id"),
    })
    if payload != {"purpose": "message_upload", **scope} or scope["directory"] != str(Path(directory).resolve()):
        raise AppServerProtocolError("附件上传所属账号、项目或目录已变化，请返回页面重新上传。")
    return scope


def initial_attachments(service, batch_id, scope):
    if not scope or not isinstance(batch_id, str) or not batch_id:
        return []
    return [public_attachment(record) for record in service.attachments.batch(batch_id, scope=scope)]
