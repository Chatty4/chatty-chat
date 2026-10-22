import asyncio
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.api.deps import AccessServiceDep, ConnectionsDep
from app.core.exceptions import Unauthorized, UserInactive
from app.core.security import decode_access_token

router = APIRouter()

# api-chat.md, GET /ws, close codes
TOKEN_EXPIRED = 4001
USER_INACTIVE = 4003


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    access: AccessServiceDep,
    connections: ConnectionsDep,
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

    connections.add(user.user_id, websocket)
    try:
        async with asyncio.timeout(claims.expires_at - time.time()):
            while True:
                await websocket.receive_text()  # CHAT-184 and CHAT-185 handle the messages
    except TimeoutError:
        await websocket.close(TOKEN_EXPIRED, "token_expired")
    except WebSocketDisconnect:
        pass
    finally:
        connections.remove(user.user_id, websocket)
