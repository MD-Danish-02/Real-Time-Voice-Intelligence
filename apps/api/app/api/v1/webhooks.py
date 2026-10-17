"""
Inbound webhooks. The tenant is resolved from the stored connection
(never from the payload). Signatures are verified BEFORE any parsing is
trusted; events are persisted for idempotency and acknowledged quickly,
with processing deferred to a worker.
"""
import json
import time
import uuid

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.signing import verify_call_webhook_signature
from app.db.session import get_async_db
from app.models.call import Call
from app.models.crm import CRMConnection
from app.providers.crm.factory import get_crm_provider
from app.providers.whatsapp.base import get_whatsapp_provider
from app.services import call_service, crm_service
from app.security.rate_limit import enforce_rate_limit

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = get_logger("webhooks")
REPLAY_WINDOW_SECONDS = 300


@router.post("/crm/{provider}/{connection_id}", status_code=202)
async def crm_webhook(provider: str, connection_id: uuid.UUID, request: Request,
                      db: AsyncSession = Depends(get_async_db)):
    await enforce_rate_limit(key=f"webhook:{connection_id}", limit_per_minute=600)
    conn = (await db.execute(select(CRMConnection).where(CRMConnection.id == connection_id,
                                                         CRMConnection.provider == provider))).scalar_one_or_none()
    if conn is None or not conn.webhook_secret:
        raise AppError("WEBHOOK_UNKNOWN", "Unknown webhook target.", status_code=404)
    body = await request.body()
    if len(body) > 1_000_000:
        raise AppError("PAYLOAD_TOO_LARGE", "Payload too large.", status_code=413)
    headers = {k.lower(): v for k, v in request.headers.items()}
    crm = get_crm_provider(provider)
    if not crm.verify_webhook_signature(headers=headers, body=body, secret=conn.webhook_secret):
        logger.warning("webhook_signature_invalid", connection_id=str(connection_id))
        raise AppError("INVALID_SIGNATURE", "Signature verification failed.", status_code=401)
    ts = headers.get("x-webhook-timestamp")
    if ts is not None:
        try:
            if abs(time.time() - float(ts)) > REPLAY_WINDOW_SECONDS:
                raise AppError("STALE_WEBHOOK", "Webhook timestamp outside replay window.", status_code=400)
        except ValueError:
            raise AppError("BAD_TIMESTAMP", "Invalid webhook timestamp.", status_code=400)
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        raise AppError("INVALID_JSON", "Body is not valid JSON.", status_code=400)
    if isinstance(payload, list):  # some CRMs batch events
        payload = payload[0] if payload else {}
    event, is_duplicate = await crm_service.ingest_webhook_event(db, conn.tenant_id, conn, payload.get("event_type", "lead.changed"), payload)
    if not is_duplicate:
        try:
            from app.workers.tasks import process_crm_event
            process_crm_event.delay(str(event.id))
        except Exception as exc:  # noqa: BLE001 - queue down: event is persisted; reconciliation will pick it up
            logger.error("webhook_enqueue_failed", error=str(exc))
    return {"accepted": True, "duplicate": is_duplicate}


@router.post("/telephony/{provider}/{call_id}")
async def telephony_webhook(provider: str, call_id: uuid.UUID, request: Request, sig: str = "",
                            db: AsyncSession = Depends(get_async_db)):
    """Provider call-status callbacks. Only updates a call that exists; authenticity of the
    call id is checked against the stored provider_call_id when the provider echoes it."""
    if not verify_call_webhook_signature(call_id, sig):  # URL was signed by us when the call was dialed
        raise AppError("INVALID_SIGNATURE", "Webhook signature is missing or invalid.", status_code=401)
    call = (await db.execute(select(Call).where(Call.id == call_id, Call.telephony_provider == provider))).scalar_one_or_none()
    if call is None:
        raise AppError("CALL_NOT_FOUND", "Unknown call.", status_code=404)
    form = dict(await request.form()) if "form" in (request.headers.get("content-type", "")) else {}
    try:
        data = form or await request.json()
    except Exception:  # noqa: BLE001
        data = {}
    echoed = data.get("CallSid") or data.get("CallUUID") or data.get("provider_call_id")
    if echoed and call.provider_call_id and echoed != call.provider_call_id:
        raise AppError("CALL_MISMATCH", "Provider call id mismatch.", status_code=400)
    status_map = {"completed": "completed", "busy": "busy", "no-answer": "no_answer", "failed": "failed",
                  "in-progress": "in_progress", "ringing": "ringing"}
    status = status_map.get(str(data.get("Status") or data.get("CallStatus") or "").lower())
    if status in ("completed", "busy", "no_answer", "failed"):
        dur = data.get("Duration") or data.get("CallDuration")
        await call_service.finalize_call(db, call, status=status, duration_seconds=int(dur) if dur else None,
                                         end_reason=status)
    elif status:
        call.status = status
        await db.commit()
    return {"ok": True}


@router.get("/whatsapp")
async def whatsapp_verify(request: Request):
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == settings.WHATSAPP_VERIFY_TOKEN:
        return Response(content=q.get("hub.challenge", ""), media_type="text/plain")
    raise AppError("VERIFY_FAILED", "Verification failed.", status_code=403)


@router.post("/whatsapp")
async def whatsapp_status(request: Request, db: AsyncSession = Depends(get_async_db)):
    body = await request.body()
    provider = get_whatsapp_provider(settings.WHATSAPP_PROVIDER)
    if settings.WHATSAPP_PROVIDER_MODE != "mock" and not provider.verify_webhook_signature(
            headers={k.lower(): v for k, v in request.headers.items()}, body=body):
        raise AppError("INVALID_SIGNATURE", "Signature verification failed.", status_code=401)
    from app.models.scheduling import WhatsAppMessage
    data = json.loads(body or b"{}")
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            for st in change.get("value", {}).get("statuses", []):
                msg = (await db.execute(select(WhatsAppMessage).where(
                    WhatsAppMessage.provider_message_id == st.get("id")))).scalar_one_or_none()
                if msg and st.get("status") in ("sent", "delivered", "read", "failed"):
                    msg.status = st["status"]
    await db.commit()
    return {"ok": True}
