import logging
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from app.services.assistant.repository import AssistantRepository
from .platform import Platform
from .store import ResourceError


logger = logging.getLogger(__name__)
Page = Literal["home", "inventory", "dashboards", "analysis", "finance", "data", "settings"]


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: Page
    title: str = Field(default="New conversation", min_length=1, max_length=100)


class MessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: Page
    content: str = Field(min_length=1, max_length=8000)


def assistant_access(workspace_id: str, request: Request) -> dict:
    service: Platform = request.app.state.platform
    return service.access(
        workspace_id,
        request.headers.get("Authorization"),
        request.headers.get("X-Workspace-Key"),
        write=False,
    )


def repository(request: Request) -> AssistantRepository:
    return AssistantRepository(request.app.state.store)


router = APIRouter(prefix="/api/prototype/workspaces/{workspace_id}/assistant", tags=["assistant"])


@router.get("/sessions")
def list_sessions(
    access: dict = Depends(assistant_access),
    repo: AssistantRepository = Depends(repository),
):
    return repo.list_sessions(access["workspace_id"], access["actor"])


@router.post("/sessions", status_code=201)
def create_session(
    body: SessionCreate,
    access: dict = Depends(assistant_access),
    repo: AssistantRepository = Depends(repository),
):
    return repo.create_session(access["workspace_id"], access["actor"], body.page, body.title)


@router.get("/sessions/{session_id}")
def get_session(
    session_id: str,
    access: dict = Depends(assistant_access),
    repo: AssistantRepository = Depends(repository),
):
    return repo.get_session(access["workspace_id"], access["actor"], session_id)


@router.post("/sessions/{session_id}/messages")
async def send_message(
    session_id: str,
    body: MessageCreate,
    request: Request,
    access: dict = Depends(assistant_access),
    repo: AssistantRepository = Depends(repository),
):
    workspace = request.app.state.platform.get_workspace(access)["workspace"]
    session = repo.add_message(
        access["workspace_id"], access["actor"], session_id,
        "user", body.content, body.page,
    )
    try:
        answer, sources = await request.app.state.assistant_service.answer(
            workspace, body.page, session["messages"]
        )
    except Exception as error:
        logger.exception("Samby Guide provider failure (%s)", type(error).__name__)
        raise ResourceError(502, "Samby Guide could not answer right now. Please try again.") from error
    return repo.add_message(
        access["workspace_id"], access["actor"], session_id,
        "assistant", answer, body.page, sources,
    )
