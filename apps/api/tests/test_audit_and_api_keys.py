import json

from tests.conftest import open_hours, synced_leads


async def test_signup_and_login_are_audited(client, tenant_a):
    r = await client.get("/api/v1/audit-logs", headers=tenant_a)
    actions = {row["action"] for row in r.json()["items"]}
    assert "user.signup" in actions


async def test_crm_connect_and_disconnect_audited(client, tenant_a):
    await client.post("/api/v1/crm/connect", headers=tenant_a, json={"provider": "mock"})
    await client.delete("/api/v1/crm/mock", headers=tenant_a)
    actions = [row["action"] for row in (await client.get("/api/v1/audit-logs", headers=tenant_a)).json()["items"]]
    assert "crm.connected" in actions and "crm.disconnected" in actions


async def test_dnc_change_and_call_lifecycle_audited(client, tenant_a):
    from sqlalchemy import create_engine, text
    from app.core.config import settings
    await open_hours(client, tenant_a)
    await synced_leads(client, tenant_a)
    lead_id = (await client.get("/api/v1/leads?q=rahul", headers=tenant_a)).json()["items"][0]["id"]
    eng = create_engine(settings.DATABASE_URL)
    with eng.begin() as c:
        c.execute(text("update leads set crm_status='DNC' where id=:i"), {"i": lead_id})
    from app.db.session import AsyncSessionLocal
    from app.models.lead import Lead, LeadCallState
    from sqlalchemy import select
    from app.services.dnc_service import apply_dnc_state
    async with AsyncSessionLocal() as db:
        lead = (await db.execute(select(Lead).where(Lead.id == lead_id))).scalar_one()
        state = (await db.execute(select(LeadCallState).where(LeadCallState.lead_id == lead.id))).scalar_one()
        await apply_dnc_state(db, lead.tenant_id, lead, state)
        await db.commit()
    rows = (await client.get("/api/v1/audit-logs?action=lead.dnc_changed", headers=tenant_a)).json()["items"]
    assert len(rows) == 1 and json.loads(rows[0]["metadata_json"])["do_not_call"] is True


async def test_api_key_lifecycle_and_audit(client, tenant_a):
    r = await client.post("/api/v1/api-keys", headers=tenant_a, json={"name": "Zapier"})
    assert r.status_code == 200 and r.json()["key"].startswith("vx_")
    keys = (await client.get("/api/v1/api-keys", headers=tenant_a)).json()
    assert len(keys) == 1 and keys[0]["prefix"] == r.json()["key"][:10] and not keys[0]["revoked"]
    await client.delete(f"/api/v1/api-keys/{keys[0]['id']}", headers=tenant_a)
    keys2 = (await client.get("/api/v1/api-keys", headers=tenant_a)).json()
    assert keys2[0]["revoked"] is True
    actions = [row["action"] for row in (await client.get("/api/v1/audit-logs", headers=tenant_a)).json()["items"]]
    assert "api_key.created" in actions and "api_key.revoked" in actions


async def test_audit_logs_are_tenant_isolated(client, tenant_a, tenant_b):
    await client.post("/api/v1/crm/connect", headers=tenant_a, json={"provider": "mock"})
    rows_b = (await client.get("/api/v1/audit-logs", headers=tenant_b)).json()["items"]
    assert all(row["action"] != "crm.connected" for row in rows_b)


async def test_audit_logs_admin_only(client, tenant_a):
    r = await client.post("/api/v1/users", headers=tenant_a, json={
        "email": "viewer2@example.com", "full_name": "View Er", "password": "Sup3rSecret!", "role": "agent_viewer"})
    login = await client.post("/api/v1/auth/login", json={"email": "viewer2@example.com", "password": "Sup3rSecret!"})
    vh = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert (await client.get("/api/v1/audit-logs", headers=vh)).status_code == 403


async def test_call_created_and_ended_are_audited(client, tenant_a):
    from app.db.session import AsyncSessionLocal
    from app.models.call import Call
    from app.models.lead import Lead, LeadCallState
    from app.models.tenant import Tenant, TenantBusinessRules
    from app.services.call_service import dispatch_call_job, finalize_call
    from sqlalchemy import select
    await open_hours(client, tenant_a)
    await synced_leads(client, tenant_a)
    lead_id = (await client.get("/api/v1/leads?q=rahul", headers=tenant_a)).json()["items"][0]["id"]
    await client.post(f"/api/v1/leads/{lead_id}/call", headers=tenant_a)
    async with AsyncSessionLocal() as db:
        from app.models.scheduling import CallJob
        lead = (await db.execute(select(Lead).where(Lead.id == lead_id))).scalar_one()
        job = (await db.execute(select(CallJob).where(CallJob.lead_id == lead.id))).scalar_one()
        tenant = (await db.execute(select(Tenant).where(Tenant.id == lead.tenant_id))).scalar_one()
        rules = (await db.execute(select(TenantBusinessRules).where(TenantBusinessRules.tenant_id == lead.tenant_id))).scalar_one()
        state = (await db.execute(select(LeadCallState).where(LeadCallState.lead_id == lead.id))).scalar_one()
        call = await dispatch_call_job(db, job, tenant, rules, lead, state, "mock", "http://x")
        await finalize_call(db, call, status="completed", duration_seconds=30, end_reason="completed")
    actions = [row["action"] for row in (await client.get("/api/v1/audit-logs", headers=tenant_a)).json()["items"]]
    assert "call.created" in actions and "call.ended" in actions


async def test_api_key_can_authenticate_requests_but_revoked_key_cannot(client, tenant_a):
    r = await client.post("/api/v1/api-keys", headers=tenant_a, json={"name": "Zapier"})
    raw_key = r.json()["key"]
    kh = {"Authorization": f"Bearer {raw_key}"}
    ok = await client.get("/api/v1/leads", headers=kh)
    assert ok.status_code == 200
    key_id = (await client.get("/api/v1/api-keys", headers=tenant_a)).json()[0]["id"]
    await client.delete(f"/api/v1/api-keys/{key_id}", headers=tenant_a)
    assert (await client.get("/api/v1/leads", headers=kh)).status_code == 401


async def test_api_key_is_tenant_scoped_and_cannot_see_other_tenant_data(client, tenant_a, tenant_b):
    from tests.conftest import synced_leads
    await synced_leads(client, tenant_a)
    r = await client.post("/api/v1/api-keys", headers=tenant_b, json={"name": "B key"})
    kh = {"Authorization": f"Bearer {r.json()['key']}"}
    assert (await client.get("/api/v1/leads", headers=kh)).json()["total"] == 0


async def test_garbage_vx_token_rejected(client):
    assert (await client.get("/api/v1/leads", headers={"Authorization": "Bearer vx_totallyfake12345"})).status_code == 401
