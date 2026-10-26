import asyncio
import time
from collections.abc import AsyncGenerator

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.api.deps import AccessServiceDep, ConnectionsDep, EventBusDep
from app.core.exceptions import Unauthorized, UserInactive
from app.core.security import decode_access_token
from app.schemas.events import ChannelEvent
from app.schemas.ws import Heartbeat

router = APIRouter()

# api-chat.md, GET /ws, close codes
TOKEN_EXPIRED = 4001
HEARTBEAT_TIMEOUT = 4002
USER_INACTIVE = 4003

HEARTBEAT_TIMEOUT_SECONDS = 60


async def forward_events(websocket: WebSocket, events: AsyncGenerator[ChannelEvent]) -> None:
    # shortcut: if Redis drops, this socket gets no more events until the client reconnects;
    # close it with 1011 instead once CHAT-185 owns the subscription.
    async for event in events:
        await websocket.send_json({"type": "event", "event": event.model_dump(mode="json")})


async def handle_message(websocket: WebSocket, raw: str) -> bool:
    """Returns True for a valid heartbeat. CHAT-215 refreshes the presence key here."""
    try:
        heartbeat = Heartbeat.model_validate_json(raw)
    except ValidationError:
        return False  # invalid JSON, not an object, unknown type, bad field: ignored
    websocket.state.visible = heartbeat.visible
    websocket.state.last_heartbeat_at = asyncio.get_running_loop().time()
    return True


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    access: AccessServiceDep,
    connections: ConnectionsDep,
    bus: EventBusDep,
    token: str = "",
) -> None:
    # Accept first: a close before accept reaches the browser as 1006, without our code.
    await websocket.accept()
    try:
        claims = decode_access_token(token)
        user = await access.get_user(claims.user_id)
    except Unauthorized:
        await websocket.close(TOKEN_EXPIRED, "token_expired")
        return
    except UserInactive:
        await websocket.close(USER_INACTIVE, "user_inactive")
        return

    channel_ids = [channel.channel_id for channel in user.channels]
    # SUBSCRIBE with no channel names is a Redis error, so no channels means no task.
    forward = (
        asyncio.create_task(forward_events(websocket, bus.subscribe(channel_ids)))
        if channel_ids
        else None
    )

    loop = asyncio.get_running_loop()
    token_deadline = loop.time() + (claims.expires_at - time.time())
    heartbeat_deadline = loop.time() + HEARTBEAT_TIMEOUT_SECONDS

    connections.add(user.user_id, websocket)
    try:
        async with asyncio.timeout_at(min(token_deadline, heartbeat_deadline)) as timeout:
            while True:
                raw = await websocket.receive_text()
                if await handle_message(websocket, raw):
                    heartbeat_deadline = (
                        websocket.state.last_heartbeat_at + HEARTBEAT_TIMEOUT_SECONDS
                    )
                    timeout.reschedule(min(token_deadline, heartbeat_deadline))
    except TimeoutError:
        if token_deadline <= heartbeat_deadline:
            await websocket.close(TOKEN_EXPIRED, "token_expired")
        else:
            await websocket.close(HEARTBEAT_TIMEOUT, "heartbeat_timeout")
    except WebSocketDisconnect:
        pass
    finally:
        if forward is not None:
            forward.cancel()
        connections.remove(user.user_id, websocket)
