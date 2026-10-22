from fastapi import APIRouter

from app.api.routers import ws

api_router = APIRouter()
api_router.include_router(ws.router)
