from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_secret, encrypt_secret
from app.models.integration import IntegrationCredential
from app.providers.calendar.base import CalendarProviderError, GoogleCalendarProvider


async def get_calendar_access_token(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    row = (await db.execute(select(IntegrationCredential).where(
        IntegrationCredential.tenant_id == tenant_id,
        IntegrationCredential.provider == "google_calendar",
    ))).scalar_one_or_none()
    if row is None:
        raise CalendarProviderError("Connect Google Calendar for this workspace first.")
    expiry = row.token_expires_at
    if expiry is not None:
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry <= datetime.now(timezone.utc) + timedelta(minutes=2):
            if not row.encrypted_refresh_token:
                raise CalendarProviderError("Google Calendar access token expired; reconnect the calendar.")
            refreshed = await GoogleCalendarProvider().refresh_access_token(
                refresh_token=decrypt_secret(row.encrypted_refresh_token))
            row.encrypted_access_token = encrypt_secret(refreshed["access_token"])
            row.token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(refreshed.get("expires_in", 3600)))
            if refreshed.get("refresh_token"):
                row.encrypted_refresh_token = encrypt_secret(refreshed["refresh_token"])
            await db.commit()
    return decrypt_secret(row.encrypted_access_token)
