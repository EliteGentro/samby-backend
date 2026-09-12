from typing import Any
import asyncio

from fastapi import APIRouter, Depends, File, Form, Header, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.auth import LoginRequest, RegisterRequest, normalize_email
from .imports import preview
from .platform import Platform
from .store import ResourceError


class WorkspaceCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    workspace: dict[str, Any]


class WorkspaceUpdate(WorkspaceCreate):
    expected_revision: int = Field(ge=0, strict=True)


class MemberCreate(BaseModel):
    email: str
    role: str

    @field_validator('email')
    @classmethod
    def email_valid(cls, value: str):
        return normalize_email(value)


class MemberUpdate(BaseModel):
    role: str


def platform(request: Request) -> Platform:
    return request.app.state.platform


def signed_in(request: Request, service: Platform = Depends(platform)):
    return service.user(request.headers.get('Authorization'))


def path_access(workspace_id: str, request: Request, service: Platform = Depends(platform)):
    return service.access(workspace_id, request.headers.get('Authorization'), request.headers.get('X-Workspace-Key'), request.method not in {'GET', 'HEAD', 'OPTIONS'})


def namespace(request: Request, x_workspace_id: str = Header(alias='X-Workspace-ID'), service: Platform = Depends(platform)) -> str:
    access = service.access(x_workspace_id, request.headers.get('Authorization'), request.headers.get('X-Workspace-Key'), request.method not in {'GET', 'HEAD', 'OPTIONS'})
    request.state.workspace_access = access
    return x_workspace_id


router = APIRouter(prefix='/api/prototype')


@router.post('/auth/register', status_code=201)
def register(body: RegisterRequest, service: Platform = Depends(platform)):
    return service.register(body.email, body.password, body.name)


@router.post('/auth/login')
def login(body: LoginRequest, request: Request, service: Platform = Depends(platform)):
    return service.login(body.email, body.password, request.client.host if request.client else 'local')


@router.get('/auth/me')
def me(user: dict = Depends(signed_in)):
    return user


@router.post('/auth/logout', status_code=204)
def logout(request: Request, service: Platform = Depends(platform)):
    service.logout(request.headers.get('Authorization'))
    return Response(status_code=204)


@router.post('/workspaces/guest', status_code=201)
def guest_create(body: WorkspaceCreate, request: Request, service: Platform = Depends(platform)):
    return service.create_workspace(body.workspace, guest_key=request.headers.get('X-Workspace-Key'))


@router.post('/workspaces', status_code=201)
def create_workspace(body: WorkspaceCreate, user: dict = Depends(signed_in), service: Platform = Depends(platform)):
    return service.create_workspace(body.workspace, user=user)


@router.get('/workspaces')
def list_workspaces(include_archived: bool = False, user: dict = Depends(signed_in), service: Platform = Depends(platform)):
    return service.list_workspaces(user, include_archived)


@router.get('/workspaces/{workspace_id}')
def get_workspace(access: dict = Depends(path_access), service: Platform = Depends(platform)):
    return service.get_workspace(access)


@router.put('/workspaces/{workspace_id}')
def update_workspace(body: WorkspaceUpdate, access: dict = Depends(path_access), service: Platform = Depends(platform)):
    return service.put_workspace(access, body.workspace, body.expected_revision)


@router.get('/workspaces/{workspace_id}/export')
def export_workspace(access: dict = Depends(path_access), service: Platform = Depends(platform)):
    envelope = service.get_workspace(access)
    return JSONResponse(envelope['workspace'], headers={'Content-Disposition': f'attachment; filename="samby-{access["workspace_id"]}-revision-{envelope["revision"]}.json"'})


@router.delete('/workspaces/{workspace_id}')
def archive_workspace(access: dict = Depends(path_access), service: Platform = Depends(platform)):
    return service.archive(access)


@router.post('/workspaces/{workspace_id}/restore')
def restore_workspace(workspace_id: str, request: Request, service: Platform = Depends(platform)):
    access = service.access(workspace_id, request.headers.get('Authorization'), request.headers.get('X-Workspace-Key'))
    return service.archive(access, False)


@router.post('/workspaces/{workspace_id}/claim')
def claim(workspace_id: str, request: Request, user: dict = Depends(signed_in), service: Platform = Depends(platform)):
    return service.claim(workspace_id, user, request.headers.get('X-Workspace-Key'))


@router.get('/workspaces/{workspace_id}/members')
def members(access: dict = Depends(path_access), service: Platform = Depends(platform)):
    return service.members(access)


@router.post('/workspaces/{workspace_id}/members')
def add_member(body: MemberCreate, access: dict = Depends(path_access), service: Platform = Depends(platform)):
    return service.member_change(access, body.role, email=body.email)


@router.patch('/workspaces/{workspace_id}/members/{user_id}')
def update_member(user_id: str, body: MemberUpdate, access: dict = Depends(path_access), service: Platform = Depends(platform)):
    return service.member_change(access, body.role, user_id=user_id)


@router.delete('/workspaces/{workspace_id}/members/{user_id}')
def remove_member(user_id: str, access: dict = Depends(path_access), service: Platform = Depends(platform)):
    return service.member_change(access, None, user_id=user_id)


@router.post('/imports/preview')
async def import_preview(file: UploadFile = File(...), sheet_name: str | None = Form(default=None), workspace: str = Depends(namespace)):
    content = await file.read(5_000_001)
    return await asyncio.to_thread(preview, file.filename or '', content, sheet_name)


@router.post('/workspaces/{workspace_id}/imports/preview')
async def workspace_import_preview(file: UploadFile = File(...), sheet_name: str | None = Form(default=None), access: dict = Depends(path_access)):
    content = await file.read(5_000_001)
    return await asyncio.to_thread(preview, file.filename or '', content, sheet_name)
