import json
import logging
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from app.services.assistant.repository import AssistantRepository
from app.services.assistant.service import AssistantService

from .platform import Platform
from .store import ResourceError, Store


logger = logging.getLogger(__name__)
Page = Literal["home", "insights", "inventory", "dashboards", "analysis", "finance", "data", "settings"]


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: Page
    title: str = Field(default="New conversation", min_length=1, max_length=80)


class MessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: Page
    content: str = Field(min_length=1, max_length=4000)


def assistant_access(workspace_id: str, request: Request) -> dict:
    service: Platform = request.app.state.platform
    return service.access(
        workspace_id,
        request.headers.get("Authorization"),
        request.headers.get("X-Workspace-Key"),
        write=False,
    )


def repository(request: Request) -> AssistantRepository:
    store: Store = request.app.state.store
    return AssistantRepository(store)


def assistant(request: Request) -> AssistantService:
    return request.app.state.assistant_service


router = APIRouter(prefix="/api/prototype/workspaces/{workspace_id}/assistant", tags=["assistant"])


@router.get("/sessions")
def list_sessions(access: dict = Depends(assistant_access), repo: AssistantRepository = Depends(repository)):
    return repo.list(access["workspace_id"], access["actor"])


@router.post("/sessions", status_code=201)
def create_session(body: SessionCreate, access: dict = Depends(assistant_access), repo: AssistantRepository = Depends(repository)):
    title = body.title.strip()
    if not title:
        raise ResourceError(422, "Give the conversation a title.")
    return repo.create(access["workspace_id"], access["actor"], title, body.page)


@router.get("/sessions/{session_id}")
def get_session(session_id: str, access: dict = Depends(assistant_access), repo: AssistantRepository = Depends(repository)):
    return repo.get(access["workspace_id"], access["actor"], session_id)


@router.post("/sessions/{session_id}/messages")
async def create_message(
    session_id: str,
    body: MessageCreate,
    access: dict = Depends(assistant_access),
    repo: AssistantRepository = Depends(repository),
    guide: AssistantService = Depends(assistant),
):
    content = body.content.strip()
    if not content:
        raise ResourceError(422, "Write a question for Samby Guide.")
    repo.add_message(access["workspace_id"], access["actor"], session_id, "user", content, body.page)
    with repo.store.connection() as db:
        row = db.execute("SELECT document FROM platform_workspaces WHERE id=?", (access["workspace_id"],)).fetchone()
    workspace = json.loads(row["document"])
    history = repo.get(access["workspace_id"], access["actor"], session_id)["messages"]
    try:
        answer, sources = await guide.answer(workspace, body.page, history)
    except Exception as error:
        logger.exception("Samby guide failed: %s", type(error).__name__)
        raise ResourceError(502, "Samby Guide could not answer right now. Your question was saved; try again shortly.") from error
    repo.add_message(access["workspace_id"], access["actor"], session_id, "assistant", answer, body.page, sources)
    return repo.get(access["workspace_id"], access["actor"], session_id)
