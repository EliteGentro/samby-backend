from fastapi import APIRouter

from app.api.routes import ai, auth, events, examples, health

api_router = APIRouter()
api_router.include_router(health.router, prefix="/health", tags=["health"])
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(examples.router, prefix="/examples", tags=["REST example"])
api_router.include_router(events.router, prefix="/events", tags=["SSE example"])
api_router.include_router(ai.router, prefix="/ai", tags=["AI"])
