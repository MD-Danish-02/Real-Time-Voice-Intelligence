"""
Do-Not-Call normalization. CRMs spell "don't call this person" a dozen
different ways (DO_NOT_CALL, DNC, "Do Not Call", OPT_OUT, UNSUBSCRIBED,
"Unsubscribe", "Closed Lost - DNC", ...). This module is the single place
that decides whether a raw CRM status string means do-not-call, so the
eligibility engine and the sync/webhook paths never have to guess.
"""
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.enums import CallJobStatus
from app.models.lead import Lead, LeadCallState
from app.models.scheduling import CallJob

logger = get_logger("services.dnc")

_DNC_PATTERNS = [
    re.compile(r"do[\s_-]*not[\s_-]*call", re.I),
    re.compile(r"\bdnc\b", re.I),
    re.compile(r"opt[\s_-]*out", re.I),
    re.compile(r"unsubscribe", re.I),
    re.compile(r"\bblocked\b", re.I),
    re.compile(r"\bsuppressed\b", re.I),
    re.compile(r"\bcompliance[\s_-]*hold\b", re.I),
]


def status_means_do_not_call(crm_status: str | None) -> bool:
    if not crm_status:
        return False
    return any(p.search(crm_status) for p in _DNC_PATTERNS)


async def apply_dnc_state(db: AsyncSession, tenant_id: uuid.UUID, lead: Lead, call_state: LeadCallState) -> bool:
    """
    Syncs `call_state.do_not_call` from the lead's current CRM status.
    Returns True if the DNC flag changed (used by callers to log/emit an
    event). Flipping ON cancels every pending/locked/dispatched call job
    for this lead immediately, so the platform can never dial someone
    the CRM has just told it to stop calling.
    """
    should_be_dnc = call_state.manual_do_not_call or status_means_do_not_call(lead.crm_status)
    if should_be_dnc == call_state.do_not_call:
        return False

    call_state.do_not_call = should_be_dnc
    if should_be_dnc:
        active = await db.execute(
            select(CallJob).where(
                CallJob.lead_id == lead.id,
                CallJob.status.in_([CallJobStatus.PENDING.value, CallJobStatus.LOCKED.value,
                                     CallJobStatus.DISPATCHED.value]),
            )
        )
        cancelled = 0
        for job in active.scalars().all():
            job.status = CallJobStatus.CANCELED.value
            job.error = f"Lead status changed to '{lead.crm_status}' (interpreted as do-not-call)"
            cancelled += 1
        if cancelled:
            logger.info("dnc_cancelled_pending_jobs", lead_id=str(lead.id), count=cancelled)
        call_state.calling_status = "do_not_call"
        call_state.next_call_at = None
        call_state.callback_at = None
    else:
        # CRM lifted the restriction - do NOT re-queue automatically; the normal
        # lead.created/reconciliation/eligibility path picks it up on its own schedule.
        if call_state.calling_status == "do_not_call":
            call_state.calling_status = "not_called"
    logger.info("dnc_state_changed", lead_id=str(lead.id), do_not_call=should_be_dnc,
               crm_status=lead.crm_status, at=datetime.now(timezone.utc).isoformat())
    from app.services import audit_service
    await audit_service.record(db, tenant_id=tenant_id, action="lead.dnc_changed", resource_type="lead",
                               resource_id=str(lead.id), metadata={"do_not_call": should_be_dnc, "crm_status": lead.crm_status})
    return True
