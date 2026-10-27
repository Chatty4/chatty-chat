from typing import Annotated

from fastapi import Depends, WebSocket
from fastapi.requests import HTTPConnection
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.core import CoreClient
from app.core.exceptions import Unauthorized
from app.core.security import decode_access_token
from app.db.session import get_session
from app.repositories import (
    ChannelCounterRepository,
    ChannelEventRepository,
    MessageRepository,
    ReadStateRepository,
)
from app.schemas.core import Memberships
from app.services.access_service import AccessService
from app.services.connection_registry import ConnectionRegistry
from app.services.event_bus import EventBus
from app.services.gateway import Gateway
from app.services.health_service import HealthService
from app.services.message_service import MessageService

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_redis(conn: HTTPConnection) -> Redis:
    return conn.app.state.redis


RedisDep = Annotated[Redis, Depends(get_redis)]


def get_health_service(session: SessionDep, redis: RedisDep) -> HealthService:
    return HealthService(session, redis)


def get_core_client(conn: HTTPConnection, redis: RedisDep) -> CoreClient:
    return CoreClient(conn.app.state.core_http, redis)


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
    return await access.get_user(decode_access_token(credentials.credentials).user_id)


CurrentUser = Annotated[Memberships, Depends(get_current_user)]


def get_event_bus(redis: RedisDep) -> EventBus:
    return EventBus(redis)


EventBusDep = Annotated[EventBus, Depends(get_event_bus)]


def get_connections(conn: HTTPConnection) -> ConnectionRegistry[WebSocket]:
    return conn.app.state.connections


ConnectionsDep = Annotated[ConnectionRegistry[WebSocket], Depends(get_connections)]


def get_message_service(
    session: SessionDep, access: AccessServiceDep, bus: EventBusDep
) -> MessageService:
    return MessageService(
        session,
        MessageRepository(session),
        ChannelCounterRepository(session),
        ChannelEventRepository(session),
        ReadStateRepository(session),
        access,
        bus,
    )


MessageServiceDep = Annotated[MessageService, Depends(get_message_service)]


def get_gateway(conn: HTTPConnection) -> Gateway:
    return conn.app.state.gateway


GatewayDep = Annotated[Gateway, Depends(get_gateway)]
