import asyncio
from contextlib import asynccontextmanager, suppress
import logging
import os
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from app.core.config import get_settings
from app.services.ai.openrouter import OpenRouterProvider
from app.services.assistant.service import AssistantService
from .engine import execute
from .assistant_routes import router as assistant_router
from .schema import DefinitionCreate, DefinitionPatch, InputError, RunCreate
from .store import ResourceError, Store
from .platform import Platform
from .platform_routes import namespace, router as platform_router


logger = logging.getLogger(__name__)


class ArchivePatch(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    archived: bool


async def worker_loop(store: Store, worker_id: str, poll_seconds: float, timeout_seconds: float):
    while True:
        await asyncio.to_thread(store.maintain)
        claimed = await asyncio.to_thread(store.claim, worker_id, timeout_seconds + 10)
        if claimed is None:
            await asyncio.sleep(poll_seconds)
            continue
        workspace_id, run = claimed
        try:
            config = run['config']
            dependency = await asyncio.to_thread(store.get_run, workspace_id, config['forecast_run_id']) if config.get('forecast_run_id') else None
            baseline = await asyncio.to_thread(store.get_run, workspace_id, config['baseline_run_id']) if config.get('baseline_run_id') else None
            if dependency and dependency['status'] != 'succeeded':
                raise InputError('The pinned forecast is not succeeded. It cannot supply numerical output.')
            result = await asyncio.wait_for(asyncio.to_thread(execute, run['kind'], config, run['snapshot'], run['id'], dependency, baseline), timeout=timeout_seconds)
            await asyncio.to_thread(store.finish, workspace_id, run['id'], worker_id, result)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            await asyncio.to_thread(store.finish, workspace_id, run['id'], worker_id, error='Execution exceeded the local worker time limit. Review the input size and retry as a new run.')
        except InputError as error:
            await asyncio.to_thread(store.finish, workspace_id, run['id'], worker_id, error=str(error))
        except Exception:
            logger.exception('Prototype analytical worker failed for run %s', run['id'])
            await asyncio.to_thread(store.finish, workspace_id, run['id'], worker_id, error='The local analytical worker encountered an unexpected error. The original inputs were retained. Check the service log and retry as a new run.')


def create_app(database_path: str | Path | None = None, start_worker: bool = True, poll_seconds: float = 0.1, timeout_seconds: float = 15) -> FastAPI:
    path = database_path or os.environ.get('SAMBY_PROTOTYPE_DB') or Path(__file__).parent / 'data' / 'samby.sqlite3'

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        store = Store(path)
        application.state.store = store
        application.state.platform = Platform(store)
        settings = get_settings()
        provider = OpenRouterProvider(settings)
        application.state.assistant_service = AssistantService(provider, settings.ai_context_max_tokens)
        worker_id = str(uuid4())
        await asyncio.to_thread(store.maintain)
        task = asyncio.create_task(worker_loop(store, worker_id, poll_seconds, timeout_seconds)) if start_worker else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                await asyncio.to_thread(store.release_worker, worker_id)
            await provider.close()

    application = FastAPI(title='Samby local analytical prototype', version='0.4.0', lifespan=lifespan)
    application.add_middleware(CORSMiddleware, allow_origins=[f'http://{host}:{port}' for host in ('localhost', '127.0.0.1') for port in (4173, 5173)], allow_methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'], allow_headers=['Content-Type', 'X-Workspace-ID', 'X-Workspace-Key', 'Authorization'], allow_credentials=False)
    application.include_router(platform_router)
    application.include_router(assistant_router)

    @application.exception_handler(ResourceError)
    async def resource_error(_request: Request, error: ResourceError):
        return JSONResponse({'detail': error.detail}, status_code=error.status)

    @application.exception_handler(InputError)
    async def input_error(_request: Request, error: InputError):
        return JSONResponse({'detail': str(error)}, status_code=422)

    @application.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, error: RequestValidationError):
        detail = '; '.join(f'{".".join(str(p) for p in issue["loc"] if p != "body")}: {issue["msg"]}' for issue in error.errors())
        return JSONResponse({'detail': detail}, status_code=422)

    def store(request: Request) -> Store:
        return request.app.state.store

    @application.get('/api/prototype/health')
    def health():
        return {'status': 'ok', 'mode': 'local-prototype', 'version': '0.4.0', 'persistence': 'sqlite', 'worker': start_worker}

    @application.post('/api/prototype/definitions', status_code=201)
    def create_definition(body: DefinitionCreate, request: Request, workspace: str = Depends(namespace), repository: Store = Depends(store)):
        request.app.state.platform.require_analysis(request.state.workspace_access, body.kind, body.config.model_dump())
        return repository.create_definition(workspace, body.model_dump())

    @application.get('/api/prototype/definitions')
    def list_definitions(include_archived: bool = False, archived: bool = False, workspace: str = Depends(namespace), repository: Store = Depends(store)):
        return repository.list_definitions(workspace, include_archived, archived)

    @application.get('/api/prototype/definitions/{definition_id}')
    def get_definition(definition_id: str, workspace: str = Depends(namespace), repository: Store = Depends(store)):
        return repository.get_definition(workspace, definition_id)

    @application.patch('/api/prototype/definitions/{definition_id}')
    def patch_definition(definition_id: str, body: DefinitionPatch, request: Request, workspace: str = Depends(namespace), repository: Store = Depends(store)):
        definition = repository.get_definition(workspace, definition_id)
        request.app.state.platform.require_analysis(request.state.workspace_access, definition['kind'], body.config.model_dump() if body.config else definition['config'])
        return repository.patch_definition(workspace, definition_id, body.model_dump(exclude_unset=True))

    @application.post('/api/prototype/definitions/{definition_id}/runs', status_code=202)
    def submit(definition_id: str, body: RunCreate, request: Request, workspace: str = Depends(namespace), repository: Store = Depends(store)):
        definition = repository.get_definition(workspace, definition_id)
        request.app.state.platform.require_analysis(request.state.workspace_access, definition['kind'], definition['config'])
        return repository.submit(workspace, definition_id, body.model_dump())

    @application.get('/api/prototype/runs')
    def list_runs(kind: str | None = None, status: str | None = None, question: str | None = None, include_archived: bool = False, archived: bool = False, workspace: str = Depends(namespace), repository: Store = Depends(store)):
        return repository.list_runs(workspace, kind, status, question, include_archived, archived)

    @application.get('/api/prototype/runs/{run_id}')
    def get_run(run_id: str, workspace: str = Depends(namespace), repository: Store = Depends(store)):
        return repository.get_run(workspace, run_id)

    @application.patch('/api/prototype/runs/{run_id}')
    def archive_run(run_id: str, body: ArchivePatch, request: Request, workspace: str = Depends(namespace), repository: Store = Depends(store)):
        run = repository.get_run(workspace, run_id)
        request.app.state.platform.require_analysis(request.state.workspace_access, run['kind'], run['config'])
        return repository.archive(workspace, run_id, body.archived)

    @application.post('/api/prototype/runs/{run_id}/cancel')
    def cancel_run(run_id: str, request: Request, workspace: str = Depends(namespace), repository: Store = Depends(store)):
        run = repository.get_run(workspace, run_id)
        request.app.state.platform.require_analysis(request.state.workspace_access, run['kind'], run['config'])
        return repository.cancel(workspace, run_id)

    def result_resource(repository: Store, workspace: str, run_id: str, key: str | None = None):
        run = repository.get_run(workspace, run_id)
        if run['status'] != 'succeeded':
            return JSONResponse({'run_id': run['id'], 'status': run['status'], 'phase': run['phase'], 'error': run['error']}, status_code=409 if run['status'] in {'failed', 'cancelled'} else 202)
        return run['result'][key] if key else run['result']

    @application.get('/api/prototype/runs/{run_id}/results')
    def results(run_id: str, workspace: str = Depends(namespace), repository: Store = Depends(store)):
        return result_resource(repository, workspace, run_id)

    @application.get('/api/prototype/runs/{run_id}/series')
    def series(run_id: str, workspace: str = Depends(namespace), repository: Store = Depends(store)):
        return result_resource(repository, workspace, run_id, 'series')

    @application.get('/api/prototype/runs/{run_id}/events')
    def events(run_id: str, workspace: str = Depends(namespace), repository: Store = Depends(store)):
        return result_resource(repository, workspace, run_id, 'events')

    @application.get('/api/prototype/runs/{run_id}/scene-manifest')
    def scene(run_id: str, workspace: str = Depends(namespace), repository: Store = Depends(store)):
        return result_resource(repository, workspace, run_id, 'scene_manifest')

    @application.get('/api/prototype/runs/{run_id}/transitions')
    def transitions(run_id: str, workspace: str = Depends(namespace), repository: Store = Depends(store)):
        return repository.transitions(workspace, run_id)

    return application


app = create_app()
