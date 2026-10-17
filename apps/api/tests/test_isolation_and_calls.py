import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from tests.conftest import open_hours, synced_leads


async def first_lead_id(client, headers):
    return (await client.get("/api/v1/leads?q=rahul", headers=headers)).json()["items"][0]["id"]


# ---------- tenant isolation ----------
async def test_cross_tenant_lead_access_denied(client, tenant_a, tenant_b):
    await synced_leads(client, tenant_a)
    lead_id = await first_lead_id(client, tenant_a)
    for path in ("", "/eligibility"):
        assert (await client.get(f"/api/v1/leads/{lead_id}{path}", headers=tenant_b)).status_code == 404
    assert (await client.post(f"/api/v1/leads/{lead_id}/call", headers=tenant_b)).status_code == 404
    assert (await client.post(f"/api/v1/leads/{lead_id}/do-not-call", headers=tenant_b)).status_code == 404
    assert (await client.get("/api/v1/leads", headers=tenant_b)).json()["total"] == 0
    assert (await client.get("/api/v1/leads", headers=tenant_a)).json()["total"] == 4


async def test_cross_tenant_knowledge_and_crm_isolation(client, tenant_a, tenant_b):
    r = await client.post("/api/v1/knowledge/text", headers=tenant_a, json={
        "source_type": "faq", "title": "Pricing", "content": "The premium plan costs 4999 rupees per month."})
    assert r.status_code == 200 and r.json()["status"] == "processed"
    doc_id = r.json()["id"]
    hits_a = (await client.get("/api/v1/knowledge/search?q=premium plan cost", headers=tenant_a)).json()
    assert hits_a and "4999" in hits_a[0]["content"]
    assert (await client.get("/api/v1/knowledge/search?q=premium plan cost", headers=tenant_b)).json() == []
    assert (await client.get("/api/v1/knowledge", headers=tenant_b)).json() == []
    assert (await client.delete(f"/api/v1/knowledge/{doc_id}", headers=tenant_b)).status_code == 404
    assert (await client.post(f"/api/v1/knowledge/{doc_id}/reprocess", headers=tenant_b)).status_code == 404
    await synced_leads(client, tenant_a)
    assert (await client.get("/api/v1/crm/connections", headers=tenant_b)).json() == []
    assert (await client.get("/api/v1/crm/sync-history", headers=tenant_b)).json() == []


async def test_tenant_id_in_body_or_query_is_ignored(client, tenant_a, tenant_b):
    await synced_leads(client, tenant_a)
    other = (await client.get("/api/v1/tenants/me", headers=tenant_a)).json()["id"]
    r = await client.get(f"/api/v1/leads?tenant_id={other}", headers=tenant_b)
    assert r.json()["total"] == 0


# ---------- duplicate-call protection & pre-dial ----------
async def test_enqueue_is_idempotent_single_job(client, tenant_a):
    await open_hours(client, tenant_a)
    await synced_leads(client, tenant_a)
    lead_id = await first_lead_id(client, tenant_a)
    first = await client.post(f"/api/v1/leads/{lead_id}/call", headers=tenant_a)
    second = await client.post(f"/api/v1/leads/{lead_id}/call", headers=tenant_a)
    assert first.json() == {"queued": True, "reason": "NEW_LEAD"}
    assert second.json()["queued"] is False and second.json()["reason"] == "ALREADY_QUEUED_OR_CALLING"
    eng = create_engine(settings.DATABASE_URL)
    with eng.begin() as c:
        assert c.execute(text("select count(*) from call_jobs")).scalar() == 1


async def test_db_partial_unique_index_blocks_second_active_job(client, tenant_a):
    await open_hours(client, tenant_a)
    await synced_leads(client, tenant_a)
    lead_id = await first_lead_id(client, tenant_a)
    await client.post(f"/api/v1/leads/{lead_id}/call", headers=tenant_a)
    eng = create_engine(settings.DATABASE_URL)
    import pytest, sqlalchemy
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        with eng.begin() as c:
            c.execute(text("""insert into call_jobs (id, tenant_id, lead_id, scheduled_at, priority, attempt_number,
                status, idempotency_key) select gen_random_uuid(), tenant_id, lead_id, now(), 1, 9, 'pending', 'other-key'
                from call_jobs limit 1"""))


async def test_dnc_and_outside_hours_prevent_queueing(client, tenant_a):
    await synced_leads(client, tenant_a)  # default hours Mon-Fri 9-19 IST: may or may not be open now
    await open_hours(client, tenant_a)
    lead_id = await first_lead_id(client, tenant_a)
    await client.post(f"/api/v1/leads/{lead_id}/do-not-call", headers=tenant_a)
    r = await client.post(f"/api/v1/leads/{lead_id}/call", headers=tenant_a)
    assert r.json() == {"queued": False, "reason": "DO_NOT_CALL"}
    await client.put("/api/v1/tenants/business-rules", headers=tenant_a,
                     json={"calling_hours_start": "00:00:00", "calling_hours_end": "00:00:01", "calling_days_mask": 0})
    other = (await client.get("/api/v1/leads?q=neha", headers=tenant_a)).json()["items"][0]["id"]
    r = await client.post(f"/api/v1/leads/{other}/call", headers=tenant_a)
    assert r.json()["reason"] == "OUTSIDE_CALLING_HOURS"


async def _load(lead_id):
    from app.models.lead import Lead, LeadCallState
    from app.models.scheduling import CallJob
    from app.models.tenant import Tenant, TenantBusinessRules
    db = AsyncSessionLocal()
    lead = (await db.execute(select(Lead).where(Lead.id == uuid.UUID(lead_id)))).scalar_one()
    return db, lead


async def test_final_predial_check_cancels_when_dnc_set_after_queueing(client, tenant_a):
    from app.models.lead import LeadCallState
    from app.models.scheduling import CallJob
    from app.models.tenant import Tenant, TenantBusinessRules
    from app.services.call_service import dispatch_call_job
    await open_hours(client, tenant_a)
    await synced_leads(client, tenant_a)
    lead_id = await first_lead_id(client, tenant_a)
    assert (await client.post(f"/api/v1/leads/{lead_id}/call", headers=tenant_a)).json()["queued"]
    await client.post(f"/api/v1/leads/{lead_id}/do-not-call", headers=tenant_a)  # flips AFTER queueing
    db, lead = await _load(lead_id)
    async with db:
        job = (await db.execute(select(CallJob))).scalar_one()
        tenant = (await db.execute(select(Tenant).where(Tenant.id == lead.tenant_id))).scalar_one()
        rules = (await db.execute(select(TenantBusinessRules).where(TenantBusinessRules.tenant_id == lead.tenant_id))).scalar_one()
        state = (await db.execute(select(LeadCallState).where(LeadCallState.lead_id == lead.id))).scalar_one()
        call = await dispatch_call_job(db, job, tenant, rules, lead, state, "mock", "http://x")
        assert call is None
        await db.refresh(job)
        assert job.status == "canceled" and "DO_NOT_CALL" in job.error


async def test_dispatch_dials_via_mock_provider_and_records_state(client, tenant_a):
    from app.models.call import Call
    from app.models.lead import LeadCallState
    from app.models.scheduling import CallJob
    from app.models.tenant import Tenant, TenantBusinessRules
    from app.services.call_service import dispatch_call_job
    await open_hours(client, tenant_a)
    await synced_leads(client, tenant_a)
    lead_id = await first_lead_id(client, tenant_a)
    await client.post(f"/api/v1/leads/{lead_id}/call", headers=tenant_a)
    db, lead = await _load(lead_id)
    async with db:
        job = (await db.execute(select(CallJob))).scalar_one()
        tenant = (await db.execute(select(Tenant).where(Tenant.id == lead.tenant_id))).scalar_one()
        rules = (await db.execute(select(TenantBusinessRules).where(TenantBusinessRules.tenant_id == lead.tenant_id))).scalar_one()
        state = (await db.execute(select(LeadCallState).where(LeadCallState.lead_id == lead.id))).scalar_one()
        call = await dispatch_call_job(db, job, tenant, rules, lead, state, "mock", "http://x")
        assert call.status == "dialing" and call.provider_call_id.startswith("mock-call-")
        assert state.attempt_count == 1 and state.calling_status == "calling"
    calls = await client.get("/api/v1/calls", headers=tenant_a)
    assert calls.json()["total"] == 1
    detail = await client.get(f"/api/v1/calls/{call.id}", headers=tenant_a)
    assert detail.status_code == 200
