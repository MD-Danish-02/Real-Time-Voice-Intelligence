import io
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from tests.conftest import open_hours, synced_leads


async def _ctx(client, headers, enable=True):
    """Builds a real DB-backed call context: (db, tenant, rules, lead, state, call)."""
    from app.models.call import Call
    from app.models.lead import Lead, LeadCallState
    from app.models.tenant import Tenant, TenantBusinessRules
    await open_hours(client, headers)
    await synced_leads(client, headers)
    if enable:
        await client.put("/api/v1/tenants/business-rules", headers=headers, json={
            "meeting_booking_enabled": True, "whatsapp_enabled": True, "callback_enabled": True,
            "crm_auto_update_enabled": True})
    db = AsyncSessionLocal()
    lead = (await db.execute(select(Lead).where(Lead.name == "Rahul Sharma"))).scalar_one()
    tenant = (await db.execute(select(Tenant).where(Tenant.id == lead.tenant_id))).scalar_one()
    rules = (await db.execute(select(TenantBusinessRules).where(TenantBusinessRules.tenant_id == tenant.id))).scalar_one()
    state = (await db.execute(select(LeadCallState).where(LeadCallState.lead_id == lead.id))).scalar_one()
    call = Call(tenant_id=tenant.id, lead_id=lead.id, direction="outbound", status="in_progress", telephony_provider="mock")
    db.add(call)
    await db.commit()
    return db, tenant, rules, lead, state, call


async def test_orchestrator_full_turn_with_tools_and_events(client, tenant_a):
    from app.providers.voice.llm.base import MockLLMProvider
    from app.providers.voice.stt.base import MockSTTProvider
    from app.providers.voice.tts.base import MockTTSProvider
    from app.voice_pipeline.agent_tools import AgentToolExecutor
    from app.voice_pipeline.context_engine import build_call_context
    from app.voice_pipeline.orchestrator import VoiceCallOrchestrator
    db, tenant, rules, lead, state, call = await _ctx(client, tenant_a)
    async with db:
        ctx = await build_call_context(db, tenant, rules, lead, state)
        assert ctx.lead["name"] == "Rahul Sharma" and "end_call" in ctx.available_actions
        events = []

        async def on_event(t, p):
            events.append(t)

        class MeetingSTT(MockSTTProvider):
            async def transcribe_chunk(self, audio_bytes, sample_rate=16000):
                r = await super().transcribe_chunk(audio_bytes, sample_rate)
                r.text = "Can we set up a demo meeting?"
                return r

        orch = VoiceCallOrchestrator(stt=MeetingSTT(), llm=MockLLMProvider(), tts=MockTTSProvider(),
                                     tool_executor=AgentToolExecutor(db, tenant.id, lead, state, rules, call.id),
                                     context=ctx.to_dict(), on_event=on_event)
        greeting = await orch.opening_greeting()
        assert greeting.audio_bytes
        turn = await orch.handle_user_audio_chunk(b"\x00\x00" * 160)
        assert "check_calendar" in turn.tool_calls_made and turn.audio is not None
        for e in ("SPEECH_STARTED", "SPEECH_ENDED", "TOOL_CALLED", "AI_RESPONSE_STARTED", "AI_RESPONSE_COMPLETED"):
            assert e in events, e
        assert "user:" in orch.full_transcript_text()


async def test_agent_tools_are_gated_by_tenant_rules(client, tenant_a):
    from app.voice_pipeline.agent_tools import AgentToolExecutor
    db, tenant, rules, lead, state, call = await _ctx(client, tenant_a, enable=False)
    async with db:
        ex = AgentToolExecutor(db, tenant.id, lead, state, rules, call.id)
        assert (await ex.execute("check_calendar", {}))["available"] is False
        assert (await ex.execute("send_whatsapp", {"message": "hi"}))["sent"] is False
        assert (await ex.execute("update_lead", {"status": "Hot"}))["skipped"] is True
        assert "error" in await ex.execute("drop_database", {})  # unknown tools are rejected, not executed


async def test_book_meeting_callback_and_whatsapp_persist(client, tenant_a):
    from app.models.scheduling import Callback, Meeting, WhatsAppMessage
    from app.voice_pipeline.agent_tools import AgentToolExecutor
    db, tenant, rules, lead, state, call = await _ctx(client, tenant_a)
    async with db:
        ex = AgentToolExecutor(db, tenant.id, lead, state, rules, call.id)
        when = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        assert (await ex.execute("book_meeting", {"start_iso": when}))["booked"] is True
        assert (await ex.execute("send_whatsapp", {"message": "Meeting confirmed"}))["sent"] is True
        cb = await ex.execute("schedule_callback", {"callback_at_iso": when, "reason": "busy now"})
        assert cb["scheduled"] is True
        await db.commit()
        assert (await db.execute(select(Meeting))).scalar_one().status == "booked"
        assert (await db.execute(select(WhatsAppMessage))).scalar_one().status == "sent"
        assert (await db.execute(select(Callback))).scalar_one().status == "scheduled"
        assert state.callback_at is not None
    ms = (await client.get("/api/v1/meetings", headers=tenant_a)).json()
    assert len(ms) == 1 and ms[0]["join_url"]


async def test_callback_blocks_calling_until_due_then_allows(client, tenant_a):
    from app.eligibility.engine import evaluate_eligibility
    from app.voice_pipeline.agent_tools import AgentToolExecutor
    db, tenant, rules, lead, state, call = await _ctx(client, tenant_a)
    async with db:
        ex = AgentToolExecutor(db, tenant.id, lead, state, rules, call.id)
        due = datetime.now(timezone.utc) + timedelta(hours=2)
        await ex.execute("schedule_callback", {"callback_at_iso": due.isoformat()})
        state.calling_status = "callback_scheduled"
        r = evaluate_eligibility(tenant=tenant, rules=rules, lead=lead, call_state=state)
        assert r.reason == "CALLBACK_NOT_DUE"
        r = evaluate_eligibility(tenant=tenant, rules=rules, lead=lead, call_state=state, now_utc=due + timedelta(minutes=1))
        assert (r.eligible, r.reason) == (True, "CALLBACK_DUE")


async def test_crm_writeback_respects_setting_and_uses_provider(client, tenant_a):
    from app.voice_pipeline.agent_tools import AgentToolExecutor
    db, tenant, rules, lead, state, call = await _ctx(client, tenant_a)
    async with db:
        ex = AgentToolExecutor(db, tenant.id, lead, state, rules, call.id)
        assert (await ex.execute("update_lead", {"status": "Interested", "note": "asked pricing"}))["updated"] is True
        assert lead.crm_status == "Interested"
        assert (await ex.execute("create_crm_activity", {"subject": "AI call"}))["logged"] is True


async def test_call_analysis_validated_and_persisted(client, tenant_a):
    from app.services.call_service import CallAnalysisSchema, analyze_call, save_transcript
    import pytest, pydantic
    db, tenant, rules, lead, state, call = await _ctx(client, tenant_a)
    async with db:
        await save_transcript(db, call, [{"speaker": "customer", "text": "pricing?", "started_at_ms": 10, "confidence": 0.9},
                                         {"speaker": "ai", "text": "It's 4999.", "started_at_ms": 900}])
        a = await analyze_call(db, call, "customer: pricing?", "mock")
        assert a.intent == "pricing_inquiry" and a.follow_up_required is True
    with pytest.raises(pydantic.ValidationError):
        CallAnalysisSchema.model_validate({"confidence": "not-a-number"})
    d = (await client.get(f"/api/v1/calls/{call.id}", headers=tenant_a)).json()
    assert len(d["transcript_segments"]) == 2 and d["analysis"]["sentiment"] == "positive"


async def test_analysis_falls_back_when_llm_returns_garbage(client, tenant_a):
    from app.providers.voice.llm import base as llm_base
    from app.services import call_service
    db, tenant, rules, lead, state, call = await _ctx(client, tenant_a)

    class Bad(llm_base.MockLLMProvider):
        async def structured_completion(self, **kw):
            return {"confidence": "banana", "meeting_requested": "maybe?!"}
    orig = call_service.get_llm_provider
    call_service.get_llm_provider = lambda mode: Bad()
    try:
        async with db:
            a = await call_service.analyze_call(db, call, "x", "mock")
            assert a.confidence == 0.0 and "could not" in a.summary.lower()
    finally:
        call_service.get_llm_provider = orig


# ---------- security ----------
async def test_ssrf_blocked_for_private_and_bad_schemes(client, tenant_a):
    for url in ("http://127.0.0.1:8000/health", "http://localhost/", "http://169.254.169.254/latest/meta-data",
                "file:///etc/passwd", "ftp://example.com/x"):
        r = await client.post("/api/v1/knowledge/website", headers=tenant_a, json={"url": url})
        assert r.status_code == 200 and r.json()["status"] == "failed", url


async def test_upload_validation(client, tenant_a):
    ok = await client.post("/api/v1/knowledge/upload", headers=tenant_a,
                           files={"file": ("faq.txt", io.BytesIO(b"Our office opens at 9am. Support is free."), "text/plain")})
    assert ok.status_code == 200 and ok.json()["status"] == "processed"
    bad_ext = await client.post("/api/v1/knowledge/upload", headers=tenant_a,
                                files={"file": ("run.exe", io.BytesIO(b"MZ"), "application/octet-stream")})
    assert bad_ext.status_code == 415
    spoof = await client.post("/api/v1/knowledge/upload", headers=tenant_a,
                              files={"file": ("fake.pdf", io.BytesIO(b"<script>alert(1)</script>"), "application/pdf")})
    assert spoof.status_code == 415
    big = await client.post("/api/v1/knowledge/upload", headers=tenant_a,
                            files={"file": ("big.txt", io.BytesIO(b"a" * (10 * 1024 * 1024 + 5)), "text/plain")})
    assert big.status_code == 413
    traversal = await client.post("/api/v1/knowledge/upload", headers=tenant_a,
                                  files={"file": ("../../etc/passwd.txt", io.BytesIO(b"hello world"), "text/plain")})
    assert traversal.status_code == 200 and "/" not in traversal.json()["title"]


async def test_mock_crm_and_secrets_never_leak(client, tenant_a):
    await synced_leads(client, tenant_a)
    conns = (await client.get("/api/v1/crm/connections", headers=tenant_a)).json()
    blob = str(conns).lower()
    assert "token" not in blob and "secret" not in blob
    from sqlalchemy import create_engine, text
    from app.core.config import settings
    with create_engine(settings.DATABASE_URL).begin() as c:
        stored = c.execute(text("select encrypted_access_token from crm_connections")).scalar()
    assert stored and "mock-access-token" not in stored  # encrypted at rest


async def test_telephony_webhook_finalizes_and_rejects_mismatch(client, tenant_a):
    db, tenant, rules, lead, state, call = await _ctx(client, tenant_a)
    async with db:
        call.provider_call_id = "sid-1"
        await db.commit()
    from app.core.signing import call_webhook_signature
    sig = call_webhook_signature(call.id)
    assert (await client.post(f"/api/v1/webhooks/telephony/mock/{call.id}", json={"Status": "completed"})).status_code == 401
    assert (await client.post(f"/api/v1/webhooks/telephony/mock/{call.id}?sig=forged", json={"Status": "completed"})).status_code == 401
    bad = await client.post(f"/api/v1/webhooks/telephony/mock/{call.id}?sig={sig}", json={"CallSid": "other", "Status": "completed"})
    assert bad.status_code == 400
    ok = await client.post(f"/api/v1/webhooks/telephony/mock/{call.id}?sig={sig}", json={"CallSid": "sid-1", "Status": "completed", "Duration": "42"})
    assert ok.status_code == 200
    d = (await client.get(f"/api/v1/calls/{call.id}", headers=tenant_a)).json()
    assert d["status"] == "completed" and d["duration_seconds"] == 42
    ov = (await client.get("/api/v1/analytics/overview", headers=tenant_a)).json()
    assert ov["calls"]["total"] == 1 and ov["calls"]["answered"] == 1 and ov["calls"]["average_duration_seconds"] == 42.0


async def test_analytics_empty_tenant_reports_zeros_not_fabricated(client, tenant_a):
    ov = (await client.get("/api/v1/analytics/overview", headers=tenant_a)).json()
    assert ov["calls"]["total"] == 0 and ov["calls"]["average_duration_seconds"] is None
    assert ov["leads"]["total"] == 0 and ov["crm_sync"]["last_sync_at"] is None
    st = (await client.get("/api/v1/tenants/ai-status", headers=tenant_a)).json()
    assert st["crm_connected"] is False and st["ai_ready"] is False


async def test_interruption_suppresses_ai_speech(client, tenant_a):
    from app.providers.voice.llm.base import MockLLMProvider
    from app.providers.voice.stt.base import MockSTTProvider
    from app.providers.voice.tts.base import MockTTSProvider
    from app.voice_pipeline.agent_tools import AgentToolExecutor
    from app.voice_pipeline.context_engine import build_call_context
    from app.voice_pipeline.orchestrator import VoiceCallOrchestrator
    db, tenant, rules, lead, state, call = await _ctx(client, tenant_a)
    async with db:
        ctx = await build_call_context(db, tenant, rules, lead, state)
        events = []

        async def on_event(t, p):
            events.append(t)
        orch = VoiceCallOrchestrator(stt=MockSTTProvider(), llm=MockLLMProvider(), tts=MockTTSProvider(),
                                     tool_executor=AgentToolExecutor(db, tenant.id, lead, state, rules, call.id),
                                     context=ctx.to_dict(), on_event=on_event)
        orch.interrupt()
        turn = await orch.handle_user_audio_chunk(b"\x00\x00" * 160)
        assert "USER_INTERRUPTED" in events and turn.audio is None


async def test_oauth_state_signed_and_callback_needs_no_bearer(client, tenant_a):
    import uuid
    from app.core.signing import make_oauth_state, read_oauth_state
    import pytest
    tid, uid = uuid.uuid4(), uuid.uuid4()
    st = make_oauth_state(tenant_id=tid, user_id=uid, provider="hubspot")
    assert read_oauth_state(st, provider="hubspot") == (tid, uid)
    with pytest.raises(ValueError):
        read_oauth_state(st, provider="zoho")            # state bound to the provider it was issued for
    with pytest.raises(ValueError):
        read_oauth_state(st + "x", provider="hubspot")   # forged
    r = await client.get("/api/v1/crm/oauth/hubspot/callback?code=abc&state=forged", follow_redirects=False)
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_STATE"


async def test_ari_events_drive_call_lifecycle(client, tenant_a):
    from app.telephony.asterisk.event_handler import handle_ari_event
    db, tenant, rules, lead, state, call = await _ctx(client, tenant_a)
    async with db:
        call.provider_call_id, call.status, call.answered_at = "chan-1", "dialing", None
        await db.commit()
    await handle_ari_event({"type": "ChannelStateChange", "channel": {"id": "chan-1", "state": "Ringing"}})
    await handle_ari_event({"type": "ChannelStateChange", "channel": {"id": "chan-1", "state": "Up"}})
    await handle_ari_event({"type": "ChannelStateChange", "channel": {"id": "unknown-chan", "state": "Up"}})  # ignored
    d = (await client.get(f"/api/v1/calls/{call.id}", headers=tenant_a)).json()
    assert d["status"] == "in_progress" and d["answered_at"]
    await handle_ari_event({"type": "StasisEnd", "channel": {"id": "chan-1"}})
    d = (await client.get(f"/api/v1/calls/{call.id}", headers=tenant_a)).json()
    assert d["status"] == "completed" and d["duration_seconds"] is not None


async def test_security_headers_present(client):
    r = await client.get("/health")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"
    assert r.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
