from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.security.rate_limit import api_rate_limit_middleware

from app.api.v1 import api_router
from app.api.v1 import ws as ws_module
from app.core.config import settings
from app.core.errors import RequestIDMiddleware, register_exception_handlers
from app.core.logging import configure_logging
from app.db.session import async_engine

REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "status"])
LATENCY = Histogram("http_request_seconds", "HTTP request latency", ["path"])


import asyncio

@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.DEBUG)
    if settings.ENV == "production":
        assert settings.JWT_SECRET != "CHANGE_ME_DEV_ONLY", "JWT_SECRET must be set in production"
        assert not settings.ALLOW_DEMO_SEED, "ALLOW_DEMO_SEED must be false in production"
        assert settings.AUTH_REFRESH_COOKIE_SECURE, "AUTH_REFRESH_COOKIE_SECURE must be true in production"
        assert settings.SMTP_HOST and settings.SMTP_FROM_EMAIL, "SMTP_HOST and SMTP_FROM_EMAIL must be configured in production"
        assert settings.SMTP_USE_STARTTLS, "SMTP_USE_STARTTLS must be true in production"
        for name in ("CRM", "TELEPHONY", "CALENDAR", "WHATSAPP", "STT", "LLM", "TTS"):
            assert getattr(settings, f"{name}_PROVIDER_MODE") != "mock", f"{name} mock provider not allowed in production"

    # Start background ARI listener if Asterisk is reachable
    ari_task = None

    async def _start_ari_listener():
        try:
            from app.telephony.asterisk.ari_client import AsteriskARIClient, run_event_loop
            from app.telephony.asterisk.event_handler import handle_ari_event
            ari = AsteriskARIClient()
            if await ari.ping():
                await run_event_loop(handle_ari_event)
        except Exception:
            pass

    ari_task = asyncio.create_task(_start_ari_listener())
    try:
        yield
    finally:
        if ari_task and not ari_task.done():
            ari_task.cancel()


app = FastAPI(title=settings.APP_NAME, version="1.0.0", lifespan=lifespan,
              description="Multi-tenant AI Voice Automation Platform API.")
app.add_middleware(BaseHTTPMiddleware, dispatch=api_rate_limit_middleware)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=settings.CORS_ORIGINS, allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])
register_exception_handlers(app)


@app.middleware("http")
async def security_headers_mw(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(self), camera=()"
    if not settings.DEBUG:
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


@app.middleware("http")
async def metrics_mw(request, call_next):
    with LATENCY.labels(request.url.path).time():
        response = await call_next(request)
    REQUESTS.labels(request.method, response.status_code).inc()
    return response


app.include_router(api_router, prefix=settings.API_V1_PREFIX)
app.include_router(ws_module.router)


@app.get("/health", tags=["ops"])
async def health():
    return {"status": "ok"}


@app.get("/ready", tags=["ops"])
async def ready():
    checks = {}
    try:
        async with async_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception:  # noqa: BLE001
        checks["database"] = False
    try:
        from redis.asyncio import Redis
        await Redis.from_url(settings.REDIS_URL).ping()
        checks["redis"] = True
    except Exception:  # noqa: BLE001
        checks["redis"] = False
    ok = all(checks.values())
    return Response(status_code=200 if ok else 503, media_type="application/json",
                    content=__import__("json").dumps({"ready": ok, "checks": checks}))


@app.get("/metrics", tags=["ops"], include_in_schema=False)
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
