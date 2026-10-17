from datetime import datetime, time, timedelta, timezone

from app.eligibility.engine import evaluate_eligibility
from app.models.lead import Lead, LeadCallState
from app.models.tenant import Tenant, TenantBusinessRules

NOW = datetime(2026, 9, 28, 6, 30, tzinfo=timezone.utc)  # Monday 12:00 IST


def build(**over):
    tenant = Tenant(name="t", slug="t", is_active=over.pop("tenant_active", True))
    rules = TenantBusinessRules(timezone="Asia/Kolkata", calling_hours_start=time(9), calling_hours_end=time(19),
                                calling_days_mask=0b0111110, max_attempts=3, retry_interval_minutes=240,
                                auto_call_enabled=True, allowed_crm_statuses="", consent_required=False)
    lead = Lead(crm_provider="mock", phone="+919876543210", normalized_phone="+919876543210", crm_status="New")
    state = LeadCallState(calling_status="not_called", attempt_count=0, do_not_call=False, eligible=True)
    for k, v in over.items():
        for obj in (tenant, rules, lead, state):
            if hasattr(obj, k):
                setattr(obj, k, v)
    return tenant, rules, lead, state


def ev(now=NOW, **kw):
    active = kw.pop("active", False)
    t, r, l, s = build(**kw)
    return evaluate_eligibility(tenant=t, rules=r, lead=l, call_state=s, now_utc=now, already_has_active_call_job=active)


def test_new_lead_eligible():
    r = ev()
    assert (r.eligible, r.reason, r.next_action) == (True, "NEW_LEAD", "CALL")


def test_dnc_blocked():
    assert ev(do_not_call=True).reason == "DO_NOT_CALL"


def test_invalid_phone():
    assert ev(phone=None, normalized_phone=None).reason == "INVALID_PHONE"


def test_outside_hours_and_weekend():
    assert ev(now=datetime(2026, 9, 28, 17, 0, tzinfo=timezone.utc)).reason == "OUTSIDE_CALLING_HOURS"  # 22:30 IST
    assert ev(now=datetime(2026, 9, 27, 6, 30, tzinfo=timezone.utc)).reason == "OUTSIDE_CALLING_HOURS"  # Sunday


def test_retry_interval_and_max_attempts():
    r = ev(attempt_count=1, last_call_at=NOW - timedelta(minutes=10))
    assert (r.reason, r.next_action) == ("RETRY_INTERVAL_NOT_REACHED", "WAIT")
    assert ev(attempt_count=1, last_call_at=NOW - timedelta(hours=5)).reason == "RETRY_DUE"
    assert ev(attempt_count=3).reason == "MAX_ATTEMPTS_REACHED"


def test_callback_not_due_then_due():
    assert ev(callback_at=NOW + timedelta(hours=1)).reason == "CALLBACK_NOT_DUE"
    assert ev(callback_at=NOW - timedelta(minutes=1)).reason == "CALLBACK_DUE"


def test_status_rules():
    assert ev(crm_status="Do Not Call").reason == "CRM_STATUS_NOT_ALLOWED"
    assert ev(crm_status="Hot", allowed_crm_statuses="New,Follow-up").reason == "CRM_STATUS_NOT_ALLOWED"
    assert ev(crm_status="new", allowed_crm_statuses="New").eligible


def test_active_job_calling_inactive_tenant_autocall():
    assert ev(active=True).reason == "ALREADY_QUEUED_OR_CALLING"
    assert ev(calling_status="calling").reason == "CALL_IN_PROGRESS"
    assert ev(tenant_active=False).reason == "TENANT_INACTIVE"
    assert ev(auto_call_enabled=False).reason == "AUTO_CALL_DISABLED"
