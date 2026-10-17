import os
os.environ.update({
    "ENV": "test",
    "DATABASE_URL": os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg2://voxai:voxai@localhost:5432/voxai_test"),
    "ASYNC_DATABASE_URL": os.environ.get("TEST_ASYNC_DATABASE_URL", "postgresql+asyncpg://voxai:voxai@localhost:5432/voxai_test"),
    "REDIS_URL": os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/5"),
    "CRM_PROVIDER_MODE": "mock", "ALLOW_DEMO_SEED": "true",
    "CELERY_BROKER_URL": os.environ.get("TEST_CELERY_BROKER_URL", "redis://localhost:6379/6"),
    "CELERY_RESULT_BACKEND": os.environ.get("TEST_CELERY_RESULT_BACKEND", "redis://localhost:6379/7"),
})
import uuid
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, text

from app.core.config import settings

_sync = create_engine(settings.DATABASE_URL)


@pytest.fixture(scope="session", autouse=True)
def migrated():
    from alembic import command
    from alembic.config import Config
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(autouse=True)
def clean(migrated):
    with _sync.begin() as c:
        tables = [r[0] for r in c.execute(text("select tablename from pg_tables where schemaname='public' and tablename <> 'alembic_version'"))]
        c.execute(text("TRUNCATE " + ",".join(tables) + " RESTART IDENTITY CASCADE"))
    import redis
    redis.Redis.from_url(settings.REDIS_URL).flushdb()
    yield


@pytest_asyncio.fixture
async def client():
    from app.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def make_tenant(client, name="Acme"):
    email = f"{uuid.uuid4().hex[:8]}@example.com"
    r = await client.post("/api/v1/auth/signup", json={"company_name": name, "full_name": "Owner", "email": email, "password": "Sup3rSecret!"})
    assert r.status_code == 200, r.text
    tok = r.json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}, email


@pytest_asyncio.fixture
async def tenant_a(client):
    return (await make_tenant(client, "Tenant A"))[0]


@pytest_asyncio.fixture
async def tenant_b(client):
    return (await make_tenant(client, "Tenant B"))[0]


async def open_hours(client, headers):
    r = await client.put("/api/v1/tenants/business-rules", headers=headers, json={
        "calling_hours_start": "00:00:00", "calling_hours_end": "23:59:59", "calling_days_mask": 127})
    assert r.status_code == 200, r.text


async def synced_leads(client, headers):
    r = await client.post("/api/v1/crm/connect", headers=headers, json={"provider": "mock"})
    assert r.status_code == 200, r.text
    r = await client.post("/api/v1/crm/mock/sync?initial=true", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()
