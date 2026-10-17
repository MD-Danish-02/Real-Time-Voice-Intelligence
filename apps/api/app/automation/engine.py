"""
Automation Engine. Built-in automations (new lead -> eligibility -> call,
interested -> follow-up, meeting requested -> calendar/WhatsApp, callback
requested -> scheduler, call completed -> analysis/analytics) are
expressed as simple EVENT -> CONDITION -> ACTION handlers registered
here. This intentionally does NOT expose an n8n-style visual workflow
builder (per spec section 40) - it's a fixed, auditable rule set per
tenant, toggle-able via AutomationRule.is_enabled.
"""
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


async def emit(db: AsyncSession, event_name: str, **kwargs) -> None:
    for handler in _HANDLERS.get(event_name, []):
        resource_type = kwargs.get("resource_type", event_name)
        resource_id = str(kwargs.get("resource_id", ""))
        try:
            await handler(db, **kwargs)
            db.add(AutomationRunLog(
                tenant_id=kwargs["tenant_id"], trigger_event=event_name, resource_type=resource_type,
                resource_id=resource_id, success=True,
            ))
        except Exception as exc:  # noqa: BLE001 - one automation failing must not break the request
            logger.error("automation_handler_failed", event_name=event_name, error=str(exc))
            db.add(AutomationRunLog(
                tenant_id=kwargs["tenant_id"], trigger_event=event_name, resource_type=resource_type,
                resource_id=resource_id, success=False, detail=str(exc)[:2000],
            ))
        await db.commit()


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
    queued, reason = await enqueue_call_if_eligible(db, tenant, rules, lead, call_state, reason_hint="new_lead")
    logger.info("automation_new_lead", lead_id=str(lead.id), queued=queued, reason=reason)


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
