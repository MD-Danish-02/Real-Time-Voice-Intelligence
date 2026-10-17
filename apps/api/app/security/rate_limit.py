"""
Simple, real Redis-backed fixed-window rate limiter. Applied to
auth endpoints (tighter limit) and, via middleware, to the general API
surface per-tenant. Fails open (allows the request) if Redis is
unreachable, logging a warning, rather than taking the whole API down.
"""
import time

from fastapi import HTTPException, Request, status
from redis.asyncio import Redis

from app.core.config import settings
from app.core.logging import get_logger
from app.core.redis import get_redis

logger = get_logger("security.rate_limit")
def _get_redis() -> Redis:
    return get_redis()


async def enforce_rate_limit(*, key: str, limit_per_minute: int) -> None:
    window = int(time.time() // 60)
    redis_key = f"ratelimit:{key}:{window}"
    try:
        r = _get_redis()
        count = await r.incr(redis_key)
        if count == 1:
            await r.expire(redis_key, 65)
        if count > limit_per_minute:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={"error": {"code": "RATE_LIMITED", "message": "Too many requests - please slow down."}},
            )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - Redis outage must not break the API
        logger.warning("rate_limit_backend_unavailable", error=str(exc))


async def auth_rate_limit_dependency(request: Request) -> None:
    client_ip = request.client.host if request.client else "unknown"
    await enforce_rate_limit(key=f"auth:{client_ip}", limit_per_minute=settings.RATE_LIMIT_AUTH_PER_MINUTE)


async def api_rate_limit_middleware(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        auth = request.headers.get("authorization")
        ident = f"tok:{auth[-24:]}" if auth else f"ip:{request.client.host if request.client else 'unknown'}"
        try:
            await enforce_rate_limit(key=f"api:{ident}", limit_per_minute=settings.RATE_LIMIT_API_PER_MINUTE)
        except HTTPException as exc:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=exc.status_code, content=exc.detail, headers={"retry-after": "30"})
    return await call_next(request)
