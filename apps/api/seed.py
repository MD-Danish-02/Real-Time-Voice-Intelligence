"""Development/demo seed. Creates a clearly-labelled DEMO tenant with sample leads, calls, meetings and knowledge.
Refuses to run when ENV=production or ALLOW_DEMO_SEED=false. Demo tenant is named 'DEMO - ...' so it can never be
mistaken for real data. Usage: python seed.py   (demo login: demo@example.com / DemoPass123!)"""
import asyncio
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.config import settings
from app.core.crypto import encrypt_secret
from app.core.security import hash_password
from app.db.session import AsyncSessionLocal
from app.models import (Call, CallAnalysis, CallEvent, CRMConnection, KnowledgeDocument, Lead, LeadCallState,
                        Meeting, Tenant, TenantBusinessRules, Transcript, TranscriptSegment, User)
from app.services.crm_service import normalize_phone
from app.services.knowledge_service import ingest_document

DEMO_EMAIL = "demo@example.com"
LEADS = [("Asha Menon", "98450 11001", "Menon Foods", "New"), ("Ravi Iyer", "98450 11002", "Iyer Labs", "Follow-up"),
         ("Sara Khan", "98450 11003", "Khan Studio", "Interested"), ("Dev Patel", "98450 11004", "Patel Freight", "New"),
         ("Meera Nair", "98450 11005", "Nair Clinics", "Not Interested")]


async def main() -> int:
    if settings.ENV == "production" or not settings.ALLOW_DEMO_SEED:
        print("Refusing to seed: ENV=production or ALLOW_DEMO_SEED=false.")
        return 1
    async with AsyncSessionLocal() as db:
        if (await db.execute(select(User).where(User.email == DEMO_EMAIL))).scalar_one_or_none():
            print("Demo data already present.")
            return 0
        tenant = Tenant(name="DEMO - Sample Company", slug=f"demo-{uuid.uuid4().hex[:6]}", plan="demo")
        db.add(tenant); await db.flush()
        db.add(TenantBusinessRules(tenant_id=tenant.id, meeting_booking_enabled=True, whatsapp_enabled=True))
        db.add(User(tenant_id=tenant.id, email=DEMO_EMAIL, full_name="Demo Owner", role="owner",
                    hashed_password=hash_password("DemoPass123!"), is_email_verified=True))
        conn = CRMConnection(tenant_id=tenant.id, provider="mock", status="connected", external_account_name="DEMO CRM",
                             encrypted_access_token=encrypt_secret("mock-access-token"),
                             last_successful_sync_at=datetime.now(timezone.utc))
        db.add(conn); await db.flush()
        leads = []
        for i, (name, phone, company, status) in enumerate(LEADS):
            lead = Lead(tenant_id=tenant.id, crm_connection_id=conn.id, crm_provider="mock", provider_lead_id=f"demo-{i}",
                        name=name, phone=phone, normalized_phone=normalize_phone(phone), company=company,
                        email=f"{name.split()[0].lower()}@example.com", crm_status=status)
            db.add(lead); await db.flush()
            db.add(LeadCallState(tenant_id=tenant.id, lead_id=lead.id, do_not_call=(status == "Not Interested")))
            leads.append(lead)
        now = datetime.now(timezone.utc)
        for i, (lead, st, dur) in enumerate([(leads[2], "completed", 272), (leads[1], "no_answer", None), (leads[0], "completed", 118)]):
            call = Call(tenant_id=tenant.id, lead_id=lead.id, direction="outbound", status=st, telephony_provider="mock",
                        started_at=now - timedelta(hours=i + 1), ended_at=now - timedelta(hours=i + 1) + timedelta(seconds=dur or 20),
                        duration_seconds=dur, to_number=lead.normalized_phone)
            db.add(call); await db.flush()
            db.add(CallEvent(tenant_id=tenant.id, call_id=call.id, event_type="CALL_ENDED", occurred_at=call.ended_at,
                             payload_json=json.dumps({"demo": True})))
            if st == "completed":
                t = Transcript(tenant_id=tenant.id, call_id=call.id, full_text="demo transcript"); db.add(t); await db.flush()
                for ms, sp, tx in [(0, "ai", "Hi, this is the AI assistant from DEMO Sample Company."), (2500, "customer", "Yes, tell me about pricing.")]:
                    db.add(TranscriptSegment(tenant_id=tenant.id, transcript_id=t.id, speaker=sp, text=tx, started_at_ms=ms, confidence=0.9))
                call.transcript_id = t.id
                db.add(CallAnalysis(tenant_id=tenant.id, call_id=call.id, summary="[DEMO] Lead asked about pricing.", intent="pricing_inquiry",
                                    lead_status="interested", sentiment="positive", next_action="send_pricing", follow_up_required=True, confidence=0.8))
        db.add(Meeting(tenant_id=tenant.id, lead_id=leads[2].id, title="[DEMO] Product walkthrough", status="booked",
                       scheduled_at=now + timedelta(days=1, hours=2), calendar_provider="mock", join_url="https://meet.mock.local/demo"))
        doc = KnowledgeDocument(tenant_id=tenant.id, source_type="faq", title="[DEMO] Pricing FAQ",
                                raw_text="Starter plan is 999 per month. Business plan is 4999 per month with priority support.")
        db.add(doc); await db.commit()
        await ingest_document(db, tenant.id, doc, doc.raw_text)
    print(f"Demo tenant created. Login: {DEMO_EMAIL} / DemoPass123!  (all records are marked DEMO)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
