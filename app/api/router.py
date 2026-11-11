from fastapi import APIRouter

from app.api.routers import messages, pins, presence, push, reactions, read_state, sync, ws

api_router = APIRouter()
api_router.include_router(messages.router)
api_router.include_router(pins.router)
api_router.include_router(push.router)
api_router.include_router(reactions.router)
api_router.include_router(sync.router)
api_router.include_router(ws.router)
api_router.include_router(presence.router)
api_router.include_router(read_state.router)
