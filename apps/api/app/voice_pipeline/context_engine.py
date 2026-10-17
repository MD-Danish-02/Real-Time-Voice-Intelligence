from dataclasses import asdict, dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.call import Call, CallAnalysis
from app.models.lead import Lead, LeadCallState
from app.models.tenant import Tenant, TenantBusinessRules
from app.services.knowledge_service import search_knowledge


@dataclass
class CallContext:
    lead: dict
    company: dict
    knowledge: list[dict] = field(default_factory=list)
    previous_calls: list[dict] = field(default_factory=list)
    business_rules: dict = field(default_factory=dict)
    available_actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


AGENT_AVAILABLE_ACTIONS = [
    "get_lead", "search_knowledge", "get_previous_calls",
    "update_lead", "add_crm_note", "create_crm_activity",
    "check_calendar", "book_meeting", "send_whatsapp",
    "schedule_callback", "transfer_to_human", "end_call",
]


async def build_call_context(
    db: AsyncSession, tenant: Tenant, rules: TenantBusinessRules, lead: Lead, call_state: LeadCallState,
    *, opening_query_hint: str = "",
) -> CallContext:
    previous_calls_result = await db.execute(
        select(Call, CallAnalysis)
        .join(CallAnalysis, CallAnalysis.call_id == Call.id, isouter=True)
        .where(Call.lead_id == lead.id)
        .order_by(Call.created_at.desc())
        .limit(5)
    )
    previous_calls = []
    for call, analysis in previous_calls_result.all():
        previous_calls.append({
            "call_id": str(call.id), "direction": call.direction, "status": call.status,
            "started_at": call.started_at.isoformat() if call.started_at else None,
            "summary": analysis.summary if analysis else None,
            "lead_status": analysis.lead_status if analysis else None,
        })

    knowledge_query = opening_query_hint or f"{lead.company or ''} {lead.crm_status or ''} pricing product info"
    knowledge_hits = await search_knowledge(db, tenant.id, knowledge_query, top_k=5)

    return CallContext(
        lead={
            "id": str(lead.id), "name": lead.name, "phone": lead.phone, "email": lead.email,
            "company": lead.company, "crm_status": lead.crm_status,
        },
        company={"tenant_id": str(tenant.id), "name": tenant.name},
        knowledge=knowledge_hits,
        previous_calls=previous_calls,
        business_rules={
            "calling_hours_start": str(rules.calling_hours_start), "calling_hours_end": str(rules.calling_hours_end),
            "timezone": rules.timezone, "meeting_booking_enabled": rules.meeting_booking_enabled,
            "whatsapp_enabled": rules.whatsapp_enabled, "callback_enabled": rules.callback_enabled,
        },
        available_actions=AGENT_AVAILABLE_ACTIONS,
    )
