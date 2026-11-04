"""Worker tasks executed eagerly against the real test Postgres/Redis (sync tests: tasks use asyncio.run)."""
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.core.config import settings
from app.main import app
from app.workers.celery_app import celery_app
import pytest


@pytest.fixture(autouse=True)
def _eager_celery(monkeypatch):
    from app.core import config as config_module
    from app.events import bus
    from app.services import eligibility_service

    class FakeRedis:
        async def set(self, *args, **kwargs):
            return True

        async def delete(self, *args, **kwargs):
            return 1

        async def publish(self, *args, **kwargs):
            return 1

    fake_redis = FakeRedis()
    monkeypatch.setattr(eligibility_service, "_get_redis", lambda: fake_redis)
    monkeypatch.setattr(bus, "_r", lambda: fake_redis)
    monkeypatch.setattr(config_module.settings, "TELEPHONY_PROVIDER_MODE", "mock")
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    yield
    celery_app.conf.task_always_eager = False
    celery_app.conf.task_eager_propagates = False


def _signup(c):
    r = c.post("/api/v1/auth/signup", json={"company_name": "Worker Co", "full_name": "W W",
                                            "email": f"{uuid.uuid4().hex[:6]}@example.com", "password": "Sup3rSecret!"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_sync_creates_leads_and_automation_queues_calls_then_dispatch_dials():
    from app.workers.tasks import dial_call, dispatch_due_call_jobs, run_crm_sync
    with TestClient(app) as c:
        h = _signup(c)
        c.put("/api/v1/tenants/business-rules", headers=h, json={
            "calling_hours_start": "00:00:00", "calling_hours_end": "23:59:59", "calling_days_mask": 127})
        assert c.post("/api/v1/crm/connect", headers=h, json={"provider": "mock"}).status_code == 200
        conn = c.get("/api/v1/crm/connections", headers=h).json()[0]
        tenant = c.get("/api/v1/tenants/me", headers=h).json()["id"]
        run_crm_sync.delay(tenant, conn["id"], "initial")            # sync -> lead.created automation -> queue
        assert c.get("/api/v1/leads", headers=h).json()["total"] == 4
        eng = create_engine(settings.DATABASE_URL)
        with eng.begin() as db:
            assert db.execute(text("select count(*) from call_jobs where status='pending'")).scalar() == 4
            assert db.execute(text("select count(*) from automation_run_logs where trigger_event='lead.created' and success=true")).scalar() == 4
        run_crm_sync.delay(tenant, conn["id"], "reconciliation")     # re-sync must not queue duplicates
        with eng.begin() as db:
            assert db.execute(text("select count(*) from call_jobs")).scalar() == 4
            assert db.execute(text("select count(*) from automation_run_logs where trigger_event='lead.created'")).scalar() == 4
        dispatch_due_call_jobs.delay()                                # -> dial_call per job (mock telephony)
        calls = c.get("/api/v1/calls", headers=h).json()
        assert calls["total"] == 4 and all(i["status"] == "dialing" for i in calls["items"])
        with eng.begin() as db:
            assert db.execute(text("select count(*) from call_jobs where status='dispatched'")).scalar() == 4
        dispatch_due_call_jobs.delay()                                # nothing pending -> no double dial
        assert c.get("/api/v1/calls", headers=h).json()["total"] == 4


def test_due_callback_is_requeued_via_eligibility():
    from datetime import datetime, timedelta, timezone
    from app.workers.tasks import enqueue_due_callbacks, run_crm_sync
    with TestClient(app) as c:
        h = _signup(c)
        c.put("/api/v1/tenants/business-rules", headers=h, json={
            "calling_hours_start": "00:00:00", "calling_hours_end": "23:59:59", "calling_days_mask": 127,
            "auto_call_enabled": False})
        c.post("/api/v1/crm/connect", headers=h, json={"provider": "mock"})
        conn = c.get("/api/v1/crm/connections", headers=h).json()[0]
        run_crm_sync.delay(c.get("/api/v1/tenants/me", headers=h).json()["id"], conn["id"], "initial")
        lead = c.get("/api/v1/leads", headers=h).json()["items"][0]["id"]
        eng = create_engine(settings.DATABASE_URL)
        with eng.begin() as db:
            db.execute(text("insert into callbacks (id, tenant_id, lead_id, callback_at, timezone, status) "
                            "select gen_random_uuid(), tenant_id, id, :t, 'Asia/Kolkata', 'scheduled' from leads where id=:l"),
                       {"t": datetime.now(timezone.utc) - timedelta(minutes=1), "l": lead})
        c.put("/api/v1/tenants/business-rules", headers=h, json={"auto_call_enabled": True})
        enqueue_due_callbacks.delay()
        with eng.begin() as db:
            assert db.execute(text("select count(*) from call_jobs where lead_id=:l"), {"l": lead}).scalar() == 1
            assert db.execute(text("select status from callbacks")).scalar() == "completed"


def test_webhook_detected_new_lead_is_queued_for_calling_exactly_once():
    import hashlib, hmac, json
    from app.workers.tasks import process_crm_event
    with TestClient(app) as c:
        h = _signup(c)
        c.put("/api/v1/tenants/business-rules", headers=h, json={"calling_hours_start": "00:00:00", "calling_hours_end": "23:59:59", "calling_days_mask": 127})
        c.post("/api/v1/crm/connect", headers=h, json={"provider": "mock"})
        cid = c.get("/api/v1/crm/connections", headers=h).json()[0]["id"]
        eng = create_engine(settings.DATABASE_URL)
        with eng.begin() as db:
            db.execute(text("update crm_connections set webhook_secret='whsec' where id=:i"), {"i": cid})
        body = json.dumps({"event_id": "evt-new-1", "id": "mock-1001"}).encode()
        sig = hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
        r = c.post(f"/api/v1/webhooks/crm/mock/{cid}", content=body, headers={"x-mock-signature": sig})
        assert r.status_code == 202          # persisted + acknowledged; (eager celery cannot nest asyncio.run inside the app loop)
        with eng.begin() as db:
            event_id = db.execute(text("select id from crm_events")).scalar()
        process_crm_event.delay(str(event_id))  # what the worker does
        assert c.get("/api/v1/leads", headers=h).json()["total"] == 1
        with eng.begin() as db:
            assert db.execute(text("select count(*) from call_jobs")).scalar() == 1
            assert db.execute(text("select status from crm_events")).scalar() == "processed"
        c.post(f"/api/v1/webhooks/crm/mock/{cid}", content=body, headers={"x-mock-signature": sig})  # duplicate delivery
        with eng.begin() as db:
            assert db.execute(text("select count(*) from call_jobs")).scalar() == 1
