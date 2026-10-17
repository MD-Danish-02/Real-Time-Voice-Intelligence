"""Redis pub/sub event bus. Channels are per-tenant so WebSocket subscribers only ever see their own tenant's events."""
import json
import uuid
from datetime import datetime, timezone

from redis.asyncio import Redis

from app.core.redis import get_redis

from app.core.logging import get_logger

logger = get_logger("events.bus")

EVENT_TYPES = {
    "CALL_CREATED", "CALL_QUEUED", "CALL_DIALING", "CALL_RINGING", "CALL_ANSWERED", "CALL_STARTED",
    "SPEECH_STARTED", "SPEECH_ENDED", "USER_INTERRUPTED", "AI_RESPONSE_STARTED", "AI_RESPONSE_COMPLETED",
    "TOOL_CALLED", "MEETING_REQUESTED", "TRANSFER_REQUESTED", "CALL_ENDED", "CALL_FAILED",
}


def _r() -> Redis:
    return get_redis()


def channel_for(tenant_id: uuid.UUID) -> str:
    return f"events:{tenant_id}"


async def publish_event(tenant_id: uuid.UUID, event_type: str, payload: dict | None = None) -> None:
    message = json.dumps({"type": event_type, "payload": payload or {},
                          "at": datetime.now(timezone.utc).isoformat()})
    try:
        await _r().publish(channel_for(tenant_id), message)
    except Exception as exc:  # noqa: BLE001 - Redis outage must not fail the request
        logger.warning("event_publish_failed", event_type=event_type, error=str(exc))


async def subscribe(tenant_id: uuid.UUID):
    pubsub = _r().pubsub()
    await pubsub.subscribe(channel_for(tenant_id))
    return pubsub
