"""
The Call Eligibility Engine. This is deliberately the single place that
decides whether a lead may be called - the API layer, the scheduler and
the final pre-dial check in the worker all call into this same function
so the rule is enforced consistently and server-side only.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.models.lead import Lead, LeadCallState
from app.models.tenant import Tenant, TenantBusinessRules

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore


@dataclass
class EligibilityResult:
    eligible: bool
    reason: str
    next_action: str  # CALL | WAIT | SKIP | BLOCKED


def _is_within_calling_hours(rules: TenantBusinessRules, now_utc: datetime) -> bool:
    tz = ZoneInfo(rules.timezone) if ZoneInfo else timezone.utc
    local_now = now_utc.astimezone(tz)
    # Convert Python weekday (Mon=0) to Sun=0..Sat=6 bit position used in calling_days_mask
    sun_indexed_bit = 1 << ((local_now.weekday() + 1) % 7)
    if not (rules.calling_days_mask & sun_indexed_bit):
        return False
    local_time = local_now.time()
    return rules.calling_hours_start <= local_time <= rules.calling_hours_end


def evaluate_eligibility(
    *, tenant: Tenant, rules: TenantBusinessRules, lead: Lead, call_state: LeadCallState,
    now_utc: Optional[datetime] = None, telephony_available: bool = True,
    already_has_active_call_job: bool = False,
) -> EligibilityResult:
    now_utc = now_utc or datetime.now(timezone.utc)

    if not tenant.is_active:
        return EligibilityResult(False, "TENANT_INACTIVE", "BLOCKED")

    if not rules.auto_call_enabled:
        return EligibilityResult(False, "AUTO_CALL_DISABLED", "SKIP")

    if not lead.normalized_phone and not lead.phone:
        return EligibilityResult(False, "INVALID_PHONE", "BLOCKED")

    if call_state.do_not_call:
        return EligibilityResult(False, "DO_NOT_CALL", "BLOCKED")

    if rules.consent_required and not call_state.eligible:
        return EligibilityResult(False, "CONSENT_NOT_RECORDED", "BLOCKED")

    allowed_statuses = [s.strip() for s in (rules.allowed_crm_statuses or "").split(",") if s.strip()]
    disallowed_default = {"do not call", "unqualified", "closed lost", "converted"}
    if allowed_statuses:
        if (lead.crm_status or "").strip().lower() not in [s.lower() for s in allowed_statuses]:
            return EligibilityResult(False, "CRM_STATUS_NOT_ALLOWED", "SKIP")
    elif (lead.crm_status or "").strip().lower() in disallowed_default:
        return EligibilityResult(False, "CRM_STATUS_NOT_ALLOWED", "SKIP")

    if already_has_active_call_job:
        return EligibilityResult(False, "ALREADY_QUEUED_OR_CALLING", "WAIT")

    if call_state.calling_status == "calling":
        return EligibilityResult(False, "CALL_IN_PROGRESS", "WAIT")

    if call_state.attempt_count >= rules.max_attempts:
        return EligibilityResult(False, "MAX_ATTEMPTS_REACHED", "SKIP")

    if call_state.callback_at and call_state.callback_at > now_utc:
        return EligibilityResult(False, "CALLBACK_NOT_DUE", "WAIT")

    if call_state.next_call_at and call_state.next_call_at > now_utc:
        return EligibilityResult(False, "RETRY_INTERVAL_NOT_REACHED", "WAIT")

    if call_state.last_call_at:
        earliest_retry = call_state.last_call_at + timedelta(minutes=rules.retry_interval_minutes)
        if now_utc < earliest_retry:
            return EligibilityResult(False, "RETRY_INTERVAL_NOT_REACHED", "WAIT")

    if not _is_within_calling_hours(rules, now_utc):
        return EligibilityResult(False, "OUTSIDE_CALLING_HOURS", "WAIT")

    if not telephony_available:
        return EligibilityResult(False, "TELEPHONY_UNAVAILABLE", "WAIT")

    reason = "CALLBACK_DUE" if call_state.callback_at else (
        "RETRY_DUE" if call_state.attempt_count > 0 else "NEW_LEAD"
    )
    return EligibilityResult(True, reason, "CALL")
