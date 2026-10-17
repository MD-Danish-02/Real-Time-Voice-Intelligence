"""Redis client accessor. One client per running event loop (redis.asyncio clients are loop-bound),
which is one client in production and avoids cross-loop reuse in tests / Celery's asyncio.run tasks."""
import asyncio
import weakref

from redis.asyncio import Redis

from app.core.config import settings

_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Redis]" = weakref.WeakKeyDictionary()


def get_redis() -> Redis:
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None:
        client = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        _clients[loop] = client
    return client
