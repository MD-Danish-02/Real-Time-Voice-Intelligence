import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.services.dnc_service import status_means_do_not_call
from tests.conftest import open_hours, synced_leads


def test_status_pattern_matching():
    for s in ["DO_NOT_CALL", "Do Not Call", "do-not-call", "DNC", "OPT_OUT", "Unsubscribe",
              "Closed Lost - DNC", "Suppressed", "Compliance Hold"]:
        assert status_means_do_not_call(s), s
    for s in ["New", "Interested", "Follow-up", None, "", "Converted"]:
        assert not status_means_do_not_call(s), s


async def _lead_and_state(name="Rahul Sharma"):
    from app.models.lead import Lead, LeadCallState
    db = AsyncSessionLocal()
    lead = (await db.execute(select(Lead).where(Lead.name == name))).scalar_one()
    state = (await db.execute(select(LeadCallState).where(LeadCallState.lead_id == lead.id))).scalar_one()
    return db, lead, state


async def test_crm_status_change_to_dnc_cancels_pending_job_and_blocks_future_calls(client, tenant_a):
    from app.models.scheduling import CallJob
    from sqlalchemy import create_engine, text
    from app.core.config import settings
    await open_hours(client, tenant_a)
    await synced_leads(client, tenant_a)
    lead_id = (await client.get("/api/v1/leads?q=rahul", headers=tenant_a)).json()["items"][0]["id"]
    r = await client.post(f"/api/v1/leads/{lead_id}/call", headers=tenant_a)
    assert r.json()["queued"] is True
    eng = create_engine(settings.DATABASE_URL)
    with eng.begin() as c:
        c.execute(text("update leads set crm_status='Do Not Call' where id=:i"), {"i": lead_id})
    db, lead, state = await _lead_and_state()
    async with db:
        from app.services.dnc_service import apply_dnc_state
        changed = await apply_dnc_state(db, lead.tenant_id, lead, state)
        await db.commit()
        assert changed is True and state.do_not_call is True
    with eng.begin() as c:
        assert c.execute(text("select status from call_jobs")).scalar() == "canceled"
    r2 = await client.post(f"/api/v1/leads/{lead_id}/call", headers=tenant_a)
    assert r2.json() == {"queued": False, "reason": "DO_NOT_CALL"}


async def test_dnc_lifted_does_not_auto_requeue(client, tenant_a):
    await open_hours(client, tenant_a)
    await synced_leads(client, tenant_a)
    db, lead, state = await _lead_and_state()
    async with db:
        from app.services.dnc_service import apply_dnc_state
        lead.crm_status = "Do Not Call"
        await apply_dnc_state(db, lead.tenant_id, lead, state)
        await db.commit()
        assert state.do_not_call is True
        lead.crm_status = "New"
        changed = await apply_dnc_state(db, lead.tenant_id, lead, state)
        await db.commit()
        assert changed is True and state.do_not_call is False and state.calling_status == "not_called"


async def test_manual_dnc_is_persisted_and_blocks_calls(client, tenant_a):
    await open_hours(client, tenant_a)
    await synced_leads(client, tenant_a)
    lead_id = (await client.get("/api/v1/leads?q=rahul", headers=tenant_a)).json()["items"][0]["id"]
    enabled = await client.post(f"/api/v1/leads/{lead_id}/do-not-call?value=true", headers=tenant_a)
    assert enabled.status_code == 200
    assert enabled.json()["manual_do_not_call"] is True and enabled.json()["do_not_call"] is True
    blocked = await client.post(f"/api/v1/leads/{lead_id}/call", headers=tenant_a)
    assert blocked.json() == {"queued": False, "reason": "DO_NOT_CALL"}
    disabled = await client.post(f"/api/v1/leads/{lead_id}/do-not-call?value=false", headers=tenant_a)
    assert disabled.json()["manual_do_not_call"] is False and disabled.json()["do_not_call"] is False


async def test_sync_paginates_through_every_page(client, tenant_a, monkeypatch):
    from app.providers.crm.base import NormalizedLead, SyncPage
    from app.providers.crm import mock as mock_module

    calls = {"n": 0}
    real_fetch = mock_module.MockCRMProvider.fetch_leads

    async def paged_fetch(self, *, access_token, cursor, updated_since=None):
        calls["n"] += 1
        page_num = int(cursor or "0")
        if page_num >= 3:
            return SyncPage(leads=[], next_cursor=None, has_more=False)
        leads = [NormalizedLead(provider_lead_id=f"page{page_num}-lead{i}", name=f"P{page_num}L{i}",
                                phone=f"+9199999{page_num}{i:02d}", crm_status="New") for i in range(2)]
        return SyncPage(leads=leads, next_cursor=str(page_num + 1), has_more=page_num < 2)

    monkeypatch.setattr(mock_module.MockCRMProvider, "initial_sync",
                        lambda self, **kw: paged_fetch(self, access_token=kw["access_token"], cursor=None))
    monkeypatch.setattr(mock_module.MockCRMProvider, "fetch_leads", paged_fetch)

    job = await synced_leads(client, tenant_a)
    assert job["records_created"] == 6 and calls["n"] == 3  # 3 pages of 2 leads, not just the first page
    leads = await client.get("/api/v1/leads?page_size=100", headers=tenant_a)
    assert leads.json()["total"] == 6
    conn = (await client.get("/api/v1/crm/connections", headers=tenant_a)).json()[0]
    assert conn["status"] == "synced"


async def test_connection_status_lifecycle_never_stuck_pending(client, tenant_a):
    r = await client.post("/api/v1/crm/connect", headers=tenant_a, json={"provider": "mock"})
    assert r.status_code == 200
    conn = (await client.get("/api/v1/crm/connections", headers=tenant_a)).json()[0]
    assert conn["status"] == "connected"  # never "pending" for a provider that needs no external redirect
    job = await client.post("/api/v1/crm/mock/sync?initial=true", headers=tenant_a)
    assert job.status_code == 200
    conn = (await client.get("/api/v1/crm/connections", headers=tenant_a)).json()[0]
    assert conn["status"] == "synced"


async def test_first_sync_failure_marks_error_not_stuck_connected(client, tenant_a, monkeypatch):
    from app.providers.crm import mock as mock_module
    from app.providers.crm.base import CRMProviderError

    async def boom(self, **kw):
        raise CRMProviderError("simulated provider outage", retryable=True)
    monkeypatch.setattr(mock_module.MockCRMProvider, "initial_sync", boom)
    await client.post("/api/v1/crm/connect", headers=tenant_a, json={"provider": "mock"})
    job = await client.post("/api/v1/crm/mock/sync?initial=true", headers=tenant_a)
    assert job.json()["status"] == "failed"
    conn = (await client.get("/api/v1/crm/connections", headers=tenant_a)).json()[0]
    assert conn["status"] == "error" and conn["last_error"]
