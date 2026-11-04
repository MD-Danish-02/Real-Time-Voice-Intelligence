"""
Automation Engine. Built-in automations (new lead -> eligibility -> call,
interested -> follow-up, meeting requested -> calendar/WhatsApp, callback
requested -> scheduler, call completed -> analysis/analytics) are
expressed as simple EVENT -> CONDITION -> ACTION handlers registered
here. This intentionally does NOT expose an n8n-style visual workflow
builder (per spec section 40) - it's a fixed, auditable rule set per
tenant, toggle-able via AutomationRule.is_enabled.
"""
import uuid
from datetime import datetime, timezone
from typing import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.enums import CallingStatus
from app.models.lead import Lead, LeadCallState
from app.models.scheduling import AutomationRunLog
from app.models.tenant import Tenant, TenantBusinessRules
from app.services.eligibility_service import enqueue_call_if_eligible

logger = get_logger("automation.engine")

_HANDLERS: dict[str, list[Callable[..., Awaitable[None]]]] = {}


def on_event(event_name: str):
    def _decorator(fn):
        _HANDLERS.setdefault(event_name, []).append(fn)
        return fn
    return _decorator


async def emit(
    db: AsyncSession, event_name: str, *, automation_log_id: uuid.UUID | None = None, **kwargs,
) -> bool:
    """Run handlers and persist their outcome, reusing a durable log when supplied."""
    succeeded = True
    errors: list[str] = []
    for handler in _HANDLERS.get(event_name, []):
        resource_type = kwargs.get("resource_type", event_name)
        resource_id = str(kwargs.get("resource_id", ""))
        handler_succeeded = True
        error_detail = None
        try:
            await handler(db, **kwargs)
        except Exception as exc:  # noqa: BLE001 - one automation failing must not break the request
            # Handlers may have left the session in a failed transaction. Roll
            # back before recording failure so the durable retry marker survives.
            await db.rollback()
            handler_succeeded = False
            succeeded = False
            error_detail = str(exc)[:2000]
            errors.append(error_detail)
            logger.error("automation_handler_failed", event_name=event_name, error=str(exc))
        if automation_log_id is not None:
            run_log = await db.get(AutomationRunLog, automation_log_id)
            if run_log is None:
                raise RuntimeError("Automation retry record disappeared before completion")
            run_log.success = succeeded
            run_log.detail = None if succeeded else f"outbox:failed {'; '.join(errors) or 'automation failed'}"[:2000]
        else:
            db.add(AutomationRunLog(
                tenant_id=kwargs["tenant_id"], trigger_event=event_name, resource_type=resource_type,
                resource_id=resource_id, success=handler_succeeded,
                detail=error_detail,
            ))
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return succeeded


async def retry_pending_lead_created_automations(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Retry durable lead-created work left pending by a failed enqueue or process exit."""
    result = await db.execute(
        select(AutomationRunLog.id, AutomationRunLog.resource_id)
        .where(
            AutomationRunLog.tenant_id == tenant_id,
            AutomationRunLog.trigger_event == "lead.created",
            AutomationRunLog.resource_type == "lead",
            AutomationRunLog.success.is_(False),
            AutomationRunLog.detail.like("outbox:%"),
            AutomationRunLog.resource_id.is_not(None),
        )
        .order_by(AutomationRunLog.created_at)
    )
    pending = result.all()
    for log_id, resource_id in pending:
        try:
            lead_id = uuid.UUID(resource_id)
        except (TypeError, ValueError):
            logger.error("invalid_lead_created_automation_resource", log_id=str(log_id))
            continue
        await emit(
            db,
            "lead.created",
            automation_log_id=log_id,
            tenant_id=tenant_id,
            resource_id=lead_id,
            resource_type="lead",
        )


@on_event("lead.created")
async def _automation_new_lead_call(db: AsyncSession, *, tenant_id, resource_id, **_) -> None:
    """New lead -> eligibility -> call queue."""
    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one()
    rules = (await db.execute(
        select(TenantBusinessRules).where(TenantBusinessRules.tenant_id == tenant_id)
    )).scalar_one()
    lead = (await db.execute(select(Lead).where(Lead.id == resource_id))).scalar_one()
    call_state = (await db.execute(
        select(LeadCallState).where(LeadCallState.lead_id == lead.id)
    )).scalar_one()
    lead_id_for_log = str(lead.id)
    queued, reason = await enqueue_call_if_eligible(db, tenant, rules, lead, call_state, reason_hint="new_lead")
    if not queued and reason == "ALREADY_LOCKED":
        # Another attempt may have crashed while holding the short-lived lock.
        # Keep this outbox item retryable instead of acknowledging it as done.
        raise RuntimeError("lead call enqueue is already in progress")
    logger.info("automation_new_lead", lead_id=lead_id_for_log, queued=queued, reason=reason)


@on_event("call.completed")
async def _automation_call_completed(db: AsyncSession, *, tenant_id, resource_id, analysis=None, **_) -> None:
    """Call completed -> analytics already recorded via CallAnalysis; here we react to outcome."""
    if analysis is None:
        return
    lead_id = analysis.get("lead_id")
    if not lead_id:
        return
    call_state = (await db.execute(select(LeadCallState).where(LeadCallState.lead_id == lead_id))).scalar_one_or_none()
    if not call_state:
        return
    if analysis.get("lead_status") == "interested":
        call_state.calling_status = CallingStatus.CALLED.value
    if analysis.get("follow_up_required") and not analysis.get("meeting_requested"):
        from datetime import timedelta
        call_state.next_call_at = datetime.now(timezone.utc) + timedelta(hours=4)
    await db.commit()
