"""
Plivo webhook endpoints for answer_url (Plivo XML) and status callbacks.
"""
import hmac
import hashlib
import base64
import uuid
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.core.signing import verify_call_webhook_signature, verify_plivo_signature
from app.db.session import get_async_db
from app.models.call import Call

logger = get_logger("plivo.webhooks")

router = APIRouter(prefix="/plivo", tags=["plivo"])


def _safe_host_for_log(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        parsed = urlsplit(f"//{value}")
        hostname = parsed.hostname
        if not hostname:
            return "invalid"
        rendered_host = f"[{hostname}]" if ":" in hostname else hostname
        return f"{rendered_host}:{parsed.port}" if parsed.port else rendered_host
    except ValueError:
        return "invalid"


def _safe_url_for_log(value: str) -> str:
    """Keep URL structure for diagnosis while redacting every query value."""
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname or "invalid"
        rendered_host = f"[{hostname}]" if ":" in hostname else hostname
        netloc = f"{rendered_host}:{parsed.port}" if parsed.port else rendered_host
        redacted_query = urlencode(
            [(key, "[redacted]") for key, _ in parse_qsl(parsed.query, keep_blank_values=True)]
        )
        return urlunsplit((parsed.scheme, netloc, parsed.path, redacted_query, ""))
    except ValueError:
        return "<invalid-url>"


async def verify_plivo_request(request: Request) -> bool:
    """
    Verify Plivo webhook signature.
    
    Supports Plivo V3, V2 (with nonce), and V1 signatures across direct
    connections and SSL-terminating reverse proxies (e.g. Cloudflare / Nginx).
    """
    nonce = (
        request.headers.get("X-Plivo-Signature-V3-Nonce")
        or request.headers.get("X-Plivo-Signature-V2-Nonce")
    )
    signature_headers = (
        ("v3", "X-Plivo-Signature-V3"),
        ("v3-ma", "X-Plivo-Signature-Ma-V3"),
        ("v2", "X-Plivo-Signature-V2"),
        ("v2-ma", "X-Plivo-Signature-Ma-V2"),
        ("v1", "X-Plivo-Signature"),
    )
    provided_signatures = [
        (version, value)
        for version, header in signature_headers
        if (value := request.headers.get(header))
    ]

    # Get form data if present
    form_data = {}
    form_parse_failed = False
    if "form" in (request.headers.get("content-type", "")):
        try:
            form = await request.form()
            form_data = dict(form)
        except Exception:
            form_parse_failed = True
            form_data = {}

    # Build candidate URLs for verification:
    # 1. Public URL reconstructed from standard reverse proxy headers
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    path = request.url.path
    query = f"?{request.url.query}" if request.url.query else ""
    public_url = f"{proto}://{host}{path}{query}"

    candidate_urls = [public_url, str(request.url)]
    if hasattr(settings, "BACKEND_URL") and settings.BACKEND_URL:
        candidate_urls.append(f"{settings.BACKEND_URL.rstrip('/')}{path}{query}")

    for header_version, signature_values in provided_signatures:
        version = header_version.split("-", 1)[0]
        for candidate_url in candidate_urls:
            if verify_plivo_signature(
                auth_token=settings.PLIVO_AUTH_TOKEN,
                url=candidate_url,
                post_params=form_data,
                signature=signature_values,
                nonce=nonce,
                version=version,
                method=request.method,
            ):
                return True

    safe_forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    if safe_forwarded_proto not in ("http", "https"):
        safe_forwarded_proto = "other" if safe_forwarded_proto else None
    logger.warning(
        "plivo_signature_verification_failed",
        signature_versions=[version for version, _ in provided_signatures],
        method=request.method,
        candidate_urls=[_safe_url_for_log(url) for url in candidate_urls],
        forwarded_host=_safe_host_for_log(request.headers.get("x-forwarded-host")),
        forwarded_proto=safe_forwarded_proto,
        parameter_names=sorted(
            key for key in form_data if key.isascii() and key.replace("_", "").replace("-", "").isalnum()
        ),
        nonce_present=bool(nonce),
        form_parse_failed=form_parse_failed,
    )
    return False


@router.api_route("/xml/{call_id}", methods=["GET", "POST"])
async def plivo_answer_xml(
    call_id: str,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
):
    """
    Plivo answer_url endpoint.
    
    Plivo POSTs here when a call is answered and expects Plivo XML response.
    Returns Plivo XML that instructs Plivo to dial our Asterisk SIP endpoint
    with the call_id encoded into the extension to correlate the media leg.
    """
    try:
        call_uuid = uuid.UUID(call_id)
    except ValueError:
        logger.warning("plivo_invalid_call_id", call_id=call_id)
        raise HTTPException(status_code=400, detail="Invalid call ID")

    # Defense-in-depth: verify internal cryptographic HMAC token if present in query
    sig = request.query_params.get("sig")
    if sig and not verify_call_webhook_signature(call_uuid, sig):
        logger.warning("plivo_internal_signature_invalid", call_id=call_id)
        raise HTTPException(status_code=401, detail="Invalid call signature")

    # Verify Plivo provider signature
    if not await verify_plivo_request(request):
        logger.warning("plivo_signature_invalid", call_id=call_id)
        raise HTTPException(status_code=401, detail="Invalid Plivo signature")

    # Look up the call to get context
    call = await db.execute(
        select(Call).where(Call.id == call_uuid, Call.telephony_provider == "plivo")
    )
    call = call.scalar_one_or_none()
    
    if not call:
        logger.warning("plivo_call_not_found", call_id=call_id)
        raise HTTPException(status_code=404, detail="Call not found")

    sip_host = getattr(settings, "SIP_PUBLIC_HOST", None)
    if not sip_host:
        logger.error("sip_public_host_not_configured")
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Speak voice="WOMAN" language="en-IN">Service temporarily unavailable. Please try again later.</Speak>
    <Hangup/>
</Response>"""
        return Response(content=xml, media_type="application/xml")

    # Route SIP leg to Asterisk with the call_id in the SIP extension for correlation
    sip_uri = f"sip:call_{call_id}@{sip_host}"
    
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial>
        <User>{sip_uri}</User>
    </Dial>
</Response>"""
    
    logger.info("plivo_xml_generated", call_id=call_id, sip_uri=sip_uri)
    return Response(content=xml, media_type="application/xml")


@router.post("/status/{call_id}")
async def plivo_status_callback(
    call_id: str,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
):
    """
    Plivo status callback (hangup_url).
    
    Plivo POSTs here when call status changes (completed, failed, etc.).
    """
    try:
        call_uuid = uuid.UUID(call_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid call ID")

    # Defense-in-depth: verify internal cryptographic HMAC token if present in query
    sig = request.query_params.get("sig")
    if sig and not verify_call_webhook_signature(call_uuid, sig):
        logger.warning("plivo_status_internal_signature_invalid", call_id=call_id)
        raise HTTPException(status_code=401, detail="Invalid call signature")

    # Verify Plivo signature
    if not await verify_plivo_request(request):
        logger.warning("plivo_status_signature_invalid", call_id=call_id)
        raise HTTPException(status_code=401, detail="Invalid Plivo signature")

    call = await db.execute(
        select(Call).where(Call.id == call_uuid, Call.telephony_provider == "plivo")
    )
    call = call.scalar_one_or_none()
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")

    # Get form data
    form_data = dict(await request.form()) if "form" in (request.headers.get("content-type", "")) else {}
    
    status_map = {
        "completed": "completed", "busy": "busy", "no-answer": "no_answer", "failed": "failed",
        "in-progress": "in_progress", "ringing": "ringing"
    }
    raw_status = str(form_data.get("CallStatus") or "").lower()
    status = status_map.get(raw_status)
    
    if status in ("completed", "busy", "no_answer", "failed"):
        dur = form_data.get("Duration") or form_data.get("CallDuration")
        from app.services import call_service
        await call_service.finalize_call(
            db, call, status=status, 
            duration_seconds=int(dur) if dur else None,
            end_reason=status
        )
    elif status:
        call.status = status
        await db.commit()
    
    return {"ok": True}
