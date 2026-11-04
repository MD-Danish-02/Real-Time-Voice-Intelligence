import hashlib, hmac, json

from sqlalchemy import create_engine, text

from app.core.config import settings
from app.services.crm_service import normalize_phone
from tests.conftest import synced_leads


def test_phone_normalization():
    assert normalize_phone("98765 43210") == "+919876543210"
    assert normalize_phone("+91 98765-43210") == "+919876543210"
    assert normalize_phone("919876543210") == "+919876543210"
    assert normalize_phone("") is None and normalize_phone(None) is None


async def test_initial_sync_and_dedup(client, tenant_a):
    job = await synced_leads(client, tenant_a)
    assert job["status"] == "succeeded" and job["records_created"] == 4
    again = await client.post("/api/v1/crm/mock/sync", headers=tenant_a)
    assert again.json()["records_created"] == 0 and again.json()["records_updated"] == 4
    leads = await client.get("/api/v1/leads", headers=tenant_a)
    assert leads.json()["total"] == 4
    assert all(l["phone"].startswith("+91") for l in leads.json()["items"])


async def test_manual_sync_queues_new_leads_once_and_retries_failed_automation(client, tenant_a, monkeypatch):
    from app.events import bus
    from app.db.session import AsyncSessionLocal
    from app.models.crm import CRMConnection
    from app.providers.crm.base import NormalizedLead, SyncPage
    from app.services import crm_service
    from app.services import eligibility_service

    class FakeRedis:
        fail_next_set = False

        async def set(self, *args, **kwargs):
            if self.fail_next_set:
                self.fail_next_set = False
                raise RuntimeError("simulated Redis outage")
            return True

        async def delete(self, *args, **kwargs):
            return 1

        async def publish(self, *args, **kwargs):
            return 1

    fake_redis = FakeRedis()
    monkeypatch.setattr(eligibility_service, "_get_redis", lambda: fake_redis)
    monkeypatch.setattr(bus, "_r", lambda: fake_redis)

    class FakeSheetsProvider:
        async def initial_sync(self, *, access_token, **kwargs):
            return SyncPage(leads=[NormalizedLead(
                provider_lead_id="safe-test-lead", name="Automation Test", phone="+919999000001",
                crm_status="New",
            )], next_cursor=None, has_more=False)

        async def fetch_leads(self, *, access_token, cursor, updated_since=None, **kwargs):
            return SyncPage(leads=[NormalizedLead(
                provider_lead_id="safe-test-lead", name="Automation Test", phone="+919999000001",
                crm_status="New",
            )], next_cursor=None, has_more=False)

    monkeypatch.setattr(crm_service, "get_crm_provider", lambda provider: FakeSheetsProvider())

    tenant = (await client.get("/api/v1/tenants/me", headers=tenant_a)).json()
    async with AsyncSessionLocal() as db:
        db.add(CRMConnection(
            tenant_id=uuid.UUID(tenant["id"]), provider="google_sheets", status="connected",
            encrypted_access_token="test-ciphertext",
        ))
        await db.commit()
    async def fake_token(db, connection):
        return "test-access-token"
    monkeypatch.setattr(crm_service, "get_valid_access_token", fake_token)

    await client.put("/api/v1/tenants/business-rules", headers=tenant_a, json={
        "auto_call_enabled": True, "calling_hours_start": "00:00:00",
        "calling_hours_end": "23:59:59", "calling_days_mask": 127,
    })
    assert (await client.put("/api/v1/crm/google_sheets/config", headers=tenant_a, json={
        "spreadsheet_id": "test-spreadsheet-id", "sheet_range": "Leads!A2:C",
        "columns": ["name", "phone", "status"],
    })).status_code == 200

    # One transient lock-store error must leave durable lead.created work.
    fake_redis.fail_next_set = True
    first = await client.post("/api/v1/crm/google_sheets/sync?initial=true", headers=tenant_a)
    assert first.status_code == 200 and first.json()["records_created"] == 1

    eng = create_engine(settings.DATABASE_URL)
    with eng.begin() as db:
        assert db.execute(text("select count(*) from call_jobs where status='pending'")).scalar() == 0
        assert db.execute(text("select count(*) from automation_run_logs where trigger_event='lead.created' and success=false")).scalar() == 1
        assert db.execute(text("select count(*) from automation_run_logs where trigger_event='lead.created'")).scalar() == 1

    # An unchanged manual sync retries only the durable failed event; it does
    # not emit a new event for the already-imported lead.
    second = await client.post("/api/v1/crm/google_sheets/sync", headers=tenant_a)
    assert second.status_code == 200 and second.json()["records_created"] == 0
    with eng.begin() as db:
        assert db.execute(text("select count(*) from call_jobs where status='pending'")).scalar() == 1
        assert db.execute(text("select count(*) from automation_run_logs where trigger_event='lead.created'")).scalar() == 1
        assert db.execute(text("select count(*) from automation_run_logs where trigger_event='lead.created' and success=false")).scalar() == 0

    # Simulate a process exit after job creation but before the outbox success
    # marker committed. Replaying must not redial after the first job completes.
    with eng.begin() as db:
        db.execute(text("update call_jobs set status='completed'"))
        db.execute(text("update lead_call_state set attempt_count=1, calling_status='called', "
                        "last_call_at=now() - interval '5 hours'"))
        db.execute(text("update automation_run_logs set success=false, detail='outbox:simulated interrupted acknowledgement' "
                        "where trigger_event='lead.created'"))
    from app.automation.engine import retry_pending_lead_created_automations
    async with AsyncSessionLocal() as db:
        await retry_pending_lead_created_automations(db, uuid.UUID(tenant["id"]))
    with eng.begin() as db:
        assert db.execute(text("select count(*) from call_jobs")).scalar() == 1
        assert db.execute(text("select count(*) from automation_run_logs where trigger_event='lead.created' and success=false")).scalar() == 0


async def test_google_sheets_oauth_callback_defers_initial_sync(client, tenant_a, monkeypatch):
    import uuid

    from app.core.signing import make_oauth_state
    from app.models.crm import CRMConnection
    from app.services import crm_service
    from app.workers.tasks import run_crm_sync

    tenant = (await client.get("/api/v1/tenants/me", headers=tenant_a)).json()
    user = (await client.get("/api/v1/auth/me", headers=tenant_a)).json()
    queued = []

    async def fake_complete(db, tenant_id, user_id, provider, code):
        connection = CRMConnection(
            tenant_id=tenant_id, provider=provider, status="connected",
            encrypted_access_token="test-ciphertext", connected_by_user_id=user_id,
        )
        db.add(connection)
        await db.commit()
        await db.refresh(connection)
        return connection

    async def fake_register(*args, **kwargs):
        return None

    monkeypatch.setattr(crm_service, "complete_oauth_connect", fake_complete)
    monkeypatch.setattr(crm_service, "register_webhooks_safe", fake_register)
    monkeypatch.setattr(run_crm_sync, "delay", lambda *args, **kwargs: queued.append(args))

    state = make_oauth_state(
        tenant_id=uuid.UUID(tenant["id"]), user_id=uuid.UUID(user["id"]), provider="google_sheets",
    )
    response = await client.get("/api/v1/crm/oauth/google_sheets/callback", params={"code": "fake", "state": state})
    assert response.status_code == 307
    assert queued == []
    connection = (await client.get("/api/v1/crm/connections", headers=tenant_a)).json()
    assert len(connection) == 1 and connection[0]["provider"] == "google_sheets"


async def test_leads_search_filter_pagination(client, tenant_a):
    await synced_leads(client, tenant_a)
    r = await client.get("/api/v1/leads?q=rahul", headers=tenant_a)
    assert r.json()["total"] == 1
    r = await client.get("/api/v1/leads?page_size=2&page=2", headers=tenant_a)
    assert len(r.json()["items"]) == 2
    r = await client.get("/api/v1/leads?crm_status=Follow-up", headers=tenant_a)
    assert r.json()["total"] == 1


async def _webhook_setup(client, headers):
    await client.post("/api/v1/crm/connect", headers=headers, json={"provider": "mock"})
    conns = (await client.get("/api/v1/crm/connections", headers=headers)).json()
    cid = conns[0]["id"]
    eng = create_engine(settings.DATABASE_URL)
    with eng.begin() as c:
        c.execute(text("update crm_connections set webhook_secret='whsec', webhook_enabled=true where id=:i"), {"i": cid})
    return cid


def _sign(body: bytes, secret="whsec"):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def test_webhook_rejects_bad_signature(client, tenant_a):
    cid = await _webhook_setup(client, tenant_a)
    body = json.dumps({"event_id": "e1", "id": "mock-1001"}).encode()
    r = await client.post(f"/api/v1/webhooks/crm/mock/{cid}", content=body, headers={"x-mock-signature": "bad"})
    assert r.status_code == 401
    r = await client.post(f"/api/v1/webhooks/crm/mock/{cid}", content=body)
    assert r.status_code == 401


async def test_webhook_idempotent_and_creates_lead(client, tenant_a):
    cid = await _webhook_setup(client, tenant_a)
    body = json.dumps({"event_id": "evt-1", "id": "mock-1001"}).encode()
    h = {"x-mock-signature": _sign(body), "content-type": "application/json"}
    r1 = await client.post(f"/api/v1/webhooks/crm/mock/{cid}", content=body, headers=h)
    r2 = await client.post(f"/api/v1/webhooks/crm/mock/{cid}", content=body, headers=h)
    assert r1.status_code == 202 and r1.json()["duplicate"] is False
    assert r2.status_code == 202 and r2.json()["duplicate"] is True
    eng = create_engine(settings.DATABASE_URL)
    with eng.begin() as c:
        assert c.execute(text("select count(*) from crm_events")).scalar() == 1


async def test_webhook_replay_window_and_unknown_connection(client, tenant_a):
    cid = await _webhook_setup(client, tenant_a)
    body = json.dumps({"event_id": "old", "id": "mock-1001"}).encode()
    h = {"x-mock-signature": _sign(body), "x-webhook-timestamp": "1000"}
    assert (await client.post(f"/api/v1/webhooks/crm/mock/{cid}", content=body, headers=h)).status_code == 400
    assert (await client.post("/api/v1/webhooks/crm/mock/00000000-0000-0000-0000-000000000000", content=body, headers=h)).status_code == 404
