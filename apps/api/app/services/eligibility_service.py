import uuid
from datetime import datetime, timezone

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import get_redis
from app.core.logging import get_logger
from app.eligibility.engine import EligibilityResult, evaluate_eligibility
from app.models.enums import CallJobStatus
from app.models.lead import Lead, LeadCallState
from app.models.scheduling import CallJob
from app.models.tenant import Tenant, TenantBusinessRules

logger = get_logger("services.eligibility")

_LOCK_TTL_SECONDS = 120


def _get_redis() -> Redis:
    return get_redis()


async def _has_active_call_job(db: AsyncSession, lead_id: uuid.UUID) -> bool:
    result = await db.execute(
        select(CallJob).where(
            CallJob.lead_id == lead_id,
            CallJob.status.in_([CallJobStatus.PENDING.value, CallJobStatus.LOCKED.value,
                                 CallJobStatus.DISPATCHED.value]),
        )
    )
    return result.scalar_one_or_none() is not None


async def check_eligibility(
    db: AsyncSession, tenant: Tenant, rules: TenantBusinessRules, lead: Lead, call_state: LeadCallState,
) -> EligibilityResult:
    active_job = await _has_active_call_job(db, lead.id)
    return evaluate_eligibility(
        tenant=tenant, rules=rules, lead=lead, call_state=call_state,
        already_has_active_call_job=active_job,
    )


async def enqueue_call_if_eligible(
    db: AsyncSession, tenant: Tenant, rules: TenantBusinessRules, lead: Lead, call_state: LeadCallState,
    *, reason_hint: str = "auto",
) -> tuple[bool, str]:
    """
    Acquires a short-lived Redis lock keyed on the lead before doing the
    eligibility check + insert, so two concurrent triggers (webhook +
    polling reconciliation, for instance) can never both queue the same
    lead. The DB unique constraint on (tenant_id, idempotency_key) is the
    second, durable layer of the same protection.
    """
    lock_key = f"lead-call-lock:{lead.tenant_id}:{lead.id}"
    r = _get_redis()
    got_lock = await r.set(lock_key, "1", nx=True, ex=_LOCK_TTL_SECONDS)
    if not got_lock:
        return False, "ALREADY_LOCKED"

    try:
        result = await check_eligibility(db, tenant, rules, lead, call_state)
        if not result.eligible:
            return False, result.reason

        # Replaying the durable lead.created outbox must never create another
        # call even if its first job has already completed and attempt_count
        # has advanced. Other retry/callback paths remain attempt-scoped.
        idempotency_key = (
            f"{lead.id}:new_lead" if reason_hint == "new_lead"
            else f"{lead.id}:{call_state.attempt_count + 1}:{reason_hint}"
        )
        job = CallJob(
            tenant_id=lead.tenant_id, lead_id=lead.id, scheduled_at=datetime.now(timezone.utc),
            attempt_number=call_state.attempt_count + 1, idempotency_key=idempotency_key, reason=result.reason,
        )
        db.add(job)
        call_state.calling_status = "queued"
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            return False, "DUPLICATE_IDEMPOTENCY_KEY"
        return True, result.reason
    finally:
        await r.delete(lock_key)


async def final_pre_dial_check(
    db: AsyncSession, tenant: Tenant, rules: TenantBusinessRules, lead: Lead, call_state: LeadCallState,
) -> EligibilityResult:
    """
    Re-evaluates eligibility immediately before dialing, using fresh state
    loaded right before this call. If the CRM status flipped to a
    disallowed value (e.g. DO_NOT_CALL) between queueing and dispatch,
    this catches it and the dial is aborted.
    """
    return evaluate_eligibility(tenant=tenant, rules=rules, lead=lead, call_state=call_state)
