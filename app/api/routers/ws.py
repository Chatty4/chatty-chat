import asyncio
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.api.deps import AccessServiceDep, GatewayDep
from app.core.codes import CloseCode
from app.core.exceptions import Unauthorized, UserInactive
from app.core.security import decode_access_token
from app.schemas.ws import ClientMessage, Heartbeat, Unwatch, Watch

router = APIRouter()

HEARTBEAT_TIMEOUT_SECONDS = 60


async def handle_message(websocket: WebSocket, raw: str) -> bool:
    """Returns True for a valid heartbeat. CHAT-215 refreshes the presence key here."""
    try:
        message = ClientMessage.validate_json(raw)
    except ValidationError:
        return False  # invalid JSON, not an object, unknown type, bad field: ignored
    match message:
        case Heartbeat():
            websocket.state.visible = message.visible
            websocket.state.last_heartbeat_at = asyncio.get_running_loop().time()
            return True
        case Watch() | Unwatch():  # CHAT-231 replaces this
            await websocket.send_json(
                {
                    "type": "error",
                    "code": "cannot_watch",
                    "message": "channel is not public in your teams",
                }
            )
    return False


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    access: AccessServiceDep,
    gateway: GatewayDep,
    token: str = "",
) -> None:
    # Accept first: a close before accept reaches the browser as 1006, without our code.
    await websocket.accept()
    try:
        claims = decode_access_token(token)
        user = await access.get_user(claims.user_id)
    except Unauthorized:
        await websocket.close(CloseCode.TOKEN_EXPIRED, "token_expired")
        return
    except UserInactive:
        await websocket.close(CloseCode.USER_INACTIVE, "user_inactive")
        return

    loop = asyncio.get_running_loop()
    token_deadline = loop.time() + (claims.expires_at - time.time())
    heartbeat_deadline = loop.time() + HEARTBEAT_TIMEOUT_SECONDS

    await gateway.connect(user.user_id, websocket, [c.channel_id for c in user.channels])
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
            await websocket.close(CloseCode.TOKEN_EXPIRED, "token_expired")
        else:
            await websocket.close(CloseCode.HEARTBEAT_TIMEOUT, "heartbeat_timeout")
    except WebSocketDisconnect:
        pass
    finally:
        await gateway.disconnect(user.user_id, websocket)
