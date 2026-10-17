import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.signing import call_webhook_signature
from app.models.call import Call, CallAnalysis, CallEvent, Transcript, TranscriptSegment
from app.models.enums import CallDirection, CallingStatus, CallStatus
from app.models.lead import Lead, LeadCallState
from app.models.scheduling import CallJob
from app.models.tenant import TenantBusinessRules
from app.providers.telephony.base import TelephonyProviderError
from app.providers.telephony.factory import get_telephony_provider
from app.providers.voice.llm.base import get_llm_provider
from app.eligibility.engine import evaluate_eligibility

logger = get_logger("services.call")


class CallAnalysisSchema(BaseModel):
    """Validated shape for LLM-produced call intelligence - section 33/48 of the spec."""
    summary: str = ""
    intent: str = "unknown"
    lead_status: str = "unknown"
    interest_level: str = Field(default="unknown")
    sentiment: str = Field(default="neutral")
    requirements: list[str] = []
    objections: list[str] = []
    questions: list[str] = []
    action_items: list[str] = []
    meeting_requested: bool = False
    follow_up_required: bool = False
    next_action: str = "none"
    confidence: float = 0.5


async def dispatch_call_job(
    db: AsyncSession, job: CallJob, tenant, rules: TenantBusinessRules, lead: Lead, call_state: LeadCallState,
    telephony_provider_name: str, webhook_base_url: str,
) -> Optional[Call]:
    """
    The final pre-dial check + actual dial. Runs inside the worker
    (see app.workers.tasks.dial_call), never inline in an HTTP handler.
    """
    check = evaluate_eligibility(tenant=tenant, rules=rules, lead=lead, call_state=call_state)
    if not check.eligible:
        job.status = "canceled"
        job.error = f"Pre-dial check failed: {check.reason}"
        call_state.calling_status = CallingStatus.NOT_CALLED.value
        await db.commit()
        logger.info("predial_check_failed", lead_id=str(lead.id), reason=check.reason)
        return None

    telephony = get_telephony_provider(telephony_provider_name)
    call = Call(
        tenant_id=lead.tenant_id, lead_id=lead.id, direction=CallDirection.OUTBOUND.value,
        status=CallStatus.QUEUED.value, attempt_number=job.attempt_number,
        telephony_provider=telephony_provider_name, to_number=lead.normalized_phone or lead.phone,
    )
    db.add(call)
    await db.flush()

    webhook_url = (f"{webhook_base_url}/api/v1/webhooks/telephony/{telephony_provider_name}/{call.id}"
                   f"?sig={call_webhook_signature(call.id)}")
    try:
        dial_result = await telephony.dial(
            to_number=call.to_number, from_number="", webhook_url=webhook_url,
            metadata={"call_id": str(call.id), "tenant_id": str(lead.tenant_id)},
        )
        call.provider_call_id = dial_result.provider_call_id
        call.status = CallStatus.DIALING.value
        call.started_at = datetime.now(timezone.utc)
        job.status = "dispatched"
        call_state.calling_status = "calling"
        call_state.attempt_count += 1
        call_state.last_call_id = call.id
        call_state.last_call_at = datetime.now(timezone.utc)
    except TelephonyProviderError as exc:
        call.status = CallStatus.FAILED.value
        call.end_reason = str(exc)
        job.status = "failed"
        job.error = str(exc)
        call_state.calling_status = CallingStatus.NOT_CALLED.value
        logger.error("dial_failed", lead_id=str(lead.id), error=str(exc))

    db.add(CallEvent(tenant_id=lead.tenant_id, call_id=call.id, event_type="CALL_DIALING",
                      occurred_at=datetime.now(timezone.utc), payload_json=json.dumps({"to": call.to_number})))
    await db.commit()
    await db.refresh(call)
    from app.services import audit_service
    await audit_service.record(db, tenant_id=lead.tenant_id, action="call.created", resource_type="call",
                               resource_id=str(call.id), metadata={"direction": call.direction, "status": call.status})
    return call


async def initiate_outbound_call(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    to_number: str,
    lead_id: Optional[uuid.UUID] = None,
    telephony_provider_name: Optional[str] = None,
    webhook_base_url: Optional[str] = None,
    custom_prompt: Optional[str] = None,
) -> Call:
    """Initiates an outbound phone call on-demand, linking or generating lead context."""
    from app.core.config import settings
    provider_name = telephony_provider_name or settings.TELEPHONY_PROVIDER_MODE
    base_url = webhook_base_url or settings.BACKEND_URL

    lead = None
    if lead_id:
        lead = (await db.execute(select(Lead).where(Lead.id == lead_id, Lead.tenant_id == tenant_id))).scalar_one_or_none()

    if lead is None:
        lead = (await db.execute(select(Lead).where(Lead.tenant_id == tenant_id, Lead.phone == to_number))).scalar_one_or_none()
        if lead is None:
            lead = Lead(
                tenant_id=tenant_id,
                name=f"Direct Dial {to_number[-4:] if len(to_number) >= 4 else to_number}",
                phone=to_number,
                crm_provider="manual",
                provider_lead_id=f"direct_{uuid.uuid4().hex[:8]}",
            )
            db.add(lead)
            await db.flush()

    call_state = (await db.execute(select(LeadCallState).where(LeadCallState.lead_id == lead.id))).scalar_one_or_none()
    if call_state is None:
        call_state = LeadCallState(tenant_id=tenant_id, lead_id=lead.id, calling_status="calling")
        db.add(call_state)
        await db.flush()
    else:
        call_state.calling_status = "calling"

    telephony = get_telephony_provider(provider_name)
    call = Call(
        tenant_id=tenant_id,
        lead_id=lead.id,
        direction=CallDirection.OUTBOUND.value,
        status=CallStatus.QUEUED.value,
        attempt_number=1,
        telephony_provider=provider_name,
        to_number=to_number,
    )
    db.add(call)
    await db.flush()

    webhook_url = f"{base_url.rstrip('/')}/api/v1/webhooks/telephony/{provider_name}/{call.id}?sig={call_webhook_signature(call.id)}"
    try:
        dial_result = await telephony.dial(
            to_number=call.to_number,
            from_number="",
            webhook_url=webhook_url,
            metadata={"call_id": str(call.id), "tenant_id": str(tenant_id)},
        )
        call.provider_call_id = dial_result.provider_call_id
        call.status = CallStatus.DIALING.value
        call.started_at = datetime.now(timezone.utc)
        call_state.attempt_count += 1
        call_state.last_call_id = call.id
        call_state.last_call_at = datetime.now(timezone.utc)
    except TelephonyProviderError as exc:
        call.status = CallStatus.FAILED.value
        call.end_reason = str(exc)
        call_state.calling_status = CallingStatus.NOT_CALLED.value
        logger.error("direct_dial_failed", to_number=to_number, error=str(exc))
        raise

    db.add(CallEvent(tenant_id=tenant_id, call_id=call.id, event_type="CALL_DIALING",
                      occurred_at=datetime.now(timezone.utc), payload_json=json.dumps({"to": call.to_number})))
    await db.commit()
    await db.refresh(call)

    from app.services import audit_service
    await audit_service.record(
        db, tenant_id=tenant_id, action="call.created", resource_type="call",
        resource_id=str(call.id), metadata={"direction": call.direction, "status": call.status, "to": to_number}
    )
    return call


async def record_call_event(db: AsyncSession, call: Call, event_type: str, payload: Optional[dict] = None) -> None:
    db.add(CallEvent(tenant_id=call.tenant_id, call_id=call.id, event_type=event_type,
                      occurred_at=datetime.now(timezone.utc), payload_json=json.dumps(payload or {})))
    await db.commit()


async def finalize_call(
    db: AsyncSession, call: Call, *, status: str, duration_seconds: Optional[int], end_reason: Optional[str],
) -> None:
    call.status = status
    call.ended_at = datetime.now(timezone.utc)
    call.duration_seconds = duration_seconds
    call.end_reason = end_reason
    if call.lead_id:
        result = await db.execute(select(LeadCallState).where(LeadCallState.lead_id == call.lead_id))
        call_state = result.scalar_one_or_none()
        if call_state:
            call_state.calling_status = CallingStatus.CALLED.value
            call_state.last_call_result = status
    await db.commit()
    from app.services import audit_service
    await audit_service.record(db, tenant_id=call.tenant_id, action="call.ended", resource_type="call",
                               resource_id=str(call.id), metadata={"status": status, "duration_seconds": duration_seconds})


async def analyze_call(db: AsyncSession, call: Call, transcript_text: str, llm_mode: str) -> CallAnalysis:
    """Runs the transcript through the LLM for structured call intelligence, validated by Pydantic."""
    llm = get_llm_provider(llm_mode)
    schema = CallAnalysisSchema.model_json_schema()
    messages = [{"role": "user", "content": f"Analyze this sales call transcript and extract structured "
                                             f"insights.\n\nTranscript:\n{transcript_text}"}]
    try:
        raw = await llm.structured_completion(messages=messages, json_schema=schema)
        validated = CallAnalysisSchema.model_validate(raw)
    except (ValidationError, Exception) as exc:  # noqa: BLE001
        logger.warning("call_analysis_validation_failed", error=str(exc))
        validated = CallAnalysisSchema(summary="Analysis could not be generated.", confidence=0.0)

    analysis = CallAnalysis(
        tenant_id=call.tenant_id, call_id=call.id, summary=validated.summary, intent=validated.intent,
        lead_status=validated.lead_status, interest_level=validated.interest_level, sentiment=validated.sentiment,
        requirements_json=json.dumps(validated.requirements), objections_json=json.dumps(validated.objections),
        questions_json=json.dumps(validated.questions), action_items_json=json.dumps(validated.action_items),
        meeting_requested=validated.meeting_requested, follow_up_required=validated.follow_up_required,
        next_action=validated.next_action, confidence=validated.confidence,
    )
    db.add(analysis)
    await db.commit()
    await db.refresh(analysis)
    return analysis


async def save_transcript(db: AsyncSession, call: Call, segments: list[dict]) -> Transcript:
    transcript = Transcript(tenant_id=call.tenant_id, call_id=call.id,
                             full_text="\n".join(s["text"] for s in segments))
    db.add(transcript)
    await db.flush()
    for seg in segments:
        db.add(TranscriptSegment(
            tenant_id=call.tenant_id, transcript_id=transcript.id, speaker=seg["speaker"],
            text=seg["text"], started_at_ms=seg.get("started_at_ms", 0), confidence=seg.get("confidence"),
        ))
    call.transcript_id = transcript.id
    await db.commit()
    await db.refresh(transcript)
    return transcript


async def sync_post_call_to_crm(db: AsyncSession, tenant_id: uuid.UUID, lead: Lead, call: Call, analysis: CallAnalysis) -> bool:
    """Updates connected CRM with call outcome, notes, and activity."""
    from app.models.crm import CRMConnection
    from app.providers.crm.factory import get_crm_provider
    from app.services.crm_service import get_valid_access_token

    conn_result = await db.execute(
        select(CRMConnection).where(CRMConnection.tenant_id == tenant_id, CRMConnection.status.in_(["connected", "synced"]))
    )
    conn = conn_result.scalars().first()
    if conn is None:
        logger.info("crm_post_call_skip_no_active_connection", tenant_id=str(tenant_id))
        return False

    try:
        token = await get_valid_access_token(db, conn)
        crm = get_crm_provider(conn.provider)
        if lead.provider_lead_id:
            note_content = (
                f"VoxAI Call Outcome ({call.status}):\n"
                f"Summary: {analysis.summary}\n"
                f"Intent: {analysis.intent}\n"
                f"Next Action: {analysis.next_action}\n"
                f"Duration: {call.duration_seconds or 0}s"
            )
            await crm.create_note(access_token=token, provider_lead_id=lead.provider_lead_id, note=note_content)
            if analysis.lead_status and analysis.lead_status != "unknown":
                await crm.update_lead(access_token=token, provider_lead_id=lead.provider_lead_id, fields={"status": analysis.lead_status})
            logger.info("crm_post_call_synced_successfully", provider=conn.provider, lead_id=str(lead.id))
            return True
    except Exception as exc:
        logger.warning("crm_post_call_sync_failed", error=str(exc), provider=conn.provider)
        return False
    return False


async def execute_post_call_pipeline(
    db: AsyncSession, call: Call, segments: list[dict], full_text: Optional[str] = None, llm_mode: Optional[str] = None,
) -> tuple[Transcript, CallAnalysis]:
    """
    End-to-end post-call pipeline triggered after CALL_ENDED:
    1. Persist transcript & segments to Postgres
    2. Run LLM structured call intelligence analysis
    3. Update lead state and status
    4. Sync outcomes back to CRM if configured
    """
    transcript = await save_transcript(db, call, segments)
    text_for_analysis = full_text or transcript.full_text or ""

    from app.core.config import settings
    mode = llm_mode or settings.LLM_PROVIDER_MODE
    analysis = await analyze_call(db, call, text_for_analysis, mode)

    if call.lead_id:
        lead = (await db.execute(select(Lead).where(Lead.id == call.lead_id))).scalar_one_or_none()
        if lead:
            if analysis.lead_status and analysis.lead_status != "unknown":
                lead.crm_status = analysis.lead_status
            await db.commit()
            await sync_post_call_to_crm(db, call.tenant_id, lead, call, analysis)

    return transcript, analysis
