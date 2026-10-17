from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crypto import encrypt_secret
from app.core.errors import AppError
from app.core.signing import make_oauth_state, read_oauth_state
from app.db.session import get_async_db
from app.models.integration import IntegrationCredential
from app.providers.calendar.base import CalendarProviderError, get_calendar_provider
from app.security.rbac import require_admin, require_any_role
from app.security.tenant import RequestContext
from app.services import audit_service

router = APIRouter(prefix="/integrations", tags=["integrations"])


@router.get("", summary="Integration catalogue and honest configuration status")
async def list_integrations(ctx: RequestContext = Depends(require_any_role), db: AsyncSession = Depends(get_async_db)):
    calendar = (await db.execute(select(IntegrationCredential.id).where(
        IntegrationCredential.tenant_id == ctx.tenant_id,
        IntegrationCredential.provider == "google_calendar",
    ))).scalar_one_or_none()
    return {
        "modes": {"crm": settings.CRM_PROVIDER_MODE, "telephony": settings.TELEPHONY_PROVIDER_MODE,
                  "calendar": settings.CALENDAR_PROVIDER_MODE, "whatsapp": settings.WHATSAPP_PROVIDER_MODE},
        "credentials_configured": {
            "hubspot": bool(settings.HUBSPOT_CLIENT_ID), "salesforce": bool(settings.SALESFORCE_CLIENT_ID),
            "zoho": bool(settings.ZOHO_CLIENT_ID), "pipedrive": bool(settings.PIPEDRIVE_CLIENT_ID),
            "google": bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET),
            "exotel": bool(settings.EXOTEL_API_KEY), "plivo": bool(settings.PLIVO_AUTH_ID),
            "whatsapp": bool(settings.WHATSAPP_ACCESS_TOKEN), "groq": bool(settings.GROQ_API_KEY),
        },
        "connections": {"google_calendar": bool(calendar)},
    }


@router.get("/google/authorize")
async def google_authorize(ctx: RequestContext = Depends(require_admin)):
    if settings.CALENDAR_PROVIDER_MODE != "google":
        raise AppError("CALENDAR_PROVIDER_NOT_ACTIVE", "Set CALENDAR_PROVIDER_MODE=google before connecting Google Calendar.", status_code=409)
    provider = get_calendar_provider("google")
    state = make_oauth_state(tenant_id=ctx.tenant_id, user_id=ctx.user_id, provider="google_calendar")
    try:
        return {"authorization_url": await provider.get_authorization_url(state=state)}
    except CalendarProviderError as exc:
        raise AppError("GOOGLE_CALENDAR_NOT_CONFIGURED", str(exc), status_code=409)


@router.get("/google/callback", include_in_schema=False)
async def google_callback(code: str = "", state: str = "", error: str = "",
                         db: AsyncSession = Depends(get_async_db)):
    if error:
        return RedirectResponse(f"{settings.FRONTEND_URL}/?view=Integrations&calendar_error=authorization_denied")
    try:
        tenant_id, user_id = read_oauth_state(state, provider="google_calendar")
    except ValueError:
        return RedirectResponse(f"{settings.FRONTEND_URL}/?view=Integrations&calendar_error=invalid_state")
    try:
        token = await get_calendar_provider("google").exchange_code_for_token(code=code)
    except CalendarProviderError:
        return RedirectResponse(f"{settings.FRONTEND_URL}/?view=Integrations&calendar_error=token_exchange_failed")
    row = (await db.execute(select(IntegrationCredential).where(
        IntegrationCredential.tenant_id == tenant_id,
        IntegrationCredential.provider == "google_calendar",
    ))).scalar_one_or_none()
    if row is None:
        row = IntegrationCredential(tenant_id=tenant_id, provider="google_calendar",
                                    encrypted_access_token=encrypt_secret(token["access_token"]))
        db.add(row)
    row.encrypted_access_token = encrypt_secret(token["access_token"])
    if token.get("refresh_token"):
        row.encrypted_refresh_token = encrypt_secret(token["refresh_token"])
    row.token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(token.get("expires_in", 3600)))
    row.external_account_name = "Google Calendar"
    row.connected_by_user_id = user_id
    await db.commit()
    await audit_service.record(db, tenant_id=tenant_id, actor_user_id=user_id, action="integration.google_calendar.connected",
                               resource_type="integration", resource_id=str(row.id))
    return RedirectResponse(f"{settings.FRONTEND_URL}/?view=Integrations&calendar_connected=1")
