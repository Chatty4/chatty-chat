from fastapi import APIRouter

from app.api.routers import messages, sync, ws

api_router = APIRouter()
api_router.include_router(messages.router)
api_router.include_router(sync.router)
api_router.include_router(ws.router)
