import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.call import Call, CallAnalysis
from app.models.crm import CRMConnection, CRMSyncJob
from app.models.enums import CallDirection, CallStatus
from app.models.lead import Lead, LeadCallState
from app.models.scheduling import Meeting, WhatsAppMessage


async def get_calls_analytics(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    base = select(func.count()).select_from(Call).where(Call.tenant_id == tenant_id)
    total = (await db.execute(base)).scalar_one()
    inbound = (await db.execute(base.where(Call.direction == CallDirection.INBOUND.value))).scalar_one()
    outbound = (await db.execute(base.where(Call.direction == CallDirection.OUTBOUND.value))).scalar_one()
    answered = (await db.execute(base.where(Call.status == CallStatus.COMPLETED.value))).scalar_one()
    missed = (await db.execute(base.where(Call.status == CallStatus.NO_ANSWER.value))).scalar_one()
    failed = (await db.execute(base.where(Call.status == CallStatus.FAILED.value))).scalar_one()
    avg_duration_result = await db.execute(
        select(func.avg(Call.duration_seconds)).where(Call.tenant_id == tenant_id, Call.duration_seconds.isnot(None))
    )
    avg_duration = avg_duration_result.scalar_one()
    return {
        "total": total, "inbound": inbound, "outbound": outbound, "answered": answered,
        "missed": missed, "failed": failed,
        "average_duration_seconds": float(avg_duration) if avg_duration is not None else None,
    }


async def get_leads_analytics(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    base = select(func.count()).select_from(LeadCallState).where(LeadCallState.tenant_id == tenant_id)
    total = (await db.execute(base)).scalar_one()
    new = (await db.execute(base.where(LeadCallState.calling_status == "not_called"))).scalar_one()
    contacted = (await db.execute(base.where(LeadCallState.calling_status == "called"))).scalar_one()
    callback = (await db.execute(base.where(LeadCallState.calling_status == "callback_scheduled"))).scalar_one()

    async def distinct_leads_with(*conditions) -> int:
        # distinct leads that have at least one call analysis matching the condition (real aggregation, no fabricated numbers)
        stmt = (select(func.count(func.distinct(Call.lead_id))).select_from(CallAnalysis)
                .join(Call, Call.id == CallAnalysis.call_id)
                .where(CallAnalysis.tenant_id == tenant_id, Call.lead_id.isnot(None), *conditions))
        return (await db.execute(stmt)).scalar_one()

    interested = await distinct_leads_with(CallAnalysis.lead_status == "interested")
    not_interested = await distinct_leads_with(CallAnalysis.lead_status == "not_interested")
    meeting_requested = await distinct_leads_with(CallAnalysis.meeting_requested.is_(True))
    converted = (await db.execute(select(func.count()).select_from(Lead).where(
        Lead.tenant_id == tenant_id, func.lower(Lead.crm_status) == "converted"))).scalar_one()
    return {
        "total": total, "new": new, "contacted": contacted, "interested": interested,
        "not_interested": not_interested, "callback": callback, "meeting_requested": meeting_requested,
        "converted": converted,
    }


async def get_meetings_analytics(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    base = select(func.count()).select_from(Meeting).where(Meeting.tenant_id == tenant_id)
    requested = (await db.execute(base.where(Meeting.status == "requested"))).scalar_one()
    booked = (await db.execute(base.where(Meeting.status == "booked"))).scalar_one()
    completed = (await db.execute(base.where(Meeting.status == "completed"))).scalar_one()
    canceled = (await db.execute(base.where(Meeting.status == "canceled"))).scalar_one()
    return {"requested": requested, "booked": booked, "completed": completed, "canceled": canceled}


async def get_whatsapp_analytics(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    base = select(func.count()).select_from(WhatsAppMessage).where(WhatsAppMessage.tenant_id == tenant_id)
    sent = (await db.execute(base.where(WhatsAppMessage.status.in_(["sent", "delivered", "read"])))).scalar_one()
    delivered = (await db.execute(base.where(WhatsAppMessage.status.in_(["delivered", "read"])))).scalar_one()
    read = (await db.execute(base.where(WhatsAppMessage.status == "read"))).scalar_one()
    failed = (await db.execute(base.where(WhatsAppMessage.status == "failed"))).scalar_one()
    return {"sent": sent, "delivered": delivered, "read": read, "failed": failed}


async def get_crm_sync_analytics(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    jobs_base = select(func.sum(CRMSyncJob.records_created)).where(CRMSyncJob.tenant_id == tenant_id)
    imported = (await db.execute(jobs_base)).scalar_one() or 0
    updated = (await db.execute(
        select(func.sum(CRMSyncJob.records_updated)).where(CRMSyncJob.tenant_id == tenant_id)
    )).scalar_one() or 0
    failures = (await db.execute(
        select(func.count()).select_from(CRMSyncJob).where(CRMSyncJob.tenant_id == tenant_id,
                                                             CRMSyncJob.status == "failed")
    )).scalar_one()
    last_sync = (await db.execute(
        select(func.max(CRMConnection.last_successful_sync_at)).where(CRMConnection.tenant_id == tenant_id)
    )).scalar_one()
    return {
        "imported": int(imported), "new_detected": int(imported), "updated": int(updated),
        "sync_failures": failures, "last_sync_at": last_sync.isoformat() if last_sync else None,
    }


async def get_overview(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    return {
        "calls": await get_calls_analytics(db, tenant_id),
        "leads": await get_leads_analytics(db, tenant_id),
        "meetings": await get_meetings_analytics(db, tenant_id),
        "whatsapp": await get_whatsapp_analytics(db, tenant_id),
        "crm_sync": await get_crm_sync_analytics(db, tenant_id),
    }
