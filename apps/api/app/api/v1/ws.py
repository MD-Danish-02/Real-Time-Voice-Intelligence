import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from jose import JWTError

from app.core.security import decode_token
from app.events.bus import subscribe
import uuid

router = APIRouter(tags=["realtime"])


@router.websocket("/ws/events")
async def events_ws(websocket: WebSocket, token: str):
    """Live dashboard events. Tenant comes from the verified token, never from the client."""
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            raise JWTError("wrong type")
        tenant_id = uuid.UUID(payload["tenant_id"])
    except Exception:  # noqa: BLE001
        await websocket.close(code=4401)
        return
    await websocket.accept()
    pubsub = await subscribe(tenant_id)
    try:
        while True:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg and msg.get("data"):
                await websocket.send_text(msg["data"])
            await asyncio.sleep(0.05)
    except WebSocketDisconnect:
        pass
    finally:
        await pubsub.unsubscribe()
        await pubsub.close()
