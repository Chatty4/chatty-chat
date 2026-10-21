from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.core import CoreClient
from app.core.exceptions import Unauthorized
from app.core.security import decode_access_token
from app.db.session import get_session
from app.schemas.core import Memberships
from app.services.access_service import AccessService
from app.services.health_service import HealthService

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_redis(request: Request) -> Redis:
    return request.app.state.redis


RedisDep = Annotated[Redis, Depends(get_redis)]


def get_health_service(session: SessionDep, redis: RedisDep) -> HealthService:
    return HealthService(session, redis)


def get_core_client(request: Request, redis: RedisDep) -> CoreClient:
    return CoreClient(request.app.state.core_http, redis)


def get_access_service(
    core: Annotated[CoreClient, Depends(get_core_client)],
) -> AccessService:
    return AccessService(core)


AccessServiceDep = Annotated[AccessService, Depends(get_access_service)]

bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    access: AccessServiceDep,
) -> Memberships:
    if credentials is None:
        raise Unauthorized()
    return await access.get_user(decode_access_token(credentials.credentials))


CurrentUser = Annotated[Memberships, Depends(get_current_user)]
