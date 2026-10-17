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
