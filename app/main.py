from contextlib import asynccontextmanager
import os

from app.prototype.main import create_app
from app.prototype.platform_routes import router as platform_router


def create_integrated_app(database_url=None, start_worker=True):
    application = create_app(database_url, start_worker=start_worker)
    application.title = 'Samby API'
    application.description = 'Durable business workspaces, authenticated access, reviewed file intake and asynchronous analytics.'
    for route in platform_router.routes:
        if route.path.startswith('/api/prototype/auth/'):
            application.add_api_route(route.path.replace('/api/prototype', '/api/v1', 1), route.endpoint, methods=route.methods, status_code=route.status_code)
    if os.environ.get('SAMBY_ENABLE_LEGACY_TEMPLATE') == '1':
        from app.api.routes import ai, events, examples, health
        from app.db.session import engine
        from app.services.ai.factory import close_ai_provider
        from app.services.redis import close_redis

        platform_lifespan = application.router.lifespan_context

        @asynccontextmanager
        async def lifespan(app):
            async with platform_lifespan(app):
                try:
                    yield
                finally:
                    await close_ai_provider()
                    await close_redis()
                    await engine.dispose()

        application.router.lifespan_context = lifespan
        for prefix, router in (('health', health.router), ('examples', examples.router), ('events', events.router), ('ai', ai.router)):
            application.include_router(router, prefix=f'/api/v1/{prefix}')
    return application


app = create_integrated_app()
