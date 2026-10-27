"""Only a synthetic ASGI fixture; no real model, state, notifier or service unit."""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from contextlib import asynccontextmanager
from fastapi import FastAPI
import uvicorn
from engine.conversation_store import ConversationStore
from engine.conversation_runtime import ConversationRuntime
from engine.conversation_service import ConversationService
from shared.conversation_artifacts import ConversationArtifactService
from test_conversation_runtime import FakeFactory
from routes.maintenance_admission import router
import engine.conversation_service as service_module

root = Path(os.environ['FIXTURE_ROOT'])


@asynccontextmanager
async def lifespan(app):
    store = ConversationStore(root / 'conversations.sqlite3')
    artifacts = ConversationArtifactService(store, root / 'artifacts')
    runtime = ConversationRuntime(store, artifacts, FakeFactory())
    service = ConversationService(store, artifacts, runtime, enable_automations=False)
    service_module._service = service
    client=await runtime._ensure_client()  # fake transport; forces real close DB paths.
    from unittest.mock import AsyncMock
    client.list_loaded_thread_ids=AsyncMock(return_value=[])
    try:
        yield
    finally:
        await service.close()
        service_module._service = None
        (root / 'shutdown-finished').write_text('graceful', encoding='utf-8')


app = FastAPI(lifespan=lifespan)
app.include_router(router)

if __name__ == '__main__':
    uvicorn.run(app, fd=int(os.environ['FIXTURE_SOCKET_FD']), log_level='warning',
                timeout_graceful_shutdown=5, proxy_headers=False)
